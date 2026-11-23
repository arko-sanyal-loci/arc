"""Utility-aware stopping for active token generation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UtilitySignal:
    """Observable value of the latest generated segment."""

    usefulness: float
    depth: float
    criticality: float

    def score(self) -> float:
        """Return the bounded utility score used for plateau detection."""
        return (
            0.45 * self.usefulness
            + 0.35 * self.depth
            + 0.20 * self.criticality
        )


@dataclass(frozen=True)
class GenerationDecision:
    """Decision and explanation produced after observing one segment."""

    stop: bool
    score: float
    improvement: float
    reason: str


class ActiveGenerationController:
    """Stop when semantic utility has plateaued, not at a fixed token count.

    The token budget remains an external safety limit. This controller only
    decides whether another generated segment is likely to add value.
    """

    def __init__(
        self,
        *,
        plateau_window: int = 3,
        min_improvement: float = 0.01,
        min_score_to_stop: float = 0.20,
    ) -> None:
        if plateau_window < 2:
            raise ValueError("plateau_window must be at least 2")
        if min_improvement < 0.0:
            raise ValueError("min_improvement must be non-negative")
        if not 0.0 <= min_score_to_stop <= 1.0:
            raise ValueError("min_score_to_stop must be between 0 and 1")
        self.plateau_window = plateau_window
        self.min_improvement = min_improvement
        self.min_score_to_stop = min_score_to_stop
        self._scores: list[float] = []

    def observe(self, signal: UtilitySignal) -> GenerationDecision:
        """Record a segment and decide whether generation should continue."""
        values = (signal.usefulness, signal.depth, signal.criticality)
        if any(not 0.0 <= value <= 1.0 for value in values):
            raise ValueError("utility signals must be between 0 and 1")

        score = signal.score()
        previous = self._scores[-1] if self._scores else score
        improvement = score - previous
        self._scores.append(score)

        recent = self._scores[-self.plateau_window :]
        plateaued = (
            len(recent) == self.plateau_window
            and recent[-1] >= self.min_score_to_stop
            and max(recent) - min(recent) <= self.min_improvement
        )
        reason = "utility plateau" if plateaued else "utility still increasing"
        return GenerationDecision(
            stop=plateaued,
            score=score,
            improvement=improvement,
            reason=reason,
        )

    def reset(self) -> None:
        """Start a fresh generation decision sequence."""
        self._scores.clear()


__all__ = [
    "ActiveGenerationController",
    "GenerationDecision",
    "UtilitySignal",
]
