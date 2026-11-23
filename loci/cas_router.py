"""
cas_router.py — Category Adaptive Specialization: cross-model routing.

Routes each category to the model(s) with the best learned affinity.
Dispatches K models in parallel (K usually 2–3).

Affinity update rule:
  affinity(model, category) ← (1−η) · affinity_prev + η · signal(user_action)

  signal:  +1.0  verbatim accept
           +0.5  accept with edits
            0.0  ignore
           −0.5  reject / correct
           −1.0  "not this model"

  η = 0.1 default; 0.3 when affinity < 0.2 (fast escape from bad priors)

Invariant: affinity ∈ (α, 1.0) — open interval (same as heat)
Exception: sovereignty categories (tagged "private") force Local 70B at 1.00.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine, Optional

from slp_encoding import ALPHA, H_MAX, category_hash

logger = logging.getLogger(__name__)


# ── Signal enum ───────────────────────────────────────────────────────────────

class UserSignal(Enum):
    VERBATIM_ACCEPT = auto()   # +1.0
    ACCEPT_WITH_EDITS = auto() # +0.5
    IGNORE = auto()            # 0.0
    REJECT = auto()            # −0.5
    NOT_THIS_MODEL = auto()    # −1.0

SIGNAL_VALUES = {
    UserSignal.VERBATIM_ACCEPT:  +1.0,
    UserSignal.ACCEPT_WITH_EDITS: +0.5,
    UserSignal.IGNORE:            0.0,
    UserSignal.REJECT:           -0.5,
    UserSignal.NOT_THIS_MODEL:   -1.0,
}


# ── Default affinity table ────────────────────────────────────────────────────

DEFAULT_AFFINITIES: dict[str, dict[str, float]] = {
    # category_name → {model_id → affinity}
    "code-review":           {"claude": 0.82, "gpt": 0.71, "local-70b": 0.55},
    "current-events":        {"perplexity": 0.88, "gemini": 0.75, "claude": 0.60},
    "long-form-writing":     {"claude": 0.79, "local-70b": 0.65, "gpt": 0.60},
    "quick-factual-lookup":  {"local-7b": 0.74, "gpt": 0.70, "claude": 0.65},
    "math-proof":            {"gpt": 0.81, "claude": 0.78, "local-70b": 0.55},
    "emotional-nuance-vad":  {"claude": 0.86, "gpt": 0.60},
    "private-sovereign":     {"local-70b": 1.00},   # forced sovereignty
    "architectural-synthesis": {"claude": 0.84, "local-70b": 0.70, "gpt": 0.65},
    "debugging-agent-loops": {"claude": 0.80, "gpt": 0.72, "local-70b": 0.60},
}

# Default affinity when a (model, category) pair has no prior
DEFAULT_PRIOR: float = 0.50

# Sovereignty marker — categories matching this get forced to local-70b
SOVEREIGNTY_CATEGORIES: set[str] = {"private", "private-sovereign", "sensitive"}


# ── CASRouter ────────────────────────────────────────────────────────────────

@dataclass
class ProbeResult:
    """Result returned from one model probe."""
    model_id: str
    category: str
    response: Any
    latency_ms: float = 0.0
    cost_tokens: int = 0


@dataclass
class CASRouter:
    """
    Category Adaptive Specialization router.
    Maintains a learned affinity table; dispatches parallel probes.
    """
    # affinity[category_name][model_id] = float ∈ (α, 1.0)
    affinities: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            cat: dict(models) for cat, models in DEFAULT_AFFINITIES.items()
        }
    )
    # learning rates
    eta_default: float = 0.1
    eta_fast: float = 0.3       # used when affinity < 0.2
    eta_threshold: float = 0.2  # affinity below this → use eta_fast

    def _clamp(self, v: float, sovereign: bool = False) -> float:
        """Clamp affinity to open interval (α, 1.0). Sovereignty gets exactly 1.0 (exception)."""
        if sovereign:
            return 1.0
        return max(ALPHA, min(H_MAX, v))

    # ── Affinity retrieval ────────────────────────────────────────────────

    def affinity(self, model_id: str, category: str) -> float:
        return self.affinities.get(category, {}).get(model_id, DEFAULT_PRIOR)

    def ranked_slate(self, category: str, k: int = 3) -> list[tuple[str, float]]:
        """
        Return top-K (model_id, affinity) pairs for a category, sorted descending.
        Sovereignty: forces a single-model slate at affinity 1.0.
        Cost acts as a tiebreaker — not modeled here, but add cost penalty in production.
        """
        if category in SOVEREIGNTY_CATEGORIES:
            return [("local-70b", 1.0)]

        cat_map = self.affinities.get(category, {})
        if not cat_map:
            # Unknown category — seed with equal priors for all known models
            cat_map = {m: DEFAULT_PRIOR for m in self._all_models()}

        ranked = sorted(cat_map.items(), key=lambda x: -x[1])
        return ranked[:k]

    def _all_models(self) -> set[str]:
        models: set[str] = set()
        for cat_map in self.affinities.values():
            models.update(cat_map.keys())
        return models

    # ── Affinity update ───────────────────────────────────────────────────

    def update(self, model_id: str, category: str, signal: UserSignal) -> float:
        """
        Apply a user signal to the affinity for (model_id, category).
        Returns the new affinity value.
        """
        if category in SOVEREIGNTY_CATEGORIES:
            # Sovereignty affinity is fixed at 1.0 — not updated by signal
            return 1.0

        prev = self.affinity(model_id, category)
        eta = self.eta_fast if prev < self.eta_threshold else self.eta_default
        s = SIGNAL_VALUES[signal]
        new = self._clamp((1.0 - eta) * prev + eta * s)

        if category not in self.affinities:
            self.affinities[category] = {}
        self.affinities[category][model_id] = new

        logger.debug(
            "CAS affinity update model=%s cat=%s signal=%s %.4f → %.4f",
            model_id, category, signal.name, prev, new,
        )
        return new

    def update_from_overlap(
        self,
        winner_model: str,
        corroborating_model: str,
        category: str,
        overlap_c: float,
        theta_agree: float = 0.75,
    ) -> None:
        """
        Partial affinity credit for a corroborating model that agreed with the winner
        (overlap_c > θ_agree = third-party corroboration, per cas_router affinity_rank).
        """
        if overlap_c < theta_agree:
            return
        prev = self.affinity(corroborating_model, category)
        # Partial credit proportional to overlap_c
        credit = 0.5 * overlap_c
        eta = self.eta_fast if prev < self.eta_threshold else self.eta_default
        new = self._clamp((1.0 - eta) * prev + eta * credit)
        if category not in self.affinities:
            self.affinities[category] = {}
        self.affinities[category][corroborating_model] = new
        logger.debug(
            "CAS overlap credit model=%s cat=%s overlap_c=%.4f %.4f → %.4f",
            corroborating_model, category, overlap_c, prev, new,
        )

    # ── Probe dispatch ────────────────────────────────────────────────────

    def dispatch(
        self,
        category: str,
        content: str,
        k: int = 3,
        model_callers: Optional[dict[str, Callable[[str, str], Any]]] = None,
    ) -> list[ProbeResult]:
        """
        Synchronous dispatch to top-K models for a category.
        In production replace with async parallel dispatch.

        model_callers: {model_id: callable(category, content) → response}
        Returns list of ProbeResult.
        """
        slate = self.ranked_slate(category, k)
        results = []

        for model_id, aff in slate:
            if model_callers and model_id in model_callers:
                import time as _time
                t0 = _time.monotonic()
                try:
                    response = model_callers[model_id](category, content)
                    latency = (_time.monotonic() - t0) * 1000
                    results.append(ProbeResult(
                        model_id=model_id,
                        category=category,
                        response=response,
                        latency_ms=latency,
                    ))
                except Exception as e:
                    logger.warning("CAS probe failed model=%s: %s", model_id, e)
            else:
                # No caller registered — record stub result for orchestration
                results.append(ProbeResult(
                    model_id=model_id,
                    category=category,
                    response=None,
                ))

        return results

    # ── Introspection ─────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Dashboard-ready snapshot of affinity table."""
        return {
            cat: dict(sorted(models.items(), key=lambda x: -x[1]))
            for cat, models in sorted(self.affinities.items())
        }
