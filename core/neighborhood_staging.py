"""Heat-calibrated one-hop context staging for active token generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from core.tiered_memory import Tier, heat_to_tier


@dataclass(frozen=True)
class ContextNode:
    """A context/KV candidate and its direct neighbourhood."""

    node_id: str
    heat: float
    tokens: int
    neighbors: tuple[str, ...] = ()


@dataclass(frozen=True)
class StagedContext:
    """A planned active or standby context item."""

    node_id: str
    hop: int
    source_heat: float
    effective_heat: float
    tokens: int
    target_tier: Tier
    standby: bool
    marginal_return: float


def plan_neighborhood_staging(
    active_ids: Iterable[str],
    nodes: Mapping[str, ContextNode],
    *,
    heat_gradient: float = 0.85,
    standby_token_budget: int | None = None,
    max_hops: int = 1,
    min_effective_heat: float = 0.05,
    diminishing_return_floor: float = 0.0005,
) -> list[StagedContext]:
    """Plan active context plus a heat-attenuated one-hop standby set.

    ``heat_gradient`` is the retained heat per hop. A value of 1.0 preserves
    heat; lower values make neighbourhood context less likely to be staged.
    Standby selection is limited primarily by diminishing return
    ``effective_heat² / tokens``. ``standby_token_budget`` is an optional
    operational guard, not the residency boundary.

    This planner only produces a residency plan. It does not allocate KV
    memory or perform VRAM/RAM/NVMe I/O.
    """
    if not 0.0 < heat_gradient <= 1.0:
        raise ValueError("heat_gradient must be in (0, 1]")
    if standby_token_budget is not None and standby_token_budget < 0:
        raise ValueError("standby_token_budget must be non-negative")
    if not 0 <= max_hops <= 1:
        raise ValueError("max_hops must be 0 or 1")
    if not 0.0 <= min_effective_heat <= 1.0:
        raise ValueError("min_effective_heat must be between 0 and 1")
    if diminishing_return_floor < 0.0:
        raise ValueError("diminishing_return_floor must be non-negative")

    active = {node_id for node_id in active_ids if node_id in nodes}
    candidates: dict[tuple[str, int], StagedContext] = {}

    for node_id in active:
        node = nodes[node_id]
        candidates[(node_id, 0)] = StagedContext(
            node_id=node_id,
            hop=0,
            source_heat=node.heat,
            effective_heat=node.heat,
            tokens=max(1, node.tokens),
            target_tier=Tier.VRAM,
            standby=False,
            marginal_return=1.0,
        )

    if max_hops:
        for node_id in active:
            for neighbor_id in nodes[node_id].neighbors:
                neighbor = nodes.get(neighbor_id)
                if neighbor is None or neighbor_id in active:
                    continue
                effective_heat = neighbor.heat * heat_gradient
                if effective_heat < min_effective_heat:
                    continue
                candidates[(neighbor_id, 1)] = StagedContext(
                    node_id=neighbor_id,
                    hop=1,
                    source_heat=neighbor.heat,
                    effective_heat=effective_heat,
                    tokens=max(1, neighbor.tokens),
                    target_tier=heat_to_tier(effective_heat),
                    standby=True,
                    marginal_return=(effective_heat ** 2) / max(1, neighbor.tokens),
                )

    active_context = sorted(
        (item for item in candidates.values() if not item.standby),
        key=lambda item: (-item.effective_heat, item.node_id),
    )
    standby_context = sorted(
        (item for item in candidates.values() if item.standby),
        key=lambda item: (
            -item.marginal_return,
            -item.effective_heat,
            item.node_id,
        ),
    )
    selected = list(active_context)
    used_tokens = 0
    for item in standby_context:
        if item.marginal_return < diminishing_return_floor:
            break
        if (
            standby_token_budget is not None
            and used_tokens + item.tokens > standby_token_budget
        ):
            continue
        selected.append(item)
        used_tokens += item.tokens
    return selected


__all__ = ["ContextNode", "StagedContext", "plan_neighborhood_staging"]
