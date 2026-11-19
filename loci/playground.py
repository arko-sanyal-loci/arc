"""
playground.py — Playground: Heat(P) tie-breaker loop for unmatched hypotheses.

Unmatched hypotheses from CAS crystallization enter here.
They are NEVER dropped suddenly — they transit the Playground,
where failures feed the L term rather than killing the entry.

Heat(P) = 0.40·E + 0.20·T + 0.25·V + 0.15·L

  E = empirical evidence from prior attempts          [0, 1]
  T = transfer similarity to solved paradigms          [0, 1]
  V = estimated value if the hypothesis crystallizes   [0, 1]
  L = 1/(1 + 0.5·tries) + 0.3·(1 − e^(−failures))   ← failures RAISE Heat(P)

Fate decisions:
  Heat(P) > θ_rerun  → re-probe with varied params
  θ_hold ≤ Heat(P)   → hold, keep observing
  Heat(P) < θ_hold   → hyperbolic decay toward α (never zero — Constant of Hope)

Dream (DSymPrev) + Simulation (SSymPrev) run here 24/7 at the α floor.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

from .slp_encoding import ALPHA, H_MAX, SLPRegister

logger = logging.getLogger(__name__)


# ── Thresholds ────────────────────────────────────────────────────────────────

THETA_RERUN: float = 0.65   # Heat(P) above this → re-probe
THETA_HOLD: float = 0.35    # Heat(P) above this → hold; below → decay
DECAY_GAMMA: float = 0.98   # per-tick hyperbolic decay rate (approaches α, never reaches 0)
MAX_RERUN_CYCLES: int = 12  # after this many reruns with no crystallisation → allow decay


# ── Heat(P) components ────────────────────────────────────────────────────────

@dataclass
class HeatComponents:
    """
    Components of the Heat(P) formula.
    All values are floats in [0, 1].
    """
    E: float = 0.0  # empirical evidence from prior attempts
    T: float = 0.0  # transfer similarity to solved paradigms
    V: float = 0.0  # estimated value if hypothesis crystallizes
    tries: int = 0   # number of re-probe attempts
    failures: int = 0  # number of informative failures

    @property
    def L(self) -> float:
        """
        L = 1/(1 + 0.5·tries) + 0.3·(1 − e^(−failures))
        More failures → higher L → failures rewarded, not punished.
        """
        return 1.0 / (1.0 + 0.5 * self.tries) + 0.3 * (1.0 - math.exp(-self.failures))

    @property
    def heat_p(self) -> float:
        """Heat(P) = 0.40·E + 0.20·T + 0.25·V + 0.15·L, clamped to [α, H_max]."""
        raw = 0.40 * self.E + 0.20 * self.T + 0.25 * self.V + 0.15 * self.L
        return max(ALPHA, min(H_MAX, raw))


# ── Hypothesis ────────────────────────────────────────────────────────────────

class HypothesisState(Enum):
    ACTIVE    = auto()  # currently in the benchmark loop
    RERUN     = auto()  # queued for re-probe with varied params
    DECAYING  = auto()  # heat < θ_hold, drifting toward α
    PROMOTED  = auto()  # crystallised, written to Compendium
    EXPIRED   = auto()  # heat < α + ε (practically zero — still > 0 per B20)


@dataclass
class Hypothesis:
    """A single hypothesis entry in the Playground."""
    hyp_id: str
    category: str
    content: str
    source: str                            # e.g. "cas_crystallize" | "dream" | "simulation"
    slp: SLPRegister
    components: HeatComponents = field(default_factory=HeatComponents)
    state: HypothesisState = HypothesisState.ACTIVE
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_tick: int = field(default_factory=lambda: int(time.time()))
    rerun_count: int = 0
    model_slate: Optional[list[str]] = None  # which models were tried last run
    notes: str = ""                          # human/agent annotation

    @property
    def heat_p(self) -> float:
        return self.components.heat_p

    def record_failure(self) -> None:
        """Call when a re-probe returns no crystallisation."""
        self.components.failures += 1
        self.components.tries += 1
        self.rerun_count += 1
        logger.debug(
            "hypothesis %s failure #%d  Heat(P)=%.4f",
            self.hyp_id, self.components.failures, self.heat_p,
        )

    def record_evidence(self, delta_e: float, delta_t: float = 0.0) -> None:
        """Call when new evidence arrives (partial crystallisation signal)."""
        self.components.E = min(1.0, self.components.E + delta_e)
        self.components.T = min(1.0, self.components.T + delta_t)
        self.components.tries += 1

    def decay_one_tick(self) -> None:
        """Hyperbolic decay toward α. Never reaches α exactly."""
        h = self.slp.heat
        new_h = ALPHA + (h - ALPHA) * DECAY_GAMMA
        new_h = max(ALPHA, min(H_MAX, new_h))
        self.slp = self.slp.with_heat(new_h)
        self.last_tick = int(time.time())


# ── Playground ────────────────────────────────────────────────────────────────

class Playground:
    """
    Holds unmatched hypotheses, runs Heat(P) benchmark loop,
    routes to rerun / hold / decay based on thresholds.

    Thread model: single-threaded tick loop; call tick() on each heartbeat.
    """

    def __init__(
        self,
        rerun_callback: Optional[Callable[[Hypothesis], None]] = None,
        promote_callback: Optional[Callable[[Hypothesis], None]] = None,
        theta_rerun: float = THETA_RERUN,
        theta_hold: float = THETA_HOLD,
    ):
        """
        Args:
            rerun_callback:   called when Heat(P) > θ_rerun (dispatch variated probe)
            promote_callback: called when a hypothesis crystallises (write to Compendium)
            theta_rerun:      Heat(P) threshold for re-probe dispatch
            theta_hold:       Heat(P) threshold below which decay begins
        """
        self._hypotheses: dict[str, Hypothesis] = {}
        self.rerun_callback = rerun_callback or (lambda h: None)
        self.promote_callback = promote_callback or (lambda h: None)
        self.theta_rerun = theta_rerun
        self.theta_hold = theta_hold
        self._tick_count = 0

    # ── Ingest ────────────────────────────────────────────────────────────

    def admit(self, hyp: Hypothesis) -> None:
        """Add a hypothesis to the Playground."""
        # Hold at α floor on entry
        hyp.slp = hyp.slp.with_heat(ALPHA)
        hyp.state = HypothesisState.ACTIVE
        self._hypotheses[hyp.hyp_id] = hyp
        logger.info("Playground admit hyp=%s cat=%s", hyp.hyp_id, hyp.category)

    def admit_new(
        self,
        category: str,
        content: str,
        slp: SLPRegister,
        source: str = "cas_crystallize",
        V_estimate: float = 0.5,
    ) -> Hypothesis:
        """Convenience: create and admit a hypothesis from raw fields."""
        hyp = Hypothesis(
            hyp_id=str(uuid.uuid4()),
            category=category,
            content=content,
            source=source,
            slp=slp.with_heat(ALPHA),
            components=HeatComponents(V=V_estimate),
        )
        self.admit(hyp)
        return hyp

    # ── Crystallisation ───────────────────────────────────────────────────

    def crystallise(self, hyp_id: str, promoted_by: str = "user") -> Optional[Hypothesis]:
        """
        Mark a hypothesis as crystallised (promoted).
        Triggers promote_callback and removes from active set.
        """
        hyp = self._hypotheses.get(hyp_id)
        if hyp is None:
            return None
        hyp.state = HypothesisState.PROMOTED
        hyp.notes += f" [promoted by {promoted_by} at tick {self._tick_count}]"
        self.promote_callback(hyp)
        del self._hypotheses[hyp_id]
        logger.info("Playground crystallise hyp=%s", hyp_id)
        return hyp

    # ── Tick loop ─────────────────────────────────────────────────────────

    def tick(self) -> dict:
        """
        Run one Playground tick. Called at the heartbeat boundary (~140 ms).
        Returns a snapshot dict for the :8877 dashboard.
        """
        self._tick_count += 1
        snapshot = {"tick": self._tick_count, "rerun": [], "decaying": [], "expired": [], "held": []}

        for hyp_id, hyp in list(self._hypotheses.items()):
            if hyp.state in (HypothesisState.PROMOTED, HypothesisState.EXPIRED):
                continue

            hp = hyp.heat_p

            if hp > self.theta_rerun and hyp.rerun_count < MAX_RERUN_CYCLES:
                # Re-probe with varied parameters
                hyp.state = HypothesisState.RERUN
                self.rerun_callback(hyp)
                snapshot["rerun"].append(hyp_id)
                logger.debug("Playground RERUN hyp=%s Heat(P)=%.4f", hyp_id, hp)

            elif hp >= self.theta_hold:
                # Hold — keep observing
                hyp.state = HypothesisState.ACTIVE
                snapshot["held"].append(hyp_id)

            else:
                # Below θ_hold — decay toward α
                hyp.state = HypothesisState.DECAYING
                hyp.decay_one_tick()
                new_h = hyp.slp.heat

                # "Expired" = heat < α + 0.001 (still > 0, per B20 Constant of Hope)
                if new_h <= ALPHA + 0.001:
                    hyp.state = HypothesisState.EXPIRED
                    del self._hypotheses[hyp_id]
                    snapshot["expired"].append(hyp_id)
                    logger.debug("Playground EXPIRED hyp=%s (H→α)", hyp_id)
                else:
                    snapshot["decaying"].append(hyp_id)

        return snapshot

    # ── Dream / Simulation feed ───────────────────────────────────────────

    def feed_dream(self, category: str, content: str, slp: SLPRegister) -> Hypothesis:
        """
        DSymPrev Dream Worker admits a generated 'what-if' hypothesis.
        Runs at α floor 24/7, consuming minimal compute.
        """
        return self.admit_new(category, content, slp, source="dream", V_estimate=0.3)

    def feed_simulation(self, category: str, content: str, slp: SLPRegister, E_seed: float = 0.1) -> Hypothesis:
        """
        SSymPrev Simulation Worker admits a synthetic stress-test result.
        """
        hyp = self.admit_new(category, content, slp, source="simulation", V_estimate=0.4)
        hyp.components.E = min(1.0, E_seed)
        return hyp

    # ── Introspection ─────────────────────────────────────────────────────

    def top_k(self, k: int = 5) -> list[Hypothesis]:
        """Return top-K hypotheses by Heat(P), descending."""
        active = [h for h in self._hypotheses.values()
                  if h.state not in (HypothesisState.PROMOTED, HypothesisState.EXPIRED)]
        return sorted(active, key=lambda h: h.heat_p, reverse=True)[:k]

    def stats(self) -> dict:
        """Dashboard-ready stats."""
        counts = {s: 0 for s in HypothesisState}
        for hyp in self._hypotheses.values():
            counts[hyp.state] += 1
        return {
            "total_active": len(self._hypotheses),
            "tick": self._tick_count,
            "by_state": {s.name: c for s, c in counts.items()},
            "top3": [
                {"id": h.hyp_id, "cat": h.category, "heat_p": round(h.heat_p, 4), "source": h.source}
                for h in self.top_k(3)
            ],
        }

    def __len__(self) -> int:
        return len(self._hypotheses)
