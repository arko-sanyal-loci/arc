#!/usr/bin/env python3
"""
core/heat.py — Arc hyperbolic heat mathematics (A1–A3)
======================================================
Port of the canonical LOCI heat law from LOCI-MEMORY-ENGINE/loci_engine/heat.py.
This is the single source of truth for the open-interval hyperbolic activation
law that tier placement and evidence accumulation build on.

Authorship: ARKO (definitions, principles), ARC (math formalization),
            CLAUDE (arc-runtime port, session 2026-11-18)

OPEN-INTERVAL INVARIANT (A3)
-----------------------------
No quantity derived from this module ever reaches its theoretical extreme:

  evidence_heat(e, alpha, beta)  strictly inside (alpha, 1.0)

The floor α_k ("Constant of Hope") means heat never reaches zero.
The ceiling clamp means heat never reaches 1.0 (infinite evidence).
Every higher layer (scar heat, competence curves, autonomy) uses the
same curve applied to a different kind of evidence.

Tier boundaries (aligned with LOCI-MEMORY-ENGINE heat_schema.py + Spark Nov 2026):
  HOT   H ≥ 0.80  → VRAM active working set
  WARM  H ≥ 0.35  → DDR5 host RAM
  COLD  H <  0.35 → NVMe archival ledger

VRAM_ADMISSION_THRESHOLD = 0.70 (θ_HOT from Spark sweep) is the
*admission* threshold — a node must cross 0.70 before it enters VRAM.
The *tier boundary* is 0.80. See tiered_memory.py for the distinction.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


# ===========================================================================
# B1 — Memory Spectrum: standard half-life / floor pairs
# ===========================================================================

@dataclass(frozen=True)
class MemoryClass:
    """One row in the Memory Spectrum table.

    name     : human-readable class name
    tau_days : decay half-life in days (τ_k in A1/A2)
    alpha    : Constant of Hope floor (α_k in A2/A3) — strictly inside (0, 1)
    """
    name:     str
    tau_days: float
    alpha:    float


MEMORY_CLASSES: Dict[str, MemoryClass] = {
    # Formative scars are the stickiest: τ=45d keeps the protective
    # pattern hot for six weeks; α=0.14 means it never fully fades.
    "formative_scar": MemoryClass("formative_scar", tau_days=45.0, alpha=0.14),
    # Valued skills decay slower than routine observations.
    "valued_skill": MemoryClass("valued_skill", tau_days=20.0, alpha=0.08),
    # Routine observations are meant to fade quickly.
    "routine_observation": MemoryClass("routine_observation", tau_days=3.0, alpha=0.05),
}

# ===========================================================================
# B2 — Hardware Tier Schema (Spark Nov-2026 sweep, 300 trials)
# ===========================================================================

ALPHA_FLOOR:              float = 0.0500   # Constant of Hope — heat never 0
HEAT_CEILING:             float = 0.9999   # Open-interval ceiling — never 1.0
VRAM_ADMISSION_THRESHOLD: float = 0.700    # θ_HOT: nodes must cross this to enter VRAM
TIER_HOT_BOUNDARY:        float = 0.8000   # H ≥ this → HOT tier (VRAM working set)
TIER_MILD_BOUNDARY:       float = 0.3500   # H ≥ this → MILD tier (Host RAM)
                                            # H <  this → COLD tier (NVMe)

# How quickly nodes decay *through* a tier when unaccessed (days)
TIER_TAU: Dict[str, float] = {
    "hot":  1.25,  # recency-sensitive; cools within ~1.25 days unaccessed
    "mild": 1.75,  # intermediate decay
    "cold": 2.25,  # archival; preserved longer before eviction
}

# Arc-runtime tier label → canonical LOCI tier label
# (arc-runtime uses HOT/WARM/COLD; LOCI uses hot/mild/cold)
_ARC_TO_LOCI: Dict[str, str] = {
    "hot":  "hot",
    "warm": "mild",
    "cold": "cold",
}


# ===========================================================================
# A1 — Evidence Accumulation
# ===========================================================================

def evidence_accumulation(
    events: Sequence[Tuple[float, float, float]],
    *,
    now: Optional[float] = None,
    tau: Optional[float] = None,
) -> float:
    """A1 — Evidence Accumulation.

    Formula
    -------
    E_k(t) = Σ_{j, t_j ≤ t}  r_j · w_j / (1 + (t − t_j) / τ_k)

    The denominator grows hyperbolically with elapsed time, so every past
    observation contributes a nonzero amount forever — evidence decays but
    NEVER reaches zero.

    Parameters
    ----------
    events : sequence of (t_j, relevance, valence_weight) triples
        t_j            — UNIX timestamp of the observation (seconds)
        relevance      — relevance weight, typically in [0, 1]
        valence_weight — signed weight (positive = good, negative = bad)
    now : query time in UNIX seconds. Defaults to time.time().
    tau : half-life in days. Defaults to formative_scar tau (45 days).
          Pass MEMORY_CLASSES[name].tau_days for a different class.

    Returns
    -------
    float — accumulated evidence E_k(now). May be negative if
    valence_weights are negative (net negative evidence accumulation).
    """
    if now is None:
        now = time.time()
    if tau is None:
        tau = MEMORY_CLASSES["formative_scar"].tau_days
    # tau is in days; timestamps are in seconds — normalise
    tau_seconds = tau * 86400.0
    total = 0.0
    for t_j, relevance, valence_weight in events:
        if t_j <= now:
            dt = max(0.0, now - t_j)
            total += (relevance * valence_weight) / (1.0 + dt / tau_seconds)
    return total


# ===========================================================================
# A2 / A3 — Heat Activation (open-interval thermodynamics)
# ===========================================================================

def evidence_heat(
    evidence: float,
    *,
    alpha: Optional[float] = None,
    beta: float = 1.0,
) -> float:
    """A2/A3 — Heat Activation (open-interval thermodynamics).

    Formula
    -------
    H_k(t) = α_k + (1 − α_k)(1 − e^{−β · E_k(t)})

    Open-Interval Proof (A3)
    -------------------------
    - NEVER 0:   α_k > 0 is the floor every heat value approaches but
                 never crosses as evidence → 0 (the "Constant of Hope").
    - NEVER 1:   e^{−βE} > 0 for all finite E, so heat < 1.0 always.
                 A fresh formative scar peaks at ≈0.912 and immediately
                 begins cooling.

    Parameters
    ----------
    evidence : E_k(t) from evidence_accumulation(); must be >= 0.
    alpha    : Constant of Hope floor, strictly inside (0, 1).
               Defaults to formative_scar alpha (0.14).
    beta     : sensitivity parameter (default 1.0).

    Returns
    -------
    float strictly inside (alpha, 1.0).

    Raises
    ------
    ValueError if alpha is not strictly inside (0, 1).
    """
    if alpha is None:
        alpha = MEMORY_CLASSES["formative_scar"].alpha
    if not (0.0 < alpha < 1.0):
        raise ValueError(
            f"alpha must be strictly inside (0, 1) to preserve the "
            f"open-interval invariant (A3); got {alpha!r}"
        )
    raw = alpha + (1.0 - alpha) * (1.0 - math.exp(-beta * max(0.0, evidence)))
    # Nudge away from both bounds so the open-interval invariant holds
    # even at evidence=0 (floor) or evidence→∞ (ceiling).
    return max(alpha + 1e-9, min(1.0 - 1e-9, raw))


def decay(heat_value: float, elapsed_seconds: float, tau_days: float) -> float:
    """Simple one-shot hyperbolic decay: h / (1 + elapsed/tau).

    Use this when a caller already has a scalar heat value and needs to
    age it forward without re-deriving from the full event history.

    Note: scar heat in LOCI deliberately does NOT use this function —
    it always re-derives from the full event history via
    evidence_accumulation → evidence_heat so the open-interval invariant
    holds structurally rather than by convention.

    Parameters
    ----------
    heat_value      : current heat (output of evidence_heat)
    elapsed_seconds : time since the heat was computed
    tau_days        : decay half-life in days

    Returns
    -------
    float — decayed heat value. May fall below alpha_floor if many days
    have elapsed; callers should clamp to alpha_floor if needed.
    """
    tau_seconds = tau_days * 86400.0
    return heat_value / (1.0 + elapsed_seconds / tau_seconds)


def tier(heat_value: float) -> str:
    """Map a heat value to the canonical 3-tier hardware routing label.

    Returns
    -------
    "hot"  → VRAM active working set  (H ≥ 0.80)
    "mild" → Host RAM SymLinkPrev     (H ≥ 0.35)
    "cold" → NVMe archival ledger     (H <  0.35)
    """
    if heat_value >= TIER_HOT_BOUNDARY:
        return "hot"
    if heat_value >= TIER_MILD_BOUNDARY:
        return "mild"
    return "cold"


def vram_eligible(heat_value: float) -> bool:
    """True if a node meets the VRAM admission threshold (θ_HOT = 0.700).

    A node must cross θ_HOT before it is *admitted* to VRAM.
    Once inside, the tier boundary at 0.80 controls *classification*.
    This two-threshold design prevents thrashing on 0.79 ↔ 0.81 oscillation.
    """
    return heat_value >= VRAM_ADMISSION_THRESHOLD


# ===========================================================================
# Gaussian heat conduction (from thermal_gradient.py)
# ===========================================================================

def gaussian_conduction(
    cosine_distance: float,
    sigma: float = 0.32,
) -> float:
    """Gaussian heat conduction kernel over cosine distance.

    conduction = exp(−d² / (2σ²))

    where d is the cosine distance between two node embeddings (in [0, 2]).

    Parameters
    ----------
    cosine_distance : 1 − cosine_similarity, clamped to [0, 2]
    sigma           : spatial dispersion radius (default 0.32)

    Returns
    -------
    float in (0, 1] — 1.0 when d=0 (identical embeddings), asymptotically
    0 as d→∞.
    """
    d = max(0.0, cosine_distance)
    return math.exp(-(d * d) / (2.0 * sigma * sigma))


def diffuse_heat(
    active_heat: float,
    cosine_distance: float,
    sigma: float = 0.32,
    rel_weight: float = 1.0,
) -> float:
    """Heat radiated from one active node to one candidate node.

    Parameters
    ----------
    active_heat     : heat of the radiating (active) node
    cosine_distance : semantic distance from active → candidate
    sigma           : Gaussian kernel width
    rel_weight      : candidate-side relevance weight

    Returns
    -------
    float — additional heat the candidate receives from this active node.
    """
    return active_heat * gaussian_conduction(cosine_distance, sigma) * rel_weight


# ===========================================================================
# Convenience: heat from a single recent access
# ===========================================================================

def heat_from_access(
    memory_class: str = "routine_observation",
    relevance: float = 1.0,
    valence: float = 1.0,
    age_seconds: float = 0.0,
) -> float:
    """Compute the heat produced by a single access event right now.

    Convenience wrapper for the common case: one event, fresh (or aged
    by age_seconds), using a named memory class.

    Parameters
    ----------
    memory_class : one of MEMORY_CLASSES keys
    relevance    : how relevant the access was (0–1)
    valence      : emotional weight (+1 positive, −1 negative)
    age_seconds  : how many seconds ago the access happened (default 0 = now)

    Returns
    -------
    float — heat strictly inside (alpha, 1.0)
    """
    mc = MEMORY_CLASSES.get(memory_class, MEMORY_CLASSES["routine_observation"])
    now = time.time()
    t_event = now - age_seconds
    evidence = evidence_accumulation(
        [(t_event, relevance, valence)],
        now=now,
        tau=mc.tau_days,
    )
    return evidence_heat(evidence, alpha=mc.alpha)
