"""Tests for Arc's dynamic context on/offload ledger (core/tiered_memory.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.heat import MEMORY_CLASSES, decay  # noqa: E402
from core.tiered_memory import (  # noqa: E402
    TIER_CAPACITIES, Tier, TieredMemoryManager, plan_migrations, target_tier,
)


@pytest.fixture
def mgr(tmp_path):
    return TieredMemoryManager(nvme_base_path=str(tmp_path),
                               shm_state_path=str(tmp_path / "state.json"))


def test_place_puts_node_in_heat_tier(mgr):
    assert mgr.place("hot", 0.9, 1024).tier is Tier.VRAM
    assert mgr.place("warm", 0.5, 1024).tier is Tier.DDR5
    assert mgr.place("cold", 0.1, 1024).tier is Tier.NVME


def test_heat_drop_produces_a_planned_demotion(mgr):
    """Regression: update_heat used to rewrite .tier itself, so nothing was ever planned."""
    mgr.place("a", 0.9, 1024)
    assert mgr.update_heat("a", 0.2) is not None
    assert mgr.tier_for("a") is Tier.VRAM          # data has not moved yet
    plan = plan_migrations(mgr)
    assert [(p.node_id, t) for p, t in plan.demotions] == [("a", Tier.NVME)]
    assert mgr.commit_migration("a", Tier.NVME)
    assert mgr.tier_for("a") is Tier.NVME
    assert plan_migrations(mgr).demotions == []


def test_heat_rise_produces_a_planned_promotion(mgr):
    mgr.place("b", 0.1, 1024)
    mgr.update_heat("b", 0.95)
    plan = plan_migrations(mgr)
    assert [(p.node_id, t) for p, t in plan.promotions] == [("b", Tier.VRAM)]


def test_vram_hysteresis_prevents_thrash(mgr):
    assert target_tier(Tier.VRAM, 0.75) is Tier.VRAM    # stays (>= 0.70)
    assert target_tier(Tier.DDR5, 0.75) is Tier.DDR5    # not promoted (< 0.80)
    assert target_tier(Tier.VRAM, 0.65) is Tier.DDR5
    mgr.place("c", 0.85, 1024)
    assert mgr.update_heat("c", 0.78) is None           # no migration


def test_decay_does_not_compound_across_ticks(mgr):
    """Regression: decay was re-applied to the decayed value on every tick."""
    p = mgr.place("d", 0.9, 1024)
    t0 = p.last_access_ts
    tau = MEMORY_CLASSES["routine_observation"].tau_days
    for k in range(1, 11):                    # ten ticks, one hour apart
        mgr.decay_heat(now=t0 + k * 3600)
    expected = decay(0.9, 10 * 3600, tau)
    assert p.heat == pytest.approx(expected, rel=1e-9)


def test_decay_respects_alpha_floor_and_pins(mgr):
    p = mgr.place("e", 0.9, 1024)
    q = mgr.place("f", 0.9, 1024, pinned=True)
    mgr.decay_heat(now=p.last_access_ts + 10_000 * 86400)
    floor = MEMORY_CLASSES["routine_observation"].alpha
    assert floor < p.heat < 0.01 + floor
    assert q.heat == 0.9


def test_touch_restarts_decay_from_current_heat(mgr):
    p = mgr.place("g", 0.9, 1024)
    t0 = p.last_access_ts
    mgr.decay_heat(now=t0 + 86400)
    h1 = p.heat
    mgr.touch("g")
    mgr.decay_heat(now=p.last_access_ts)       # zero elapsed since touch
    assert p.heat == pytest.approx(h1)


def test_capacity_override_does_not_leak_between_managers(tmp_path):
    before = TIER_CAPACITIES[Tier.VRAM].total_mb
    TieredMemoryManager(nvme_base_path=str(tmp_path), shm_state_path=str(tmp_path / "s"),
                        vram_total_mb=1)
    assert TIER_CAPACITIES[Tier.VRAM].total_mb == before


def test_plan_respects_capacity(tmp_path):
    m = TieredMemoryManager(nvme_base_path=str(tmp_path), shm_state_path=str(tmp_path / "s"),
                            vram_total_mb=2)
    m.place("big", 0.1, 3 * 1024 * 1024)
    m.update_heat("big", 0.95)
    plan = plan_migrations(m)
    assert plan.promotions == []
    assert plan.skipped and plan.skipped[0][1] == "no_space_in_vram"
