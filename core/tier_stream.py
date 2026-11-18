"""
core/tier_stream.py — continuous tier streaming (VRAM ⇄ DDR5 ⇄ NVMe)

The placement ledger in ``core.tiered_memory`` answers *where* a node should
live. This module answers *how it gets there*: as a stream, not a jump.

Model
-----
* Every item (a context node, or one layer of model weights) is split into
  fixed-size chunks. Each tier holds a *prefix* of the item's chunks
  (``copies[tier]`` bytes). NVMe is the durable archive and always holds the
  whole item, so ``copies[NVME] == size`` for everything we track.
* Movement is hop-by-hop along the tier ladder NVMe → DDR5 → VRAM. A hop
  can only carry bytes that the tier below already has, so a promotion
  NVMe → VRAM is a pipeline: chunk k goes DDR5 → VRAM while chunk k+1 is
  still coming off the SSD. Nothing ever skips a tier, and nothing waits for
  a whole item to land before the next hop starts.
* Promotion copies; demotion releases. Moving up leaves the lower copy in
  place (a warm shadow), so dropping back down a tier is free as long as the
  shadow is still there. Only dirty items (written in VRAM, e.g. KV state)
  must stream back down before the upper copy is released, chunk by chunk.
* Each link has a bandwidth (bytes/s). ``step(dt)`` spends at most
  ``bw * dt`` bytes per link, so a heartbeat tick moves a slice of many items
  rather than all of one. Promotions get bandwidth first, ordered by heat;
  then buoyancy prefetch; then write-backs.
* Heat crossing the boundaries drives the flow, using the same thresholds as
  ``core.heat`` / ``tiered_memory.target_tier`` (hot ≥ 0.80, mild ≥ 0.35,
  with the 0.70 VRAM hysteresis), so VRAM → RAM happens when relevance falls
  to *mild*, and RAM → NVMe when it falls to *cold*.
* Buoyancy: an item whose heat is *rising* fast enough that it will cross
  the next boundary within ``lookahead_s`` starts streaming up early, into
  free space only (it never evicts anything to prefetch). Relevance that is
  climbing floats an item up before it is needed.
* Pressure: when a promotion needs space in a tier, the coldest unpinned
  resident with lower heat than the incoming item is released (its shadow
  below makes that instant) or written back if dirty. Pinned items and the
  active model are never evicted by pressure.

This is a scheduler and residency ledger. The actual byte movers are pluggable
(``Mover``): the default only updates the ledger, which is what the tests and
the status panel use. ``core.model_stager`` drives model weights through the
same stream.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Tuple

from core.heat import TIER_HOT_BOUNDARY, TIER_MILD_BOUNDARY, VRAM_ADMISSION_THRESHOLD
from core.tiered_memory import TIER_ORDER, Tier, target_tier

MB = 1024 * 1024
GB = 1024 * MB

DEFAULT_CHUNK_BYTES = 4 * MB   # matches the 4 MiB /dev/shm ring the bus uses

# Practical (not theoretical) sustained link bandwidth, bytes/s, per direction.
# NVMe: PCIe 4.0 x4 sequential. DDR5→VRAM: pinned-host H2D copy; the B70 is
# PCIe 5.0 x16 and the 5060 Ti PCIe 5.0 x8, so ~24 GB/s is a safe floor for
# either. Measure on the box and pass real numbers in.
DEFAULT_LINK_BW: Dict[Tuple[Tier, Tier], float] = {
    (Tier.NVME, Tier.DDR5): 7.0 * GB,
    (Tier.DDR5, Tier.NVME): 5.0 * GB,
    (Tier.DDR5, Tier.VRAM): 24.0 * GB,
    (Tier.VRAM, Tier.DDR5): 24.0 * GB,
}

_UP = {Tier.NVME: Tier.DDR5, Tier.DDR5: Tier.VRAM}
_DOWN = {Tier.VRAM: Tier.DDR5, Tier.DDR5: Tier.NVME}
_RANK = {t: i for i, t in enumerate(TIER_ORDER)}

# Boundary an item must reach to be *admitted* to each tier from below.
_ADMIT = {Tier.DDR5: TIER_MILD_BOUNDARY, Tier.VRAM: TIER_HOT_BOUNDARY}


# ── Items ─────────────────────────────────────────────────────────────────────

@dataclass
class StreamItem:
    item_id:     str
    size_bytes:  int
    heat:        float
    kind:        str = "context"          # "context" | "weights"
    pinned:      bool = False             # never evicted by pressure, never demoted
    dirty:       bool = False             # VRAM copy newer than the copy below
    priority:    float = 0.0              # added to heat when ordering promotions
    forced_tier: Optional[Tier] = None    # stager override: ignore heat, go here
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    copies:      Dict[Tier, int] = field(default_factory=dict)
    wb_bytes:    int = 0                  # write-back progress for dirty items
    # heat velocity (per second), EMA of observed changes
    velocity:    float = 0.0
    _last_heat_ts: float = field(default_factory=time.time, repr=False)

    def __post_init__(self) -> None:
        for t in Tier:
            self.copies.setdefault(t, 0)
        self.copies[Tier.NVME] = self.size_bytes     # NVMe is the archive

    # Highest tier holding the complete item.
    @property
    def resident(self) -> Tier:
        for t in reversed(TIER_ORDER):
            if self.copies[t] >= self.size_bytes:
                return t
        return Tier.NVME

    # Highest tier holding *any* of the item (it may be mid-stream).
    @property
    def frontier(self) -> Tier:
        for t in reversed(TIER_ORDER):
            if self.copies[t] > 0:
                return t
        return Tier.NVME

    def fraction_in(self, tier: Tier) -> float:
        return self.copies[tier] / self.size_bytes if self.size_bytes else 1.0

    @property
    def target(self) -> Tier:
        if self.forced_tier is not None:
            return self.forced_tier
        if self.pinned:
            return max(self.frontier, self.resident, key=_RANK.get)
        return target_tier(self.resident, self.heat)

    def predicted_heat(self, lookahead_s: float) -> float:
        return min(1.0, self.heat + max(0.0, self.velocity) * lookahead_s)


@dataclass
class TransferEvent:
    item_id:   str
    src:       Tier
    dst:       Tier
    nbytes:    int
    reason:    str            # "promote" | "prefetch" | "writeback" | "release" | "evict"
    completed: bool           # item now fully present in dst (or fully gone from src)


class Mover(Protocol):
    """Moves real bytes. Called once per transfer slice, after the ledger
    decided it. Must be idempotent per (item, offset) and may block."""

    def __call__(self, item: StreamItem, src: Tier, dst: Tier,
                 offset: int, nbytes: int) -> None: ...


def ledger_only_mover(item: StreamItem, src: Tier, dst: Tier,
                      offset: int, nbytes: int) -> None:
    return None


# ── Stream engine ─────────────────────────────────────────────────────────────

class TierStream:
    def __init__(
        self,
        capacity_bytes: Optional[Dict[Tier, int]] = None,
        link_bw:        Optional[Dict[Tuple[Tier, Tier], float]] = None,
        lookahead_s:    float = 2.0,
        velocity_alpha: float = 0.5,
        mover:          Mover = ledger_only_mover,
        manager=None,
    ) -> None:
        cap = capacity_bytes or {}
        self.capacity: Dict[Tier, int] = {
            Tier.VRAM: cap.get(Tier.VRAM, 48_152 * MB),
            Tier.DDR5: cap.get(Tier.DDR5, 32_768 * MB),
            Tier.NVME: cap.get(Tier.NVME, 1 << 62),
        }
        self.link_bw = dict(DEFAULT_LINK_BW)
        if link_bw:
            self.link_bw.update(link_bw)
        self.lookahead_s = lookahead_s
        self.velocity_alpha = velocity_alpha
        self.mover = mover
        self.manager = manager            # optional core.tiered_memory ledger
        self.items: Dict[str, StreamItem] = {}
        self.reserved: Dict[Tier, int] = {t: 0 for t in Tier}   # e.g. KV cache
        self.log: List[TransferEvent] = []
        self._lock = threading.RLock()
        self._listeners: List[Callable[[TransferEvent], None]] = []

    # ── registration ──────────────────────────────────────────────────────

    def add(self, item: StreamItem, at: Tier = Tier.NVME) -> StreamItem:
        """Track an item. ``at`` says where its full copy already is; every
        tier below that is assumed to hold a copy too (it got there by
        streaming up, and promotion copies)."""
        with self._lock:
            for t in TIER_ORDER:
                if _RANK[t] <= _RANK[at]:
                    item.copies[t] = item.size_bytes
            self.items[item.item_id] = item
        return item

    def remove(self, item_id: str) -> Optional[StreamItem]:
        with self._lock:
            return self.items.pop(item_id, None)

    def on_event(self, fn: Callable[[TransferEvent], None]) -> None:
        self._listeners.append(fn)

    def set_heat(self, item_id: str, heat: float, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            it = self.items[item_id]
            dt = max(1e-3, now - it._last_heat_ts)
            inst = (heat - it.heat) / dt
            a = self.velocity_alpha
            it.velocity = a * inst + (1 - a) * it.velocity
            it.heat = heat
            it._last_heat_ts = now

    def sync_from_manager(self) -> None:
        """Mirror the placement ledger: new nodes are added at the tier the
        ledger says their data is in; heat is refreshed for known ones."""
        if self.manager is None:
            return
        with self.manager._lock:
            placements = list(self.manager._placements.values())
        for p in placements:
            it = self.items.get(p.node_id)
            if it is None:
                self.add(StreamItem(p.node_id, p.size_bytes, p.heat,
                                    pinned=p.pinned, dirty=p.dirty), at=p.tier)
            else:
                if abs(it.heat - p.heat) > 1e-12:
                    self.set_heat(p.node_id, p.heat)
                it.pinned = p.pinned

    # ── accounting ────────────────────────────────────────────────────────

    def used(self, tier: Tier) -> int:
        return sum(it.copies[tier] for it in self.items.values())

    def free(self, tier: Tier) -> int:
        return max(0, self.capacity[tier] - self.reserved[tier] - self.used(tier))

    def reserve(self, tier: Tier, nbytes: int) -> None:
        """Hold space that no item may stream into (KV cache, activations)."""
        with self._lock:
            self.reserved[tier] = max(0, nbytes)

    def snapshot(self) -> Dict[str, dict]:
        with self._lock:
            return {
                t.value: {
                    "used_mb": self.used(t) / MB,
                    "capacity_mb": self.capacity[t] / MB if t is not Tier.NVME else None,
                    "streaming": sorted(
                        i.item_id for i in self.items.values()
                        if 0 < i.copies[t] < i.size_bytes
                    ),
                }
                for t in TIER_ORDER
            }

    # ── the tick ──────────────────────────────────────────────────────────

    def step(self, dt: float) -> List[TransferEvent]:
        """Advance the stream by ``dt`` seconds of link bandwidth."""
        if self.manager is not None:
            self.sync_from_manager()
        with self._lock:
            budget = {link: bw * dt for link, bw in self.link_bw.items()}
            events: List[TransferEvent] = []
            self._demote(budget, events)            # free releases first
            self._promote(budget, events, prefetch=False)
            self._promote(budget, events, prefetch=True)
            self._demote(budget, events)            # down-links, after up-links spent
        for ev in events:
            self.log.append(ev)
            if ev.completed and self.manager is not None:
                it = self.items.get(ev.item_id)
                if it is not None and self.manager.tier_for(ev.item_id) is not None \
                        and self.manager.tier_for(ev.item_id) is not it.resident:
                    self.manager.commit_migration(ev.item_id, it.resident)
            for fn in self._listeners:
                fn(ev)
        return events

    def run_until_idle(self, dt: float = 0.01, max_steps: int = 100_000) -> float:
        """Step until nothing moves. Returns simulated seconds elapsed."""
        t = 0.0
        for _ in range(max_steps):
            if not self.step(dt):
                return t
            t += dt
        return t

    # ── internals: up ─────────────────────────────────────────────────────

    def _wants_up(self, it: StreamItem, prefetch: bool) -> Optional[Tier]:
        """Next tier this item should stream *into* (one hop above the
        highest complete tier), or None."""
        if it.resident is Tier.VRAM:
            return None
        nxt = _UP[it.resident]
        tgt = it.target
        if not prefetch:
            return nxt if _RANK[tgt] >= _RANK[nxt] else None
        # buoyancy: heat rising toward the next boundary
        if it.pinned or it.forced_tier is not None or _RANK[tgt] >= _RANK[nxt]:
            return None
        if it.predicted_heat(self.lookahead_s) >= _ADMIT[nxt]:
            return nxt
        return None

    def _promote(self, budget, events, prefetch: bool) -> None:
        cands = []
        for it in self.items.values():
            dst = self._wants_up(it, prefetch)
            if dst is not None:
                cands.append((it, dst))
        cands.sort(key=lambda c: -(c[0].heat + c[0].priority))
        for it, dst in cands:
            self._stream_up(it, dst, budget, events, prefetch)
            # pipelining: while DDR5 fills, VRAM takes the chunks that landed
            if dst is Tier.DDR5 and not prefetch and _RANK[it.target] >= _RANK[Tier.VRAM]:
                self._stream_up(it, Tier.VRAM, budget, events, prefetch)

    def _stream_up(self, it, dst, budget, events, prefetch) -> None:
        src = _DOWN[dst]
        link = (src, dst)
        want = min(it.copies[src] - it.copies[dst], int(budget.get(link, 0)))
        if want <= 0:
            return
        room = self.free(dst)
        if room < want and not prefetch:
            self._make_room(dst, want - room, it, events)
            room = self.free(dst)
        want = min(want, room)       # prefetch only ever uses free space
        if want <= 0:
            return
        self.mover(it, src, dst, it.copies[dst], want)
        it.copies[dst] += want
        budget[link] -= want
        events.append(TransferEvent(it.item_id, src, dst, want,
                                    "prefetch" if prefetch else "promote",
                                    it.copies[dst] >= it.size_bytes))

    def _make_room(self, tier: Tier, need: int, incoming: StreamItem, events) -> None:
        """Pressure: release the coldest clean, unpinned residents of ``tier``
        whose heat is below the incoming item's. Clean data always has a copy
        on NVMe, so release is instant. Dirty residents are never dropped by
        pressure (they demote through write-back instead). A DDR5 copy that
        backs a VRAM copy is kept as its shadow."""
        below = _DOWN.get(tier)
        if below is None:
            return
        rank_in = incoming.heat + incoming.priority
        victims = sorted(
            (v for v in self.items.values()
             if v is not incoming and v.copies[tier] > 0 and not v.pinned
             and not v.dirty and v.forced_tier is not tier
             and (v.heat + v.priority) < rank_in
             and not (tier is Tier.DDR5 and v.copies[Tier.VRAM] > 0)),
            key=lambda v: v.heat + v.priority,
        )
        for v in victims:
            if need <= 0:
                break
            n = v.copies[tier]
            v.copies[tier] = 0
            need -= n
            events.append(TransferEvent(v.item_id, tier, below, n, "evict", True))

    # ── internals: down ───────────────────────────────────────────────────

    def _demote(self, budget, events) -> None:
        """Move items whose target is below where their data is.

        For each upper tier the item occupies above its target:
          * dirty and this is its newest copy: write it back one hop first;
          * clean and the tier below already covers it (or the tier below is
            NVMe, which always has everything): release instantly;
          * clean, target is exactly the tier below, shadow is short: stream
            the missing bytes down (it is landing in RAM, so RAM must get it);
          * clean, target is further down (VRAM → NVMe): release; the
            archive has it.
        """
        for it in sorted(self.items.values(), key=lambda i: i.heat + i.priority):
            if it.pinned:
                continue
            tgt = it.target
            for t in (Tier.VRAM, Tier.DDR5):
                if _RANK[t] <= _RANK[tgt] or it.copies[t] == 0:
                    continue
                if (it.forced_tier is None
                        and it.predicted_heat(self.lookahead_s) >= _ADMIT[t]):
                    continue                           # buoyant: keep the prefetch
                below = _DOWN[t]
                if it.dirty and t is it.frontier:
                    self._writeback(it, t, below, budget, events)
                    if it.dirty and t is it.frontier:
                        break                          # still writing
                    continue
                covered = below is Tier.NVME or it.copies[below] >= it.copies[t]
                if not covered and below is tgt:
                    self._stream_down(it, t, below, budget, events)
                    covered = it.copies[below] >= it.copies[t]
                if covered or _RANK[tgt] < _RANK[below]:
                    n = it.copies[t]
                    it.copies[t] = 0
                    events.append(TransferEvent(it.item_id, t, below, n, "release", True))

    def _stream_down(self, it, src, dst, budget, events) -> int:
        link = (src, dst)
        n = min(it.copies[src] - it.copies[dst], int(budget.get(link, 0)))
        if n <= 0:
            return 0
        if dst is not Tier.NVME and self.free(dst) < n:
            self._make_room(dst, n - self.free(dst), it, events)
            n = min(n, self.free(dst))
            if n <= 0:
                return 0
        self.mover(it, src, dst, it.copies[dst], n)
        it.copies[dst] += n
        budget[link] -= n
        events.append(TransferEvent(it.item_id, src, dst, n, "writeback",
                                    it.copies[dst] >= it.copies[src]))
        return n

    def _writeback(self, it, src, dst, budget, events) -> None:
        """Dirty: the copy in ``src`` is newer than anything below it."""
        if dst is Tier.NVME:
            link = (src, dst)
            n = min(it.copies[src] - it.wb_bytes, int(budget.get(link, 0)))
            if n > 0:
                self.mover(it, src, dst, it.wb_bytes, n)
                it.wb_bytes += n
                budget[link] -= n
                done = it.wb_bytes >= it.copies[src]
                events.append(TransferEvent(it.item_id, src, dst, n, "writeback", done))
                if done:
                    it.dirty = False
                    it.wb_bytes = 0
            return
        # VRAM → DDR5: re-copy the whole VRAM prefix (the DDR5 shadow is stale)
        if it.wb_bytes == 0 and it.copies[dst] > 0:
            it.copies[dst] = 0                         # stale shadow
        n = min(it.copies[src] - it.copies[dst], int(budget.get((src, dst), 0)))
        if n > 0:
            if self.free(dst) < n:
                self._make_room(dst, n - self.free(dst), it, events)
                n = min(n, self.free(dst))
            if n > 0:
                self.mover(it, src, dst, it.copies[dst], n)
                it.copies[dst] += n
                it.wb_bytes += n
                budget[(src, dst)] -= n
        if it.copies[dst] >= it.copies[src]:
            # DDR5 now holds the newest copy; drop VRAM, stay dirty vs NVMe
            m = it.copies[src]
            it.copies[src] = 0
            it.wb_bytes = 0
            events.append(TransferEvent(it.item_id, src, dst, n, "writeback", True))
            events.append(TransferEvent(it.item_id, src, dst, m, "release", True))
        elif n > 0:
            events.append(TransferEvent(it.item_id, src, dst, n, "writeback", False))


# ── Analytic estimate (used by the stager and the UI) ─────────────────────────

def pipelined_load_seconds(
    size_bytes: int,
    in_ddr5_bytes: int = 0,
    nvme_bw: float = DEFAULT_LINK_BW[(Tier.NVME, Tier.DDR5)],
    pcie_bw: float = DEFAULT_LINK_BW[(Tier.DDR5, Tier.VRAM)],
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> float:
    """Time to bring ``size_bytes`` into VRAM when NVMe→DDR5 and DDR5→VRAM
    overlap chunk by chunk. The slower link dominates; the other only adds
    the one-chunk fill latency at the start."""
    cold = max(0, size_bytes - in_ddr5_bytes)
    t_nvme = cold / nvme_bw
    t_pcie = size_bytes / pcie_bw
    if cold == 0:
        return t_pcie
    fill = min(chunk_bytes, cold) / nvme_bw
    return max(t_nvme + min(chunk_bytes, size_bytes) / pcie_bw, t_pcie + fill)
