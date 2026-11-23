from __future__ import annotations

import pytest

from core.active_generation import ActiveGenerationController, UtilitySignal


def test_generation_continues_while_utility_improves() -> None:
    controller = ActiveGenerationController(plateau_window=3)

    decisions = [
        controller.observe(UtilitySignal(0.30, 0.30, 0.20)),
        controller.observe(UtilitySignal(0.55, 0.50, 0.35)),
        controller.observe(UtilitySignal(0.75, 0.70, 0.50)),
    ]

    assert all(not decision.stop for decision in decisions)


def test_generation_stops_on_a_sustained_utility_plateau() -> None:
    controller = ActiveGenerationController(
        plateau_window=3,
        min_improvement=0.01,
        min_score_to_stop=0.20,
    )

    for signal in (
        UtilitySignal(0.70, 0.60, 0.50),
        UtilitySignal(0.71, 0.60, 0.50),
    ):
        assert not controller.observe(signal).stop

    decision = controller.observe(UtilitySignal(0.70, 0.61, 0.50))

    assert decision.stop
    assert decision.reason == "utility plateau"


def test_low_value_plateau_does_not_stop_generation() -> None:
    controller = ActiveGenerationController(plateau_window=2, min_score_to_stop=0.5)

    decision = controller.observe(UtilitySignal(0.1, 0.1, 0.1))
    decision = controller.observe(UtilitySignal(0.1, 0.1, 0.1))

    assert not decision.stop


def test_invalid_signal_is_rejected() -> None:
    controller = ActiveGenerationController()

    with pytest.raises(ValueError, match="between 0 and 1"):
        controller.observe(UtilitySignal(1.1, 0.2, 0.2))
