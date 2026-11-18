#!/usr/bin/env python3
"""
arc-runtime/core/calendar_memory.py
=====================================
Time-indexed memory store for the Arc model runtime.

Ports the temporal-retrieval patterns from LOCI-MEMORY-ENGINE/loci_engine/storage.py
into the arc-runtime tiered memory context.

Calendar-aware retrieval means:
  - Buckets by era: near (≤7d), mid (≤45d), far (>45d)  — matches LOCI schema
  - Retrieves facts valid at a given wall-clock time (valid_from / valid_until)
  - Orders results by heat DESC so HOT items surface first regardless of era
  - Integrates with TieredMemory: heat ≥ 0.80 → VRAM, ≥ 0.35 → DDR5, else NVMe

Heat decay uses the hyperbolic law from core/heat.py:
    E(t) = r·w / (1 + Δt/τ)   for each evidence event
    H(t) = α + (1−α)(1−exp(−β·E))   open-interval in (α, 1.0)

Temporal write path:
    write() → upsert into tiered_memory with era annotation
    touch()  → appends an evidence event and refreshes heat (never increments a scalar)

Temporal read path:
    at(wall_time)        → facts valid at that instant
    range(start, end)    → facts with valid_from inside [start, end)
    era(era_name)        → all facts in one era bucket
    recent(n, threshold) → top-n by heat among near+mid items above threshold
"""

from __future__ import annotations

import time
import uuid
import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Heat constants (mirror core/heat.py — imported lazily to avoid circular)
# ---------------------------------------------------------------------------
_HOT_BOUNDARY:  float = 0.800
_MILD_BOUNDARY: float = 0.350

# Era age thresholds (calendar days since last_observed_at)
_ERA_NEAR_DAYS: float = 7.0
_ERA_MID_DAYS:  float = 45.0
# beyond 45d → far

# Default memory-class parameters (valued_skill from LOCI paper)
_DEFAULT_TAU:   float = 20.0   # half-life days
_DEFAULT_ALPHA: float = 0.08   # floor activation
_DEFAULT_BETA:  float = 2.0    # steepness of H(E) sigmoid


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class CalendarEntry:
    """
    A single time-indexed memory item.

    Fields mirror the LOCI storage.py memory_item schema so entries
    can be serialised to / from context2.db without structural gaps.
    """
    id:               str
    namespace:        str
    item_type:        str          # fact | decision | observation | summary | goal_context
    canonical_text:   str
    valid_from:       float        # POSIX timestamp
    valid_until:      Optional[float]   # None → indefinitely valid
    era:              str          # near | mid | far
    heat:             float        # open-interval (alpha, 1.0)
    alpha:            float        # floor activation
    created_at:       float        # POSIX timestamp
    last_observed_at: float        # POSIX timestamp
    source_actor:     str
    source_trust:     float        # 0–1
    confidence:       float        # 0–1
    events:           List[Tuple[float, float, float]]  # (ts, relevance, hop_weight)
    metadata:         Dict[str, Any] = field(default_factory=dict)

    # ---- computed properties ----

    def age_days(self, now: Optional[float] = None) -> float:
        """Days since last_observed_at."""
        t = now if now is not None else time.time()
        return max(0.0, (t - self.last_observed_at) / 86400.0)

    def is_valid_at(self, wall_time: float) -> bool:
        """True if wall_time falls within [valid_from, valid_until)."""
        if wall_time < self.valid_from:
            return False
        if self.valid_until is not None and wall_time >= self.valid_until:
            return False
        return True

    def to_context_line(self) -> str:
        """Single-line context snippet with heat and era annotation."""
        ts = datetime.fromtimestamp(self.valid_from, tz=timezone.utc).strftime("%Y-%m-%d")
        tier = (
            "HOT"  if self.heat >= _HOT_BOUNDARY  else
            "MILD" if self.heat >= _MILD_BOUNDARY else "COLD"
        )
        return f"[{self.era.upper()} | {tier} h={self.heat:.3f} | {ts}] {self.canonical_text}"


# ---------------------------------------------------------------------------
# Era classification
# ---------------------------------------------------------------------------

def classify_era(last_observed_at: float, now: Optional[float] = None) -> str:
    """
    Assign an era bucket based on age since last observation.

        near : ≤ 7 days   (high temporal salience)
        mid  : ≤ 45 days  (medium salience)
        far  : > 45 days  (low / archival)
    """
    t = now if now is not None else time.time()
    age_days = max(0.0, (t - last_observed_at) / 86400.0)
    if age_days <= _ERA_NEAR_DAYS:
        return "near"
    if age_days <= _ERA_MID_DAYS:
        return "mid"
    return "far"


# ---------------------------------------------------------------------------
# Heat computation (self-contained to avoid circular import with heat.py)
# ---------------------------------------------------------------------------

def _compute_heat(
    events: List[Tuple[float, float, float]],
    now:    float,
    tau:    float = _DEFAULT_TAU,
    alpha:  float = _DEFAULT_ALPHA,
    beta:   float = _DEFAULT_BETA,
) -> float:
    """
    Re-derive heat from the full event history.  Never mutate a scalar.

    E(t) = Σ_k  r_k · w_k / (1 + Δt_k / τ)   (hyperbolic, never zero)
    H(t) = α + (1−α) · (1 − exp(−β · E))       (open-interval in (α, 1.0))

    events: list of (timestamp_posix, relevance, hop_weight)
    tau   : half-life in days
    """
    E = 0.0
    for ts, r, w in events:
        delta_days = max(0.0, (now - ts) / 86400.0)
        E += (r * w) / (1.0 + delta_days / tau)
    H = alpha + (1.0 - alpha) * (1.0 - __import__("math").exp(-beta * E))
    return min(H, 1.0 - 1e-9)   # strictly below 1.0


def _make_id() -> str:
    return str(uuid.uuid4())


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# CalendarMemoryStore
# ---------------------------------------------------------------------------

class CalendarMemoryStore:
    """
    In-process, thread-safe time-indexed memory store.

    Backed by a plain dict keyed by entry id.  Designed to be a hot-tier
    overlay over TieredMemory's NVMe cold store: at runtime, recently-active
    entries live here; the full corpus persists to SQLite via tiered_memory.

    Not persistent across process restarts — that's TieredMemory's job.
    Use flush_to_tiered() to drain entries down into the durable store.
    """

    def __init__(
        self,
        namespace:   str   = "arc_calendar",
        default_tau: float = _DEFAULT_TAU,
        default_alpha: float = _DEFAULT_ALPHA,
    ):
        self.namespace     = namespace
        self.default_tau   = default_tau
        self.default_alpha = default_alpha
        self._store: Dict[str, CalendarEntry] = {}
        self._lock  = threading.RLock()

    # ------------------------------------------------------------------ writes

    def write(
        self,
        text:         str,
        item_type:    str   = "fact",
        source_actor: str   = "arc",
        source_trust: float = 1.0,
        confidence:   float = 1.0,
        valid_from:   Optional[float] = None,
        valid_until:  Optional[float] = None,
        hop_weight:   float = 1.0,
        metadata:     Optional[Dict[str, Any]] = None,
        entry_id:     Optional[str] = None,
        *,
        now: Optional[float] = None,
    ) -> CalendarEntry:
        """
        Upsert a calendar entry.

        If an entry with the same content_hash already exists in this
        namespace, touch() it instead of inserting a duplicate.
        Returns the entry (new or updated).
        """
        t   = now if now is not None else time.time()
        chk = _content_hash(text)

        with self._lock:
            # Dedup by content hash
            existing = self._find_by_hash(chk)
            if existing is not None:
                return self.touch(existing.id, hop_weight=hop_weight, now=t)

            eid    = entry_id or _make_id()
            vf     = valid_from  if valid_from  is not None else t
            era    = classify_era(t, now=t)
            events: List[Tuple[float, float, float]] = [(t, 1.0, hop_weight)]
            heat   = _compute_heat(events, t, self.default_tau, self.default_alpha)

            entry = CalendarEntry(
                id               = eid,
                namespace        = self.namespace,
                item_type        = item_type,
                canonical_text   = text,
                valid_from       = vf,
                valid_until      = valid_until,
                era              = era,
                heat             = heat,
                alpha            = self.default_alpha,
                created_at       = t,
                last_observed_at = t,
                source_actor     = source_actor,
                source_trust     = source_trust,
                confidence       = confidence,
                events           = events,
                metadata         = metadata or {},
            )
            self._store[eid] = entry
            return entry

    def touch(
        self,
        entry_id:   str,
        hop_weight: float = 1.0,
        *,
        now: Optional[float] = None,
    ) -> CalendarEntry:
        """
        Reinforce an existing entry with a new evidence event.

        Appends (now, 1.0, hop_weight) to the event log and re-derives
        heat from the full history.  Never modifies a scalar in place.
        """
        t = now if now is not None else time.time()
        with self._lock:
            entry = self._store.get(entry_id)
            if entry is None:
                raise KeyError(f"CalendarMemoryStore: no entry '{entry_id}'")
            entry.events.append((t, 1.0, hop_weight))
            entry.heat             = _compute_heat(entry.events, t, self.default_tau, entry.alpha)
            entry.last_observed_at = t
            entry.era              = classify_era(t, now=t)
            return entry

    def expire(self, entry_id: str) -> bool:
        """Mark an entry as expired (valid_until = now). Returns True if found."""
        t = time.time()
        with self._lock:
            entry = self._store.get(entry_id)
            if entry is None:
                return False
            entry.valid_until = t
            return True

    def remove(self, entry_id: str) -> bool:
        """Hard-delete an entry. Returns True if it existed."""
        with self._lock:
            return self._store.pop(entry_id, None) is not None

    # ------------------------------------------------------------------ reads

    def at(
        self,
        wall_time: float,
        *,
        min_heat:    float = 0.0,
        namespace:   Optional[str] = None,
        item_types:  Optional[List[str]] = None,
    ) -> List[CalendarEntry]:
        """
        Return all entries valid at the given POSIX timestamp,
        ordered by heat descending.
        """
        ns = namespace or self.namespace
        with self._lock:
            results = [
                e for e in self._store.values()
                if e.namespace == ns
                and e.is_valid_at(wall_time)
                and e.heat >= min_heat
                and (item_types is None or e.item_type in item_types)
            ]
        results.sort(key=lambda e: e.heat, reverse=True)
        return results

    def range(
        self,
        start: float,
        end:   float,
        *,
        min_heat:   float = 0.0,
        item_types: Optional[List[str]] = None,
    ) -> List[CalendarEntry]:
        """
        Return entries whose valid_from falls within [start, end),
        ordered by valid_from ascending then heat descending.
        """
        with self._lock:
            results = [
                e for e in self._store.values()
                if e.namespace == self.namespace
                and start <= e.valid_from < end
                and e.heat >= min_heat
                and (item_types is None or e.item_type in item_types)
            ]
        results.sort(key=lambda e: (e.valid_from, -e.heat))
        return results

    def era(
        self,
        era_name: str,
        *,
        min_heat:   float = 0.0,
        item_types: Optional[List[str]] = None,
        now:        Optional[float] = None,
    ) -> List[CalendarEntry]:
        """
        Return all entries in the given era bucket (near | mid | far),
        re-classifying era live against `now`, ordered by heat desc.
        """
        t = now if now is not None else time.time()
        with self._lock:
            results = []
            for e in self._store.values():
                if e.namespace != self.namespace:
                    continue
                live_era = classify_era(e.last_observed_at, now=t)
                if live_era != era_name:
                    continue
                if e.heat < min_heat:
                    continue
                if item_types and e.item_type not in item_types:
                    continue
                results.append(e)
        results.sort(key=lambda e: e.heat, reverse=True)
        return results

    def recent(
        self,
        n:         int   = 10,
        threshold: float = _MILD_BOUNDARY,
        *,
        now: Optional[float] = None,
    ) -> List[CalendarEntry]:
        """
        Top-n entries in {near, mid} eras above heat threshold.
        Useful for injecting recent context into the kernel scratchpad.
        """
        t = now if now is not None else time.time()
        near_mid = self.era("near", min_heat=threshold, now=t) + \
                   self.era("mid",  min_heat=threshold, now=t)
        near_mid.sort(key=lambda e: e.heat, reverse=True)
        return near_mid[:n]

    def hot(self, now: Optional[float] = None) -> List[CalendarEntry]:
        """Entries above HOT boundary — candidates for VRAM tier."""
        t = now if now is not None else time.time()
        with self._lock:
            results = [
                e for e in self._store.values()
                if e.namespace == self.namespace and e.heat >= _HOT_BOUNDARY
            ]
        results.sort(key=lambda e: e.heat, reverse=True)
        return results

    def all_entries(self) -> List[CalendarEntry]:
        """All entries, no filters. Useful for flush_to_tiered()."""
        with self._lock:
            return list(self._store.values())

    # ------------------------------------------------------------------ context blocks

    def to_context_block(
        self,
        entries:   Optional[List[CalendarEntry]] = None,
        max_chars: int = 4096,
    ) -> str:
        """
        Serialise entries to an XML-like context block for injection
        into the system message.

        Format:
            <calendar_memory>
              <entry era="near" heat="0.912" ts="2026-11-18">…text…</entry>
              …
            </calendar_memory>
        """
        if entries is None:
            entries = self.recent(n=15)
        lines: List[str] = ["<calendar_memory>"]
        char_budget = max_chars - len("<calendar_memory>\n</calendar_memory>")
        for e in entries:
            ts_str = datetime.fromtimestamp(e.valid_from, tz=timezone.utc).strftime("%Y-%m-%d")
            line   = f'  <entry era="{e.era}" heat="{e.heat:.3f}" ts="{ts_str}">{e.canonical_text}</entry>'
            if len(line) + 1 > char_budget:
                break
            lines.append(line)
            char_budget -= len(line) + 1
        lines.append("</calendar_memory>")
        return "\n".join(lines)

    # ------------------------------------------------------------------ integration with TieredMemory

    def flush_to_tiered(self, tiered_memory: Any) -> int:
        """
        Drain all entries into TieredMemory (core/tiered_memory.py).

        Each entry is written as a dict with keys that TieredMemory.write() expects.
        Returns the count of entries flushed.
        """
        entries = self.all_entries()
        count   = 0
        for e in entries:
            payload = {
                "id":               e.id,
                "namespace":        e.namespace,
                "item_type":        e.item_type,
                "canonical_text":   e.canonical_text,
                "heat":             e.heat,
                "valid_from":       e.valid_from,
                "valid_until":      e.valid_until,
                "era":              e.era,
                "last_observed_at": e.last_observed_at,
                "source_actor":     e.source_actor,
                "source_trust":     e.source_trust,
                "confidence":       e.confidence,
                "metadata":         e.metadata,
            }
            try:
                tiered_memory.write(e.canonical_text, heat=e.heat, metadata=payload)
                count += 1
            except Exception:  # noqa: BLE001
                pass
        return count

    # ------------------------------------------------------------------ tool schemas

    @staticmethod
    def tool_schemas() -> List[Dict[str, Any]]:
        """OpenAI-compatible tool schemas for calendar memory operations."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "calendar_write",
                    "description": (
                        "Write or reinforce a time-indexed memory entry. "
                        "Use for facts, decisions, or observations tied to a specific time."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text":        {"type": "string",  "description": "The fact or observation to store."},
                            "item_type":   {"type": "string",  "enum": ["fact","decision","observation","summary","goal_context"],
                                            "description": "Category of the memory item."},
                            "valid_from":  {"type": "string",  "description": "ISO8601 start time (optional, defaults to now)."},
                            "valid_until": {"type": "string",  "description": "ISO8601 expiry time (optional)."},
                            "hop_weight":  {"type": "number",  "description": "Reinforcement weight 0–1 (default 1.0)."},
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "calendar_read",
                    "description": "Retrieve time-indexed memory entries by era or time range.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "era":       {"type": "string", "enum": ["near","mid","far","all"],
                                          "description": "Era bucket to retrieve."},
                            "min_heat":  {"type": "number", "description": "Minimum heat threshold (0–1)."},
                            "limit":     {"type": "integer","description": "Max entries to return (default 10)."},
                            "at_time":   {"type": "string", "description": "ISO8601 — retrieve entries valid at this instant."},
                        },
                        "required": [],
                    },
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Dispatch a calendar tool call. Returns JSON string."""
        import math as _math  # local to avoid shadowing

        if tool_name == "calendar_write":
            text       = arguments.get("text", "")
            item_type  = arguments.get("item_type", "fact")
            hop_weight = float(arguments.get("hop_weight", 1.0))
            vf_str     = arguments.get("valid_from")
            vu_str     = arguments.get("valid_until")

            vf = None
            if vf_str:
                try:
                    vf = datetime.fromisoformat(vf_str).timestamp()
                except ValueError:
                    pass

            vu = None
            if vu_str:
                try:
                    vu = datetime.fromisoformat(vu_str).timestamp()
                except ValueError:
                    pass

            entry = self.write(text, item_type=item_type, hop_weight=hop_weight,
                               valid_from=vf, valid_until=vu)
            return json.dumps({"id": entry.id, "heat": entry.heat, "era": entry.era,
                               "status": "written"})

        if tool_name == "calendar_read":
            era_name  = arguments.get("era", "near")
            min_heat  = float(arguments.get("min_heat", 0.0))
            limit     = int(arguments.get("limit", 10))
            at_str    = arguments.get("at_time")

            if at_str:
                try:
                    wall = datetime.fromisoformat(at_str).timestamp()
                    entries = self.at(wall, min_heat=min_heat)[:limit]
                except ValueError:
                    entries = []
            elif era_name == "all":
                with self._lock:
                    entries = sorted(self._store.values(), key=lambda e: e.heat, reverse=True)[:limit]
            else:
                entries = self.era(era_name, min_heat=min_heat)[:limit]

            return json.dumps([
                {
                    "id":    e.id,
                    "text":  e.canonical_text,
                    "heat":  round(e.heat, 4),
                    "era":   e.era,
                    "valid_from": datetime.fromtimestamp(e.valid_from, tz=timezone.utc).isoformat(),
                }
                for e in entries
            ])

        return json.dumps({"error": f"Unknown calendar tool: {tool_name}"})

    # ------------------------------------------------------------------ internals

    def _find_by_hash(self, chk: str) -> Optional[CalendarEntry]:
        """Return first entry whose canonical_text hashes to chk."""
        for e in self._store.values():
            if _content_hash(e.canonical_text) == chk:
                return e
        return None

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        return f"CalendarMemoryStore(namespace={self.namespace!r}, entries={len(self)})"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

calendar: CalendarMemoryStore = CalendarMemoryStore(namespace="arc_calendar")
