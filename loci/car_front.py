"""
car_front.py — Category Adaptive Router: front door (items IN).

Packs every incoming item into a 20-byte SLP register and triages.
Runs entirely in L1 cache: 3 SLP registers per 64-byte cache line.
Never touches DDR5 during the triage decision itself.

CAR routing decisions:
  DROP         → SHA prefix matches recent commit, or source blacklisted
  SCAR→Ledger  → SCAR bit set (B2 sticky trace)
  GOAL→Active  → category_id matches an active Goal (GCSD, dH/dt = 0)
  PLAYGROUND   → item re-surfaces an unmatched hypothesis
  STANDARD     → new content, no shortcut → Glassbox Ingest (6B pipeline)
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional, Set

from .slp_encoding import (
    ALPHA,
    ClassFlags,
    RoutingHint,
    SLPRegister,
    category_hash,
    sha_lo32,
)

logger = logging.getLogger(__name__)


# ── Routing outcome ───────────────────────────────────────────────────────────

class CARDecision(Enum):
    DROP       = auto()
    SCAR       = auto()   # → ledger.jsonl direct
    GOAL       = auto()   # → active queue
    PLAYGROUND = auto()   # → Heat(P) tie-breaker loop
    STANDARD   = auto()   # → Glassbox Ingest


@dataclass
class CARResult:
    decision: CARDecision
    slp: SLPRegister
    source_id: str
    content: bytes
    reason: str = ""


# ── CAR state ─────────────────────────────────────────────────────────────────

@dataclass
class CARState:
    """
    Mutable state for the CAR front door.
    Designed to fit in registers; triage decisions read from committed_sha_cache
    and active_goals_index — both kept tiny and hot.
    """
    node_id: int

    # Recent commit SHA prefixes (32-bit lo) — dedup gate
    committed_sha_cache: Set[int] = field(default_factory=set)
    committed_sha_max: int = 65536  # cap; LRU eviction when exceeded (simple: clear oldest half)

    # Active goal category IDs (from GCSD, dH/dt = 0)
    active_goals_index: Set[int] = field(default_factory=set)

    # Blacklisted source IDs
    blacklist: Set[str] = field(default_factory=set)

    # Playground hypothesis SHA cache (re-surfaces get PLAYGROUND routing)
    playground_sha_cache: Set[int] = field(default_factory=set)

    # Counters (fed to dashboard :8877)
    stats: dict = field(default_factory=lambda: {
        "drop": 0, "scar": 0, "goal": 0, "playground": 0, "standard": 0,
    })

    def register_commit(self, sha_lo: int) -> None:
        """Mark a SHA prefix as recently committed (dedup)."""
        if len(self.committed_sha_cache) >= self.committed_sha_max:
            # Simple half-eviction — not LRU but avoids unbounded growth
            half = set(list(self.committed_sha_cache)[: self.committed_sha_max // 2])
            self.committed_sha_cache = half
        self.committed_sha_cache.add(sha_lo)

    def activate_goal(self, category: str) -> None:
        self.active_goals_index.add(category_hash(category))

    def deactivate_goal(self, category: str) -> None:
        self.active_goals_index.discard(category_hash(category))

    def mark_playground(self, sha_lo: int) -> None:
        self.playground_sha_cache.add(sha_lo)

    def blacklist_source(self, source_id: str) -> None:
        self.blacklist.add(source_id)


# ── Heat seed ─────────────────────────────────────────────────────────────────

def _heat_seed(
    category_id: int,
    msym_sender_vad: Optional[float] = None,
    isym_prev_heat: Optional[float] = None,
) -> float:
    """
    Seed H₀ from MSymPrev (sender VAD) and ISymPrev lookups.
    Both are optional — fall back to ALPHA if unavailable.
    VAD here is a scalar affective energy [0, 1] derived from MSymPrev.
    """
    base = ALPHA
    if msym_sender_vad is not None:
        # Weight VAD toward MILD territory: H₀ = α + 0.4 × VAD × (1 − α)
        base = ALPHA + 0.4 * msym_sender_vad * (1.0 - ALPHA)
    if isym_prev_heat is not None:
        # Blend with ISymPrev heat (prior category activity)
        base = 0.6 * base + 0.4 * isym_prev_heat
    return max(ALPHA, min(0.9999, base))


# ── Main triage function ──────────────────────────────────────────────────────

def triage(
    state: CARState,
    source_id: str,
    category: str,
    content: bytes,
    scar: bool = False,
    msym_sender_vad: Optional[float] = None,
    isym_prev_heat: Optional[float] = None,
) -> CARResult:
    """
    Pack an incoming item into an SLP register and return a routing decision.
    This is the hot path — keep it branchless-ish, no I/O, no DDR5.

    Args:
        state:            mutable CAR state (keeps committed/goal/playground caches)
        source_id:        string identifier for the inbound source (e.g. gmail thread id)
        category:         human-readable category string
        content:          raw bytes of the item (email body, doc chunk, etc.)
        scar:             True if the item carries SCAR class (B2 sticky trace)
        msym_sender_vad:  sender affective energy from MSymPrev [0, 1] or None
        isym_prev_heat:   prior heat for this category from ISymPrev or None

    Returns:
        CARResult with decision + packed SLP register
    """
    cat_id = category_hash(category)
    sha = sha_lo32(content)

    # ── 1. DROP: blacklist or already committed ──────────────────────────
    if source_id in state.blacklist:
        slp = SLPRegister.new(
            state.node_id, category,
            heat=ALPHA,
            content=content,
            routing_hint=RoutingHint.IGNORE,
            extra_flags=ClassFlags.COLD,
        )
        state.stats["drop"] += 1
        logger.debug("CAR DROP blacklist source=%s", source_id)
        return CARResult(CARDecision.DROP, slp, source_id, content, "blacklisted source")

    if sha in state.committed_sha_cache:
        slp = SLPRegister.new(
            state.node_id, category,
            heat=ALPHA,
            content=content,
            routing_hint=RoutingHint.IGNORE,
            extra_flags=ClassFlags.COLD,
        )
        state.stats["drop"] += 1
        logger.debug("CAR DROP dedup sha=%08x", sha)
        return CARResult(CARDecision.DROP, slp, source_id, content, "duplicate SHA")

    # ── Seed heat from MSymPrev + ISymPrev ───────────────────────────────
    h0 = _heat_seed(cat_id, msym_sender_vad, isym_prev_heat)

    # ── 2. SCAR → Ledger direct (B2 sticky trace) ───────────────────────
    if scar:
        slp = SLPRegister.new(
            state.node_id, category,
            heat=h0,
            content=content,
            routing_hint=RoutingHint.FORCE_DUALWRITE,
            extra_flags=ClassFlags.SCAR,
        )
        state.stats["scar"] += 1
        logger.debug("CAR SCAR→Ledger cat=%s", category)
        return CARResult(CARDecision.SCAR, slp, source_id, content, "SCAR class")

    # ── 3. GOAL → Active queue (skip tagging) ───────────────────────────
    if cat_id in state.active_goals_index:
        slp = SLPRegister.new(
            state.node_id, category,
            heat=h0,
            content=content,
            routing_hint=RoutingHint.LOCAL_REQ,
            extra_flags=ClassFlags.GOAL,
        )
        state.stats["goal"] += 1
        logger.debug("CAR GOAL→Active cat=%s", category)
        return CARResult(CARDecision.GOAL, slp, source_id, content, "active goal")

    # ── 4. PLAYGROUND → Heat(P) tie-breaker ─────────────────────────────
    if sha in state.playground_sha_cache:
        slp = SLPRegister.new(
            state.node_id, category,
            heat=ALPHA,  # held at floor
            content=content,
            routing_hint=RoutingHint.SPECULATIVE,
            extra_flags=ClassFlags.PLAYGROUND,
        )
        state.stats["playground"] += 1
        logger.debug("CAR PLAYGROUND sha=%08x cat=%s", sha, category)
        return CARResult(CARDecision.PLAYGROUND, slp, source_id, content, "re-surfaces playground hypothesis")

    # ── 5. STANDARD → Glassbox Ingest ───────────────────────────────────
    slp = SLPRegister.new(
        state.node_id, category,
        heat=h0,
        content=content,
        routing_hint=RoutingHint.LOCAL_REQ,
    )
    state.stats["standard"] += 1
    logger.debug("CAR STANDARD cat=%s H=%.4f", category, h0)
    return CARResult(CARDecision.STANDARD, slp, source_id, content, "new content")


# ── Batch triage (cache-line aligned) ────────────────────────────────────────

def triage_batch(
    state: CARState,
    items: list[tuple[str, str, bytes]],  # (source_id, category, content)
    **kwargs,
) -> list[CARResult]:
    """
    Triage a batch of items. Items are processed in groups of 3
    (one L1 cache line) for alignment purposes.
    kwargs passed to each triage() call (e.g. msym_sender_vad).
    """
    results = []
    for source_id, category, content in items:
        results.append(triage(state, source_id, category, content, **kwargs))
    return results


# ── Dashboard stats snapshot ──────────────────────────────────────────────────

def stats_snapshot(state: CARState) -> dict:
    """
    Returns a dict suitable for the :8877 dashboard.
    """
    total = sum(state.stats.values())
    return {
        "totals": dict(state.stats),
        "total": total,
        "drop_rate": state.stats["drop"] / total if total else 0.0,
        "standard_rate": state.stats["standard"] / total if total else 0.0,
        "committed_cache_size": len(state.committed_sha_cache),
        "active_goals": len(state.active_goals_index),
        "playground_cache_size": len(state.playground_sha_cache),
    }
