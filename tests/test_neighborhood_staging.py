from __future__ import annotations

import pytest

from core.neighborhood_staging import ContextNode, plan_neighborhood_staging
from core.tiered_memory import Tier


def test_stages_active_context_and_heat_calibrated_one_hop() -> None:
    nodes = {
        "active": ContextNode("active", heat=0.92, tokens=100, neighbors=("hot", "cold")),
        "hot": ContextNode("hot", heat=0.90, tokens=200),
        "cold": ContextNode("cold", heat=0.30, tokens=50),
    }

    plan = plan_neighborhood_staging(
        ["active"],
        nodes,
        heat_gradient=0.85,
        standby_token_budget=256,
    )

    assert plan[0].node_id == "active"
    assert plan[0].target_tier is Tier.VRAM
    assert [item.node_id for item in plan if item.standby] == ["hot", "cold"]
    assert plan[1].effective_heat == pytest.approx(0.765)
    assert plan[1].hop == 1


def test_standby_selection_is_bounded_by_token_budget() -> None:
    nodes = {
        "active": ContextNode("active", heat=0.9, tokens=10, neighbors=("small", "large")),
        "small": ContextNode("small", heat=0.7, tokens=20),
        "large": ContextNode("large", heat=0.9, tokens=200),
    }

    plan = plan_neighborhood_staging(
        ["active"],
        nodes,
        heat_gradient=1.0,
        standby_token_budget=25,
    )

    assert [item.node_id for item in plan if item.standby] == ["small"]


def test_diminishing_returns_can_limit_without_a_token_boundary() -> None:
    nodes = {
        "active": ContextNode("active", heat=0.9, tokens=10, neighbors=("useful", "cold")),
        "useful": ContextNode("useful", heat=0.8, tokens=100),
        "cold": ContextNode("cold", heat=0.1, tokens=10_000),
    }

    plan = plan_neighborhood_staging(
        ["active"],
        nodes,
        heat_gradient=1.0,
        diminishing_return_floor=0.001,
    )

    assert [item.node_id for item in plan if item.standby] == ["useful"]


def test_gradient_and_hop_bounds_are_explicit() -> None:
    with pytest.raises(ValueError, match="heat_gradient"):
        plan_neighborhood_staging([], {}, heat_gradient=0)
    with pytest.raises(ValueError, match="max_hops"):
        plan_neighborhood_staging([], {}, max_hops=2)
