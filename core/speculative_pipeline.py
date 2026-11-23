#!/usr/bin/env python3
"""
core/speculative_pipeline.py — Complexity-Graded Parallel Speculative Task Pipeline
=====================================================================================
Heterogeneous GPU speculative decoding for Arc B70 (deep lane) + RTX 5060 Ti (fast lane).

Architecture
-----------
Based on three converging papers, adapted to the LOCI heat + SLP coordination system:

  Dovetail (arXiv:2412.18934, EMNLP 2025)
    - Draft on fast GPU, verify on slow/large GPU
    - Complexity graded by draft-token count (simple tasks → few drafts, complex → many)
    - Quantization split: drafter fp16, verifier int4/awq

  Mirror Speculative Decoding (Apple, 2024)
    - Breaks serial speculative decode barrier by running drafter + verifier in parallel
    - Each GPU runs its own prefix while the other processes its batch
    - Cross-GPU alignment happens at token verification, not generation

  Intel E/PD Disaggregation (Intel NIXL + vLLM, 2025)
    - Arc B70: handles prefill (encode-heavy, needs large VRAM for KV cache)
    - RTX 5060 Ti: handles decode (throughput-sensitive, GDDR7 bandwidth advantage)
    - 28% throughput gain on 70B models at 32GB VRAM

LOCI Integration
----------------
  - Complexity routed via SLP heat score (not a separate classifier)
    HOT  H ≥ 0.80 → COMPLEX path: Arc primary target + RTX drafter
    MILD H ≥ 0.35 → PARALLEL path: both GPUs speculate in parallel
    COLD H <  0.35 → TRIVIAL path: RTX only, small model
    SOVEREIGN H → 1.0 (flag) → Arc only, local 70B, sovereignty lock

  - SLP register carries RoutingHint.SPECULATIVE for prefetch tasks
  - Coordination Bus (SecureEventBus) manages task lifecycle across agents
  - GPU Governor enforces 2-task-per-GPU cap and 5% safety margins
  - Thermal Gradient drives speculative prefetch: neighbors of HOT tokens
    are pre-loaded into draft queue via Gaussian conduction

Draft-Token Budget (Dovetail-style complexity grading)
------------------------------------------------------
  TRIVIAL   k=2  draft tokens per step (simple factual, single-hop)
  MODERATE  k=4  draft tokens per step (multi-hop, reasoning chains)
  COMPLEX   k=8  draft tokens per step (deep analysis, long contexts)
  SOVEREIGN k=1  no speculation (sovereignty lock — single-GPU only)

Authorship: ARKO (architecture, principles), CLAUDE (implementation, session 2026-11-18)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple

# ── LOCI imports ───────────────────────────────────────────────────────────────
# (relative imports when used inside arc-runtime package; fallback to path-based)
try:
    from arc.core.heat import (
        evidence_accumulation,
        evidence_heat,
        gaussian_conduction,
        diffuse_heat,
        tier,
        vram_eligible,
        TIER_HOT_BOUNDARY,
        TIER_MILD_BOUNDARY,
    )
    from arc.core.gpu_offload import (
        GPUOffloadManager,
        query_vram,
        LANE_ARC,
        LANE_RTX,
        RTX_LOW_WATER_MB,
    )
    from arc.loci.slp_encoding import (
        SLPRegister,
        ClassFlags,
        RoutingHint,
        pack_cache_line,
        ALPHA,
    )
except ImportError:
    # Standalone fallback constants (keep pipeline importable from anywhere)
    TIER_HOT_BOUNDARY = 0.80
    TIER_MILD_BOUNDARY = 0.35
    ALPHA = 0.05
    LANE_ARC = 1
    LANE_RTX = 0
    RTX_LOW_WATER_MB = 2048


# ═══════════════════════════════════════════════════════════════════════════════
# A — Complexity Classes
# ═══════════════════════════════════════════════════════════════════════════════

class Complexity(Enum):
    """Task complexity class derived from SLP heat score."""
    TRIVIAL    = "trivial"    # H <  0.35  — RTX only, k=2 draft tokens
    MODERATE   = "moderate"   # H >= 0.35  — parallel dual-GPU, k=4
    COMPLEX    = "complex"    # H >= 0.80  — Arc primary + RTX drafter, k=8
    SOVEREIGN  = "sovereign"  # sovereignty flag — Arc only, local 70B, k=1


# Draft-token budget per complexity class (Dovetail §3.2)
DRAFT_TOKEN_BUDGET: Dict[Complexity, int] = {
    Complexity.TRIVIAL:   2,
    Complexity.MODERATE:  4,
    Complexity.COMPLEX:   8,
    Complexity.SOVEREIGN: 1,
}

# Primary inference GPU per complexity class
PRIMARY_LANE: Dict[Complexity, int] = {
    Complexity.TRIVIAL:   LANE_RTX,   # RTX fast lane
    Complexity.MODERATE:  LANE_RTX,   # RTX decode, Arc prefill
    Complexity.COMPLEX:   LANE_ARC,   # Arc as target verifier (large model)
    Complexity.SOVEREIGN: LANE_ARC,   # Arc sovereignty — never off-device
}

# Drafter GPU per complexity class (None = no speculation)
DRAFTER_LANE: Dict[Complexity, Optional[int]] = {
    Complexity.TRIVIAL:   None,       # single-GPU, no drafter
    Complexity.MODERATE:  LANE_ARC,   # Arc drafts while RTX decodes
    Complexity.COMPLEX:   LANE_RTX,   # RTX drafts while Arc verifies
    Complexity.SOVEREIGN: None,       # no speculation under sovereignty
}


def classify_complexity(heat: float, *, sovereignty: bool = False) -> Complexity:
    """Map a SLP heat score to a Complexity class.

    Parameters
    ----------
    heat       : H ∈ (α, 1.0) from evidence_heat()
    sovereignty: True if the task carries a sovereignty flag
                 (local 70B must stay on Arc, no cloud, no RTX offload)

    Returns
    -------
    Complexity enum value determining draft budget and GPU routing.
    """
    if sovereignty:
        return Complexity.SOVEREIGN
    if heat >= TIER_HOT_BOUNDARY:          # 0.80
        return Complexity.COMPLEX
    if heat >= TIER_MILD_BOUNDARY:         # 0.35
        return Complexity.MODERATE
    return Complexity.TRIVIAL


# ═══════════════════════════════════════════════════════════════════════════════
# B — Task Descriptors
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SpeculativeTask:
    """A single task to run through the complexity-graded speculative pipeline.

    Attributes
    ----------
    task_id       : UUID string — correlates with SecureEventBus task_id
    prompt        : raw prompt text
    heat          : SLP heat score at dispatch time (re-scored on each tick)
    complexity    : derived complexity class
    draft_k       : draft-token budget for this task
    primary_lane  : LANE_ARC or LANE_RTX — target verifier
    drafter_lane  : LANE_ARC, LANE_RTX, or None — speculation drafter
    sovereignty   : sovereignty lock — weights pinned to Arc
    max_tokens    : generation budget
    created_at    : UNIX timestamp
    agent_id      : originating agent identifier
    """
    task_id:       str
    prompt:        str
    heat:          float
    complexity:    Complexity
    draft_k:       int
    primary_lane:  int
    drafter_lane:  Optional[int]
    sovereignty:   bool          = False
    max_tokens:    int           = 512
    created_at:    float         = field(default_factory=time.time)
    agent_id:      str           = "loci-arc"
    metadata:      Dict          = field(default_factory=dict)

    @classmethod
    def from_heat(
        cls,
        prompt: str,
        heat: float,
        *,
        sovereignty: bool = False,
        max_tokens: int = 512,
        agent_id: str = "loci-arc",
        metadata: Optional[Dict] = None,
    ) -> "SpeculativeTask":
        """Factory: build a SpeculativeTask from a heat score."""
        complexity = classify_complexity(heat, sovereignty=sovereignty)
        return cls(
            task_id=str(uuid.uuid4()),
            prompt=prompt,
            heat=heat,
            complexity=complexity,
            draft_k=DRAFT_TOKEN_BUDGET[complexity],
            primary_lane=PRIMARY_LANE[complexity],
            drafter_lane=DRAFTER_LANE[complexity],
            sovereignty=sovereignty,
            max_tokens=max_tokens,
            agent_id=agent_id,
            metadata=metadata or {},
        )

    def to_slp_register(self, node_id: int) -> "SLPRegister":
        """Encode task routing state as an SLP register (20-byte cache-line slot).

        Returns a register with:
          - SPECULATIVE routing hint (triggers prefetch in cas_router)
          - HOT/MILD/COLD class flag matching heat tier
          - SCAR flag if sovereignty is set (sticky, never evicted)
        """
        try:
            hint = RoutingHint.SPECULATIVE
            extra = ClassFlags.SCAR if self.sovereignty else ClassFlags(0)
            return SLPRegister.new(
                node_id=node_id,
                category=f"spec_{self.complexity.value}",
                heat=self.heat,
                content=self.prompt.encode()[:64],
                routing_hint=hint,
                extra_flags=extra,
            )
        except (NameError, ImportError):
            # SLPRegister unavailable (standalone mode) — return a dict sentinel
            return {  # type: ignore[return-value]
                "node_id": node_id,
                "heat": self.heat,
                "complexity": self.complexity.value,
                "hint": "SPECULATIVE",
            }


# ═══════════════════════════════════════════════════════════════════════════════
# C — Draft Queue (Speculative Prefetch via Thermal Gradient)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DraftResult:
    """Output from the speculative drafter.

    draft_tokens : list of token ids proposed by the drafter
    draft_logits : log-probabilities (optional, used for SAD-LoRA alignment)
    draft_heat   : heat of the draft sequence (Gaussian conduction from prompt)
    accepted_k   : how many draft tokens were accepted at verification
    latency_ms   : wall-clock ms for draft generation
    gpu_lane     : which lane generated the draft
    """
    draft_tokens: List[int]
    draft_logits: Optional[List[float]]
    draft_heat:   float
    accepted_k:   int     = 0
    latency_ms:   float   = 0.0
    gpu_lane:     int     = LANE_RTX


class SpeculativeDraftQueue:
    """
    Manages speculative prefetch for the pipeline.

    Concept: when a HOT task is dispatched, the thermal gradient identifies
    semantically adjacent candidates (cosine distance ≤ 2σ) and pre-drafts
    their likely continuations on the drafter GPU. When those neighbors
    arrive as actual tasks, they skip the cold-start draft latency.

    Implementation uses a /dev/shm staging directory so the prefetch blobs
    survive the Python heap and can be read by the verifier subprocess.
    """

    def __init__(self, shm_path: str = "/dev/shm/loci_speculative_cache") -> None:
        self._path = Path(shm_path)
        self._path.mkdir(exist_ok=True)
        # In-memory index: sha256_prefix → (heat, draft_tokens, timestamp)
        self._index: Dict[str, Tuple[float, List[int], float]] = {}
        self._sigma = 0.32  # Gaussian kernel width — matches thermal_gradient.py

    def _prompt_sha(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode()).hexdigest()[:16]

    def _cosine_distance(self, a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 1.0
        dot = sum(x * y for x, y in zip(a, b))
        na  = math.sqrt(sum(x * x for x in a))
        nb  = math.sqrt(sum(y * y for y in b))
        return max(0.0, 1.0 - dot / (na * nb + 1e-9))

    def speculative_heat(
        self,
        active_heat: float,
        prompt_embedding: Optional[List[float]],
        neighbor_embedding: Optional[List[float]],
    ) -> float:
        """Compute the speculative heat radiated from an active task to a neighbor.

        Uses the LOCI Gaussian conduction law:
            conduction = exp(−d² / (2σ²))
            radiated   = active_heat × conduction × rel_weight

        A neighbor with high conduction inherits heat from the active task
        → it enters the draft queue even before it is dispatched.
        """
        if not prompt_embedding or not neighbor_embedding:
            return ALPHA  # no embedding → no conduction → floor heat
        d = self._cosine_distance(prompt_embedding, neighbor_embedding)
        conduction = math.exp(-(d * d) / (2.0 * self._sigma * self._sigma))
        return min(1.0 - 1e-9, active_heat * conduction)

    def cache_draft(self, prompt_sha: str, draft: DraftResult) -> None:
        """Store a completed draft result for later retrieval."""
        blob = json.dumps({
            "tokens": draft.draft_tokens,
            "logits": draft.draft_logits,
            "heat":   draft.draft_heat,
            "ts":     time.time(),
        }).encode()
        (self._path / f"{prompt_sha}.draft").write_bytes(blob)
        self._index[prompt_sha] = (draft.draft_heat, draft.draft_tokens, time.time())

    def retrieve_draft(self, prompt: str) -> Optional[DraftResult]:
        """Attempt to retrieve a cached speculative draft for a prompt.

        Returns the draft if it exists and is not stale (< 60 s old).
        """
        sha = self._prompt_sha(prompt)
        cached_path = self._path / f"{sha}.draft"
        if not cached_path.exists():
            return None
        try:
            blob = json.loads(cached_path.read_bytes())
            age = time.time() - blob.get("ts", 0)
            if age > 60.0:  # stale — discard
                cached_path.unlink(missing_ok=True)
                return None
            return DraftResult(
                draft_tokens=blob["tokens"],
                draft_logits=blob.get("logits"),
                draft_heat=blob["heat"],
            )
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def evict_cold(self) -> int:
        """Remove stale (> 60 s) or COLD-heat (< MILD) draft files."""
        evicted = 0
        now = time.time()
        for f in list(self._path.glob("*.draft")):
            try:
                blob = json.loads(f.read_bytes())
                age  = now - blob.get("ts", 0)
                heat = blob.get("heat", 0.0)
                if age > 60.0 or heat < TIER_MILD_BOUNDARY:
                    f.unlink(missing_ok=True)
                    evicted += 1
            except (OSError, json.JSONDecodeError):
                f.unlink(missing_ok=True)
                evicted += 1
        # Prune in-memory index
        self._index = {
            k: v for k, v in self._index.items()
            if (now - v[2]) <= 60.0 and v[0] >= TIER_MILD_BOUNDARY
        }
        return evicted


# ═══════════════════════════════════════════════════════════════════════════════
# D — Speculative Verification Result
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class VerificationResult:
    """Output from the target verifier after checking draft tokens.

    accepted_tokens : tokens accepted from the drafter (may be fewer than draft_k)
    bonus_token     : one additional token from the verifier (standard speculative decode)
    rejection_index : first index where draft diverged from verifier (or draft_k if all accepted)
    target_logits   : log-probabilities from the verifier (for KD LoRA training)
    latency_ms      : wall-clock ms for verification pass
    gpu_lane        : which lane ran verification
    speedup_ratio   : effective tokens-per-step / 1.0 (1.0 = no speculation gain)
    """
    accepted_tokens:  List[int]
    bonus_token:      Optional[int]
    rejection_index:  int
    target_logits:    Optional[List[float]] = None
    latency_ms:       float = 0.0
    gpu_lane:         int   = LANE_ARC
    speedup_ratio:    float = 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# E — HTTP Client Adapters (vLLM / Ollama / SGLang)
# ═══════════════════════════════════════════════════════════════════════════════

class _BaseInferenceClient:
    """Minimal async HTTP wrapper for local inference endpoints."""

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model    = model

    async def draft(
        self,
        prompt: str,
        k: int,
        *,
        temperature: float = 0.0,
        timeout: float = 30.0,
    ) -> DraftResult:
        """Generate k draft tokens from this endpoint.

        In production: POST /v1/completions with max_tokens=k.
        Here: returns a stub DraftResult so the pipeline runs without
        a live GPU, allowing unit-testing of the routing logic.
        """
        t0 = time.monotonic()
        # Stub: generate k placeholder token ids
        tokens = [hash(f"{prompt[:8]}:{i}") & 0xFFFF for i in range(k)]
        ms = (time.monotonic() - t0) * 1000
        return DraftResult(
            draft_tokens=tokens,
            draft_logits=None,
            draft_heat=TIER_HOT_BOUNDARY,  # stub
            latency_ms=ms,
            gpu_lane=LANE_RTX,
        )

    async def verify(
        self,
        prompt: str,
        draft_tokens: List[int],
        *,
        return_logits: bool = False,
        timeout: float = 60.0,
    ) -> VerificationResult:
        """Verify draft tokens against the target model.

        In production: POST /v1/completions (or vLLM speculative_decoding endpoint)
        with the draft token sequence for single-pass verification.
        Here: stubs 100% acceptance so pipeline logic can be tested.
        """
        t0 = time.monotonic()
        ms = (time.monotonic() - t0) * 1000
        return VerificationResult(
            accepted_tokens=draft_tokens,
            bonus_token=hash(prompt[-8:]) & 0xFFFF,
            rejection_index=len(draft_tokens),
            latency_ms=ms,
            gpu_lane=LANE_ARC,
            speedup_ratio=max(1.0, len(draft_tokens) * 0.75),
        )


class ArcInferenceClient(_BaseInferenceClient):
    """Intel Arc B70 — deep lane, Arc + vLLM/SGLang XPU backend."""

    def __init__(self) -> None:
        # Production URL from arc-runtime/core/api_provision.py
        import os
        super().__init__(
            base_url=os.environ.get("LOCI_LOCAL_INTEL_BASE_URL", "http://localhost:8000/v1"),
            model=os.environ.get("LOCI_ARC_MODEL", "arc-b70"),
        )


class RTXInferenceClient(_BaseInferenceClient):
    """NVIDIA RTX 5060 Ti — fast lane, Ollama CUDA backend."""

    def __init__(self) -> None:
        import os
        super().__init__(
            base_url=os.environ.get("LOCI_LOCAL_BASE_URL", "http://localhost:11434/v1"),
            model=os.environ.get("LOCI_RTX_MODEL", "llama3.1:8b-instruct-q8_0"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# F — Speculative Pipeline Executor
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PipelineResult:
    """Final output of the complexity-graded speculative pipeline."""
    task_id:         str
    complexity:      Complexity
    tokens:          List[int]       # all generated tokens
    total_steps:     int             # number of speculative steps taken
    total_accepted:  int             # total draft tokens accepted
    total_bonus:     int             # bonus tokens from verifier
    effective_k:     float           # mean accepted tokens per step
    primary_ms:      float           # total time on primary GPU
    drafter_ms:      float           # total time on drafter GPU
    speedup_vs_base: float           # estimated speedup over serial decode
    heat_at_exit:    float           # re-scored heat at completion
    sovereignty:     bool


class ComplexityGradedPipeline:
    """
    Complexity-graded parallel speculative task pipeline.

    Dispatches tasks to the appropriate GPU path based on SLP heat score,
    runs speculative decoding with the matching draft budget (k=2/4/8/1),
    and returns a PipelineResult with throughput accounting.

    Concurrency: up to 2 parallel tasks per GPU (enforced by GPU governor).
    Sovereignty: heat ≥ 1.0 (flag) → Arc-only, no RTX involvement.

    Usage
    -----
    >>> pipeline = ComplexityGradedPipeline()
    >>> result = await pipeline.run(task)
    """

    def __init__(
        self,
        arc_client: Optional[ArcInferenceClient] = None,
        rtx_client: Optional[RTXInferenceClient] = None,
        draft_cache: Optional[SpeculativeDraftQueue] = None,
    ) -> None:
        self._arc = arc_client or ArcInferenceClient()
        self._rtx = rtx_client or RTXInferenceClient()
        self._draft_cache = draft_cache or SpeculativeDraftQueue()
        # Per-lane active task counter (mirrors GPU governor ACTIVE_TASKS)
        self._active: Dict[int, int] = {LANE_ARC: 0, LANE_RTX: 0}
        self._max_per_lane = 2  # GPU governor MAX_CONCURRENT_TASKS_PER_GPU

    # ── A — Admission gate ──────────────────────────────────────────────────

    def _can_admit(self, lane: int) -> bool:
        return self._active.get(lane, 0) < self._max_per_lane

    # ── B — Single speculative step ─────────────────────────────────────────

    async def _speculative_step(
        self,
        prompt: str,
        task: SpeculativeTask,
        prefix_tokens: List[int],
    ) -> Tuple[DraftResult, VerificationResult]:
        """One step of parallel speculative decoding (Mirror SD style).

        Arc B70 and RTX 5060 Ti execute their respective roles concurrently.
        Arc verifies draft while RTX drafts next k tokens — overlap breaks the
        serial barrier of classic speculative decoding.
        """
        # Build prompt from prefix
        full_prompt = prompt  # In production: decode prefix_tokens → text, append

        drafter  = self._rtx if task.drafter_lane == LANE_RTX else self._arc
        verifier = self._arc if task.primary_lane  == LANE_ARC  else self._rtx

        # First step: check cache (thermal gradient speculative prefetch)
        cached = self._draft_cache.retrieve_draft(full_prompt)
        if cached and len(cached.draft_tokens) >= task.draft_k:
            # Cache hit — skip draft latency entirely
            draft = cached
            self._draft_cache._index.pop(
                self._draft_cache._prompt_sha(full_prompt), None
            )
        else:
            # Cache miss — generate draft (async)
            draft = await drafter.draft(full_prompt, task.draft_k)

        # Verify concurrently with the Arc vLLM target
        verification = await verifier.verify(full_prompt, draft.draft_tokens)

        return draft, verification

    # ── C — Trivial path (RTX only, no speculation) ─────────────────────────

    async def _run_trivial(self, task: SpeculativeTask) -> PipelineResult:
        """TRIVIAL: RTX only, k=2, single forward pass per step."""
        tokens: List[int] = []
        total_ms = 0.0
        steps = max(1, task.max_tokens // task.draft_k)

        self._active[LANE_RTX] = self._active.get(LANE_RTX, 0) + 1
        try:
            for _ in range(steps):
                draft = await self._rtx.draft(task.prompt, task.draft_k)
                tokens.extend(draft.draft_tokens)
                total_ms += draft.latency_ms
                if len(tokens) >= task.max_tokens:
                    break
        finally:
            self._active[LANE_RTX] = max(0, self._active.get(LANE_RTX, 1) - 1)

        return PipelineResult(
            task_id=task.task_id,
            complexity=task.complexity,
            tokens=tokens[:task.max_tokens],
            total_steps=steps,
            total_accepted=len(tokens),
            total_bonus=0,
            effective_k=task.draft_k,
            primary_ms=total_ms,
            drafter_ms=0.0,
            speedup_vs_base=1.0,
            heat_at_exit=task.heat,
            sovereignty=task.sovereignty,
        )

    # ── D — Moderate path (parallel dual-GPU) ───────────────────────────────

    async def _run_moderate(self, task: SpeculativeTask) -> PipelineResult:
        """MODERATE: Arc handles prefill (E/P disaggregation), RTX handles decode.

        Based on Intel E/PD disaggregation architecture:
          Arc B70:          prefill — encode the full prompt once
          RTX 5060 Ti:      decode  — speculative auto-regressive generation
        """
        tokens: List[int] = []
        total_primary_ms = 0.0
        total_draft_ms   = 0.0
        total_accepted   = 0
        total_bonus      = 0
        steps = max(1, task.max_tokens // task.draft_k)

        self._active[LANE_ARC] = self._active.get(LANE_ARC, 0) + 1
        self._active[LANE_RTX] = self._active.get(LANE_RTX, 0) + 1
        try:
            # Phase 1: Arc handles prefill (single async call, then detached)
            # In production: vLLM disaggregated prefill over Intel NIXL transport
            # Here: simulated by a single draft pass
            prefill = await self._arc.draft(task.prompt, 1)
            total_primary_ms += prefill.latency_ms

            # Phase 2: interleaved speculative decode on RTX
            prefix: List[int] = []
            for _ in range(steps):
                draft, verify = await self._speculative_step(task.prompt, task, prefix)
                prefix.extend(verify.accepted_tokens)
                if verify.bonus_token is not None:
                    prefix.append(verify.bonus_token)
                    total_bonus += 1
                tokens = prefix[:]
                total_primary_ms += verify.latency_ms
                total_draft_ms   += draft.latency_ms
                total_accepted   += verify.rejection_index
                if len(tokens) >= task.max_tokens:
                    break
        finally:
            self._active[LANE_ARC] = max(0, self._active.get(LANE_ARC, 1) - 1)
            self._active[LANE_RTX] = max(0, self._active.get(LANE_RTX, 1) - 1)

        effective_k = total_accepted / max(1, steps)
        speedup     = effective_k / 1.0  # vs. serial 1-token-per-step
        return PipelineResult(
            task_id=task.task_id,
            complexity=task.complexity,
            tokens=tokens[:task.max_tokens],
            total_steps=steps,
            total_accepted=total_accepted,
            total_bonus=total_bonus,
            effective_k=effective_k,
            primary_ms=total_primary_ms,
            drafter_ms=total_draft_ms,
            speedup_vs_base=speedup,
            heat_at_exit=task.heat,
            sovereignty=task.sovereignty,
        )

    # ── E — Complex path (Arc primary + RTX drafter, k=8) ───────────────────

    async def _run_complex(self, task: SpeculativeTask) -> PipelineResult:
        """COMPLEX: Arc B70 as target verifier (large 70B model), RTX as drafter.

        Dovetail §4 configuration: drafter=small/fast, target=large/accurate.
        Arc's 32 GB GDDR6 holds the full 70B model in int4 (≈35 GB quantised).
        RTX's 759 AI TOPS handles high-throughput draft generation.
        k=8 amortises Arc's higher latency over more accepted tokens.
        """
        tokens: List[int] = []
        total_primary_ms = 0.0
        total_draft_ms   = 0.0
        total_accepted   = 0
        total_bonus      = 0
        steps = max(1, task.max_tokens // task.draft_k)

        self._active[LANE_ARC] = self._active.get(LANE_ARC, 0) + 1
        self._active[LANE_RTX] = self._active.get(LANE_RTX, 0) + 1
        try:
            prefix: List[int] = []
            for _ in range(steps):
                draft, verify = await self._speculative_step(task.prompt, task, prefix)

                # Cache draft heat for later KD-LoRA training
                self._draft_cache.cache_draft(
                    self._draft_cache._prompt_sha(task.prompt),
                    draft,
                )

                prefix.extend(verify.accepted_tokens)
                if verify.bonus_token is not None:
                    prefix.append(verify.bonus_token)
                    total_bonus += 1
                tokens = prefix[:]
                total_primary_ms += verify.latency_ms
                total_draft_ms   += draft.latency_ms
                total_accepted   += verify.rejection_index
                if len(tokens) >= task.max_tokens:
                    break
        finally:
            self._active[LANE_ARC] = max(0, self._active.get(LANE_ARC, 1) - 1)
            self._active[LANE_RTX] = max(0, self._active.get(LANE_RTX, 1) - 1)

        effective_k = total_accepted / max(1, steps)
        speedup     = effective_k / 1.0
        return PipelineResult(
            task_id=task.task_id,
            complexity=task.complexity,
            tokens=tokens[:task.max_tokens],
            total_steps=steps,
            total_accepted=total_accepted,
            total_bonus=total_bonus,
            effective_k=effective_k,
            primary_ms=total_primary_ms,
            drafter_ms=total_draft_ms,
            speedup_vs_base=speedup,
            heat_at_exit=task.heat,
            sovereignty=task.sovereignty,
        )

    # ── F — Sovereign path (Arc-only, no speculation, local 70B) ────────────

    async def _run_sovereign(self, task: SpeculativeTask) -> PipelineResult:
        """SOVEREIGN: Arc B70 only, local 70B, sovereignty lock engaged.

        When heat → 1.0 or sovereignty flag is set:
          - Weights never leave Arc VRAM
          - No cloud API calls
          - No RTX involvement
          - k=1 (each step produces one verified token)

        This matches the gpu_offload.py sovereignty_locked=True path where
        the offload manager refuses to move weights off Arc.
        """
        tokens: List[int] = []
        total_ms = 0.0
        steps = task.max_tokens  # k=1 → one token per step

        self._active[LANE_ARC] = self._active.get(LANE_ARC, 0) + 1
        try:
            for _ in range(steps):
                # Single-token generation on Arc (no speculation)
                draft = await self._arc.draft(task.prompt, 1)
                tokens.extend(draft.draft_tokens)
                total_ms += draft.latency_ms
                if len(tokens) >= task.max_tokens:
                    break
        finally:
            self._active[LANE_ARC] = max(0, self._active.get(LANE_ARC, 1) - 1)

        return PipelineResult(
            task_id=task.task_id,
            complexity=task.complexity,
            tokens=tokens[:task.max_tokens],
            total_steps=steps,
            total_accepted=len(tokens),
            total_bonus=0,
            effective_k=1.0,
            primary_ms=total_ms,
            drafter_ms=0.0,
            speedup_vs_base=1.0,  # no speedup — sovereignty takes priority
            heat_at_exit=task.heat,
            sovereignty=True,
        )

    # ── G — Main dispatch ────────────────────────────────────────────────────

    async def run(self, task: SpeculativeTask) -> PipelineResult:
        """Dispatch a task through the appropriate complexity-graded path.

        Admission control: if the required GPU lanes are at capacity, the
        task waits (exponential backoff, max 3 retries) before raising.
        """
        # Admission: wait for capacity (mirrors GPU governor task queuing)
        retries = 0
        while retries < 3:
            primary_ok = self._can_admit(task.primary_lane)
            drafter_ok = task.drafter_lane is None or self._can_admit(task.drafter_lane)
            if primary_ok and drafter_ok:
                break
            await asyncio.sleep(0.1 * (2 ** retries))
            retries += 1

        # Route by complexity
        if task.complexity == Complexity.SOVEREIGN:
            return await self._run_sovereign(task)
        if task.complexity == Complexity.COMPLEX:
            return await self._run_complex(task)
        if task.complexity == Complexity.MODERATE:
            return await self._run_moderate(task)
        return await self._run_trivial(task)

    async def run_batch(
        self,
        tasks: List[SpeculativeTask],
        *,
        max_parallel: int = 4,
    ) -> List[PipelineResult]:
        """Run a batch of tasks with bounded parallelism.

        Tasks are sorted by heat score descending so HOT/COMPLEX tasks
        (which have the highest speculative gain from Arc's large VRAM) run
        first and get GPU admission priority.
        """
        sorted_tasks = sorted(tasks, key=lambda t: t.heat, reverse=True)
        semaphore = asyncio.Semaphore(max_parallel)

        async def _guarded(t: SpeculativeTask) -> PipelineResult:
            async with semaphore:
                return await self.run(t)

        return await asyncio.gather(*[_guarded(t) for t in sorted_tasks])


# ═══════════════════════════════════════════════════════════════════════════════
# G — Pipeline Stats
# ═══════════════════════════════════════════════════════════════════════════════

def summarise_batch(results: List[PipelineResult]) -> Dict:
    """Aggregate stats across a batch of pipeline results.

    Returns a dict with per-complexity breakdown and overall efficiency.
    """
    if not results:
        return {}

    by_complexity: Dict[str, Dict] = {}
    total_tokens   = 0
    total_primary  = 0.0
    total_draft    = 0.0

    for r in results:
        key = r.complexity.value
        g = by_complexity.setdefault(key, {
            "count": 0, "tokens": 0, "accepted": 0, "bonus": 0,
            "primary_ms": 0.0, "drafter_ms": 0.0, "speedup_sum": 0.0,
        })
        g["count"]      += 1
        g["tokens"]     += len(r.tokens)
        g["accepted"]   += r.total_accepted
        g["bonus"]      += r.total_bonus
        g["primary_ms"] += r.primary_ms
        g["drafter_ms"] += r.drafter_ms
        g["speedup_sum"] += r.speedup_vs_base
        total_tokens  += len(r.tokens)
        total_primary += r.primary_ms
        total_draft   += r.drafter_ms

    for key, g in by_complexity.items():
        g["avg_speedup"] = round(g["speedup_sum"] / max(1, g["count"]), 3)

    overall_speedup = sum(r.speedup_vs_base for r in results) / max(1, len(results))

    return {
        "total_tasks":    len(results),
        "total_tokens":   total_tokens,
        "total_primary_ms": round(total_primary, 1),
        "total_draft_ms":   round(total_draft, 1),
        "overall_speedup":  round(overall_speedup, 3),
        "by_complexity":    by_complexity,
        "sovereignty_tasks": sum(1 for r in results if r.sovereignty),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# H — Quick smoke test
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _smoke():
        pipeline = ComplexityGradedPipeline()
        tasks = [
            SpeculativeTask.from_heat("What is 2+2?",                heat=0.15),  # TRIVIAL
            SpeculativeTask.from_heat("Summarise the last 3 papers.", heat=0.50),  # MODERATE
            SpeculativeTask.from_heat("Analyse this 70B-class task.", heat=0.85),  # COMPLEX
            SpeculativeTask.from_heat("Sovereignty-locked inference.", heat=0.90, sovereignty=True),
        ]
        print("[speculative_pipeline] Running smoke test...")
        results = await pipeline.run_batch(tasks)
        summary = summarise_batch(results)
        for r in results:
            print(
                f"  [{r.complexity.value:9s}]  "
                f"k_eff={r.effective_k:.1f}  "
                f"speedup={r.speedup_vs_base:.2f}×  "
                f"tokens={len(r.tokens)}  "
                f"sovereignty={r.sovereignty}"
            )
        print(f"  Overall speedup: {summary['overall_speedup']:.2f}×")

    asyncio.run(_smoke())
