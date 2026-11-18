"""Tests for streaming tier movement (core/tier_stream.py) and draft→target
model staging (core/model_stager.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.model_stager import ModelSpec, ModelStager, PageCacheMover, Phase  # noqa: E402
from core.tier_stream import (  # noqa: E402
    GB, MB, StreamItem, Tier, TierStream, pipelined_load_seconds,
)
from core.tiered_memory import TieredMemoryManager  # noqa: E402

NV = (Tier.NVME, Tier.DDR5)
UP = (Tier.DDR5, Tier.VRAM)


def mk(vram=4 * GB, ddr5=8 * GB, **kw):
    return TierStream(capacity_bytes={Tier.VRAM: vram, Tier.DDR5: ddr5}, **kw)


def bump(s, iid, heat):
    it = s.items[iid]
    s.set_heat(iid, heat, now=it._last_heat_ts + 1.0)


# ── context streaming ────────────────────────────────────────────────────────

def test_promotion_is_pipelined_not_sequential():
    s = mk()
    s.add(StreamItem("a", 1 * GB, 0.95))
    t = s.run_until_idle(dt=0.005)
    seq = 1 * GB / s.link_bw[NV] + 1 * GB / s.link_bw[UP]
    assert s.items["a"].resident is Tier.VRAM
    assert t < seq * 0.9                      # overlapped hops
    assert t == pytest.approx(pipelined_load_seconds(1 * GB), abs=0.02)


def test_never_skips_a_tier():
    s = mk()
    s.add(StreamItem("a", 256 * MB, 0.95))
    s.step(0.001)
    it = s.items["a"]
    assert it.copies[Tier.VRAM] <= it.copies[Tier.DDR5]
    assert all(not (e.src is Tier.NVME and e.dst is Tier.VRAM) for e in s.log)


def test_each_tick_is_bandwidth_bounded():
    s = mk()
    s.add(StreamItem("a", 2 * GB, 0.95))
    s.add(StreamItem("b", 2 * GB, 0.90))
    ev = s.step(0.01)
    moved = sum(e.nbytes for e in ev if (e.src, e.dst) == NV)
    assert moved <= s.link_bw[NV] * 0.01 + 1


def test_relevance_drop_streams_down_through_mild_then_cold():
    s = mk()
    s.add(StreamItem("a", 64 * MB, 0.9), at=Tier.VRAM)
    bump(s, "a", 0.5)                         # hot → mild: VRAM → RAM
    s.run_until_idle()
    a = s.items["a"]
    assert a.resident is Tier.DDR5 and a.copies[Tier.VRAM] == 0
    bump(s, "a", 0.1)                         # mild → cold: RAM → NVMe
    s.run_until_idle()
    assert a.resident is Tier.NVME and a.copies[Tier.DDR5] == 0


def test_vram_hysteresis_keeps_node_between_070_and_080():
    s = mk()
    s.add(StreamItem("a", 64 * MB, 0.9), at=Tier.VRAM)
    bump(s, "a", 0.75)
    s.run_until_idle()
    assert s.items["a"].resident is Tier.VRAM


def test_clean_demotion_is_instant_because_shadow_kept():
    s = mk()
    s.add(StreamItem("a", 1 * GB, 0.95))
    s.run_until_idle()
    bump(s, "a", 0.5)
    ev = s.step(1e-6)                         # almost no bandwidth
    assert s.items["a"].copies[Tier.VRAM] == 0
    assert [e.reason for e in ev] == ["release"]


def test_dirty_item_writes_back_before_release():
    s = mk()
    s.add(StreamItem("kv", 1 * GB, 0.95), at=Tier.VRAM)
    s.items["kv"].dirty = True
    bump(s, "kv", 0.5)
    s.step(0.01)
    kv = s.items["kv"]
    assert kv.copies[Tier.VRAM] == 1 * GB      # not dropped yet
    s.run_until_idle()
    assert kv.resident is Tier.DDR5 and kv.copies[Tier.VRAM] == 0
    assert any(e.reason == "writeback" for e in s.log)
    bump(s, "kv", 0.1)                         # still newer than NVMe
    s.run_until_idle()
    assert kv.copies[Tier.DDR5] == 0 and not kv.dirty
    assert any(e.reason == "writeback" and e.dst is Tier.NVME for e in s.log)


def test_buoyancy_prefetches_rising_items_into_free_space():
    s = mk(lookahead_s=2.0)
    s.add(StreamItem("rising", 64 * MB, 0.20))
    bump(s, "rising", 0.30)                    # +0.1/s → 0.5 within 2 s
    s.run_until_idle()
    r = s.items["rising"]
    assert r.heat < 0.35 and r.resident is Tier.DDR5
    assert any(e.reason == "prefetch" for e in s.log)


def test_prefetch_never_evicts():
    s = mk(ddr5=128 * MB)
    s.add(StreamItem("resident", 128 * MB, 0.4), at=Tier.DDR5)
    s.add(StreamItem("rising", 64 * MB, 0.20))
    bump(s, "rising", 0.30)
    s.run_until_idle()
    assert s.items["resident"].resident is Tier.DDR5
    assert s.items["rising"].copies[Tier.DDR5] == 0


def test_pressure_evicts_colder_but_not_pinned_or_hotter():
    s = mk(vram=256 * MB)
    s.add(StreamItem("pinned", 64 * MB, 0.81, pinned=True), at=Tier.VRAM)
    s.add(StreamItem("hotter", 64 * MB, 0.99), at=Tier.VRAM)
    s.add(StreamItem("cool", 128 * MB, 0.72), at=Tier.VRAM)   # held by hysteresis
    s.add(StreamItem("new", 128 * MB, 0.9))
    s.run_until_idle()
    assert s.items["new"].resident is Tier.VRAM
    assert s.items["cool"].copies[Tier.VRAM] == 0
    assert s.items["pinned"].resident is Tier.VRAM
    assert s.items["hotter"].resident is Tier.VRAM


def test_drives_the_placement_ledger(tmp_path):
    mgr = TieredMemoryManager(nvme_base_path=str(tmp_path),
                              shm_state_path=str(tmp_path / "s.json"))
    mgr.place("n", 0.1, 32 * MB)
    s = mk(manager=mgr)
    s.step(0.01)
    mgr.update_heat("n", 0.9)
    s.run_until_idle()
    assert mgr.tier_for("n") is Tier.VRAM
    mgr.update_heat("n", 0.5)
    s.run_until_idle()
    assert mgr.tier_for("n") is Tier.DDR5


# ── model staging ────────────────────────────────────────────────────────────

B70 = 32 * GB


def stager(target_gb, draft_gb=1.2, keep=True, vram=B70):
    s = mk(vram=vram, ddr5=32 * GB)
    d = ModelSpec.uniform("draft", int(draft_gb * GB), 24, kv_reserve_bytes=512 * MB)
    t = ModelSpec.uniform("target", int(target_gb * GB), 48, kv_reserve_bytes=2 * GB)
    return s, ModelStager(s, d, t, keep_draft_for_speculation=keep)


def test_target_streams_in_while_draft_scaffolds():
    s, st = stager(18)
    st.begin()
    assert st.phase is Phase.SCAFFOLD and st.who_generates() == "draft"
    for _ in range(20):
        st.tick(0.01)
    assert 0 < st.fraction(st.target, Tier.DDR5) < 1
    assert st.draft_resident()                       # draft keeps working


def test_overlap_when_both_fit_then_speculative():
    s, st = stager(18)
    st.begin()
    assert st.scaffold_done() is Phase.OVERLAP
    for _ in range(2000):
        st.tick(0.01)
        if st.speculative_ready():
            break
    assert st.speculative_ready() and "speculative" in st.who_generates()
    assert st.draft_resident()


def test_handoff_when_target_does_not_fit_with_draft():
    s, st = stager(29.5)
    st.begin()
    for _ in range(50):
        st.tick(0.01)
    assert st.scaffold_done() is Phase.HANDOFF
    st.tick(1e-4)
    assert st.fraction(st.draft, Tier.VRAM) == 0        # off the card...
    assert st.fraction(st.draft, Tier.DDR5) == 1        # ...but warm in RAM
    for _ in range(3000):
        st.tick(0.01)
        if st.phase is Phase.TARGET:
            break
    assert st.phase is Phase.TARGET and st.who_generates() == "target"


def test_staging_does_not_evict_hot_context_during_scaffold():
    s, st = stager(31, vram=B70)
    s.add(StreamItem("ctx", 512 * MB, 0.95), at=Tier.VRAM)
    st.begin()
    for _ in range(1000):
        st.tick(0.01)
    assert s.items["ctx"].resident is Tier.VRAM
    st.scaffold_done()                                  # now target outranks it
    for _ in range(1000):
        st.tick(0.01)
    assert s.items["ctx"].copies[Tier.VRAM] == 0


def test_layers_arrive_in_order():
    s, st = stager(8)
    st.begin()
    first_done = {}
    step = 0
    while not st.target_resident() and step < 5000:
        for ev in st.tick(0.002):
            if ev.dst is Tier.VRAM and ev.completed and ev.item_id not in first_done:
                first_done[ev.item_id] = step
        step += 1
    ids = st.target.item_ids()
    assert first_done[ids[0]] <= first_done[ids[-1]]


def test_target_can_load_once_file_is_in_ram():
    s, st = stager(4)
    st.begin()
    assert not st.target_can_load()
    s.run_until_idle()
    assert st.target_can_load()


def test_eta_matches_simulation():
    s, st = stager(6, keep=True)
    st.begin()
    st.scaffold_done()
    eta = st.eta_seconds()
    t = 0.0
    while not st.target_resident():
        st.tick(0.005)
        t += 0.005
    assert t == pytest.approx(eta, rel=0.1)


def test_page_cache_mover_prewarms_the_right_ranges(tmp_path):
    f = tmp_path / "t.gguf"
    f.write_bytes(b"\0" * (8 * MB))
    t = ModelSpec.uniform("t", 8 * MB, 4, path=str(f))
    d = ModelSpec.uniform("d", 1 * MB, 2)
    mover = PageCacheMover([t, d])
    s = mk(mover=mover)
    st = ModelStager(s, d, t)
    st.begin()
    s.run_until_idle()
    mover.close()
    covered = sorted((o, o + n) for _, o, n in mover.calls)
    assert covered[0][0] == 0 and covered[-1][1] == 8 * MB
    assert sum(n for *_, n in mover.calls) == 8 * MB
