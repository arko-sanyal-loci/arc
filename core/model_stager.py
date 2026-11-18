"""
core/model_stager.py — draft-scaffold → target handoff, streamed through the tiers

The B70 runs speculative decoding with a small draft and a large target on the
same card (LOCI hybrid design §2a). Loading the target is the slow part, so we
do not wait for it:

  SCAFFOLD  The draft is resident in VRAM and does the first pass (plan,
            outline, skeleton code). Meanwhile the target's layers stream
            NVMe → DDR5 → VRAM in layer order, using only VRAM the draft and
            the KV reserve are not using. Hot context is not evicted for it.
  OVERLAP   Scaffold is done and both models fit: the target finishes landing
            with priority over context, then the pair runs speculative
            decoding (draft proposes, target verifies). The draft stays.
  HANDOFF   Scaffold is done and both do NOT fit: the draft is released to
            DDR5 (instant: it still has its RAM shadow) so the target can
            take its VRAM. The draft stays warm in RAM for a fast re-onload.
  TARGET    The target is fully resident (and the draft is gone, or kept for
            speculation in OVERLAP, which then reports ``speculative_ready``).

Every layer is a ``StreamItem`` in the shared ``TierStream``, so model weights
and context compete for the same links and capacity under one set of rules.
The stager steers them with ``forced_tier`` and ``priority`` instead of heat.

Real byte movement
------------------
``PageCacheMover`` makes NVMe → DDR5 real today: it asks the kernel to read
the GGUF byte range of each layer into the page cache (``posix_fadvise
WILLNEED``), so when ``llama-server`` maps the file the load is bound by PCIe,
not the SSD. DDR5 → VRAM is done by the inference runtime when it loads the
model; this module tells you *when* that can start (``target_can_load``) and
what to release first. Per-layer VRAM upload inside a running server is not
something llama.cpp exposes, so that hop is a ledger move here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from core.tier_stream import (
    MB, StreamItem, Tier, TierStream, pipelined_load_seconds,
)

# Priorities sit on top of heat (0..1), so weights outrank context when
# they need to, and the active model outranks the one being staged.
PRIO_STAGING = 0.5    # below hot context: target only uses free VRAM
PRIO_TARGET = 2.0     # after scaffold: target outranks context
PRIO_ACTIVE = 3.0     # a model that is generating is never evicted


@dataclass
class ModelSpec:
    name: str
    layer_bytes: List[int]
    kv_reserve_bytes: int = 0
    path: Optional[str] = None                  # GGUF file, for PageCacheMover
    layer_offsets: Optional[List[int]] = None   # byte offset of each layer in path

    @classmethod
    def uniform(cls, name: str, total_bytes: int, n_layers: int,
                kv_reserve_bytes: int = 0, path: Optional[str] = None) -> "ModelSpec":
        per = total_bytes // n_layers
        sizes = [per] * n_layers
        sizes[-1] += total_bytes - per * n_layers
        offs, o = [], 0
        for s in sizes:
            offs.append(o)
            o += s
        return cls(name, sizes, kv_reserve_bytes, path, offs)

    @property
    def total_bytes(self) -> int:
        return sum(self.layer_bytes)

    def item_ids(self) -> List[str]:
        return [f"model:{self.name}:L{i:03d}" for i in range(len(self.layer_bytes))]


class Phase(Enum):
    IDLE = "idle"
    SCAFFOLD = "scaffold"
    OVERLAP = "overlap"
    HANDOFF = "handoff"
    TARGET = "target"


class ModelStager:
    def __init__(self, stream: TierStream, draft: ModelSpec, target: ModelSpec,
                 keep_draft_for_speculation: bool = True) -> None:
        self.stream = stream
        self.draft = draft
        self.target = target
        self.keep_draft = keep_draft_for_speculation
        self.phase = Phase.IDLE
        self._meta: Dict[str, Tuple[ModelSpec, int]] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────

    def begin(self, draft_at: Tier = Tier.VRAM, target_at: Tier = Tier.NVME) -> None:
        """Start a request: the draft is (or becomes) active, the target
        starts streaming in behind it."""
        n = len(self.target.layer_bytes)
        for spec, at, prio, forced in (
            (self.draft, draft_at, PRIO_ACTIVE, Tier.VRAM),
            (self.target, target_at, PRIO_STAGING, Tier.VRAM),
        ):
            for i, (iid, size) in enumerate(zip(spec.item_ids(), spec.layer_bytes)):
                if iid in self.stream.items:
                    it = self.stream.items[iid]
                else:
                    it = self.stream.add(StreamItem(iid, size, heat=0.0, kind="weights"), at=at)
                it.forced_tier = forced
                # earlier layers first, so layer-ordered execution could start early
                it.priority = prio + (0.001 * (n - i) / n if spec is self.target else 0.0)
                self._meta[iid] = (spec, i)
        self.stream.reserve(Tier.VRAM, self.draft.kv_reserve_bytes)
        self.phase = Phase.SCAFFOLD

    def scaffold_done(self) -> Phase:
        """The draft finished its first pass. Decide overlap vs handoff."""
        both = self.draft.total_bytes + self.target.total_bytes
        kv = max(self.draft.kv_reserve_bytes, 0) + self.target.kv_reserve_bytes
        fits = both + kv <= self._vram_budget()
        for iid in self.target.item_ids():
            self.stream.items[iid].priority += PRIO_TARGET - PRIO_STAGING
        if self.keep_draft and fits:
            self.phase = Phase.OVERLAP
            self.stream.reserve(Tier.VRAM, kv)
        else:
            self.release_draft()
            self.phase = Phase.HANDOFF
            self.stream.reserve(Tier.VRAM, self.target.kv_reserve_bytes)
        return self.phase

    def release_draft(self, to: Tier = Tier.DDR5) -> None:
        """Offload the draft one tier down (warm), not to the archive, so it
        can come back for the next request at PCIe speed."""
        for iid in self.draft.item_ids():
            it = self.stream.items[iid]
            it.forced_tier = to
            it.priority = 0.0

    def tick(self, dt: float):
        events = self.stream.step(dt)
        self._advance()
        return events

    def _advance(self) -> None:
        if self.phase in (Phase.OVERLAP, Phase.HANDOFF) and self.target_resident():
            if self.phase is Phase.HANDOFF or not self.keep_draft:
                self.phase = Phase.TARGET
            for iid in self.target.item_ids():
                self.stream.items[iid].priority = PRIO_ACTIVE

    # ── queries ───────────────────────────────────────────────────────────

    def _vram_budget(self) -> int:
        """VRAM the two models may share: capacity minus context that is
        pinned there (the LDCE kernel never moves for weights)."""
        pinned = sum(i.copies[Tier.VRAM] for i in self.stream.items.values()
                     if i.pinned and i.kind != "weights")
        return self.stream.capacity[Tier.VRAM] - pinned

    def fraction(self, spec: ModelSpec, tier: Tier = Tier.VRAM) -> float:
        got = sum(self.stream.items[i].copies[tier] for i in spec.item_ids())
        return got / spec.total_bytes if spec.total_bytes else 1.0

    def draft_resident(self) -> bool:
        return self.fraction(self.draft) >= 1.0

    def target_resident(self) -> bool:
        return self.fraction(self.target) >= 1.0

    def target_can_load(self) -> bool:
        """True once the target's whole file is in RAM (page cache), so the
        runtime's load is PCIe-bound. Start the target server then."""
        return self.fraction(self.target, Tier.DDR5) >= 1.0

    def speculative_ready(self) -> bool:
        return self.draft_resident() and self.target_resident()

    def who_generates(self) -> Optional[str]:
        if self.speculative_ready():
            return f"{self.draft.name}->{self.target.name} (speculative)"
        if self.target_resident():
            return self.target.name
        if self.draft_resident():
            return self.draft.name
        return None

    def eta_seconds(self) -> float:
        """Estimated seconds until the target is fully in VRAM, assuming the
        links are free (pipelined NVMe→DDR5→VRAM)."""
        remaining = sum(
            self.stream.items[i].size_bytes - self.stream.items[i].copies[Tier.VRAM]
            for i in self.target.item_ids())
        in_ram = sum(
            max(0, self.stream.items[i].copies[Tier.DDR5] - self.stream.items[i].copies[Tier.VRAM])
            for i in self.target.item_ids())
        if remaining <= 0:
            return 0.0
        return pipelined_load_seconds(
            remaining, in_ram,
            self.stream.link_bw[(Tier.NVME, Tier.DDR5)],
            self.stream.link_bw[(Tier.DDR5, Tier.VRAM)])

    def status(self) -> dict:
        return {
            "phase": self.phase.value,
            "draft": {"name": self.draft.name,
                      "vram": round(self.fraction(self.draft), 4),
                      "ddr5": round(self.fraction(self.draft, Tier.DDR5), 4)},
            "target": {"name": self.target.name,
                       "vram": round(self.fraction(self.target), 4),
                       "ddr5": round(self.fraction(self.target, Tier.DDR5), 4)},
            "generating": self.who_generates(),
            "speculative_ready": self.speculative_ready(),
            "target_can_load": self.target_can_load(),
            "eta_s": round(self.eta_seconds(), 3),
        }


# ── Real NVMe → page cache prewarm ────────────────────────────────────────────

class PageCacheMover:
    """Mover that makes the NVMe → DDR5 hop real for GGUF weights: it asks
    the kernel to read the layer's byte range into the page cache. Other hops
    and non-weight items are ledger-only (context lives in SQLite, which has
    its own cache). Linux only; a no-op where posix_fadvise is missing."""

    def __init__(self, stager_specs: List[ModelSpec]) -> None:
        self._where: Dict[str, Tuple[str, int]] = {}
        for spec in stager_specs:
            if not spec.path or not spec.layer_offsets:
                continue
            for iid, off in zip(spec.item_ids(), spec.layer_offsets):
                self._where[iid] = (spec.path, off)
        self._fds: Dict[str, int] = {}
        self.calls: List[Tuple[str, int, int]] = []

    def __call__(self, item, src, dst, offset, nbytes) -> None:
        if not (src is Tier.NVME and dst is Tier.DDR5):
            return
        loc = self._where.get(item.item_id)
        if loc is None or not hasattr(os, "posix_fadvise"):
            return
        path, base = loc
        fd = self._fds.get(path)
        if fd is None:
            fd = os.open(path, os.O_RDONLY)
            self._fds[path] = fd
        os.posix_fadvise(fd, base + offset, nbytes, os.POSIX_FADV_WILLNEED)
        self.calls.append((path, base + offset, nbytes))

    def close(self) -> None:
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()
