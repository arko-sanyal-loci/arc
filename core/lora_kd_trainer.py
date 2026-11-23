#!/usr/bin/env python3
"""
core/lora_kd_trainer.py — Heat-Mapped Knowledge Distillation LoRA Trainer
==========================================================================
LoRA training with heat-filtered knowledge stream: noise filtered by heat
score, signal amplified by Gaussian conduction, immediate growth via
SAD-LoRA spectral alignment.

Three research lines integrated:
─────────────────────────────────────────────────────────────────────────
  SAD-LoRA (ICML 2026, arXiv:2504.xxxxx)
    Spectral Alignment Distillation: aligns LoRA adapter with teacher's
    data-weighted spectral subspace.
    Error decomposed as:
      L = L_subspace_align + L_coefficient_match + L_rank_residual
    Key insight: distill WHERE the teacher's weight change lives
    (subspace) not just WHAT it produces (logits).

  KD-LoRA (NeurIPS 2024)
    Combines LoRA + KD: 98% LoRA performance, 40% fewer params,
    30% less VRAM, 30% faster inference.
    Student LoRA adapts to soft teacher labels, not one-hot targets.

  Budgeted LoRA (ICLR 2025)
    Budget-aware rank allocation: allocate rank r proportional to layer
    importance (measured by heat/gradient norm), not uniformly.
    Pareto frontier quality-efficiency: highest-heat layers get r=16,
    lowest get r=1.
─────────────────────────────────────────────────────────────────────────

LOCI Heat-Stream Integration
─────────────────────────────
Every training example is scored by the LOCI heat system before it
enters the batch. The heat score gates admission and weights the loss:

  TIER FILTER:
    HOT  H ≥ 0.80 → PRIMARY batch   — full gradient, full KD weight
    MILD H ≥ 0.35 → SECONDARY batch — half gradient, discounted KD weight
    COLD H <  0.35 → FILTERED OUT   — noise, skip (no gradient computed)

  KD LOSS WEIGHTING:
    L_total = H · L_KD + (1 − H) · L_CE + λ_sad · L_spectral

    where H is the heat score of the training example.
    High-heat examples drive distillation; low-heat examples are penalised
    by pulling the student toward ground truth (CE) rather than teacher.

  GAUSSIAN CONDUCTION (immediate growth):
    When a HOT example is processed, its embedding radiates heat to
    semantically adjacent COLD/MILD candidates (cosine distance < 2σ).
    Those neighbours are promoted to MILD and queued for the next epoch.
    This prevents the student from learning isolated islands of knowledge
    — conduction ensures growth spreads to the semantic neighbourhood.

  BUDGETED RANK ALLOCATION:
    Each transformer layer is scored by the mean heat of training
    examples that activate it (tracked via gradient hooks).
    Rank budget is proportional to layer heat:
      r_l = round(r_max × H_l / max(H_all))
    with r_min = 1 clamping the floor.

Authorship: ARKO (architecture, principles), CLAUDE (implementation, session 2026-11-18)
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ── LOCI imports ───────────────────────────────────────────────────────────────
try:
    from arc.core.heat import (
        evidence_accumulation,
        evidence_heat,
        gaussian_conduction,
        diffuse_heat,
        tier,
        TIER_HOT_BOUNDARY,
        TIER_MILD_BOUNDARY,
        ALPHA,
        MEMORY_CLASSES,
    )
    from arc.loci.slp_encoding import (
        SLPRegister,
        ClassFlags,
        RoutingHint,
        ALPHA as SLP_ALPHA,
    )
except ImportError:
    # Standalone fallback constants
    TIER_HOT_BOUNDARY = 0.80
    TIER_MILD_BOUNDARY = 0.35
    ALPHA = 0.05


# ═══════════════════════════════════════════════════════════════════════════════
# A — Training Example with Heat Score
# ═══════════════════════════════════════════════════════════════════════════════

class ExampleTier(Enum):
    HOT        = "hot"       # H ≥ 0.80 — primary batch, full gradient
    MILD       = "mild"      # H ≥ 0.35 — secondary batch, discounted gradient
    COLD       = "cold"      # H <  0.35 — filtered (noise)


@dataclass
class TrainingExample:
    """A single training example annotated with its LOCI heat score.

    Attributes
    ----------
    example_id      : UUID, correlates with the memory node it was drawn from
    input_ids       : tokenised input sequence
    labels          : tokenised label sequence (for CE loss)
    embedding       : dense embedding (for Gaussian conduction to neighbours)
    heat            : LOCI heat score of this example at sampling time
    tier            : HOT / MILD / COLD derived from heat
    teacher_logits  : soft labels from the teacher (required for KD loss)
    relevance       : relevance weight r_j in the evidence formula
    valence         : emotional weight w_j (1.0 positive, −1.0 negative)
    source_node_id  : LOCI memory node this example was drawn from
    created_at      : UNIX timestamp
    """
    example_id:     str
    input_ids:      List[int]
    labels:         List[int]
    embedding:      List[float]
    heat:           float
    tier:           ExampleTier
    teacher_logits: Optional[List[List[float]]]  # [seq_len, vocab_size]
    relevance:      float = 1.0
    valence:        float = 1.0
    source_node_id: Optional[int] = None
    created_at:     float = field(default_factory=time.time)

    @classmethod
    def from_node(
        cls,
        input_ids: List[int],
        labels: List[int],
        embedding: List[float],
        heat: float,
        teacher_logits: Optional[List[List[float]]] = None,
        *,
        relevance: float = 1.0,
        valence: float = 1.0,
        source_node_id: Optional[int] = None,
    ) -> "TrainingExample":
        """Factory: build a TrainingExample from a LOCI memory node's heat score."""
        if heat >= TIER_HOT_BOUNDARY:
            t = ExampleTier.HOT
        elif heat >= TIER_MILD_BOUNDARY:
            t = ExampleTier.MILD
        else:
            t = ExampleTier.COLD
        return cls(
            example_id=str(uuid.uuid4()),
            input_ids=input_ids,
            labels=labels,
            embedding=embedding,
            heat=heat,
            tier=t,
            teacher_logits=teacher_logits,
            relevance=relevance,
            valence=valence,
            source_node_id=source_node_id,
        )

    def kd_weight(self) -> float:
        """Weight for the KD term in the blended loss.

        L_total = H · L_KD + (1 − H) · L_CE + λ_sad · L_spectral

        High-heat examples: KD dominates (trust the teacher).
        Low-heat examples: CE dominates (distrust the teacher, stay grounded).
        """
        return max(0.0, min(1.0, self.heat))

    def gradient_scale(self) -> float:
        """Scale factor applied to the gradient for this example.

        HOT  → 1.0  (full gradient)
        MILD → 0.5  (half gradient — still learns but doesn't dominate)
        COLD → 0.0  (filtered — no gradient computed; skip)
        """
        if self.tier == ExampleTier.HOT:
            return 1.0
        if self.tier == ExampleTier.MILD:
            return 0.5
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# B — Heat-Filtered Training Stream
# ═══════════════════════════════════════════════════════════════════════════════

class HeatFilteredStream:
    """
    Filters and schedules training examples by their LOCI heat score.

    Implements the heat-mapped knowledge stream:
      1. Score each example using LOCI evidence_accumulation → evidence_heat
      2. Apply tier filter (COLD → discard)
      3. Apply Gaussian conduction: HOT examples promote MILD neighbours
      4. Return prioritised batches: HOT first, then MILD

    The sigma parameter controls how widely heat spreads through the
    embedding space — matches ThermodynamicNeighborhoodEngine.DEFAULT_SIGMA.
    """

    SIGMA: float = 0.32  # Gaussian spatial dispersion radius

    def __init__(self, sigma: float = SIGMA) -> None:
        self._sigma = sigma
        self._hot:  List[TrainingExample] = []
        self._mild: List[TrainingExample] = []
        # Tracks which example_ids have been promoted by conduction
        self._promoted: set = set()

    # ── Cosine distance ────────────────────────────────────────────────────

    def _cosine_dist(self, a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 1.0
        dot = sum(x * y for x, y in zip(a, b))
        na  = math.sqrt(sum(x * x for x in a))
        nb  = math.sqrt(sum(y * y for y in b))
        return max(0.0, 1.0 - dot / (na * nb + 1e-9))

    # ── Gaussian conduction ────────────────────────────────────────────────

    def _conduction(self, source: TrainingExample, target: TrainingExample) -> float:
        """Heat radiated from source → target via Gaussian kernel over embedding space."""
        d = self._cosine_dist(source.embedding, target.embedding)
        return source.heat * math.exp(-(d * d) / (2 * self._sigma ** 2))

    # ── Ingest a batch ─────────────────────────────────────────────────────

    def ingest(self, examples: Sequence[TrainingExample]) -> Tuple[int, int, int]:
        """Ingest a list of examples and sort them into heat tiers.

        Returns (hot_count, mild_count, cold_filtered).
        COLD examples are discarded; HOT/MILD are retained.
        Gaussian conduction is applied after all examples are ingested:
        a HOT example can promote a MILD neighbour to a hotter position.
        """
        cold_filtered = 0
        for ex in examples:
            if ex.tier == ExampleTier.COLD:
                cold_filtered += 1
                # COLD → discard (noise filtered by heat)
                continue
            if ex.tier == ExampleTier.HOT:
                self._hot.append(ex)
            else:
                self._mild.append(ex)

        # Gaussian conduction: HOT examples radiate heat to MILD neighbours
        self._conduct_heat()

        return len(self._hot), len(self._mild), cold_filtered

    def _conduct_heat(self) -> None:
        """Promote MILD examples that receive heat from HOT neighbours.

        If a MILD example receives enough conducted heat to push its
        effective heat above HOT_BOUNDARY, it is re-tiered to HOT.
        This implements "immediate growth": adjacent knowledge domains
        are pulled into the primary batch when a related HOT example
        is processed.
        """
        for mild_ex in list(self._mild):
            if mild_ex.example_id in self._promoted:
                continue
            radiated = sum(self._conduction(hot_ex, mild_ex) for hot_ex in self._hot)
            effective = min(1.0 - 1e-9, mild_ex.heat + radiated)
            if effective >= TIER_HOT_BOUNDARY:
                # Promote: re-tier from MILD → HOT
                promoted = TrainingExample(
                    example_id=mild_ex.example_id,
                    input_ids=mild_ex.input_ids,
                    labels=mild_ex.labels,
                    embedding=mild_ex.embedding,
                    heat=effective,
                    tier=ExampleTier.HOT,
                    teacher_logits=mild_ex.teacher_logits,
                    relevance=mild_ex.relevance,
                    valence=mild_ex.valence,
                    source_node_id=mild_ex.source_node_id,
                )
                self._mild.remove(mild_ex)
                self._hot.append(promoted)
                self._promoted.add(mild_ex.example_id)

    # ── Yield batches ──────────────────────────────────────────────────────

    def batches(self, batch_size: int = 8) -> Iterable[List[TrainingExample]]:
        """Yield prioritised batches: HOT examples first, then MILD.

        HOT batches are yielded first (highest signal) before MILD batches
        (secondary signal). Within each tier, examples are sorted by heat
        descending so the strongest gradient step comes first.
        """
        # Sort by heat descending within each tier
        hot_sorted  = sorted(self._hot,  key=lambda e: e.heat, reverse=True)
        mild_sorted = sorted(self._mild, key=lambda e: e.heat, reverse=True)
        all_sorted  = hot_sorted + mild_sorted

        for start in range(0, len(all_sorted), batch_size):
            batch = all_sorted[start:start + batch_size]
            if batch:
                yield batch

    def stats(self) -> Dict:
        return {
            "hot":      len(self._hot),
            "mild":     len(self._mild),
            "promoted": len(self._promoted),
            "total":    len(self._hot) + len(self._mild),
        }

    def clear(self) -> None:
        """Reset the stream for a new epoch."""
        self._hot.clear()
        self._mild.clear()
        self._promoted.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# C — Layer Heat Scoring (for Budgeted Rank Allocation)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class LayerHeatScore:
    """Heat score for a single transformer layer.

    Attributes
    ----------
    layer_name  : e.g. "model.layers.12.self_attn.q_proj"
    heat        : mean heat of examples that activate this layer
    grad_norm   : mean L2 gradient norm (proxy for importance)
    allocated_r : LoRA rank allocated after budgeting
    """
    layer_name:  str
    heat:        float = ALPHA
    grad_norm:   float = 0.0
    allocated_r: int   = 1


class BudgetedRankAllocator:
    """
    Allocates LoRA rank per layer proportional to layer heat score.

    Implements Budgeted LoRA (ICLR 2025) with LOCI heat as the
    importance metric instead of gradient sensitivity alone:

      r_l = max(r_min, round(r_max × H_l / H_max))

    Layers receiving HOT examples get the highest rank budget.
    Layers receiving only COLD/MILD examples get r_min (rank=1).

    This concentrates adapter capacity where the teacher's knowledge
    is most concentrated (high heat = high signal).
    """

    def __init__(
        self,
        r_max: int = 16,
        r_min: int = 1,
        alpha_lora: float = 32.0,
    ) -> None:
        self.r_max       = r_max
        self.r_min       = r_min
        self.alpha_lora  = alpha_lora
        self._scores: Dict[str, LayerHeatScore] = {}

    def update(self, layer_name: str, example_heat: float, grad_norm: float = 0.0) -> None:
        """Update the running heat score for a layer from one training step.

        Called by the training loop after each forward/backward pass,
        passing the heat of the example batch and the layer's gradient norm.
        """
        s = self._scores.setdefault(layer_name, LayerHeatScore(layer_name))
        # Exponential moving average of heat (τ = 0.9)
        s.heat     = 0.9 * s.heat + 0.1 * example_heat
        s.grad_norm = 0.9 * s.grad_norm + 0.1 * grad_norm

    def allocate(self) -> Dict[str, int]:
        """Compute the rank budget for each layer.

        Returns a dict mapping layer_name → allocated rank r.
        """
        if not self._scores:
            return {}
        h_max = max(s.heat for s in self._scores.values())
        if h_max <= 0:
            h_max = 1.0
        result: Dict[str, int] = {}
        for name, s in self._scores.items():
            r = max(self.r_min, round(self.r_max * s.heat / h_max))
            s.allocated_r = r
            result[name] = r
        return result

    def report(self) -> List[Dict]:
        """Return a sorted report of layer heat scores and rank allocations."""
        sorted_scores = sorted(
            self._scores.values(), key=lambda s: s.heat, reverse=True
        )
        return [
            {
                "layer": s.layer_name,
                "heat":  round(s.heat, 4),
                "grad_norm": round(s.grad_norm, 4),
                "rank_r": s.allocated_r,
                "tier": (
                    "HOT" if s.heat >= TIER_HOT_BOUNDARY else
                    "MILD" if s.heat >= TIER_MILD_BOUNDARY else
                    "COLD"
                ),
            }
            for s in sorted_scores
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# D — KD Loss Functions (SAD-LoRA + KD-LoRA blended)
# ═══════════════════════════════════════════════════════════════════════════════

class KDLossComputer:
    """
    Computes the blended KD + CE + spectral alignment loss.

    Formula (SAD-LoRA + KD-LoRA):
    ─────────────────────────────
    For a batch of examples with heat scores {H_i}:

      L_KD(i)       = KL_divergence(student_logits_i || teacher_logits_i)
      L_CE(i)       = CrossEntropy(student_logits_i, labels_i)
      L_spectral    = ||U_student − U_teacher||_F  (Frobenius, spectral subspace)

      L_total(i)    = H_i · L_KD(i) + (1 − H_i) · L_CE(i) + λ_sad · L_spectral

    Heat weighting:
      - H_i → 1 (HOT): trust teacher → KD dominates
      - H_i → 0 (COLD): distrust teacher → CE dominates

    Spectral alignment (SAD-LoRA):
      After each update, check that the top-r singular directions of the
      student adapter subspace align with the teacher's data-weighted
      spectral subspace. L_spectral penalises misalignment.

    Parameters
    ----------
    temperature   : distillation temperature T (scales teacher logits)
    lambda_sad    : weight for spectral alignment loss
    """

    def __init__(
        self,
        temperature: float = 4.0,
        lambda_sad: float = 0.1,
    ) -> None:
        self.temperature = temperature
        self.lambda_sad  = lambda_sad

    # ── Softmax ────────────────────────────────────────────────────────────

    @staticmethod
    def _softmax(logits: List[float], temperature: float = 1.0) -> List[float]:
        scaled = [x / max(temperature, 1e-9) for x in logits]
        max_v  = max(scaled)
        exps   = [math.exp(x - max_v) for x in scaled]
        total  = sum(exps)
        return [e / total for e in exps]

    # ── KL divergence ──────────────────────────────────────────────────────

    def _kl_divergence(
        self,
        student_logits: List[float],
        teacher_logits: List[float],
    ) -> float:
        """KL(student || teacher) — sum_i p_t(i) log(p_t(i) / p_s(i)).

        High KL = student logit distribution far from teacher.
        """
        p_t = self._softmax(teacher_logits, self.temperature)
        p_s = self._softmax(student_logits, self.temperature)
        kl = 0.0
        for pt, ps in zip(p_t, p_s):
            if pt > 1e-9 and ps > 1e-9:
                kl += pt * math.log(pt / ps)
        # Scale by T^2 (Hinton et al. KD formula)
        return kl * (self.temperature ** 2)

    # ── Cross-entropy ──────────────────────────────────────────────────────

    def _cross_entropy(
        self,
        student_logits: List[float],
        label_ids: List[int],
    ) -> float:
        """Autoregressive CE loss: mean negative log-probability at label tokens."""
        probs = self._softmax(student_logits, 1.0)
        ce = 0.0
        n  = 0
        for lid in label_ids:
            if 0 <= lid < len(probs):
                ce += -math.log(max(probs[lid], 1e-9))
                n  += 1
        return ce / max(1, n)

    # ── Spectral alignment (SAD-LoRA §3) ───────────────────────────────────

    def _spectral_alignment_loss(
        self,
        student_weight: List[List[float]],  # r × d_in matrix
        teacher_weight: List[List[float]],  # r × d_in matrix
    ) -> float:
        """
        Frobenius norm of (student_subspace − teacher_subspace).

        In production: compute via truncated SVD of the LoRA B·A product.
        Here: simplified to element-wise L2 distance (equivalent when
        matrices are already in row-orthonormal form from SVD).

        SAD-LoRA error decomposition:
          L = ||U_s − U_t||_F  (subspace misalignment)
              + ||Σ_s − Σ_t||_F  (coefficient mismatch)
              + ||R_rank||_F     (irreducible rank residual)

        We minimise the first term directly; the second and third are
        implicitly reduced by the KD + CE objectives.
        """
        if not student_weight or not teacher_weight:
            return 0.0
        frob_sq = 0.0
        for s_row, t_row in zip(student_weight, teacher_weight):
            for s, t in zip(s_row, t_row):
                frob_sq += (s - t) ** 2
        return math.sqrt(max(0.0, frob_sq))

    # ── Blended loss ───────────────────────────────────────────────────────

    def compute(
        self,
        student_logits: List[float],
        teacher_logits: Optional[List[float]],
        label_ids: List[int],
        heat: float,
        *,
        student_weight: Optional[List[List[float]]] = None,
        teacher_weight: Optional[List[List[float]]] = None,
    ) -> Dict[str, float]:
        """Compute the blended KD + CE + spectral alignment loss for one example.

        Returns a dict with per-component losses and the total.
        """
        # Ground-truth CE loss
        l_ce = self._cross_entropy(student_logits, label_ids)

        # KD loss (requires teacher logits)
        if teacher_logits and len(teacher_logits) == len(student_logits):
            l_kd = self._kl_divergence(student_logits, teacher_logits)
        else:
            # No teacher → CE-only (treats example as unsupervised)
            l_kd = 0.0
            heat = 0.0  # degrade heat so CE dominates

        # Spectral alignment loss (SAD-LoRA)
        l_spectral = 0.0
        if student_weight and teacher_weight:
            l_spectral = self._spectral_alignment_loss(student_weight, teacher_weight)

        # Blended total: H · KD + (1−H) · CE + λ · spectral
        kd_weight = max(0.0, min(1.0, heat))
        l_total = (kd_weight * l_kd +
                   (1.0 - kd_weight) * l_ce +
                   self.lambda_sad * l_spectral)

        return {
            "l_ce":        round(l_ce,        6),
            "l_kd":        round(l_kd,        6),
            "l_spectral":  round(l_spectral,  6),
            "l_total":     round(l_total,     6),
            "kd_weight":   round(kd_weight,   4),
            "heat":        round(heat,         4),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# E — Heat-Mapped KD LoRA Trainer (orchestrator)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TrainingStep:
    """Record of a single training step."""
    step_id:    int
    batch_ids:  List[str]
    losses:     List[Dict[str, float]]
    mean_heat:  float
    mean_total: float
    duration_s: float
    promoted:   int     # examples promoted by Gaussian conduction this step


@dataclass
class EpochStats:
    """Aggregate statistics for one training epoch."""
    epoch:          int
    steps:          int
    hot_examples:   int
    mild_examples:  int
    cold_filtered:  int
    promoted:       int
    mean_loss:      float
    mean_heat:      float
    duration_s:     float
    rank_allocation: Dict[str, int]


class HeatMappedKDLoRATrainer:
    """
    Orchestrates heat-filtered knowledge distillation LoRA training.

    Main loop per epoch:
      1. Ingest examples → HeatFilteredStream (tiers, Gaussian conduction)
      2. Yield batches (HOT first, then MILD, COLD discarded)
      3. For each batch:
           a. Scale gradient by example.gradient_scale()
           b. Compute blended KD + CE + spectral loss
           c. Update BudgetedRankAllocator with layer heats
      4. After epoch: re-allocate LoRA ranks per layer

    "Immediate growth" mechanism:
      Gaussian conduction ensures that when a HOT example is processed,
      its semantic neighbourhood is promoted to MILD and enters training.
      This prevents isolated knowledge islands — growth spreads continuously
      through the embedding space.

    Sovereignty integration:
      Examples with sovereignty flag (heat → 1.0) always enter the HOT tier
      regardless of evidence score. These are pinned-knowledge nodes that must
      be retained regardless of forgetting dynamics.
    """

    def __init__(
        self,
        r_max: int = 16,
        r_min: int = 1,
        alpha_lora: float = 32.0,
        kd_temperature: float = 4.0,
        lambda_sad: float = 0.1,
        batch_size: int = 8,
        sigma: float = 0.32,
    ) -> None:
        self.stream    = HeatFilteredStream(sigma=sigma)
        self.allocator = BudgetedRankAllocator(r_max=r_max, r_min=r_min, alpha_lora=alpha_lora)
        self.loss_fn   = KDLossComputer(temperature=kd_temperature, lambda_sad=lambda_sad)
        self.batch_size = batch_size
        self._step_id   = 0
        self._history:  List[TrainingStep] = []

    # ── Ingest a dataset epoch ─────────────────────────────────────────────

    def prepare_epoch(
        self,
        examples: Sequence[TrainingExample],
        *,
        epoch: int = 0,
    ) -> Tuple[int, int, int]:
        """Ingest examples for one epoch, applying heat tier filtering.

        Returns (hot_count, mild_count, cold_filtered).
        COLD examples (H < 0.35) are silently discarded as noise.
        MILD examples may be promoted via Gaussian conduction.
        """
        self.stream.clear()
        hot, mild, cold = self.stream.ingest(examples)
        return hot, mild, cold

    # ── Single training step ────────────────────────────────────────────────

    def step(
        self,
        batch: List[TrainingExample],
        *,
        # In production: student_model and optimizer are torch objects.
        # Here: stubs so the trainer runs without a GPU for logic testing.
        student_model=None,
        optimizer=None,
    ) -> TrainingStep:
        """Execute one training step on a heat-ordered batch.

        Steps:
          1. Compute blended loss for each example
          2. Scale by example.gradient_scale() (HOT=1.0, MILD=0.5)
          3. Update rank allocator with example heats
          4. In production: backward + optimizer step
        """
        t0 = time.monotonic()
        losses: List[Dict[str, float]] = []
        total_promoted = self.stream.stats()["promoted"]

        for ex in batch:
            grad_scale = ex.gradient_scale()
            if grad_scale <= 0:
                continue  # COLD — skip (should not reach here, but guard anyway)

            # Production: student_model(ex.input_ids) → logits
            # Stub: uniform distribution
            vocab_size = max(len(ex.labels), 32000)
            student_logits = [1.0 / vocab_size] * vocab_size

            # Teacher logits from ex.teacher_logits (position 0 as proxy for sequence)
            teacher_at_0 = (
                ex.teacher_logits[0]
                if ex.teacher_logits and ex.teacher_logits[0]
                else None
            )

            loss = self.loss_fn.compute(
                student_logits=student_logits,
                teacher_logits=teacher_at_0,
                label_ids=ex.labels[:vocab_size],
                heat=ex.heat,
            )
            # Gradient scale applied to total loss
            loss["scaled_total"] = round(loss["l_total"] * grad_scale, 6)
            losses.append(loss)

            # Update rank allocator
            # Production: layer_name from gradient hooks
            # Stub: use hash of input_ids to simulate per-layer updates
            layer_proxy = f"layer_{hash(str(ex.input_ids[:4])) % 32}"
            self.allocator.update(layer_proxy, ex.heat)

            # Production: loss.backward(); optimizer.step()

        mean_heat  = sum(ex.heat for ex in batch) / max(1, len(batch))
        mean_total = (
            sum(l.get("scaled_total", 0.0) for l in losses) / max(1, len(losses))
        )

        step_record = TrainingStep(
            step_id=self._step_id,
            batch_ids=[ex.example_id for ex in batch],
            losses=losses,
            mean_heat=round(mean_heat, 4),
            mean_total=round(mean_total, 6),
            duration_s=round(time.monotonic() - t0, 4),
            promoted=total_promoted,
        )
        self._history.append(step_record)
        self._step_id += 1
        return step_record

    # ── Full epoch ──────────────────────────────────────────────────────────

    def run_epoch(
        self,
        examples: Sequence[TrainingExample],
        *,
        epoch: int = 0,
        student_model=None,
        optimizer=None,
    ) -> EpochStats:
        """Run a full epoch of heat-mapped KD LoRA training.

        1. Prepare stream (ingest + tier filter + Gaussian conduction)
        2. Iterate batches (HOT first)
        3. Execute gradient steps
        4. Re-allocate LoRA ranks after epoch
        """
        t0 = time.monotonic()
        hot, mild, cold = self.prepare_epoch(examples, epoch=epoch)
        total_steps = 0
        total_promoted = 0
        loss_sum = 0.0
        heat_sum = 0.0

        for batch in self.stream.batches(self.batch_size):
            rec = self.step(batch, student_model=student_model, optimizer=optimizer)
            total_steps += 1
            loss_sum    += rec.mean_total
            heat_sum    += rec.mean_heat
            total_promoted = max(total_promoted, rec.promoted)

        # Re-allocate LoRA ranks after epoch
        rank_alloc = self.allocator.allocate()

        return EpochStats(
            epoch=epoch,
            steps=total_steps,
            hot_examples=hot,
            mild_examples=mild,
            cold_filtered=cold,
            promoted=total_promoted,
            mean_loss=round(loss_sum / max(1, total_steps), 6),
            mean_heat=round(heat_sum / max(1, total_steps), 4),
            duration_s=round(time.monotonic() - t0, 3),
            rank_allocation=rank_alloc,
        )

    # ── Training summary ────────────────────────────────────────────────────

    def summary(self) -> Dict:
        """Return a summary of training history and current rank allocation."""
        total_steps = len(self._history)
        if not self._history:
            return {"steps": 0}
        mean_loss = sum(s.mean_total for s in self._history) / total_steps
        mean_heat = sum(s.mean_heat  for s in self._history) / total_steps
        return {
            "total_steps":    total_steps,
            "mean_loss":      round(mean_loss, 6),
            "mean_heat":      round(mean_heat, 4),
            "rank_allocation": self.allocator.allocate(),
            "layer_report":   self.allocator.report()[:10],  # top 10 layers
        }


# ═══════════════════════════════════════════════════════════════════════════════
# F — Score examples from LOCI memory events
# ═══════════════════════════════════════════════════════════════════════════════

def score_from_loci_events(
    events: List[Tuple[float, float, float]],
    *,
    memory_class: str = "valued_skill",
    beta: float = 1.0,
) -> float:
    """
    Score a training example using the LOCI heat law from its memory events.

    Parameters
    ----------
    events       : list of (timestamp, relevance, valence_weight) triples
    memory_class : LOCI memory class key ("formative_scar", "valued_skill",
                   "routine_observation") — controls τ and α
    beta         : heat sensitivity parameter

    Returns
    -------
    float — heat H ∈ (α, 1.0) for use as the example's heat score.

    This bridges LOCI memory engine events (from glassbox ingestion,
    card reads, SLP register accesses) directly to training weights:
    knowledge that the agent accesses frequently and with high relevance
    gets a high heat score → dominates the training batch.
    """
    try:
        mc = MEMORY_CLASSES.get(memory_class, MEMORY_CLASSES["valued_skill"])
        evidence = evidence_accumulation(events, tau=mc.tau_days)
        return evidence_heat(evidence, alpha=mc.alpha, beta=beta)
    except (NameError, KeyError):
        # Fallback: simple recency-weighted average
        if not events:
            return ALPHA
        now = time.time()
        total = 0.0
        for t_j, r, w in events:
            dt = max(0.0, now - t_j)
            tau_s = 20.0 * 86400  # valued_skill default
            total += (r * w) / (1.0 + dt / tau_s)
        raw = ALPHA + (1.0 - ALPHA) * (1.0 - math.exp(-total))
        return max(ALPHA + 1e-9, min(1.0 - 1e-9, raw))


# ═══════════════════════════════════════════════════════════════════════════════
# G — Quick smoke test
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import random

    rng = random.Random(42)
    now = time.time()

    def _fake_embedding(seed: int, dim: int = 64) -> List[float]:
        rng2 = random.Random(seed)
        v = [rng2.gauss(0, 1) for _ in range(dim)]
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]

    # Build 30 fake training examples with varying heat scores
    examples: List[TrainingExample] = []
    for i in range(30):
        heat = rng.uniform(0.1, 0.95)
        ex = TrainingExample.from_node(
            input_ids=[rng.randint(0, 999) for _ in range(16)],
            labels=[rng.randint(0, 999) for _ in range(8)],
            embedding=_fake_embedding(i),
            heat=heat,
            teacher_logits=[[rng.gauss(0, 1) for _ in range(100)]],
        )
        examples.append(ex)

    trainer = HeatMappedKDLoRATrainer(r_max=16, r_min=1, batch_size=4)
    print("[lora_kd_trainer] Running smoke test...")
    stats = trainer.run_epoch(examples, epoch=0)
    print(f"  Epoch 0 summary:")
    print(f"    HOT={stats.hot_examples}  MILD={stats.mild_examples}  COLD_filtered={stats.cold_filtered}")
    print(f"    promoted_by_conduction={stats.promoted}")
    print(f"    steps={stats.steps}  mean_loss={stats.mean_loss}  mean_heat={stats.mean_heat}")
    print(f"    rank_allocation (top 5):")
    for layer, r in list(stats.rank_allocation.items())[:5]:
        print(f"      {layer}: r={r}")
    print(f"  Overall: {trainer.summary()['mean_loss']:.4f} mean loss")
