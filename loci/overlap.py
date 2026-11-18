"""
overlap.py — Three overlap scalars per probe pair.

These scalars are NEVER decisions — they are tiebreaker inputs
fed to decisions that already exist elsewhere:

  overlap_w  = 𝟙[m_a = m_b ∧ g_a = g_b ∧ c_a = c_b]
                → wasted identical dispatch (scheduler bug signal)

  overlap_c  = 𝟙[m_a ≠ m_b] · cos_sim(R_a, R_b)
                → convergent cross-model agreement (corroboration signal)

  overlap_d  = 𝟙[m_a ≠ m_b] · (1 − cos_sim(R_a, R_b))
                → divergence (where the hard thinking lives)

Consumers:
  crystallize.py  → sort key = (quality, score, overlap_c, −latency)
  cas_router.py   → partial affinity credit when overlap_c > θ_agree
  fabric/router.py → deprioritize cells with equal heat but high overlap_w
  playground.py   → re-probe priority for hypotheses with high overlap_d
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Sequence


# ── Dispatch tuple ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DispatchTuple:
    """
    Identity of one probe dispatch.
    model_id: which model was called
    gpu_id:   which GPU slot was used ("rtx-5060ti" | "arc-b70" | "cpu")
    category_id: hashed category (from slp_encoding.category_hash)
    """
    model_id: str
    gpu_id: str
    category_id: int


# ── Response embedding ────────────────────────────────────────────────────────

def embed_response(text: str, dim: int = 64) -> list[float]:
    """
    Lightweight deterministic embedding of a text response.
    For production, replace with an actual embedding model (nomic-embed etc.).
    This is a byte-hash fallback for unit testing and local runs.

    Returns a unit-norm float vector of length `dim`.
    """
    # Build a pseudo-embedding by hashing n-grams
    vec = [0.0] * dim
    text_bytes = text.encode("utf-8")
    for i in range(0, len(text_bytes), 4):
        chunk = text_bytes[i:i+4].ljust(4, b"\x00")
        h = int.from_bytes(hashlib.sha256(chunk).digest()[:8], "little")
        idx = h % dim
        vec[idx] += 1.0

    # L2-normalise
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length float vectors."""
    if len(a) != len(b):
        raise ValueError(f"Vector length mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1e-9
    norm_b = math.sqrt(sum(x * x for x in b)) or 1e-9
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


# ── Overlap computation ───────────────────────────────────────────────────────

@dataclass
class OverlapScalars:
    """
    Three overlap scalars for one probe pair (a, b).
    All values are floats in [0, 1] — they are data, not decisions.
    """
    overlap_w: float  # wasted-overlap: 1.0 if identical dispatch tuple
    overlap_c: float  # convergent: cos_sim when models differ
    overlap_d: float  # divergent: 1 − cos_sim when models differ


def compute_overlap(
    dispatch_a: DispatchTuple,
    dispatch_b: DispatchTuple,
    response_a: str,
    response_b: str,
    embedder: callable = embed_response,
) -> OverlapScalars:
    """
    Compute (overlap_w, overlap_c, overlap_d) for a pair of probes.

    Args:
        dispatch_a / dispatch_b: DispatchTuple for each probe
        response_a / response_b: text response from each model
        embedder: function(text) → float vector, defaults to hash-fallback

    Returns:
        OverlapScalars
    """
    same_model = dispatch_a.model_id == dispatch_b.model_id
    same_gpu   = dispatch_a.gpu_id   == dispatch_b.gpu_id
    same_cat   = dispatch_a.category_id == dispatch_b.category_id

    # overlap_w: identical dispatch tuple (wasted compute — scheduler bug)
    if same_model and same_gpu and same_cat:
        return OverlapScalars(overlap_w=1.0, overlap_c=0.0, overlap_d=0.0)

    # Models differ: compute content similarity
    emb_a = embedder(response_a)
    emb_b = embedder(response_b)
    sim = cosine_similarity(emb_a, emb_b)
    sim = max(0.0, sim)  # floor at 0 for overlap semantics

    return OverlapScalars(
        overlap_w=0.0,
        overlap_c=sim,
        overlap_d=1.0 - sim,
    )


# ── Batch helper ──────────────────────────────────────────────────────────────

def compute_overlap_matrix(
    dispatches: list[DispatchTuple],
    responses: list[str],
    embedder: callable = embed_response,
) -> dict[tuple[int, int], OverlapScalars]:
    """
    Compute overlap for all unique pairs from a list of probes.
    Returns {(i, j): OverlapScalars} for i < j.
    """
    n = len(dispatches)
    assert n == len(responses), "dispatches and responses must have equal length"

    # Pre-compute embeddings once
    embeddings = [embedder(r) for r in responses]

    result: dict[tuple[int, int], OverlapScalars] = {}
    for i in range(n):
        for j in range(i + 1, n):
            same_model = dispatches[i].model_id == dispatches[j].model_id
            same_gpu   = dispatches[i].gpu_id   == dispatches[j].gpu_id
            same_cat   = dispatches[i].category_id == dispatches[j].category_id

            if same_model and same_gpu and same_cat:
                scalars = OverlapScalars(overlap_w=1.0, overlap_c=0.0, overlap_d=0.0)
            else:
                sim = cosine_similarity(embeddings[i], embeddings[j])
                sim = max(0.0, sim)
                scalars = OverlapScalars(overlap_w=0.0, overlap_c=sim, overlap_d=1.0 - sim)

            result[(i, j)] = scalars

    return result
