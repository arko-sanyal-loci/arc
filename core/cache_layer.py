"""
core/cache_layer.py — Prompt caching breakpoints + heat-tier TTL wiring.

Background
----------
Anthropic's prompt caching (https://platform.claude.com/docs/prompt-caching)
lets you mark message content blocks with ``cache_control`` so that prefix
encodings are reused across requests.

Two TTL tiers:
  ephemeral (5 min)   — default, low write-cost.  Reset on every hit.
  extended  (1 hour)  — 2× write cost.  Justified only for HOT-tier content
                        that is reused at > 5 min intervals.

4-breakpoint layout for a Loci-powered Arc agent
-------------------------------------------------
  BP1: Last tool definition         → tools are static; almost never change
  BP2: Static system instructions   → persona / role; stable across sessions
  BP3: Loci retrieved context       → entities + facts from tiered memory
  BP4: Final user message           → NOT cached; changes every turn

Heat-tier → TTL mapping (from Loci heat law)
--------------------------------------------
  HOT  (heat ≥ 0.80) → 1-hour  (extended)  — high read rate justifies 2× cost
  MILD (heat ≥ 0.35) → 5-min   (ephemeral) — standard cache
  COLD (heat <  0.35) → no cache            — near-zero hit probability

Pre-warming (Glass Box multi-agent)
------------------------------------
  Send max_tokens=0 to write the cache without generating output.
  No output tokens billed; response has content=[], stop_reason="max_tokens".
  Call once before spinning up an agent fleet to serialise the first cache
  write; subsequent parallel agent requests then hit the cache.

Usage from app/server.py
------------------------
    from core.cache_layer import PromptCacheLayer, CacheTTL

    cache = PromptCacheLayer(enabled=True)

    # Apply breakpoints to a messages list + tools list
    messages, tools = cache.annotate(
        messages        = messages,
        tools           = tools,
        retrieved_items = loci_items,   # List[LociItem] from tiered_memory
    )

    # Pre-warm cache (Glass Box startup)
    await provisioner.prewarm(messages, tools, cache)

Compatibility
-------------
  cache_control is an Anthropic-native feature.  For OpenAI-compatible
  providers (LOCAL / XAI) the annotation is silently dropped by this
  module so callers never need to branch.  nexos.ai passes cache_control
  through to Claude-family backends when routing to claude-* models.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Heat thresholds — must stay in sync with core/heat.py
_TIER_HOT_BOUNDARY:  float = 0.800
_TIER_MILD_BOUNDARY: float = 0.350

# Models that support cache_control  (prefix-match)
_CACHE_CAPABLE_PREFIXES = (
    "claude-",
    "anthropic/",
    "anthropic.",
)

# Anthropic prompt-caching beta header required for extended TTL
_BETA_HEADER_EXTENDED = "prompt-caching-2024-07-31"


# ---------------------------------------------------------------------------
# TTL enum
# ---------------------------------------------------------------------------

class CacheTTL(str, Enum):
    """
    Prompt cache TTL options.

    ephemeral — 5-minute TTL, standard write cost.
    extended  — 1-hour TTL, 2× write cost.  Only use for HOT-tier content.
    none      — No cache breakpoint.
    """
    EPHEMERAL = "ephemeral"
    EXTENDED  = "extended"
    NONE      = "none"


def heat_to_ttl(heat: float) -> CacheTTL:
    """
    Map a Loci heat value to a CacheTTL tier.

    >>> heat_to_ttl(0.95)
    <CacheTTL.EXTENDED: 'extended'>
    >>> heat_to_ttl(0.50)
    <CacheTTL.EPHEMERAL: 'ephemeral'>
    >>> heat_to_ttl(0.20)
    <CacheTTL.NONE: 'none'>
    """
    if heat >= _TIER_HOT_BOUNDARY:
        return CacheTTL.EXTENDED
    if heat >= _TIER_MILD_BOUNDARY:
        return CacheTTL.EPHEMERAL
    return CacheTTL.NONE


# ---------------------------------------------------------------------------
# Cache control builder
# ---------------------------------------------------------------------------

def _cache_control(ttl: CacheTTL) -> Optional[Dict[str, Any]]:
    """
    Build an Anthropic cache_control dict for a given TTL.
    Returns None for CacheTTL.NONE (no annotation needed).

    Extended TTL requires the ``anthropic-beta: prompt-caching-2024-07-31``
    request header; this module adds it to ``extra_headers`` when present.
    """
    if ttl == CacheTTL.NONE:
        return None
    # Both ephemeral and extended use type="ephemeral"; the beta header
    # enables the 1-hour variant on the Anthropic side.
    return {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# LociItem — minimal repr of a retrieved memory node
# ---------------------------------------------------------------------------

@dataclass
class LociItem:
    """
    One retrieved item from Loci's tiered memory, to be injected into the
    LLM context as part of the RAG block (Breakpoint 3).
    """
    text:  str
    heat:  float = 0.0
    key:   str   = ""
    tier:  str   = "hop"   # "kernel" | "hop" | "nvme"
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# PromptCacheLayer
# ---------------------------------------------------------------------------

class PromptCacheLayer:
    """
    Applies Anthropic prompt caching breakpoints to a messages+tools payload.

    Thread-safe: this class is stateless after construction; each call to
    ``annotate()`` returns new objects, never modifies inputs.

    Args:
        enabled:         Master switch.  When False, annotate() is a no-op
                         (returns copies unchanged).  Useful for LOCAL / XAI
                         providers that don't support cache_control.
        default_ttl:     TTL to use for BP1 (tool defs) and BP2 (system).
                         Override per breakpoint with the BP* kwargs.
        bp1_tools_ttl:   TTL for the tool definitions breakpoint (BP1).
        bp2_system_ttl:  TTL for the static system message breakpoint (BP2).
    """

    def __init__(
        self,
        *,
        enabled:        bool     = True,
        default_ttl:    CacheTTL = CacheTTL.EPHEMERAL,
        bp1_tools_ttl:  Optional[CacheTTL] = None,
        bp2_system_ttl: Optional[CacheTTL] = None,
    ) -> None:
        self.enabled        = enabled
        self._bp1_ttl       = bp1_tools_ttl  or default_ttl
        self._bp2_ttl       = bp2_system_ttl or default_ttl

    # ------------------------------------------------------------------
    # Provider detection
    # ------------------------------------------------------------------

    @staticmethod
    def model_is_cache_capable(model: str) -> bool:
        """Return True if the named model supports cache_control."""
        m = model.lower()
        return any(m.startswith(p) for p in _CACHE_CAPABLE_PREFIXES)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def annotate(
        self,
        messages:        List[Dict[str, Any]],
        tools:           Optional[List[Dict[str, Any]]] = None,
        retrieved_items: Optional[List[LociItem]]       = None,
        *,
        model:           str = "",
    ) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        """
        Annotate messages and tools with cache_control breakpoints.

        Returns:
            (annotated_messages, annotated_tools)

        Inputs are never mutated; deep copies are taken where needed.

        Breakpoints applied:
          BP1 — last tool definition (if tools is non-empty)
          BP2 — system message (if present, and it's static)
          BP3 — last Loci retrieved item with heat ≥ MILD (heat-gated)
          BP4 — final user message: NOT cached (changes every turn)
        """
        if not self.enabled:
            return messages, tools

        # If model is known and NOT cache-capable, skip silently.
        if model and not self.model_is_cache_capable(model):
            return messages, tools

        annotated_tools    = self._annotate_tools(tools)
        annotated_messages = self._annotate_messages(
            messages, retrieved_items or []
        )
        return annotated_messages, annotated_tools

    # ------------------------------------------------------------------
    # BP1: Tools
    # ------------------------------------------------------------------

    def _annotate_tools(
        self,
        tools: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """
        BP1: Mark the last tool definition with cache_control.

        Tool defs almost never change, so a single breakpoint at the end
        of the tools list covers the full tool block.
        """
        cc = _cache_control(self._bp1_ttl)
        if not tools or cc is None:
            return tools

        result = copy.deepcopy(tools)
        # Anthropic's native API wraps tools differently from OpenAI-compat.
        # For OpenAI-compat (what nexos uses), place cache_control inside
        # the last tool's "function" dict as a top-level sibling field.
        # Nexos/Claude routes pick this up; other backends ignore it.
        result[-1] = dict(result[-1])
        result[-1]["cache_control"] = cc
        logger.debug("BP1: cache_control=%s on last tool (%s)", cc, result[-1].get("function", {}).get("name"))
        return result

    # ------------------------------------------------------------------
    # BP2 + BP3: Messages
    # ------------------------------------------------------------------

    def _annotate_messages(
        self,
        messages:        List[Dict[str, Any]],
        retrieved_items: List[LociItem],
    ) -> List[Dict[str, Any]]:
        """
        BP2: Mark the system message.
        BP3: Mark the last Loci retrieved item inline in the message body.

        The final user message (BP4) is intentionally NOT cached.
        """
        result: List[Dict[str, Any]] = copy.deepcopy(messages)

        # BP2 — system message
        for msg in result:
            if msg.get("role") == "system":
                cc = _cache_control(self._bp2_ttl)
                if cc is not None:
                    msg = _ensure_content_block(msg)
                    _mark_last_content_block(msg, cc)
                    logger.debug("BP2: cache_control=%s on system message", cc)
                break

        # BP3 — Loci retrieved items injected into the assistant or last
        # non-user message.  We look for a message whose content contains
        # the sentinel <loci_context> wrapper (injected by app/server.py).
        for msg in result:
            if _has_loci_context(msg):
                cc = _best_loci_cache_control(retrieved_items)
                if cc is not None:
                    msg = _ensure_content_block(msg)
                    _mark_last_content_block(msg, cc)
                    logger.debug("BP3: cache_control=%s on loci_context block", cc)
                break

        return result

    # ------------------------------------------------------------------
    # Extra headers for extended TTL
    # ------------------------------------------------------------------

    def extra_headers(self, ttl: CacheTTL) -> Dict[str, str]:
        """
        Return extra headers needed for a given TTL.

        For CacheTTL.EXTENDED the caller must pass the beta header to
        Anthropic.  nexos.ai forwards it transparently.
        """
        if ttl == CacheTTL.EXTENDED:
            return {"anthropic-beta": _BETA_HEADER_EXTENDED}
        return {}

    def all_extra_headers(self) -> Dict[str, str]:
        """Union of extra headers for all configured TTLs."""
        headers: Dict[str, str] = {}
        for ttl in (self._bp1_ttl, self._bp2_ttl):
            headers.update(self.extra_headers(ttl))
        return headers

    # ------------------------------------------------------------------
    # Pre-warm helper
    # ------------------------------------------------------------------

    @staticmethod
    def prewarm_payload(
        messages: List[Dict[str, Any]],
        tools:    Optional[List[Dict[str, Any]]],
        model:    str,
    ) -> Dict[str, Any]:
        """
        Build a pre-warm completion payload (max_tokens=0).

        Sends the cache write without generating any output.
        No output tokens are billed; response has:
          content=[], stop_reason="max_tokens".

        Glass Box startup sequence:
            payload = PromptCacheLayer.prewarm_payload(messages, tools, model)
            await provisioner._raw_post(payload)   # first request
            # Now safe to dispatch parallel agent fleet
        """
        return {
            "model":      model,
            "messages":   messages,
            "tools":      tools or [],
            "max_tokens": 0,
            "stream":     False,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _has_loci_context(msg: Dict[str, Any]) -> bool:
    """Return True if this message contains a Loci context injection block."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return "<loci_context>" in content
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and "<loci_context>" in b.get("text", "")
            for b in content
        )
    return False


def _best_loci_cache_control(
    items: List[LociItem],
) -> Optional[Dict[str, Any]]:
    """
    Return the cache_control dict for the hottest Loci item in the batch.
    Returns None if all items are COLD.
    """
    if not items:
        return None
    best_heat = max((i.heat for i in items), default=0.0)
    ttl = heat_to_ttl(best_heat)
    return _cache_control(ttl)


def _ensure_content_block(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a string-content message to list-of-blocks format in place.

    Anthropic requires content to be a list of block dicts to attach
    cache_control.  This is a no-op if content is already a list.
    """
    content = msg.get("content", "")
    if isinstance(content, str) and content:
        msg["content"] = [{"type": "text", "text": content}]
    elif not isinstance(content, list):
        msg["content"] = []
    return msg


def _mark_last_content_block(
    msg: Dict[str, Any],
    cc:  Dict[str, Any],
) -> None:
    """Attach ``cache_control`` to the last content block of a message."""
    content = msg.get("content", [])
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = cc


# ---------------------------------------------------------------------------
# Module-level convenience instance
# ---------------------------------------------------------------------------

#: Singleton cache layer with ephemeral defaults.
#: Override enabled=False for LOCAL / XAI routes in api_provision.py.
cache_layer: PromptCacheLayer = PromptCacheLayer(enabled=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PromptCacheLayer",
    "CacheTTL",
    "LociItem",
    "heat_to_ttl",
    "cache_layer",
]
