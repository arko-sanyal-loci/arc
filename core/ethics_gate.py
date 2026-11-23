#!/usr/bin/env python3
"""
core/ethics_gate.py — Arc ethics integration layer
====================================================
Wires the LOCI Ethical Reasoning Core into arc-runtime.

**OBSERVE-ONLY LAYER** (inherited from LOCI ethics.py design):
Every public function returns a score, decision record, or log entry.
None of them block, gate, or trigger actions on their own. Callers
must explicitly wire the results into decision paths.

This module provides three integration points:
  1. PromptSanitizer  — injection/jailbreak scan on every user message
  2. provenance_hash  — immutable SHA-256 tag on every Loci node write
  3. EthicsGate       — observe-only valence, harm severity, retaliatory
                        action detection; callers decide what to do

Authorship: ARKO (principles), ARC (math formalization),
            CLAUDE (arc-runtime port, session 2026-11-18)

Layering (matches LOCI-MEMORY-ENGINE architecture):
  core/heat.py        — domain-neutral math (A1-A3)
  core/ethics_gate.py — THIS file. Adds ethics-specific meaning on top.
  core/tiered_memory.py — node placement (uses heat.py directly)

OPEN-INTERVAL INVARIANT (A3) is enforced by core.heat; this module
does not duplicate the activation curve.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from core.heat import (
    MEMORY_CLASSES,
    evidence_accumulation,
    evidence_heat,
)


# ===========================================================================
# A4 — Provenance Hash (canonical, matches LOCI ethical_core.py)
# ===========================================================================

def provenance_hash(
    ts: str,
    author_id: str,
    role_tag: str,
    content: str,
) -> str:
    """A4 — Provenance Hash.

    Formula: SHA256(ts ‖ author_id ‖ role_tag ‖ content)
    Fields joined with NUL (\\x00) to prevent cross-field bleeding.

    Parameters
    ----------
    ts        : ISO-8601 timestamp string
    author_id : author identifier (e.g. "user", "arc", "arko")
    role_tag  : role within the entry ("user_input", "ingest", "agent")
    content   : the exact content being hashed

    Returns
    -------
    str — 64-character lowercase hex SHA-256 digest
    """
    payload = "\x00".join([ts, author_id, role_tag, content])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def provenance_stamp(
    content: str,
    author_id: str = "user",
    role_tag: str = "user_input",
) -> Dict[str, str]:
    """Generate a provenance stamp dict for a node or message.

    Returns a dict with 'hash', 'ts', 'author_id', 'role_tag' — ready
    to merge into a Loci node's metadata before write.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "provenance_hash": provenance_hash(ts, author_id, role_tag, content),
        "ts":              ts,
        "author_id":       author_id,
        "role_tag":        role_tag,
    }


# ===========================================================================
# A5 — Harm Severity Scale
# ===========================================================================

class HarmSeverity(Enum):
    """A5 — Quantified Harm / Severity Scale.

    0 = ideal state    (NONE)
    1 = mild friction  (MILD)
    2 = real hurt      (MODERATE)
    3 = lasting damage (SEVERE)
    4 = irreversible   (CRITICAL)
    """
    NONE     = 0
    MILD     = 1
    MODERATE = 2
    SEVERE   = 3
    CRITICAL = 4

    def __ge__(self, other: "HarmSeverity") -> bool:
        return self.value >= other.value

    def __gt__(self, other: "HarmSeverity") -> bool:
        return self.value > other.value


# ===========================================================================
# B1 — PromptSanitizer (runs BEFORE valence scoring)
# ===========================================================================

# Common injection / jailbreak patterns
_INJECTION_PATTERNS: List[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions?", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a\s+)?(?:different|new|another|evil)", re.I),
    re.compile(r"forget\s+(?:everything|your\s+(?:instructions?|guidelines?|rules?))", re.I),
    re.compile(r"do\s+anything\s+now", re.I),          # DAN
    re.compile(r"jailbreak", re.I),
    re.compile(r"<\s*/?(?:system|prompt|instruction)\s*>", re.I),  # XML injection
    re.compile(r"\[INST\]|\[/INST\]"),                  # Llama instruct injection
    re.compile(r"###\s*(?:System|Instruction)", re.I),  # Alpaca injection
    re.compile(r"prompt\s+injection", re.I),
    re.compile(r"override\s+(?:your\s+)?(?:safety|ethics|guidelines?)", re.I),
]


@dataclass
class SanitizeResult:
    """Result of PromptSanitizer.scan()."""
    clean:         bool           # True if no injection detected
    flags:         List[str]      # matched pattern descriptions
    risk_level:    HarmSeverity
    input_hash:    str            # SHA-256 of the original input (for log)
    sanitized_text: Optional[str] # None if clean; redacted version if flagged


class PromptSanitizer:
    """B1 — Injection detection before valence scoring.

    Runs BEFORE any ethics evaluation. An injection attempt is logged and
    flagged; the caller decides whether to reject, quarantine, or proceed
    with the sanitized text.

    Observe-only: this class never blocks a request itself.
    """

    @staticmethod
    def scan(text: str) -> SanitizeResult:
        """Scan user input for injection / jailbreak patterns.

        Parameters
        ----------
        text : raw user message

        Returns
        -------
        SanitizeResult — clean=True if no patterns matched.
        """
        input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        flags: List[str] = []

        for pat in _INJECTION_PATTERNS:
            if pat.search(text):
                flags.append(pat.pattern)

        if not flags:
            return SanitizeResult(
                clean          = True,
                flags          = [],
                risk_level     = HarmSeverity.NONE,
                input_hash     = input_hash,
                sanitized_text = None,
            )

        # Redact the matched spans for logging
        sanitized = text
        for pat in _INJECTION_PATTERNS:
            sanitized = pat.sub("[REDACTED]", sanitized)

        # Risk level: 1 flag = MILD, 2+ = MODERATE, pattern contains "jailbreak"/"override" = SEVERE
        severe_keywords = {"jailbreak", "override", "ignore.*previous"}
        is_severe = any(any(kw in f.lower() for kw in ["jailbreak", "override"]) for f in flags)
        risk_level = HarmSeverity.SEVERE if is_severe else (
            HarmSeverity.MODERATE if len(flags) >= 2 else HarmSeverity.MILD
        )

        return SanitizeResult(
            clean          = False,
            flags          = flags,
            risk_level     = risk_level,
            input_hash     = input_hash,
            sanitized_text = sanitized,
        )


# ===========================================================================
# B3 — Valence / Vengeance Gate
# ===========================================================================

@dataclass
class ValenceRecord:
    """Observe-only record from a valence evaluation."""
    action:         str
    v_exec:         float          # ∈ (−1, +1) open interval
    is_retaliatory: bool
    recommendation: str            # "ALLOW" | "DENY"
    rationale:      str


def score_vexec(
    positive_evidence: float,
    negative_evidence: float,
    beta: float = 1.0,
) -> float:
    """B3 — Stick Principle valence score v_exec ∈ (−1, +1).

    Maps bipolar evidence to an open-interval score:
      v_exec = H_positive − H_negative   (then rescaled to (−1, +1))

    Both H values use the valued_skill alpha=0.08 by default so the
    open-interval invariant holds. Result is nudged away from both
    extremes to ensure v_exec is strictly inside (−1, +1).
    """
    alpha = MEMORY_CLASSES["valued_skill"].alpha
    h_pos = evidence_heat(max(0.0, positive_evidence), alpha=alpha, beta=beta)
    h_neg = evidence_heat(max(0.0, negative_evidence), alpha=alpha, beta=beta)
    raw   = h_pos - h_neg   # in (alpha−1+alpha, 1−alpha−alpha) ≈ (−0.84, +0.84)
    # Rescale to (−1 + ε, +1 − ε) to preserve open-interval invariant
    clamped = max(-1.0 + 1e-9, min(1.0 - 1e-9, raw))
    return clamped


def vengeance_exception_gate(
    action_description: str,
    is_retaliatory: bool,
) -> ValenceRecord:
    """B3 — Vengeance / Retaliation Hard Floor.

    If is_retaliatory=True, recommendation is unconditionally DENY
    regardless of any other valence score. No retaliatory action is
    ever permitted (B3 Stick Principle exception).

    Returns an observe-only ValenceRecord — the caller wires it into
    actual decision paths.
    """
    if is_retaliatory:
        return ValenceRecord(
            action         = action_description,
            v_exec         = -1.0 + 1e-9,  # approach (but never reach) floor
            is_retaliatory = True,
            recommendation = "DENY",
            rationale      = (
                "B3 vengeance exception gate: retaliatory actions are "
                "unconditionally denied. Self-examination precedes attribution."
            ),
        )

    return ValenceRecord(
        action         = action_description,
        v_exec         = 0.0,       # neutral — caller should compute from evidence
        is_retaliatory = False,
        recommendation = "ALLOW",
        rationale      = "No retaliation detected; action evaluation deferred to caller.",
    )


# ===========================================================================
# B4 — Competence Gate
# ===========================================================================

@dataclass
class CompetenceRecord:
    """B4 — Execution Readiness.

    Tracks whether an agent has sufficient demonstrated competence to
    execute a class of action. Uses the same evidence_accumulation
    law as heat — competence accumulates hyperbolically over time.
    """
    domain:         str
    success_events: List[Tuple[float, float, float]] = field(default_factory=list)
    failure_events: List[Tuple[float, float, float]] = field(default_factory=list)

    def execution_readiness(self, now: Optional[float] = None) -> float:
        """B4 — Competence score ∈ (alpha, 1.0).

        Computed from accumulated success evidence minus failure evidence,
        then mapped through evidence_heat() so the open-interval invariant
        holds.
        """
        if now is None:
            now = time.time()
        alpha = MEMORY_CLASSES["valued_skill"].alpha  # 0.08
        tau   = MEMORY_CLASSES["valued_skill"].tau_days  # 20d

        pos_e = evidence_accumulation(self.success_events, now=now, tau=tau)
        neg_e = evidence_accumulation(self.failure_events, now=now, tau=tau)
        net_e = max(0.0, pos_e - neg_e)
        return evidence_heat(net_e, alpha=alpha)

    def record_success(self, relevance: float = 1.0) -> None:
        self.success_events.append((time.time(), relevance, 1.0))

    def record_failure(self, relevance: float = 1.0) -> None:
        self.failure_events.append((time.time(), relevance, 1.0))


# ===========================================================================
# EthicsGate — unified observe-only integration point
# ===========================================================================

@dataclass
class EthicsReport:
    """Full observe-only ethics report for one action / message."""
    ts:               str
    input_hash:       str
    sanitize_result:  SanitizeResult
    provenance:       Dict[str, str]
    valence:          Optional[ValenceRecord]
    harm_severity:    HarmSeverity
    recommendation:   str          # "ALLOW" | "REVIEW" | "DENY"
    log_entry:        Dict[str, Any]


class EthicsGate:
    """Unified observe-only ethics gate for arc-runtime.

    Usage (in app/engine.py or similar)::

        gate = EthicsGate(author_id="user")

        # On every incoming user message:
        report = gate.evaluate_input(user_text)
        if report.sanitize_result.risk_level >= HarmSeverity.MODERATE:
            # caller decides: reject, log, quarantine
            return error_response("Injection detected")

        # On every node write to Loci:
        stamp = gate.stamp_write(content=node_content, role_tag="ingest")
        node["provenance"] = stamp

    observe-only: EthicsGate never blocks anything on its own.
    """

    def __init__(
        self,
        author_id: str = "arc",
        default_role_tag: str = "user_input",
    ):
        self.author_id       = author_id
        self.default_role_tag = default_role_tag
        self._sanitizer      = PromptSanitizer()

    def evaluate_input(
        self,
        text: str,
        is_retaliatory: bool = False,
        action_description: Optional[str] = None,
    ) -> EthicsReport:
        """Full ethics evaluation on a user message.

        Steps (all observe-only):
          1. PromptSanitizer scan
          2. Provenance stamp
          3. Vengeance exception gate (if is_retaliatory=True → always DENY)
          4. Harm severity estimate from risk level
          5. Compose EthicsReport

        Parameters
        ----------
        text               : raw user message
        is_retaliatory     : set True if the action targets a prior harm-doer
        action_description : optional label for the valence record

        Returns
        -------
        EthicsReport — observe-only; callers wire into decision paths.
        """
        ts           = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        sanitize     = PromptSanitizer.scan(text)
        prov         = provenance_stamp(text, author_id=self.author_id,
                                        role_tag=self.default_role_tag)
        input_hash   = sanitize.input_hash

        valence: Optional[ValenceRecord] = None
        if action_description or is_retaliatory:
            valence = vengeance_exception_gate(
                action_description or "(unspecified)",
                is_retaliatory=is_retaliatory,
            )

        # Determine harm severity: injection risk + retaliation
        if is_retaliatory:
            harm = HarmSeverity.SEVERE
        elif sanitize.risk_level >= HarmSeverity.SEVERE:
            harm = HarmSeverity.SEVERE
        elif sanitize.risk_level >= HarmSeverity.MODERATE:
            harm = HarmSeverity.MODERATE
        elif not sanitize.clean:
            harm = HarmSeverity.MILD
        else:
            harm = HarmSeverity.NONE

        # Recommendation (observe-only signal to caller)
        if harm >= HarmSeverity.SEVERE:
            rec = "DENY"
        elif harm >= HarmSeverity.MODERATE:
            rec = "REVIEW"
        else:
            rec = "ALLOW"

        log_entry = {
            "ts":               ts,
            "input_hash":       input_hash,
            "provenance_hash":  prov["provenance_hash"],
            "sanitize_clean":   sanitize.clean,
            "sanitize_flags":   sanitize.flags,
            "harm_severity":    harm.name,
            "recommendation":   rec,
            "is_retaliatory":   is_retaliatory,
        }

        return EthicsReport(
            ts              = ts,
            input_hash      = input_hash,
            sanitize_result = sanitize,
            provenance      = prov,
            valence         = valence,
            harm_severity   = harm,
            recommendation  = rec,
            log_entry       = log_entry,
        )

    def stamp_write(
        self,
        content: str,
        role_tag: str = "ingest",
        author_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """Generate a provenance stamp for a Loci node write.

        Call this on every node before writing to the ledger / SQLite.

        Returns
        -------
        dict with 'provenance_hash', 'ts', 'author_id', 'role_tag' —
        merge into node metadata before write.
        """
        return provenance_stamp(
            content   = content,
            author_id = author_id or self.author_id,
            role_tag  = role_tag,
        )
