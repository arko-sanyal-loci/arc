"""
card_stream.py — CARD: Category Adaptive Record Display (back door)

Small agents activate CARD by category and receive a heat-filtered,
budget-projected slice of the compendium as a compressed SymPrev stream.

EVERY activation is a hash-chained ledger entry (D2 Credit-Where-Due).

Agent budget tiers (design Section 6C):
  Haiku / small     ≤  560 tokens
  Local 7B          ≤ 1 000 tokens
  Opus / large      ≤ 5 000 tokens

Workflow per activation:
  1. SLP index lookup for category
  2. Heat-filter: keep nodes where heat ≥ heat_floor
  3. Project to budget → compressed SymPrev slice
  4. Write activation as hash-chained ledger entry
  5. Stream slice back to caller
"""

from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass, field
from typing import Generator, Iterator, Protocol

from .slp_encoding import ALPHA, H_MAX, SLPRegister, u16_to_heat


# ── Budget constants ───────────────────────────────────────────────────────────

BUDGET_HAIKU:  int = 560
BUDGET_7B:     int = 1_000
BUDGET_LARGE:  int = 5_000


# ── Agent tier enum ────────────────────────────────────────────────────────────

class AgentTier:
    HAIKU  = "haiku"
    SMALL  = "small"     # aliases Haiku-class
    LOCAL7B = "local-7b"
    OPUS   = "opus"
    LARGE  = "large"     # aliases Opus-class

    _BUDGET: dict[str, int] = {
        HAIKU:   BUDGET_HAIKU,
        SMALL:   BUDGET_HAIKU,
        LOCAL7B: BUDGET_7B,
        OPUS:    BUDGET_LARGE,
        LARGE:   BUDGET_LARGE,
    }

    @classmethod
    def budget_for(cls, tier: str) -> int:
        return cls._BUDGET.get(tier.lower(), BUDGET_7B)


# ── SymPrev stream labels (Section 7) ─────────────────────────────────────────

STREAM_I = "I"   # narrative   (prev narrative text)
STREAM_Q = "Q"   # scalars     (quantitative metadata)
STREAM_D = "D"   # demoted     (D-stream: hypotheses that failed crystallisation)
STREAM_M = "M"   # VAD session (Mood/Affect/Drive of original author)
STREAM_S = "S"   # synthetic   (SSymPrev rollouts)

ALL_STREAMS = (STREAM_I, STREAM_Q, STREAM_D, STREAM_M, STREAM_S)


# ── Protocols for injected dependencies ───────────────────────────────────────

class CompendiumReader(Protocol):
    def nodes_by_category(
        self,
        category: str,
        heat_floor: float,
    ) -> list[dict]:
        """Return list of node dicts: {node_id, content, heat, stream, tokens}."""
        ...

    def last_ledger_hash(self) -> str:
        """Return the hash of the most recent ledger entry (empty string if none)."""
        ...

    def insert_activation_ledger(
        self,
        entry: dict,
    ) -> None:
        """Persist a CARD activation ledger entry."""
        ...


# ── CARD activation result ────────────────────────────────────────────────────

@dataclass
class CARDSlice:
    """
    One CARD activation result.
    Yielded as chunks to the calling agent.
    """
    category:       str
    agent_id:       str
    agent_tier:     str
    budget_tokens:  int
    heat_floor:     float
    ledger_hash:    str            # hash of the activation ledger entry
    streams:        dict[str, list[dict]]   # stream_label → [node, ...]
    total_nodes:    int
    truncated:      bool           # True if budget forced a cut

    def node_count(self) -> int:
        return sum(len(v) for v in self.streams.values())


# ── Provenance hash for activation ────────────────────────────────────────────

def _activation_hash(
    timestamp_s: int,
    agent_id:    str,
    category:    str,
    prev_hash:   str,
) -> str:
    """
    Chain hash: SHA256(prev_hash ‖ timestamp ‖ agent_id ‖ category)
    Links each activation into the global ledger chain.
    """
    payload = (
        prev_hash
        + str(timestamp_s)
        + agent_id
        + category
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ── Token budget projection ────────────────────────────────────────────────────

def _project_to_budget(
    nodes: list[dict],
    budget: int,
) -> tuple[list[dict], bool]:
    """
    Greedily select nodes until budget is exhausted.
    Nodes are pre-sorted by descending heat.
    Returns (selected_nodes, truncated_flag).
    """
    selected: list[dict] = []
    used = 0
    for node in nodes:
        node_tokens = node.get("tokens", 0)
        if used + node_tokens > budget:
            return selected, True
        selected.append(node)
        used += node_tokens
    return selected, False


# ── CARD main class ───────────────────────────────────────────────────────────

class CARD:
    """
    Category Adaptive Record Display — back door of the Glassbox.

    Usage:
        card = CARD(compendium_reader)
        for chunk in card.activate("research", "agent-haiku-01", "haiku"):
            send_to_agent(chunk)
    """

    def __init__(
        self,
        compendium: CompendiumReader,
        heat_floor: float = ALPHA,
    ) -> None:
        self._compendium = compendium
        self._heat_floor = max(ALPHA, min(H_MAX, heat_floor))

    def activate(
        self,
        category:   str,
        agent_id:   str,
        agent_tier: str = AgentTier.LOCAL7B,
        heat_floor: float | None = None,
    ) -> CARDSlice:
        """
        Main entry point for a small agent requesting context.

        Args:
            category:   The category to retrieve context for.
            agent_id:   Unique ID of the calling agent (for ledger attribution).
            agent_tier: One of AgentTier constants; determines token budget.
            heat_floor: Override global heat floor for this activation.

        Returns:
            CARDSlice with filtered + budget-projected SymPrev streams.
        """
        floor = heat_floor if heat_floor is not None else self._heat_floor
        budget = AgentTier.budget_for(agent_tier)
        now_s = int(time.time())

        # 1. SLP index lookup + heat filter
        raw_nodes = self._compendium.nodes_by_category(category, floor)

        # 2. Sort by descending heat (hottest context first)
        raw_nodes.sort(key=lambda n: n.get("heat", ALPHA), reverse=True)

        # 3. Project to token budget
        selected, truncated = _project_to_budget(raw_nodes, budget)

        # 4. Partition into SymPrev streams
        streams: dict[str, list[dict]] = {s: [] for s in ALL_STREAMS}
        for node in selected:
            stream_label = node.get("stream", STREAM_I)
            if stream_label in streams:
                streams[stream_label].append(node)
            else:
                streams[STREAM_I].append(node)  # fallback: narrative

        # 5. Hash-chain ledger entry (D2 Credit-Where-Due)
        prev_hash = self._compendium.last_ledger_hash()
        activation_hash = _activation_hash(now_s, agent_id, category, prev_hash)

        ledger_entry = {
            "type":           "card_activation",
            "timestamp_s":    now_s,
            "agent_id":       agent_id,
            "agent_tier":     agent_tier,
            "category":       category,
            "heat_floor":     floor,
            "budget_tokens":  budget,
            "nodes_returned": len(selected),
            "truncated":      truncated,
            "prev_hash":      prev_hash,
            "activation_hash": activation_hash,
        }
        self._compendium.insert_activation_ledger(ledger_entry)

        return CARDSlice(
            category=category,
            agent_id=agent_id,
            agent_tier=agent_tier,
            budget_tokens=budget,
            heat_floor=floor,
            ledger_hash=activation_hash,
            streams=streams,
            total_nodes=len(raw_nodes),
            truncated=truncated,
        )

    def stream_chunks(
        self,
        category:   str,
        agent_id:   str,
        agent_tier: str = AgentTier.LOCAL7B,
        heat_floor: float | None = None,
    ) -> Generator[dict, None, None]:
        """
        Generator variant: yields one dict per node so callers can
        stream content to small-context models without buffering the
        whole slice.

        Each yielded dict has: {stream, node_id, content, heat, is_last}
        """
        result = self.activate(category, agent_id, agent_tier, heat_floor)

        all_nodes: list[tuple[str, dict]] = []
        for stream_label, nodes in result.streams.items():
            for node in nodes:
                all_nodes.append((stream_label, node))

        total = len(all_nodes)
        for idx, (stream_label, node) in enumerate(all_nodes):
            yield {
                "stream":    stream_label,
                "node_id":   node.get("node_id", ""),
                "content":   node.get("content", ""),
                "heat":      node.get("heat", ALPHA),
                "is_last":   (idx == total - 1),
                "ledger_hash": result.ledger_hash if idx == 0 else None,
            }
