"""
core/context_compression.py — LOCI LDCE context compression for Arc
====================================================================

Arc's own context handling only tracked *where* memory nodes live
(core/tiered_memory.py: VRAM / DDR5 / NVMe). It never decided *what goes
into the prompt*, and no LOCI memory reached the model at all. This module
adopts the LOCI Memory Engine's answer to that question instead of
inventing a second one:

  LDCE (Layered Dynamic Context Engine), from
  LOCI-MEMORY-ENGINE/loci_engine/session_continuity.py
    kernel   heat >= 0.50, max 7   → always in context, full text
    hop      heat >= 0.167, max 15 → symbol only, expanded on request
    offload  the rest, max 50      → not in context, reachable by search
    (below that: cold storage, DB only)

  Symbol + expansion ("symprev"), from the LOCI design spec: a hop item
  costs one short phrase in the prompt; its full text is one tool call away.

The two axes fit together rather than compete:
  LDCE tier  = what the model sees (prompt content and fidelity)
  Arc tier   = where that text is held (placement ledger)
  Bridge     = kernel items are pinned in the VRAM tier: they form the
               stable prompt prefix that the local engine keeps in its KV
               prefix cache, so they are never offloaded while in kernel.

Constants are taken from the canonical LOCI engine when it is importable
(set LOCI_ENGINE_PATH to a LOCI-MEMORY-ENGINE checkout, or install it);
otherwise identical vendored values are used. tests/test_context_compression.py
checks parity against the canonical implementation whenever it is available.

Note: this file is cherry-picked from branch claude/project-thread-gk1tzr
(PR #2) so the gradient router on this branch can depend on it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

# ── Canonical LOCI constants (with vendored fallback) ────────────────────────

_VENDORED = {
    "KERNEL_HEAT_THRESHOLD": 0.50,
    "HOP_HEAT_THRESHOLD":    0.167,
    "KERNEL_MAX_ITEMS":      7,
    "HOP_MAX_ITEMS":         15,
    "OFFLOAD_MAX_ITEMS":     50,
}


def _load_canonical() -> tuple[dict, str]:
    path = os.environ.get("LOCI_ENGINE_PATH")
    if path and path not in sys.path:
        sys.path.insert(0, path)
    try:
        from loci_engine import session_continuity as sc  # type: ignore
    except Exception:
        return dict(_VENDORED), "vendored"
    return {k: getattr(sc, k, v) for k, v in _VENDORED.items()}, "canonical"


_C, LDCE_SOURCE = _load_canonical()
KERNEL_HEAT_THRESHOLD: float = _C["KERNEL_HEAT_THRESHOLD"]
HOP_HEAT_THRESHOLD:    float = _C["HOP_HEAT_THRESHOLD"]
KERNEL_MAX_ITEMS:      int   = _C["KERNEL_MAX_ITEMS"]
HOP_MAX_ITEMS:         int   = _C["HOP_MAX_ITEMS"]
OFFLOAD_MAX_ITEMS:     int   = _C["OFFLOAD_MAX_ITEMS"]

SYMBOL_MAX_CHARS = 48
CHARS_PER_TOKEN  = 4


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class ContextItem:
    id:        str
    text:      str
    heat:      float
    namespace: str = ""
    symbol:    Optional[str] = None
    tier:      str = ""

    def gist(self) -> str:
        return self.symbol or make_symbol(self.text)


@dataclass
class LDCETiers:
    kernel:  List[ContextItem] = field(default_factory=list)
    hop:     List[ContextItem] = field(default_factory=list)
    offload: List[ContextItem] = field(default_factory=list)
    total_active: int = 0


@dataclass
class CompressedContext:
    text:            str
    kernel_ids:      List[str]
    hop_ids:         List[str]
    offload_ids:     List[str]
    demoted_ids:     List[str]
    est_tokens:      int
    full_tokens:     int

    @property
    def compression_ratio(self) -> float:
        return self.full_tokens / max(1, self.est_tokens)


# ── LDCE tiering ─────────────────────────────────────────────────────────────

def compute_tiers(items: Iterable[ContextItem]) -> LDCETiers:
    rows = sorted(items, key=lambda i: i.heat, reverse=True)
    out  = LDCETiers(total_active=len(rows))
    for it in rows:
        if it.heat >= KERNEL_HEAT_THRESHOLD and len(out.kernel) < KERNEL_MAX_ITEMS:
            it.tier = "kernel"; out.kernel.append(it)
        elif it.heat >= HOP_HEAT_THRESHOLD and len(out.hop) < HOP_MAX_ITEMS:
            it.tier = "hop"; out.hop.append(it)
        elif len(out.offload) < OFFLOAD_MAX_ITEMS:
            it.tier = "offload"; out.offload.append(it)
    return out


def make_symbol(text: str, max_chars: int = SYMBOL_MAX_CHARS) -> str:
    t = " ".join(text.split())
    for sep in (". ", "; ", " — ", ": "):
        if sep in t[: max_chars * 2]:
            t = t.split(sep, 1)[0]
            break
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars].rsplit(" ", 1)[0] or t[:max_chars]
    return cut.rstrip(",;:") + "…"


def _tok(s: str) -> int:
    return len(s.encode("utf-8")) // CHARS_PER_TOKEN + 1


def render(tiers: LDCETiers, token_budget: int = 1200) -> CompressedContext:
    kernel  = list(tiers.kernel)
    demoted: List[ContextItem] = []

    def build(kern: List[ContextItem], dem: List[ContextItem]) -> str:
        lines = ["# LOCI memory (context compressed by heat; LDCE)"]
        if kern:
            lines.append("## Kernel")
            lines += [f"- [{i.id}] {i.text}" for i in kern]
        hop_like = dem + list(tiers.hop)
        if hop_like:
            lines.append(
                "## Available (symbol only; call arc_memory_expand with the id for full text)"
            )
            lines += [f"- [{i.id}] {i.gist()}" for i in hop_like]
        if tiers.offload:
            lines.append(
                f"({len(tiers.offload)} more items reachable with arc_memory_search)"
            )
        return "\n".join(lines)

    text = build(kernel, demoted)
    while _tok(text) > token_budget and kernel:
        demoted.insert(0, kernel.pop())
        text = build(kernel, demoted)

    full = _tok("\n".join(i.text for i in tiers.kernel + tiers.hop + tiers.offload))
    return CompressedContext(
        text=text,
        kernel_ids=[i.id for i in kernel],
        hop_ids=[i.id for i in tiers.hop],
        offload_ids=[i.id for i in tiers.offload],
        demoted_ids=[i.id for i in demoted],
        est_tokens=_tok(text),
        full_tokens=full,
    )


# ── Bridge to Arc's placement ledger ──────────────────────────────────────────

def sync_placements(manager, tiers: LDCETiers) -> None:
    from core.tiered_memory import Tier
    kernel_ids = {i.id for i in tiers.kernel}
    for it in tiers.kernel + tiers.hop + tiers.offload:
        size = len(it.text.encode("utf-8"))
        cur  = manager.tier_for(it.id)
        if cur is None:
            manager.place(it.id, it.heat, size, pinned=False)
        else:
            manager.set_pinned(it.id, False)
            manager.update_heat(it.id, it.heat)
        if it.id in kernel_ids:
            if manager.tier_for(it.id) is not Tier.VRAM:
                manager.commit_migration(it.id, Tier.VRAM)
            manager.set_pinned(it.id, True)


# ── LOCI memory source (read-only) ────────────────────────────────────────────

class LociMemorySource:
    """Reads active items from a LOCI context2.db. Opened read-only."""

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).expanduser())

    @classmethod
    def from_env(cls) -> Optional["LociMemorySource"]:
        p = os.environ.get("LOCI_DB_PATH")
        return cls(p) if p and Path(p).expanduser().exists() else None

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _item(r: sqlite3.Row) -> ContextItem:
        sym = None
        try:
            meta = json.loads(r["metadata_json"] or "{}")
            sym  = meta.get("symbol") or meta.get("gist")
        except (KeyError, IndexError, ValueError, TypeError):
            pass
        return ContextItem(
            id=r["id"],
            text=r["canonical_text"],
            heat=float(r["heat"]),
            namespace=r["namespace"],
            symbol=sym,
        )

    def active_items(
        self, namespace_prefix: Optional[str] = None
    ) -> List[ContextItem]:
        q    = "SELECT * FROM memory_item WHERE status = 'active'"
        args: list = []
        if namespace_prefix:
            q += " AND namespace LIKE ?"
            args.append(f"{namespace_prefix}%")
        q += " ORDER BY heat DESC"
        with self._conn() as c:
            return [self._item(r) for r in c.execute(q, args)]

    def expand(self, item_id: str) -> Optional[ContextItem]:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM memory_item WHERE id = ?", (item_id,)
            ).fetchone()
        return self._item(r) if r else None

    def search(self, query: str, limit: int = 5) -> List[ContextItem]:
        with self._conn() as c:
            try:
                rows = c.execute(
                    "SELECT m.* FROM memory_item m "
                    "JOIN memory_item_fts f ON m.id = f.item_id "
                    "WHERE memory_item_fts MATCH ? AND m.status = 'active' "
                    "ORDER BY bm25(memory_item_fts) ASC, m.heat DESC LIMIT ?",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = c.execute(
                    "SELECT * FROM memory_item WHERE status = 'active' "
                    "AND canonical_text LIKE ? "
                    "ORDER BY heat DESC LIMIT ?",
                    (f"%{query}%", limit),
                ).fetchall()
        return [self._item(r) for r in rows]


def build_context(
    source: LociMemorySource,
    manager=None,
    token_budget: int = 1200,
    namespace_prefix: Optional[str] = None,
) -> CompressedContext:
    tiers = compute_tiers(source.active_items(namespace_prefix))
    if manager is not None:
        sync_placements(manager, tiers)
    return render(tiers, token_budget)
