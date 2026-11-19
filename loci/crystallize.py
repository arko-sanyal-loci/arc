"""
crystallize.py — Cross-model agreement check + CAS affinity update

Crystallisation is the moment a Playground hypothesis graduates (or dies):
  - Multiple models have probed the same hypothesis
  - Their responses are scored for agreement
  - If agreement is sufficient → hypothesis promoted to Compendium
  - CAS affinity table updated based on outcome + user signal

Integrates:
  - cas_router.CASRouter (affinity updates)
  - overlap.py (overlap_c / overlap_d scalars, used as sort key ONLY)
  - playground.Playground (crystallise callback)
  - dual_write.dual_write (persist on promotion)

Design invariants:
  - overlap scalars are tiebreakers for the sort; they are NOT decisions
  - user signal (UserSignal) is the primary affinity driver
  - sovereignty categories are never promoted via API models
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from .slp_encoding import ALPHA, H_MAX
from .cas_router import CASRouter, ProbeResult, UserSignal
from .overlap import DispatchTuple, OverlapScalars, compute_overlap_matrix


# ── Agreement threshold ───────────────────────────────────────────────────────

THETA_AGREE:   float = 0.65   # overlap_c above this → consider "agreement"
THETA_PROMOTE: float = 0.60   # fraction of model pairs that agree to promote


# ── Protocols for injected dependencies ───────────────────────────────────────

class PlaygroundPromoter(Protocol):
    def crystallise_hypothesis(
        self,
        hyp_id:  str,
        content: str,
        heat:    float,
    ) -> None:
        """Promote hypothesis from Playground to Active queue / Compendium."""
        ...

    def expire_hypothesis(self, hyp_id: str, reason: str) -> None:
        """Mark hypothesis as expired/failed in Playground."""
        ...


# ── Crystallisation result ────────────────────────────────────────────────────

@dataclass
class CrystallisationResult:
    hyp_id:        str
    category:      str
    promoted:      bool
    reason:        str
    winner_model:  str | None          # best-performing model
    affinity_updates: list[dict]       # [{model_id, category, signal, new_affinity}]
    agreement_ratio: float             # fraction of pairs that agreed


# ── Scoring probe results ─────────────────────────────────────────────────────

def _score_probe(probe: ProbeResult) -> float:
    """
    Simple quality score for one probe result.
    Replace with a real quality metric (LLM-as-judge, BLEU, etc.)

    Current proxy: 1.0 if succeeded, penalise by latency bucket.
    """
    if not probe.success:
        return 0.0
    # Penalise slow responses: < 2s → 1.0, 2–5s → 0.8, > 5s → 0.6
    if probe.latency_s < 2.0:
        return 1.0
    if probe.latency_s < 5.0:
        return 0.8
    return 0.6


# ── Main crystallise function ─────────────────────────────────────────────────

def crystallise(
    hyp_id:    str,
    category:  str,
    content:   str,
    probes:    list[ProbeResult],
    cas:       CASRouter,
    promoter:  PlaygroundPromoter,
    user_signal: UserSignal | None = None,
    now_s:     int | None = None,
) -> CrystallisationResult:
    """
    Run crystallisation for one Playground hypothesis.

    Args:
        hyp_id:      Hypothesis ID in the Playground
        category:    Category this hypothesis belongs to
        content:     The hypothesis content being evaluated
        probes:      List of ProbeResult from CASRouter.dispatch()
        cas:         CASRouter instance for affinity updates
        promoter:    PlaygroundPromoter for promotion/expiry callbacks
        user_signal: Optional explicit user feedback (overrides derived signal)
        now_s:       Current timestamp (defaults to time.time())

    Returns:
        CrystallisationResult
    """
    if now_s is None:
        now_s = int(time.time())

    successful_probes = [p for p in probes if p.success]

    # Edge case: no successful probes → expire with reason
    if not successful_probes:
        promoter.expire_hypothesis(hyp_id, reason="no_successful_probes")
        return CrystallisationResult(
            hyp_id=hyp_id,
            category=category,
            promoted=False,
            reason="no_successful_probes",
            winner_model=None,
            affinity_updates=[],
            agreement_ratio=0.0,
        )

    # Compute overlap matrix across successful probes
    dispatches = [
        DispatchTuple(
            model_id=p.model_id,
            gpu_id=p.gpu_id,
            category_id=hash(category) & 0xFFFFFFFF,
        )
        for p in successful_probes
    ]
    responses = [p.response_text for p in successful_probes]
    overlap_matrix = compute_overlap_matrix(dispatches, responses)

    # Count agreeing pairs (overlap_c > THETA_AGREE)
    n_probes = len(successful_probes)
    n_pairs = max(1, n_probes * (n_probes - 1) // 2)
    agreeing_pairs = sum(
        1 for scalars in overlap_matrix.values()
        if scalars.overlap_c > THETA_AGREE
    )
    agreement_ratio = agreeing_pairs / n_pairs

    # Score each probe; sort by (quality_score, overlap_c with others)
    probe_scores: dict[str, float] = {}
    for probe in successful_probes:
        base_score = _score_probe(probe)
        # Accumulate overlap_c from all pairs involving this probe
        pair_c_sum = 0.0
        for (i, j), scalars in overlap_matrix.items():
            pi = successful_probes[i]
            pj = successful_probes[j]
            if pi.model_id == probe.model_id or pj.model_id == probe.model_id:
                pair_c_sum += scalars.overlap_c
        probe_scores[probe.model_id] = base_score + 0.1 * pair_c_sum  # overlap_c tiebreaker only

    winner = max(probe_scores, key=lambda m: probe_scores[m])

    # Decide: promote or expire
    promoted = agreement_ratio >= THETA_PROMOTE

    # Affinity updates
    affinity_updates: list[dict] = []
    if user_signal is not None:
        # User signal is the primary driver — update all models in this probe
        for probe in successful_probes:
            new_affinity = cas.update(probe.model_id, category, user_signal)
            affinity_updates.append({
                "model_id":    probe.model_id,
                "category":    category,
                "signal":      user_signal.name,
                "new_affinity": new_affinity,
            })
    else:
        # Derived signal from outcome
        for probe in successful_probes:
            if promoted and probe.model_id == winner:
                derived_signal = UserSignal.ACCEPT_WITH_EDITS
            elif promoted:
                derived_signal = UserSignal.ACCEPT_WITH_EDITS
            else:
                derived_signal = UserSignal.IGNORE
            new_affinity = cas.update(probe.model_id, category, derived_signal)
            affinity_updates.append({
                "model_id":    probe.model_id,
                "category":    category,
                "signal":      derived_signal.name,
                "new_affinity": new_affinity,
            })

    # Partial affinity credit from corroborating overlap
    if promoted and len(successful_probes) > 1:
        for (i, j), scalars in overlap_matrix.items():
            pi = successful_probes[i]
            pj = successful_probes[j]
            if pi.model_id != pj.model_id:
                cas.update_from_overlap(
                    winner=winner,
                    corroborating=pj.model_id if pi.model_id == winner else pi.model_id,
                    category=category,
                    overlap_c=scalars.overlap_c,
                )

    # Execute promotion or expiry
    winner_probe = next(p for p in successful_probes if p.model_id == winner)
    if promoted:
        # Use heat that reflects how strong the agreement was
        promote_heat = max(ALPHA, min(H_MAX, 0.50 + 0.40 * agreement_ratio))
        promoter.crystallise_hypothesis(
            hyp_id=hyp_id,
            content=winner_probe.response_text,
            heat=promote_heat,
        )
        reason = f"agreement_ratio={agreement_ratio:.2f} >= threshold={THETA_PROMOTE}"
    else:
        promoter.expire_hypothesis(
            hyp_id=hyp_id,
            reason=f"agreement_ratio={agreement_ratio:.2f} < threshold={THETA_PROMOTE}",
        )
        reason = f"insufficient_agreement ({agreement_ratio:.2f})"

    return CrystallisationResult(
        hyp_id=hyp_id,
        category=category,
        promoted=promoted,
        reason=reason,
        winner_model=winner if promoted else None,
        affinity_updates=affinity_updates,
        agreement_ratio=agreement_ratio,
    )
