from __future__ import annotations

import pytest

from core.speculative_pipeline import Complexity
from core.staged_orchestration import (
    StageRole,
    build_handoff_prompt,
    build_staged_orchestration,
)


def test_complex_work_starts_fast_and_research_in_parallel() -> None:
    plan = build_staged_orchestration(0.85)

    assert plan.complexity is Complexity.COMPLEX
    assert plan.has_parallel_research
    assert plan.heavy_is_staged
    assert [assignment.role for assignment in plan.assignments] == [
        StageRole.FAST_SCAFFOLD,
        StageRole.MEDIUM_RESEARCH,
        StageRole.HEAVY_SYNTHESIS,
    ]


def test_trivial_work_uses_the_low_cost_fast_path() -> None:
    plan = build_staged_orchestration(0.10)

    assert plan.complexity is Complexity.TRIVIAL
    assert not plan.has_parallel_research
    assert not plan.heavy_is_staged


def test_handoff_prompt_combines_bounded_working_artifacts() -> None:
    prompt = build_handoff_prompt("Build a parser", "files and interfaces", "API notes")

    assert "Build a parser" in prompt
    assert "files and interfaces" in prompt
    assert "API notes" in prompt


def test_heat_is_bounded() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        build_staged_orchestration(1.1)
