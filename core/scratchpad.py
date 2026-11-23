"""
core/scratchpad.py — In-process ephemeral working memory for Arc runtime.

Design: LDCE (Layered Dynamic Context Engine) two-tier scratchpad.
  Kernel tier  — max 7 items (Miller's Law 7±2 ceiling).  Always injected
                 into the LLM context window on every turn.
  Hop tier     — max 15 items.  Available on-demand; not injected by default
                 but promoted into kernel when explicitly requested.

Both tiers live in process memory ONLY.  Nothing is persisted to disk or VPS.
Call RequestScratchPad.clear() after each request completes.

For cross-session persistent notes, use the VPS scratchpad:
  POST billo.tech/v1/scratch/write  /  POST billo.tech/v1/scratch/read
  (see Workers/billo_vps_service.py in arkosanyal/loci-engine)

Usage from app/server.py:
    from core.scratchpad import scratch

    scratch.push("tool_result", result_text, tier="kernel")
    context_block = scratch.to_context_block()   # inject into LLM messages
    scratch.clear()                               # end of request

LLM tool schemas (OpenAI-compatible) exposed via scratch.tool_schemas() so
tool_dispatcher.py can register them without circular imports.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants — mirrors LDCE parameters from LOCI-MEMORY-ENGINE/session_continuity.py
# ---------------------------------------------------------------------------

KERNEL_CAP: int = 7   # Miller's Law hard ceiling for always-in-context items
HOP_CAP: int    = 15  # On-demand tier capacity

# Token budget estimates (characters / 4 ≈ tokens for UTF-8 English)
_CHARS_PER_TOKEN: int = 4
KERNEL_TOKEN_BUDGET: int = 1_200   # ~300 tokens for kernel block
HOP_TOKEN_BUDGET: int    = 4_000   # ~1 000 tokens for hop block (on-demand)

Tier = Literal["kernel", "hop"]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ScratchEntry:
    """One item in the scratchpad."""

    key:        str                        # Caller-supplied label (e.g. "search_result")
    value:      str                        # Text payload
    tier:       Tier = "hop"
    created_at: float = field(default_factory=time.monotonic)
    # Sequence number assigned at push time — lower = older
    seq:        int   = 0

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key":        self.key,
            "value":      self.value,
            "tier":       self.tier,
            "seq":        self.seq,
            "created_at": self.created_at,
        }

    def __repr__(self) -> str:  # pragma: no cover
        snippet = self.value[:60].replace("\n", " ")
        return f"<ScratchEntry tier={self.tier} seq={self.seq} key={self.key!r} value={snippet!r}…>"


# ---------------------------------------------------------------------------
# RequestScratchPad
# ---------------------------------------------------------------------------

class RequestScratchPad:
    """
    Thread-safe, per-request ephemeral scratchpad.

    Singleton instance ``scratch`` is exported at module level.
    Each call to ``clear()`` resets both tiers and the sequence counter.

    Thread safety: a single re-entrant lock guards all mutations so
    concurrent tool callbacks during streaming can push safely.
    """

    def __init__(self) -> None:
        self._lock:   threading.RLock      = threading.RLock()
        self._kernel: List[ScratchEntry]   = []
        self._hop:    List[ScratchEntry]   = []
        self._seq:    int                  = 0

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def push(
        self,
        key:   str,
        value: str,
        tier:  Tier = "hop",
        *,
        overwrite: bool = False,
    ) -> ScratchEntry:
        """
        Append (or overwrite) an entry in the specified tier.

        If the tier is already at capacity the oldest entry is evicted
        (FIFO) to make room, maintaining the Miller's Law / hop ceiling.

        Args:
            key:       Identifier for this entry.  Duplicate keys are allowed
                       unless ``overwrite=True``.
            value:     Text content (string; callers must serialise dicts etc).
            tier:      "kernel" (always-in-context) or "hop" (on-demand).
            overwrite: If True and a matching key already exists in the same
                       tier, replace its value in-place (preserves position).

        Returns:
            The created or updated ScratchEntry.
        """
        with self._lock:
            bucket = self._kernel if tier == "kernel" else self._hop
            cap    = KERNEL_CAP   if tier == "kernel" else HOP_CAP

            # Overwrite path: find existing entry by key, update value.
            if overwrite:
                for entry in bucket:
                    if entry.key == key:
                        entry.value      = value
                        entry.created_at = time.monotonic()
                        return entry

            # Evict oldest if at capacity.
            while len(bucket) >= cap:
                bucket.pop(0)   # oldest = index 0 (insertion order)

            self._seq += 1
            entry = ScratchEntry(
                key=key, value=value, tier=tier, seq=self._seq
            )
            bucket.append(entry)
            return entry

    def promote(self, key: str) -> Optional[ScratchEntry]:
        """
        Move the named entry from hop → kernel.

        If the kernel is full the oldest kernel entry is demoted to hop
        first.  Returns the promoted entry, or None if key not found.
        """
        with self._lock:
            idx = next(
                (i for i, e in enumerate(self._hop) if e.key == key), None
            )
            if idx is None:
                return None

            entry      = self._hop.pop(idx)
            entry.tier = "kernel"

            # Demote oldest kernel item to hop if kernel is full.
            if len(self._kernel) >= KERNEL_CAP:
                demoted      = self._kernel.pop(0)
                demoted.tier = "hop"
                self._hop.insert(0, demoted)

            self._kernel.append(entry)
            return entry

    def remove(self, key: str) -> bool:
        """Remove all entries with the given key across both tiers."""
        with self._lock:
            before = len(self._kernel) + len(self._hop)
            self._kernel = [e for e in self._kernel if e.key != key]
            self._hop    = [e for e in self._hop    if e.key != key]
            return (len(self._kernel) + len(self._hop)) < before

    def clear(self) -> None:
        """Reset both tiers and the sequence counter.  Call after each request."""
        with self._lock:
            self._kernel.clear()
            self._hop.clear()
            self._seq = 0

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        """Return the value of the most recent entry with this key, or None."""
        with self._lock:
            # Search both tiers, kernel first, return latest (highest seq).
            candidates: List[ScratchEntry] = [
                e for e in (*self._kernel, *self._hop) if e.key == key
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda e: e.seq).value

    def list_keys(self) -> Tuple[List[str], List[str]]:
        """Return (kernel_keys, hop_keys) in insertion order."""
        with self._lock:
            return (
                [e.key for e in self._kernel],
                [e.key for e in self._hop],
            )

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serialisable snapshot of the full scratchpad state."""
        with self._lock:
            return {
                "kernel": [e.to_dict() for e in self._kernel],
                "hop":    [e.to_dict() for e in self._hop],
                "seq":    self._seq,
            }

    # ------------------------------------------------------------------
    # Context serialisation (LLM injection)
    # ------------------------------------------------------------------

    def to_context_block(
        self,
        *,
        include_hop: bool = False,
        max_chars:   Optional[int] = None,
    ) -> str:
        """
        Render the scratchpad as a compact XML-ish block to inject into the
        LLM system prompt or user message.

        Only the kernel tier is included by default (always-in-context).
        Pass ``include_hop=True`` to also append the hop tier (on-demand use).

        The block is budget-capped: entries are included newest-first until
        ``max_chars`` (default = KERNEL_TOKEN_BUDGET × 4) is reached.

        Returns empty string if the scratchpad is empty.
        """
        with self._lock:
            kernel_entries = list(self._kernel)   # shallow copy under lock
            hop_entries    = list(self._hop)       if include_hop else []

        if not kernel_entries and not hop_entries:
            return ""

        budget = max_chars or KERNEL_TOKEN_BUDGET * _CHARS_PER_TOKEN
        lines:  List[str] = ["<scratchpad>"]
        used:   int       = len("<scratchpad>\n</scratchpad>")

        def _add_tier(entries: List[ScratchEntry], tag: str) -> bool:
            nonlocal used
            tier_lines: List[str] = []
            for entry in reversed(entries):   # newest first
                line = f"  <{tag} key={entry.key!r}>{entry.value}</{tag}>"
                if used + len(line) + 1 > budget:
                    return False   # budget exhausted
                tier_lines.append(line)
                used += len(line) + 1
            lines.extend(reversed(tier_lines))  # restore chronological order
            return True

        _add_tier(kernel_entries, "kernel")
        if include_hop:
            _add_tier(hop_entries, "hop")

        lines.append("</scratchpad>")
        return "\n".join(lines)

    def to_hop_context_block(self, max_chars: Optional[int] = None) -> str:
        """Render *only* the hop tier as a context block."""
        with self._lock:
            hop_entries = list(self._hop)

        if not hop_entries:
            return ""

        budget = max_chars or HOP_TOKEN_BUDGET * _CHARS_PER_TOKEN
        lines:  List[str] = ["<hop_scratchpad>"]
        used:   int       = len("<hop_scratchpad>\n</hop_scratchpad>")

        for entry in reversed(hop_entries):
            line = f"  <hop key={entry.key!r}>{entry.value}</hop>"
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1

        lines.append("</hop_scratchpad>")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # OpenAI-compatible tool schemas (for tool_dispatcher.py)
    # ------------------------------------------------------------------

    def tool_schemas(self) -> List[Dict[str, Any]]:
        """
        Return OpenAI-compatible JSON tool schemas for the three scratchpad
        tools: scratch_write, scratch_read, scratch_clear.

        These schemas are identical in structure to those in
        arkosanyal/loci-engine/Workers/learning_loop.py _make_tool_schema(),
        but reference the in-process scratchpad rather than the VPS endpoint.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name":        "scratch_write",
                    "description": (
                        "Write a note to the in-process scratchpad for this "
                        "request.  Use the kernel tier for information that "
                        "must stay visible in every subsequent LLM turn.  "
                        "Use the hop tier for intermediate results that may "
                        "be needed later but do not need to be always visible."
                    ),
                    "parameters": {
                        "type":       "object",
                        "properties": {
                            "key": {
                                "type":        "string",
                                "description": "Short identifier for this note (e.g. 'search_result', 'plan_step_2').",
                            },
                            "value": {
                                "type":        "string",
                                "description": "Text content to store.",
                            },
                            "tier": {
                                "type":        "string",
                                "enum":        ["kernel", "hop"],
                                "description": "kernel = always-in-context (max 7). hop = on-demand (max 15).",
                                "default":     "hop",
                            },
                            "overwrite": {
                                "type":        "boolean",
                                "description": "If true, replace existing entry with the same key instead of appending.",
                                "default":     False,
                            },
                        },
                        "required": ["key", "value"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name":        "scratch_read",
                    "description": (
                        "Read a note from the in-process scratchpad by key.  "
                        "Returns the value or null if the key does not exist."
                    ),
                    "parameters": {
                        "type":       "object",
                        "properties": {
                            "key": {
                                "type":        "string",
                                "description": "Key of the note to read.",
                            },
                        },
                        "required": ["key"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name":        "scratch_clear",
                    "description": (
                        "Clear all entries from both tiers of the in-process "
                        "scratchpad.  Call this when starting a fresh sub-task "
                        "within the same request to avoid stale context."
                    ),
                    "parameters": {
                        "type":       "object",
                        "properties": {},
                        "required":   [],
                    },
                },
            },
        ]

    # ------------------------------------------------------------------
    # Tool dispatch handlers (called by tool_dispatcher.py)
    # ------------------------------------------------------------------

    def handle_tool_call(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> str:
        """
        Dispatch a tool call by name and return a JSON string result.

        Registered names: scratch_write, scratch_read, scratch_clear.
        Unknown names return an error JSON.
        """
        if tool_name == "scratch_write":
            entry = self.push(
                key       = str(arguments.get("key", "")),
                value     = str(arguments.get("value", "")),
                tier      = arguments.get("tier", "hop"),
                overwrite = bool(arguments.get("overwrite", False)),
            )
            return json.dumps({
                "status": "ok",
                "key":    entry.key,
                "tier":   entry.tier,
                "seq":    entry.seq,
            })

        if tool_name == "scratch_read":
            value = self.get(str(arguments.get("key", "")))
            return json.dumps({"value": value})

        if tool_name == "scratch_clear":
            self.clear()
            return json.dumps({"status": "cleared"})

        return json.dumps({"error": f"Unknown scratchpad tool: {tool_name!r}"})

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._kernel) + len(self._hop)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __repr__(self) -> str:  # pragma: no cover
        with self._lock:
            return (
                f"<RequestScratchPad kernel={len(self._kernel)}/{KERNEL_CAP} "
                f"hop={len(self._hop)}/{HOP_CAP} seq={self._seq}>"
            )


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly
# ---------------------------------------------------------------------------

#: Global per-request scratchpad.  Call ``scratch.clear()`` at the start or
#: end of each request to reset state.
scratch: RequestScratchPad = RequestScratchPad()


# ---------------------------------------------------------------------------
# Convenience re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "RequestScratchPad",
    "ScratchEntry",
    "scratch",
    "KERNEL_CAP",
    "HOP_CAP",
]
