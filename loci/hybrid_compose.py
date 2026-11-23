"""
hybrid_compose.py — Project committed content to 5 SymPrev streams

Takes a committed Glassbox Package and projects its content into the five
SymPrev stream channels according to the routing matrix ρ(category, stream_i).

SymPrev streams:
  I  — narrative      (plain text, story, prose)
  Q  — scalars        (numbers, structured data, timestamps)
  D  — demoted        (failed hypotheses, uncertainty signals)
  M  — VAD session    (Mood, Affect, Drive of original author)
  S  — synthetic      (rollouts, projections, dream content)

ρ(category, stream_i) is a soft routing weight in [0.0, 1.0].
Content with ρ above a floor threshold (ρ_floor=0.1) is projected
into that stream. If multiple streams qualify, content is tagged
with its primary stream but stored in all qualifying streams.

The composition result is a list of StreamProjection records,
one per (category, stream) pair with ρ above ρ_floor.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import NamedTuple

from slp_encoding import ALPHA, H_MAX, SLPRegister, u16_to_heat


# ── Stream labels ─────────────────────────────────────────────────────────────

STREAM_I = "I"
STREAM_Q = "Q"
STREAM_D = "D"
STREAM_M = "M"
STREAM_S = "S"
ALL_STREAMS = (STREAM_I, STREAM_Q, STREAM_D, STREAM_M, STREAM_S)

# Stream index (for SLP register encoding)
STREAM_IDX: dict[str, int] = {
    STREAM_I: 0,
    STREAM_Q: 1,
    STREAM_D: 2,
    STREAM_M: 3,
    STREAM_S: 4,
}

# ρ floor: streams with routing weight below this are skipped
RHO_FLOOR: float = 0.10


# ── Routing table ρ(category, stream) ────────────────────────────────────────
#
# Default routing matrix. Keys are category prefix patterns.
# Override per deployment by passing a custom rho_table to compose().
#
# Format: {category_prefix: {stream: weight}}
# A missing stream defaults to 0.0 (not projected).

DEFAULT_RHO_TABLE: dict[str, dict[str, float]] = {
    "default": {
        STREAM_I: 0.70,
        STREAM_Q: 0.20,
        STREAM_D: 0.05,
        STREAM_M: 0.05,
        STREAM_S: 0.00,
    },
    "finance": {
        STREAM_I: 0.30,
        STREAM_Q: 0.80,
        STREAM_D: 0.05,
        STREAM_M: 0.05,
        STREAM_S: 0.00,
    },
    "legal": {
        STREAM_I: 0.80,
        STREAM_Q: 0.30,
        STREAM_D: 0.10,
        STREAM_M: 0.00,
        STREAM_S: 0.00,
    },
    "loci-engine": {
        STREAM_I: 0.50,
        STREAM_Q: 0.30,
        STREAM_D: 0.10,
        STREAM_M: 0.00,
        STREAM_S: 0.30,
    },
    "inbox": {
        STREAM_I: 0.70,
        STREAM_Q: 0.10,
        STREAM_D: 0.00,
        STREAM_M: 0.30,
        STREAM_S: 0.00,
    },
    "playground": {
        STREAM_I: 0.20,
        STREAM_Q: 0.10,
        STREAM_D: 0.60,
        STREAM_M: 0.10,
        STREAM_S: 0.70,
    },
    "starred": {
        STREAM_I: 0.80,
        STREAM_Q: 0.40,
        STREAM_D: 0.00,
        STREAM_M: 0.20,
        STREAM_S: 0.00,
    },
    "priority": {
        STREAM_I: 0.70,
        STREAM_Q: 0.30,
        STREAM_D: 0.00,
        STREAM_M: 0.30,
        STREAM_S: 0.00,
    },
    "billo-tech": {
        STREAM_I: 0.60,
        STREAM_Q: 0.40,
        STREAM_D: 0.05,
        STREAM_M: 0.10,
        STREAM_S: 0.20,
    },
}


# ── Routing lookup ────────────────────────────────────────────────────────────

def lookup_rho(
    category: str,
    rho_table: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    """
    Return the ρ vector for a category.
    Matches longest prefix. Falls back to 'default'.
    """
    table = rho_table or DEFAULT_RHO_TABLE

    # Exact match first
    if category in table:
        return table[category]

    # Longest prefix match
    best_key = "default"
    best_len = 0
    for key in table:
        if category.startswith(key) and len(key) > best_len:
            best_key = key
            best_len = len(key)

    return table.get(best_key, table.get("default", {}))


# ── Content extraction by stream ─────────────────────────────────────────────

_SCALAR_RE = re.compile(
    r"\b(\d[\d,]*\.?\d*\s*(USD|EUR|BDT|%|percent|million|billion|units?|rows?|ms|sec|GB|MB|TB))\b",
    re.IGNORECASE,
)

_MOOD_RE = re.compile(
    r"\b(feel|felt|worried|anxious|excited|frustrated|happy|sad|angry|urgent|concerned|confused)\b",
    re.IGNORECASE,
)


def _extract_for_stream(content: str, stream: str) -> str:
    """
    Extract the most relevant content fragment for a given stream.
    For I (narrative): return as-is (truncated to 2000 chars).
    For Q (scalars): extract sentences containing numeric patterns.
    For M (VAD): extract sentences with mood/affect keywords.
    For D/S: return full content (hypothesis/rollout content is unfiltered).
    """
    if stream == STREAM_I:
        return content[:2000]

    if stream == STREAM_Q:
        lines = content.split("\n")
        q_lines = [l for l in lines if _SCALAR_RE.search(l)]
        return "\n".join(q_lines)[:1500] if q_lines else ""

    if stream == STREAM_M:
        sentences = re.split(r"(?<=[.!?])\s+", content)
        m_sentences = [s for s in sentences if _MOOD_RE.search(s)]
        return " ".join(m_sentences)[:1000] if m_sentences else ""

    # D and S: return raw content
    return content[:2000]


# ── Stream projection ─────────────────────────────────────────────────────────

@dataclass
class StreamProjection:
    """
    One projected slice of a Package into a SymPrev stream.
    """
    commit_id:  str
    category:   str
    stream:     str               # one of I/Q/D/M/S
    rho:        float             # routing weight for this (category, stream) pair
    content:    str               # extracted/filtered content for this stream
    heat:       float             # inherited from SLP register
    tokens:     int               # rough token estimate
    timestamp_s: int


def _rough_token_count(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 characters."""
    return max(1, len(text) // 4)


# ── Main compose function ─────────────────────────────────────────────────────

def compose(
    commit_id:   str,
    category:    str,
    content:     str,
    slp:         SLPRegister,
    rho_table:   dict[str, dict[str, float]] | None = None,
    timestamp_s: int | None = None,
    rho_floor:   float = RHO_FLOOR,
) -> list[StreamProjection]:
    """
    Project a committed Package into qualifying SymPrev streams.

    Args:
        commit_id:   Unique ID of the dual-write commit
        category:    Category string
        content:     Plain text content of the package
        slp:         SLPRegister (heat source)
        rho_table:   Override routing weights; defaults to DEFAULT_RHO_TABLE
        timestamp_s: Timestamp to stamp projections (defaults to now)
        rho_floor:   Minimum ρ weight to include a stream

    Returns:
        List of StreamProjection records for each qualifying (category, stream).
    """
    if timestamp_s is None:
        timestamp_s = int(time.time())

    heat = u16_to_heat(slp.heat_raw)
    rho_vec = lookup_rho(category, rho_table)

    projections: list[StreamProjection] = []

    for stream in ALL_STREAMS:
        rho = rho_vec.get(stream, 0.0)
        if rho < rho_floor:
            continue

        extracted = _extract_for_stream(content, stream)
        if not extracted.strip():
            continue

        projections.append(StreamProjection(
            commit_id=commit_id,
            category=category,
            stream=stream,
            rho=rho,
            content=extracted,
            heat=heat,
            tokens=_rough_token_count(extracted),
            timestamp_s=timestamp_s,
        ))

    return projections


def compose_from_package(package, rho_table=None) -> list[StreamProjection]:
    """
    Convenience wrapper: compose() from a dual_write.Package object.
    """
    return compose(
        commit_id=package.commit_id,
        category=package.category,
        content=package.content,
        slp=package.slp,
        rho_table=rho_table,
        timestamp_s=package.timestamp_s,
    )


# ── Batch compose (for bulk ingest) ──────────────────────────────────────────

def compose_batch(
    packages: list,
    rho_table: dict[str, dict[str, float]] | None = None,
) -> dict[str, list[StreamProjection]]:
    """
    Compose a batch of Packages.
    Returns {commit_id: [StreamProjection, ...]} dict.
    """
    result: dict[str, list[StreamProjection]] = {}
    for pkg in packages:
        result[pkg.commit_id] = compose_from_package(pkg, rho_table)
    return result
