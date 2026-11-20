"""
core/gradient_router.py — Gradient model router for Arc
========================================================

Extends core/api_provision.py with:

1. Gradient scoring — each provider's score drifts toward the
   best-performing provider as measured by real tok/s, latency, error rate,
   and local VRAM pressure.  Update rule is an EMA on normalised throughput,
   analogous to a one-step stochastic gradient on the negative-latency
   objective.  Scores persist across restarts via /dev/shm.

2. LOCI LDCE context compression — if LOCI_DB_PATH is set,
   runs the conversation through core/context_compression.build_context()
   before dispatch (kernel items full, hop items as symbols, offload listed).
   This shrinks prompt tokens and improves KV prefix-cache hit rate on the
   local engine.

3. Native Ollama backend — OllamaClient wraps /api/chat so Ollama options
   (num_ctx, mmap, num_predict, temperature) flow through without the
   OpenAI-compat shim.  Adds model listing, is_loaded(), and keep_alive
   pinning so the router can verify a model is in VRAM before dispatching.

4. Budget governor — per-day cap on remote (NEXOS / XAI) token spend.
   Remote providers are skipped (not errored) when the daily cap is hit.

Usage::

    router = GradientRouter.from_config()
    await router.start()
    result = await router.route(
        messages=[{"role": "user", "content": "hello"}],
        model="llama3.2:8b",
    )
    print(result.content, result.tokens_used)
    await router.stop()
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional, Tuple

import aiohttp

from core.api_provision import (
    APIProvisioner,
    APIKeys,
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    Provider,
    PROVIDER_ENDPOINTS,
    PROVIDER_MODELS,
    provider_for_model,
)
from core.gpu_offload import query_vram, LANE_RTX, LANE_ARC


# ── Constants ─────────────────────────────────────────────────────────────────

SCORE_PATH  = "/dev/shm/arc_router_scores.json"
BUDGET_PATH = "/dev/shm/arc_router_budget.json"
OLLAMA_BASE = os.environ.get("LOCI_LOCAL_BASE_URL", "http://localhost:11434")

# EMA decay: weight given to each new observation
ALPHA = 0.25

# Score component weights (must sum to W_THROUGHPUT + W_ERROR ≤ 1)
W_THROUGHPUT = 0.60
W_ERROR      = 0.30
W_VRAM       = 0.10

# Reference tok/s used to normalise throughput scores (conservative lower bounds)
REF_TOK_PER_S: Dict[Provider, float] = {
    Provider.LOCAL:        50.0,   # RTX 5060 Ti, 8B INT4 ≈ 60-80 tok/s
    Provider.LOCAL_INTEL:  30.0,   # Arc B70,     32B INT4 ≈ 25-35 tok/s
    Provider.LOCAL_SGLANG: 25.0,   # Arc B70 SGLang XPU (experimental)
    Provider.NEXOS:        40.0,   # cloud: fast but network-capped
    Provider.XAI:          30.0,
}

DEFAULT_DAILY_REMOTE_TOKEN_CAP = int(
    os.environ.get("ARC_DAILY_TOKEN_CAP", "200000")
)


# ── Provider score (EMA) ──────────────────────────────────────────────────────

@dataclass
class ProviderScore:
    provider:     Provider
    score:        float = 1.0    # 0.0 (avoid) … 1.0 (ideal)
    tok_per_s:    float = 0.0    # EMA of measured throughput
    error_rate:   float = 0.0    # EMA of error fraction (0–1)
    sample_count: int   = 0
    last_updated: float = field(default_factory=time.time)

    def update(self, latency_ms: float, tokens_out: int, is_error: bool) -> None:
        self.sample_count += 1
        obs_tok_s = (
            (tokens_out / max(1.0, latency_ms)) * 1000.0 if not is_error else 0.0
        )
        self.tok_per_s  = (1 - ALPHA) * self.tok_per_s  + ALPHA * obs_tok_s
        self.error_rate = (1 - ALPHA) * self.error_rate + ALPHA * (1.0 if is_error else 0.0)

        ref  = REF_TOK_PER_S.get(self.provider, 30.0)
        norm = min(1.0, self.tok_per_s / ref)
        self.score = W_THROUGHPUT * norm - W_ERROR * self.error_rate
        self.score = max(0.0, min(1.0, self.score))
        self.last_updated = time.time()


# ── Budget governor ───────────────────────────────────────────────────────────

@dataclass
class BudgetLedger:
    tokens_today: int = 0
    day_epoch:    int = 0    # int(time.time() / 86400)
    cap:          int = DEFAULT_DAILY_REMOTE_TOKEN_CAP

    def _today(self) -> int:
        return int(time.time() / 86400)

    def reset_if_new_day(self) -> None:
        today = self._today()
        if today != self.day_epoch:
            self.tokens_today = 0
            self.day_epoch    = today

    def can_spend(self, tokens: int) -> bool:
        self.reset_if_new_day()
        return self.tokens_today + tokens <= self.cap

    def spend(self, tokens: int) -> None:
        self.reset_if_new_day()
        self.tokens_today += tokens

    def to_dict(self) -> dict:
        return {
            "tokens_today": self.tokens_today,
            "day_epoch":    self.day_epoch,
            "cap":          self.cap,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BudgetLedger":
        b = cls()
        b.tokens_today = int(d.get("tokens_today", 0))
        b.day_epoch    = int(d.get("day_epoch", 0))
        b.cap          = int(d.get("cap", DEFAULT_DAILY_REMOTE_TOKEN_CAP))
        return b


# ── Native Ollama client ──────────────────────────────────────────────────────

class OllamaClient:
    """
    Thin async wrapper around the native Ollama HTTP API.

    Uses /api/chat (message-list form) rather than /v1/chat/completions
    so Ollama options (num_ctx, mmap, num_predict, …) pass through without
    the OpenAI-compat shim.  Also exposes model listing and keep_alive
    pinning so the router can verify a model is warm in VRAM.
    """

    def __init__(self, base_url: str = OLLAMA_BASE) -> None:
        self._base    = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._session:
            await self._session.close()

    # ── Model discovery ───────────────────────────────────────────────────────

    async def list_models(self) -> List[str]:
        """Return names of models available in Ollama (loaded or on disk)."""
        if not self._session:
            return []
        try:
            async with self._session.get(
                f"{self._base}/api/tags",
                timeout=aiohttp.ClientTimeout(total=4),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    async def is_loaded(self, model: str) -> bool:
        """Check whether a model (by base name) is in the Ollama library."""
        models = await self.list_models()
        base   = model.split(":")[0].lower()
        return any(m.lower().startswith(base) for m in models)

    # ── Chat completion ───────────────────────────────────────────────────────

    async def chat(
        self,
        messages: List[Dict],
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        options: Optional[Dict] = None,
        keep_alive: str = "5m",
    ) -> Tuple[str, int, float]:
        """Non-streaming chat.  Returns (content, tokens_out, latency_ms)."""
        if not self._session:
            raise RuntimeError("OllamaClient.start() must be called first")
        payload: dict = {
            "model":      model,
            "messages":   messages,
            "stream":     False,
            "keep_alive": keep_alive,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
                **(options or {}),
            },
        }
        t0 = time.monotonic()
        async with self._session.post(
            f"{self._base}/api/chat",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            latency = (time.monotonic() - t0) * 1000
            data    = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Ollama HTTP {resp.status}: {data}")
            content    = data.get("message", {}).get("content", "")
            tokens_out = data.get("eval_count", max(1, len(content) // 4))
            return content, tokens_out, latency

    async def stream_chat(
        self,
        messages: List[Dict],
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        options: Optional[Dict] = None,
        keep_alive: str = "5m",
    ) -> AsyncGenerator[str, None]:
        """Streaming chat — yields content chunks."""
        if not self._session:
            raise RuntimeError("OllamaClient.start() must be called first")
        payload: dict = {
            "model":      model,
            "messages":   messages,
            "stream":     True,
            "keep_alive": keep_alive,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
                **(options or {}),
            },
        }
        async with self._session.post(
            f"{self._base}/api/chat",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            async for raw in resp.content:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    text  = chunk.get("message", {}).get("content", "")
                    if text:
                        yield text
                    if chunk.get("done"):
                        return
                except json.JSONDecodeError:
                    continue

    async def set_keep_alive(self, model: str, keep_alive: str = "24h") -> bool:
        """Pin a model in VRAM by sending a minimal no-op with extended keep_alive."""
        try:
            await self.chat(
                messages=[{"role": "user", "content": "."}],
                model=model,
                max_tokens=1,
                keep_alive=keep_alive,
            )
            return True
        except Exception:
            return False


# ── LOCI compression bridge ───────────────────────────────────────────────────

def _try_compression_module():
    """Lazy import — context_compression may not be on main yet."""
    try:
        from core import context_compression as cc  # type: ignore
        return cc
    except ImportError:
        return None


def _apply_loci_compression(
    messages: List[Dict],
    token_budget: int = 1200,
) -> Tuple[List[Dict], Optional[float]]:
    """
    Prepend a LOCI memory block to the system message if LOCI_DB_PATH is set.
    Returns (new_messages, compression_ratio) or (messages, None) if unavailable.
    """
    cc = _try_compression_module()
    if cc is None:
        return messages, None

    source = cc.LociMemorySource.from_env()
    if source is None:
        return messages, None

    try:
        compressed = cc.build_context(source, token_budget=token_budget)
    except Exception:
        return messages, None

    if not compressed.text:
        return messages, None

    new_messages = list(messages)
    if new_messages and new_messages[0].get("role") == "system":
        new_messages[0] = {
            "role":    "system",
            "content": compressed.text + "\n\n---\n" + new_messages[0]["content"],
        }
    else:
        new_messages.insert(0, {"role": "system", "content": compressed.text})

    return new_messages, compressed.compression_ratio


# ── GradientRouter ────────────────────────────────────────────────────────────

class GradientRouter:
    """
    Adaptive router that wraps APIProvisioner.

    Provider selection is gradient-scored from live metrics rather than
    the static size-heuristic in api_provision.provider_for_model.
    """

    def __init__(
        self,
        provisioner: APIProvisioner,
        ollama_base: str = OLLAMA_BASE,
        enable_loci_compression: bool = True,
        loci_token_budget: int = 1200,
        daily_token_cap: int = DEFAULT_DAILY_REMOTE_TOKEN_CAP,
        use_native_ollama: bool = True,
    ) -> None:
        self._prov              = provisioner
        self._ollama            = OllamaClient(ollama_base)
        self._scores: Dict[Provider, ProviderScore] = {
            p: ProviderScore(provider=p) for p in Provider
        }
        self._budget            = BudgetLedger(cap=daily_token_cap)
        self._enable_loci       = enable_loci_compression
        self._loci_budget       = loci_token_budget
        self._use_native_ollama = use_native_ollama
        self._restore_state()

    # ── Constructors ──────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "GradientRouter":
        return cls(APIProvisioner.from_env())

    @classmethod
    def from_config(cls, path: str = "config/arc_config.yaml") -> "GradientRouter":
        return cls(APIProvisioner.from_config(path))

    # ── State persistence (shm) ───────────────────────────────────────────────

    def _restore_state(self) -> None:
        try:
            raw = json.loads(Path(SCORE_PATH).read_text())
            for name, d in raw.items():
                try:
                    p = Provider[name]
                    s = self._scores[p]
                    s.score        = float(d.get("score", 1.0))
                    s.tok_per_s    = float(d.get("tok_per_s", 0.0))
                    s.error_rate   = float(d.get("error_rate", 0.0))
                    s.sample_count = int(d.get("sample_count", 0))
                except (KeyError, ValueError):
                    pass
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        try:
            raw_b = json.loads(Path(BUDGET_PATH).read_text())
            self._budget = BudgetLedger.from_dict(raw_b)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def _save_state(self) -> None:
        try:
            Path(SCORE_PATH).write_text(json.dumps({
                p.name: {
                    "score":        round(s.score, 4),
                    "tok_per_s":    round(s.tok_per_s, 2),
                    "error_rate":   round(s.error_rate, 4),
                    "sample_count": s.sample_count,
                    "last_updated": round(s.last_updated, 1),
                }
                for p, s in self._scores.items()
            }, indent=2))
            Path(BUDGET_PATH).write_text(json.dumps(self._budget.to_dict()))
        except OSError:
            pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._prov.start()
        await self._ollama.start()

    async def stop(self) -> None:
        self._save_state()
        await self._prov.stop()
        await self._ollama.stop()

    # ── VRAM pressure helper ──────────────────────────────────────────────────

    def _vram_pressure(self, provider: Provider) -> float:
        """0.0 = plenty of VRAM; 1.0 = fully saturated."""
        try:
            if provider == Provider.LOCAL:
                return query_vram(LANE_RTX).utilisation
            if provider in (Provider.LOCAL_INTEL, Provider.LOCAL_SGLANG):
                return query_vram(LANE_ARC).utilisation
        except Exception:
            pass
        return 0.0

    # ── Gradient picker ───────────────────────────────────────────────────────

    def _pick_by_gradient(
        self,
        preferred: Provider,
        estimated_tokens: int = 0,
    ) -> Provider:
        """
        Score all healthy providers; return the highest-scoring one.
        The preferred provider wins ties within 5% to minimise unnecessary
        backend switches.
        """
        health = self._prov.health
        candidates: List[Tuple[float, Provider]] = []

        for p, s in self._scores.items():
            h = health.get(p)
            if h is None or not h.reachable:
                continue
            if p in (Provider.NEXOS, Provider.XAI):
                if not self._budget.can_spend(max(1, estimated_tokens)):
                    continue
            vram_pen = self._vram_pressure(p) * W_VRAM
            candidates.append((s.score - vram_pen, p))

        if not candidates:
            return self._prov._pick_provider(preferred)  # noqa: SLF001

        best_score, best_p = max(candidates)
        for score, p in candidates:
            if p == preferred and score >= best_score - 0.05:
                return p
        return best_p

    # ── Score update ──────────────────────────────────────────────────────────

    def _record(
        self,
        provider: Provider,
        latency_ms: float,
        tokens_out: int,
        error: bool,
    ) -> None:
        self._scores[provider].update(latency_ms, tokens_out, error)
        if provider in (Provider.NEXOS, Provider.XAI):
            self._budget.spend(tokens_out)
        self._save_state()

    # ── Main route entry points ───────────────────────────────────────────────

    async def route(
        self,
        messages: List[Dict],
        model: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        stream: bool = False,
        force_provider: Optional[Provider] = None,
        ollama_options: Optional[Dict] = None,
        compress_context: Optional[bool] = None,
    ) -> CompletionResponse:
        """
        Route a completion request to the best provider.

        compress_context defaults to self._enable_loci.  Set False to
        skip LOCI compression for a specific call (e.g. embeddings).
        """
        # 1. LOCI context compression
        compress = self._enable_loci if compress_context is None else compress_context
        if compress:
            messages, _ratio = _apply_loci_compression(messages, self._loci_budget)

        # 2. Choose provider
        preferred = force_provider or (
            provider_for_model(model) if model else Provider.LOCAL
        )
        provider = self._pick_by_gradient(preferred, max_tokens)

        # 3a. Native Ollama (non-streaming LOCAL only)
        if self._use_native_ollama and provider == Provider.LOCAL and not stream:
            return await self._dispatch_ollama(
                messages, model, max_tokens, temperature, ollama_options
            )

        # 3b. OpenAI-compat path
        req = CompletionRequest(
            messages=[ChatMessage(role=m["role"], content=m["content"]) for m in messages],
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
            provider=provider,
        )
        t0   = time.monotonic()
        resp = await self._prov.complete(req)
        lat  = (time.monotonic() - t0) * 1000
        self._record(provider, lat, resp.tokens_used, resp.error is not None)
        return resp

    async def stream_route(
        self,
        messages: List[Dict],
        model: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        force_provider: Optional[Provider] = None,
        ollama_options: Optional[Dict] = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming route — yields content chunks."""
        if self._enable_loci:
            messages, _ = _apply_loci_compression(messages, self._loci_budget)

        preferred = force_provider or (
            provider_for_model(model) if model else Provider.LOCAL
        )
        provider = self._pick_by_gradient(preferred)

        if self._use_native_ollama and provider == Provider.LOCAL:
            m = model or PROVIDER_MODELS[Provider.LOCAL]
            async for chunk in self._ollama.stream_chat(
                messages=messages,
                model=m,
                max_tokens=max_tokens,
                temperature=temperature,
                options=ollama_options,
            ):
                yield chunk
            return

        req = CompletionRequest(
            messages=[ChatMessage(role=m2["role"], content=m2["content"]) for m2 in messages],
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
            provider=provider,
        )
        async for chunk in self._prov.stream(req):
            yield chunk

    # ── Ollama native dispatch ────────────────────────────────────────────────

    async def _dispatch_ollama(
        self,
        messages: List[Dict],
        model: Optional[str],
        max_tokens: int,
        temperature: float,
        options: Optional[Dict],
    ) -> CompletionResponse:
        m = model or PROVIDER_MODELS[Provider.LOCAL]
        t0 = time.monotonic()
        try:
            content, tokens_out, latency = await self._ollama.chat(
                messages=messages,
                model=m,
                max_tokens=max_tokens,
                temperature=temperature,
                options=options,
            )
            self._record(Provider.LOCAL, latency, tokens_out, False)
            return CompletionResponse(
                content=content,
                provider=Provider.LOCAL,
                model=m,
                tokens_used=tokens_out,
                latency_ms=latency,
            )
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000
            self._record(Provider.LOCAL, latency, 0, True)
            # Graceful degradation: fall back to OpenAI-compat on Ollama error
            req = CompletionRequest(
                messages=[ChatMessage(role=x["role"], content=x["content"]) for x in messages],
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                provider=Provider.LOCAL,
            )
            return await self._prov.complete(req)

    # ── Inspection / benchmark helpers ───────────────────────────────────────

    async def benchmark_summary(self) -> Dict:
        """Per-provider gradient scores, VRAM pressure, and budget state."""
        out: Dict = {}
        for p, s in self._scores.items():
            out[p.name] = {
                "score":         round(s.score, 4),
                "tok_per_s":     round(s.tok_per_s, 2),
                "error_rate":    round(s.error_rate, 4),
                "vram_pressure": round(self._vram_pressure(p), 3),
                "sample_count":  s.sample_count,
                "reachable":     (
                    self._prov.health.get(p, None) is not None
                    and self._prov.health[p].reachable
                ),
            }
        out["_budget"] = self._budget.to_dict()
        return out

    async def ollama_models(self) -> List[str]:
        """List models currently available in the local Ollama instance."""
        return await self._ollama.list_models()

    async def pin_ollama_model(self, model: str, keep_alive: str = "24h") -> bool:
        """Pin a model in Ollama VRAM for the specified duration."""
        return await self._ollama.set_keep_alive(model, keep_alive)


# ── Standalone smoke test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def main() -> None:
        router = GradientRouter.from_env()
        await router.start()

        print("[gradient_router] benchmark summary (pre-request):")
        summary = await router.benchmark_summary()
        for name, stats in summary.items():
            if name == "_budget":
                print(f"  budget: {stats}")
            else:
                print(
                    f"  {name:14s}  score={stats['score']:.3f}"
                    f"  tok/s={stats['tok_per_s']:.1f}"
                    f"  err={stats['error_rate']:.3f}"
                    f"  vram={stats['vram_pressure']:.2f}"
                    f"  reachable={stats.get('reachable')}"
                )

        print("\n[gradient_router] ollama models:")
        models = await router.ollama_models()
        print(" ", models or "(none / not running)")

        await router.stop()

    asyncio.run(main())
