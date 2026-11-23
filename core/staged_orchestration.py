"""Seamless fast/research/heavy orchestration policy for Arc."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.speculative_pipeline import Complexity, classify_complexity


class StageRole(str, Enum):
    FAST_SCAFFOLD = "fast-scaffold"
    MEDIUM_RESEARCH = "medium-research"
    HEAVY_SYNTHESIS = "heavy-synthesis"


@dataclass(frozen=True)
class StageAssignment:
    role: StageRole
    model: str
    starts_immediately: bool
    starts_after_fast: bool


@dataclass(frozen=True)
class StagedOrchestrationPlan:
    """Execution plan for parallel scaffolding, research, and synthesis."""

    complexity: Complexity
    heat: float
    assignments: tuple[StageAssignment, ...]
    handoff_policy: str
    neighborhood_hop: int

    @property
    def has_parallel_research(self) -> bool:
        return any(
            assignment.role is StageRole.MEDIUM_RESEARCH
            and assignment.starts_immediately
            for assignment in self.assignments
        )

    @property
    def heavy_is_staged(self) -> bool:
        return any(
            assignment.role is StageRole.HEAVY_SYNTHESIS
            and assignment.starts_after_fast
            for assignment in self.assignments
        )


def build_staged_orchestration(
    heat: float,
    *,
    sovereignty: bool = False,
    fast_model: str = "qwen3.5:4b-q4_K_M",
    medium_model: str = "gemma4:26b-a4b-it-q4_K_M",
    heavy_model: str = "qwen3.6:27b-q4_K_M",
) -> StagedOrchestrationPlan:
    """Build the user-transparent staged plan from the shared complexity heat."""
    if not 0.0 <= heat <= 1.0:
        raise ValueError("heat must be between 0 and 1")

    complexity = classify_complexity(heat, sovereignty=sovereignty)
    if complexity is Complexity.TRIVIAL:
        assignments = (
            StageAssignment(
                StageRole.FAST_SCAFFOLD,
                fast_model,
                starts_immediately=True,
                starts_after_fast=False,
            ),
        )
        policy = "fast-only"
    else:
        assignments = (
            StageAssignment(
                StageRole.FAST_SCAFFOLD,
                fast_model,
                starts_immediately=True,
                starts_after_fast=False,
            ),
            StageAssignment(
                StageRole.MEDIUM_RESEARCH,
                medium_model,
                starts_immediately=True,
                starts_after_fast=False,
            ),
            StageAssignment(
                StageRole.HEAVY_SYNTHESIS,
                heavy_model,
                starts_immediately=False,
                starts_after_fast=True,
            ),
        )
        policy = "fast-scaffold+parallel-research->heavy-synthesis"

    return StagedOrchestrationPlan(
        complexity=complexity,
        heat=heat,
        assignments=assignments,
        handoff_policy=policy,
        neighborhood_hop=1,
    )


def build_handoff_prompt(
    user_prompt: str,
    fast_scaffold: str,
    research_pass: str,
) -> str:
    """Create the heavy-lane handoff without exposing internal reasoning text."""
    return (
        "Produce the final answer or implementation plan using these bounded "
        "working artifacts. Preserve useful scaffolding, apply verified "
        "research, resolve conflicts, and do not claim checks that were not run.\n\n"
        f"USER REQUEST:\n{user_prompt}\n\n"
        f"FAST SCAFFOLD:\n{fast_scaffold}\n\n"
        f"RESEARCH PASS:\n{research_pass}"
    )


__all__ = [
    "StageAssignment",
    "StageRole",
    "StagedOrchestrationPlan",
    "build_handoff_prompt",
    "build_staged_orchestration",
]
