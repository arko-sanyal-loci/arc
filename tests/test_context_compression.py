"""Tests for core/context_compression.py (LOCI LDCE adoption).

The parity test runs Arc's tiering and the canonical LOCI
compute_ldce_tiers() over the same database. It needs a LOCI-MEMORY-ENGINE
checkout: set LOCI_ENGINE_PATH, or keep it as a sibling folder of arc.
"""
from __future__ import annotations

import os
import random
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_sibling = ROOT.parent / "LOCI-MEMORY-ENGINE"
if "LOCI_ENGINE_PATH" not in os.environ and _sibling.is_dir():
    os.environ["LOCI_ENGINE_PATH"] = str(_sibling)

from core import context_compression as cc  # noqa: E402
from core.tiered_memory import Tier, TieredMemoryManager  # noqa: E402

try:
    from loci_engine.storage import StorageEngine  # type: ignore  # noqa: E402
    from loci_engine.session_continuity import compute_ldce_tiers  # type: ignore  # noqa: E402
    HAVE_LOCI = True
except Exception:  # pragma: no cover
    HAVE_LOCI = False


def _items(n=100, seed=7):
    rnd = random.Random(seed)
    heats = rnd.sample(range(1, 10_000), n)            # distinct, no ties
    return [cc.ContextItem(id=f"m{i}", text=f"Fact number {i}. Detail " + "x" * rnd.randint(5, 200),
                           heat=h / 10_000) for i, h in enumerate(heats)]


@pytest.mark.skipif(not HAVE_LOCI, reason="LOCI-MEMORY-ENGINE not importable")
def test_constants_come_from_canonical_loci():
    assert cc.LDCE_SOURCE == "canonical"


@pytest.mark.skipif(not HAVE_LOCI, reason="LOCI-MEMORY-ENGINE not importable")
def test_tiering_matches_canonical_loci_exactly(tmp_path):
    db = str(tmp_path / "context2.db")
    eng = StorageEngine(db)
    items = _items()
    for it in items:
        eng.insert_fact(it.id, "arc.test", it.text, heat=it.heat)
    loci = compute_ldce_tiers(eng)
    arc = cc.compute_tiers(cc.LociMemorySource(db).active_items())
    for tier in ("kernel", "hop", "offload"):
        assert [i["id"] for i in loci[tier]] == [i.id for i in getattr(arc, tier)], tier
    assert loci["stats"]["total_active"] == arc.total_active


def test_tier_caps_and_thresholds():
    t = cc.compute_tiers(_items(200))
    assert len(t.kernel) == cc.KERNEL_MAX_ITEMS
    assert len(t.hop) == cc.HOP_MAX_ITEMS
    assert len(t.offload) == cc.OFFLOAD_MAX_ITEMS
    assert all(i.heat >= cc.KERNEL_HEAT_THRESHOLD for i in t.kernel)
    assert all(i.heat >= cc.HOP_HEAT_THRESHOLD for i in t.hop)


def test_overflow_falls_through_to_lower_tier():
    hot = [cc.ContextItem(id=f"h{i}", text="t", heat=0.9 - i * 0.001) for i in range(10)]
    t = cc.compute_tiers(hot)
    assert [i.id for i in t.hop] == ["h7", "h8", "h9"]


def test_symbol_is_short_phrase():
    s = cc.make_symbol("Arko prefers local-first inference. The remote is expensive and slow.")
    assert s == "Arko prefers local-first inference"
    assert len(cc.make_symbol("word " * 100)) <= cc.SYMBOL_MAX_CHARS + 1


def test_render_compresses_and_respects_budget():
    t = cc.compute_tiers(_items(200))
    out = cc.render(t, token_budget=200)
    assert out.est_tokens <= 200
    assert out.demoted_ids, "kernel should have been demoted to symbols to fit"
    assert set(out.kernel_ids) | set(out.demoted_ids) == {i.id for i in t.kernel}
    assert out.compression_ratio > 1
    for i in t.offload:
        assert i.text not in out.text


def test_render_without_pressure_keeps_full_kernel():
    t = cc.compute_tiers(_items(30))
    out = cc.render(t, token_budget=100_000)
    assert out.demoted_ids == []
    for i in t.kernel:
        assert f"[{i.id}] {i.text}" in out.text


def test_kernel_items_are_pinned_in_vram(tmp_path):
    m = TieredMemoryManager(nvme_base_path=str(tmp_path), shm_state_path=str(tmp_path / "s"))
    t = cc.compute_tiers(_items(100))
    cc.sync_placements(m, t)
    for i in t.kernel:
        assert m.tier_for(i.id) is Tier.VRAM
        assert m._placements[i.id].pinned
    for i in t.hop + t.offload:
        assert not m._placements[i.id].pinned
    # an item leaving the kernel is unpinned and becomes offloadable again
    leaving = t.kernel[0]
    leaving.heat = 0.2
    cc.sync_placements(m, cc.compute_tiers(t.kernel + t.hop + t.offload))
    assert not m._placements[leaving.id].pinned
    assert m._placements[leaving.id].target is Tier.NVME


def test_source_is_read_only(tmp_path):
    db = tmp_path / "c.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE memory_item (id TEXT, namespace TEXT, canonical_text TEXT, heat REAL,"
                  " status TEXT, metadata_json TEXT)")
        c.execute("INSERT INTO memory_item VALUES ('a','n','Alpha fact. More.',0.9,'active','{\"gist\":\"alpha\"}')")
    src = cc.LociMemorySource(str(db))
    assert src.expand("a").gist() == "alpha"
    assert [i.id for i in src.search("Alpha")] == ["a"]          # LIKE fallback, no FTS table
    with pytest.raises(sqlite3.OperationalError):
        with src._conn() as c:
            c.execute("DELETE FROM memory_item")


@pytest.mark.skipif(not HAVE_LOCI, reason="LOCI-MEMORY-ENGINE not importable")
def test_chat_messages_get_loci_system_block(tmp_path, monkeypatch):
    db = str(tmp_path / "context2.db")
    eng = StorageEngine(db)
    eng.insert_fact("k1", "arc.test", "Arko wants local-first inference.", heat=0.9)
    eng.insert_fact("h1", "arc.test", "The remote is expensive. Use it for oversight only.", heat=0.3)
    monkeypatch.setenv("LOCI_DB_PATH", db)
    from app import server
    out = server._with_loci_context([{"role": "user", "content": "hi"}])
    assert out[0]["role"] == "system"
    assert "[k1] Arko wants local-first inference." in out[0]["content"]
    assert "[h1] The remote is expensive" in out[0]["content"]
    assert "Use it for oversight only" not in out[0]["content"]      # hop = symbol only
    assert out[1] == {"role": "user", "content": "hi"}
    monkeypatch.delenv("LOCI_DB_PATH")
    assert server._with_loci_context([{"role": "user", "content": "hi"}]) == [{"role": "user", "content": "hi"}]
