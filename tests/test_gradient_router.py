"""
tests/test_gradient_router.py — offline / deterministic tests for the gradient router.

All tests use mock objects so they run without aiohttp, GPU drivers, or
a live Ollama / LOCI DB instance.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

# ── Lightweight stubs so imports succeed without real dependencies ─────────────

_aiohttp = types.ModuleType("aiohttp")
_aiohttp.ClientSession = MagicMock  # type: ignore[attr-defined]
_aiohttp.ClientTimeout = MagicMock  # type: ignore[attr-defined]
sys.modules.setdefault("aiohttp", _aiohttp)

_gpu = types.ModuleType("core.gpu_offload")
_gpu.query_vram = MagicMock(return_value=MagicMock(utilisation=0.3))  # type: ignore[attr-defined]
_gpu.LANE_RTX = 0  # type: ignore[attr-defined]
_gpu.LANE_ARC = 1  # type: ignore[attr-defined]
sys.modules.setdefault("core.gpu_offload", _gpu)

# Ensure core package is importable
import core.api_provision as _ap  # noqa: E402  (after stubs)

from core.gradient_router import (  # noqa: E402
    BudgetLedger,
    DEFAULT_DAILY_REMOTE_TOKEN_CAP,
    GradientRouter,
    OllamaClient,
    Provider,
    ProviderScore,
    _apply_loci_compression,
)


# ─────────────────────────────────────────────────────────────────────────────
# ProviderScore EMA
# ─────────────────────────────────────────────────────────────────────────────

class TestProviderScore(unittest.TestCase):

    def test_initial_score_is_one(self):
        s = ProviderScore(provider=Provider.LOCAL)
        self.assertAlmostEqual(s.score, 1.0)

    def test_good_responses_keep_score_high(self):
        s = ProviderScore(provider=Provider.LOCAL)
        for _ in range(20):
            # 100 tokens in 200 ms = 500 tok/s >> REF (50) → norm = 1.0
            s.update(latency_ms=200, tokens_out=100, is_error=False)
        # W_THROUGHPUT * 1.0 - W_ERROR * 0.0 = 0.60
        self.assertGreater(s.score, 0.4)

    def test_errors_drive_score_to_zero(self):
        s = ProviderScore(provider=Provider.LOCAL)
        for _ in range(40):
            s.update(latency_ms=100, tokens_out=0, is_error=True)
        self.assertLess(s.score, 0.05)

    def test_score_always_clamped_zero_to_one(self):
        s = ProviderScore(provider=Provider.LOCAL)
        for _ in range(60):
            s.update(latency_ms=1, tokens_out=100_000, is_error=False)
        self.assertLessEqual(s.score, 1.0)
        s2 = ProviderScore(provider=Provider.LOCAL)
        for _ in range(60):
            s2.update(latency_ms=1, tokens_out=0, is_error=True)
        self.assertGreaterEqual(s2.score, 0.0)

    def test_sample_count_increments(self):
        s = ProviderScore(provider=Provider.LOCAL)
        s.update(100, 50, False)
        s.update(100, 50, False)
        self.assertEqual(s.sample_count, 2)


# ─────────────────────────────────────────────────────────────────────────────
# BudgetLedger
# ─────────────────────────────────────────────────────────────────────────────

class TestBudgetLedger(unittest.TestCase):

    def test_fresh_ledger_allows_spend(self):
        b = BudgetLedger(cap=1000)
        self.assertTrue(b.can_spend(500))

    def test_over_cap_blocks(self):
        b = BudgetLedger(cap=100)
        b.spend(90)
        self.assertFalse(b.can_spend(20))

    def test_exactly_at_cap_is_blocked(self):
        b = BudgetLedger(cap=100)
        b.spend(100)
        self.assertFalse(b.can_spend(1))

    def test_new_day_resets_counter(self):
        b = BudgetLedger(cap=100)
        b.spend(99)
        b.day_epoch -= 1   # fake yesterday
        b.reset_if_new_day()
        self.assertEqual(b.tokens_today, 0)
        self.assertTrue(b.can_spend(99))

    def test_serialisation_round_trip(self):
        b = BudgetLedger(cap=5000)
        b.spend(1234)
        b2 = BudgetLedger.from_dict(b.to_dict())
        self.assertEqual(b2.tokens_today, 1234)
        self.assertEqual(b2.cap, 5000)
        self.assertEqual(b2.day_epoch, b.day_epoch)


# ─────────────────────────────────────────────────────────────────────────────
# LOCI compression passthrough (no DB)
# ─────────────────────────────────────────────────────────────────────────────

class TestLociCompressionNoDb(unittest.TestCase):

    def test_passthrough_when_loci_db_unset(self):
        import os
        os.environ.pop("LOCI_DB_PATH", None)
        msgs = [{"role": "user", "content": "hello"}]
        out, ratio = _apply_loci_compression(msgs)
        self.assertEqual(out, msgs)
        self.assertIsNone(ratio)

    def test_passthrough_when_compression_module_missing(self):
        import os, sys
        os.environ.pop("LOCI_DB_PATH", None)
        old = sys.modules.pop("core.context_compression", None)
        try:
            msgs = [{"role": "user", "content": "test"}]
            out, ratio = _apply_loci_compression(msgs)
            self.assertEqual(out, msgs)
        finally:
            if old is not None:
                sys.modules["core.context_compression"] = old


# ─────────────────────────────────────────────────────────────────────────────
# GradientRouter._pick_by_gradient
# ─────────────────────────────────────────────────────────────────────────────

def _make_router(health_map: dict) -> GradientRouter:
    prov = MagicMock()
    prov.health = health_map
    prov._pick_provider = MagicMock(return_value=Provider.LOCAL)
    router                       = GradientRouter.__new__(GradientRouter)
    router._prov                 = prov
    router._ollama               = MagicMock()
    router._scores               = {p: ProviderScore(provider=p) for p in Provider}
    router._budget               = BudgetLedger(cap=DEFAULT_DAILY_REMOTE_TOKEN_CAP)
    router._enable_loci          = False
    router._loci_budget          = 1200
    router._use_native_ollama    = False
    return router


class TestGradientPick(unittest.TestCase):

    def _health(self, **reachable_map):
        return {
            p: MagicMock(reachable=reachable_map.get(p.name, False))
            for p in Provider
        }

    def test_prefers_requested_when_scores_equal(self):
        router = _make_router(self._health(LOCAL="true", LOCAL_INTEL="true"))
        result = router._pick_by_gradient(Provider.LOCAL)
        self.assertEqual(result, Provider.LOCAL)

    def test_falls_back_to_intel_when_local_down(self):
        router = _make_router(self._health(LOCAL_INTEL="true"))
        result = router._pick_by_gradient(Provider.LOCAL)
        self.assertEqual(result, Provider.LOCAL_INTEL)

    def test_budget_cap_blocks_remote(self):
        router = _make_router(self._health(NEXOS="true", XAI="true"))
        router._budget.spend(DEFAULT_DAILY_REMOTE_TOKEN_CAP)
        # all remote blocked → static fallback
        router._pick_by_gradient(Provider.LOCAL)
        router._prov._pick_provider.assert_called()

    def test_higher_scored_provider_wins(self):
        router = _make_router(self._health(LOCAL="true", LOCAL_INTEL="true"))
        router._scores[Provider.LOCAL_INTEL].score = 0.9
        router._scores[Provider.LOCAL].score       = 0.1
        result = router._pick_by_gradient(Provider.LOCAL)
        # Score diff (0.8) >> 5% tie-break → LOCAL_INTEL wins
        self.assertEqual(result, Provider.LOCAL_INTEL)

    def test_tiebreak_keeps_preferred_within_five_percent(self):
        router = _make_router(self._health(LOCAL="true", LOCAL_INTEL="true"))
        router._scores[Provider.LOCAL].score       = 0.80
        router._scores[Provider.LOCAL_INTEL].score = 0.82   # <5% diff
        result = router._pick_by_gradient(Provider.LOCAL)
        # Within tie-break range → keep preferred
        self.assertEqual(result, Provider.LOCAL)

    def test_all_offline_uses_static_fallback(self):
        router = _make_router({})
        router._pick_by_gradient(Provider.LOCAL)
        router._prov._pick_provider.assert_called_with(Provider.LOCAL)


# ─────────────────────────────────────────────────────────────────────────────
# OllamaClient
# ─────────────────────────────────────────────────────────────────────────────

class TestOllamaClientListModels(unittest.IsolatedAsyncioTestCase):

    async def test_returns_model_names(self):
        client          = OllamaClient.__new__(OllamaClient)
        client._base    = "http://localhost:11434"
        mock_resp       = AsyncMock()
        mock_resp.status = 200
        mock_resp.json   = AsyncMock(return_value={
            "models": [{"name": "llama3.2:8b"}, {"name": "nomic-embed-text:latest"}]
        })
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__  = AsyncMock(return_value=None)
        session         = MagicMock()
        session.get     = MagicMock(return_value=cm)
        client._session = session

        models = await client.list_models()
        self.assertIn("llama3.2:8b", models)
        self.assertIn("nomic-embed-text:latest", models)

    async def test_returns_empty_when_session_none(self):
        client          = OllamaClient.__new__(OllamaClient)
        client._base    = "http://localhost:11434"
        client._session = None
        self.assertEqual(await client.list_models(), [])

    async def test_is_loaded_matches_base_name(self):
        client = OllamaClient.__new__(OllamaClient)
        client._base    = "http://localhost:11434"
        mock_resp       = AsyncMock()
        mock_resp.status = 200
        mock_resp.json   = AsyncMock(return_value={
            "models": [{"name": "llama3.2:8b-instruct-q4_0"}]
        })
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__  = AsyncMock(return_value=None)
        session         = MagicMock()
        session.get     = MagicMock(return_value=cm)
        client._session = session

        self.assertTrue(await client.is_loaded("llama3.2"))
        self.assertFalse(await client.is_loaded("mistral"))


if __name__ == "__main__":
    unittest.main()
