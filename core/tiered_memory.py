"""
tiered_memory.py — 3-tier memory hierarchy for Arc SLP nodes

Tier map (heat → storage):
  HOT  (H ≥ 0.70)              → VRAM  (Arc B70 32 GB GDDR6 + RTX 5060 Ti 16 GB GDDR7)
  WARM (0.35 ≤ H < 0.70)       → DDR5  (32 GB DDR5-5200; XMP1 required for full BW)
  COLD (H < 0.35)              → NVMe  (PCIe SSD)

Promotion path:  NVMe → DDR5 → VRAM
Demotion path:   VRAM → DDR5 → NVMe

NOTE: DDR5-5200 defaults to DDR5-4800 (JEDEC) until XMP1 / EXPO is enabled in BIOS.
      At 5200 MT/s, peak BW ≈ 83 GB/s (theoretical) / ~41 GB/s real per channel.
      XMP1 must be ON to avoid the VRAM→DDR5 spill path being a bandwidth bottleneck.

Heat boundaries are aligned with SLP fidelity levels from slp_encoding.py:
  alpha_floor  H=0.05  32 B    → NVMe (register stub only)
  low          H=0.15  76 B    → NVMe
  warm         H=0.35  268 B   → DDR5  ← tier boundary
  active       H=0.55  2060 B  → DDR5
  hot          H=0.70  8204 B  → VRAM  ← tier boundary
  burning      H=0.85  16396 B → VRAM
  sovereign    H=0.9999 32780 B→ VRAM
"""

from __future__ import annotations

import json
import mmap
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Tier enum ────────────────────────────────────────────────────────────────

class Tier(Enum):
    NVME  = "nvme"    # cold: NVMe SSD
    DDR5  = "ddr5"    # warm: DDR5 RAM (XMP1 → 5200 MT/s)
    VRAM  = "vram"    # hot:  VRAM (Arc B70 + RTX 5060 Ti)

# ── Heat boundaries ──────────────────────────────────────────────────────────

TIER_THRESHOLDS: Dict[Tier, Tuple[float, float]] = {
    Tier.NVME: (0.00,  0.35),   # H in [0.0,  0.35)
    Tier.DDR5: (0.35,  0.70),   # H in [0.35, 0.70)
    Tier.VRAM: (0.70,  1.0001), # H in [0.70, 1.0]
}

def heat_to_tier(heat: float) -> Tier:
    """Map an SLP heat value to the appropriate storage tier."""
    if heat >= 0.70:
        return Tier.VRAM
    elif heat >= 0.35:
        return Tier.DDR5
    else:
        return Tier.NVME

TIER_ORDER = [Tier.NVME, Tier.DDR5, Tier.VRAM]  # low → high

def tier_distance(src: Tier, dst: Tier) -> int:
    """Steps required to move src → dst. Negative = demotion."""
    return TIER_ORDER.index(dst) - TIER_ORDER.index(src)

# ── Hardware capacities ───────────────────────────────────────────────────────

@dataclass
class TierCapacity:
    tier:        Tier
    total_mb:    int
    # Practical bandwidth ceiling (not theoretical peak)
    bw_read_gbs:  float
    bw_write_gbs: float
    # Note shown when this tier is misconfigured
    note:         str = ""

TIER_CAPACITIES: Dict[Tier, TierCapacity] = {
    Tier.NVME: TierCapacity(
        tier         = Tier.NVME,
        total_mb     = 0,       # discovered at runtime from df
        bw_read_gbs  = 7.0,     # PCIe 4.0 NVMe typical sequential
        bw_write_gbs = 5.0,
        note         = "Size discovered at runtime from NVMe mount",
    ),
    Tier.DDR5: TierCapacity(
        tier         = Tier.DDR5,
        total_mb     = 32_768,  # 32 GB
        bw_read_gbs  = 41.6,    # DDR5-5200 single-channel real BW
        bw_write_gbs = 41.6,
        note         = "⚠ XMP1 must be enabled in BIOS to reach DDR5-5200. "
                       "Without XMP1 the stick runs at DDR5-4800 (~37.5 GB/s). "
                       "Check: sudo dmidecode -t memory | grep Speed",
    ),
    Tier.VRAM: TierCapacity(
        tier         = Tier.VRAM,
        total_mb     = 48_152,  # 32768 Arc + 16384 RTX (minus OS headroom ≈ 1%)
        bw_read_gbs  = 608.0,   # Arc B70 internal VRAM BW (dominant path)
        bw_write_gbs = 448.0,   # RTX 5060 Ti internal VRAM BW
        note         = "Cross-GPU inter-bus capped at 7.9 GB/s via /dev/shm",
    ),
}

# ── Node placement ────────────────────────────────────────────────────────────

@dataclass
class NodePlacement:
    node_id:      str
    heat:         float
    tier:         Tier
    size_bytes:   int
    placed_at_ns: int = field(default_factory=lambda: time.time_ns())
    pinned:       bool = False       # sovereignty lock — never demote
    dirty:        bool = False       # written to VRAM, not yet flushed to DDR5

@dataclass
class TierStats:
    tier:          Tier
    used_mb:       float
    total_mb:      int
    node_count:    int
    hot_ratio:     float   # fraction of nodes actually hot (heat ≥ tier lower bound)

    @property
    def free_mb(self) -> float:
        return max(0.0, self.total_mb - self.used_mb)

    @property
    def utilisation_pct(self) -> float:
        return (self.used_mb / self.total_mb * 100) if self.total_mb else 0.0

# ── Tiered memory manager ─────────────────────────────────────────────────────

class TieredMemoryManager:
    """
    Tracks SLP node placement across NVMe / DDR5 / VRAM.

    This is a placement ledger, not a DMA engine — actual data movement
    (VRAM↔DDR5) is handled by GPUOffloadManager; (DDR5↔NVMe) by standard
    file I/O through the Loci SQLite WAL path.

    The manager answers:
      • Which tier should node X live in right now? (by heat)
      • Which nodes should be promoted / demoted?
      • What is the current utilisation of each tier?
    """

    def __init__(
        self,
        nvme_base_path: str = "data/",
        shm_state_path: str = "/dev/shm/arc_tier_state.json",
        ddr5_total_mb:  int = 32_768,
        vram_total_mb:  int = 48_152,
    ):
        self.nvme_base   = Path(nvme_base_path)
        self.shm_path    = shm_state_path
        # RLock (reentrant) so place()/update_heat() can call _publish_state()
        # without deadlocking — both hold the lock before the nested acquire.
        self._lock       = threading.RLock()

        # Update capacities from constructor args (allow override for testing)
        TIER_CAPACITIES[Tier.DDR5].total_mb = ddr5_total_mb
        TIER_CAPACITIES[Tier.VRAM].total_mb = vram_total_mb

        # {node_id: NodePlacement}
        self._placements: Dict[str, NodePlacement] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def place(
        self,
        node_id:    str,
        heat:       float,
        size_bytes: int,
        pinned:     bool = False,
    ) -> NodePlacement:
        """Record where a node should live given its current heat."""
        tier = heat_to_tier(heat)
        with self._lock:
            placement = NodePlacement(
                node_id    = node_id,
                heat       = heat,
                tier       = tier,
                size_bytes = size_bytes,
                pinned     = pinned,
            )
            self._placements[node_id] = placement
        self._publish_state()
        return placement

    def update_heat(self, node_id: str, new_heat: float) -> Optional[NodePlacement]:
        """
        Update a node's heat. Returns the placement if tier changed,
        None if the node stayed in the same tier (no migration needed).
        """
        with self._lock:
            existing = self._placements.get(node_id)
            if existing is None:
                return None
            if existing.pinned:
                existing.heat = new_heat
                return None   # pinned nodes never migrate

            old_tier = existing.tier
            new_tier = heat_to_tier(new_heat)
            existing.heat = new_heat
            existing.tier = new_tier

            if old_tier != new_tier:
                self._publish_state()
                return existing
            return None

    def evict(self, node_id: str) -> Optional[NodePlacement]:
        """Remove a node from the tier ledger (node expired / dropped)."""
        with self._lock:
            return self._placements.pop(node_id, None)

    def pending_migrations(self) -> List[Tuple[NodePlacement, Tier]]:
        """
        Return a list of (placement, target_tier) for nodes whose current
        tier no longer matches their heat. Sorted: promotions first (VRAM),
        then demotions.
        """
        migrations = []
        with self._lock:
            for p in self._placements.values():
                target = heat_to_tier(p.heat)
                if target != p.tier and not p.pinned:
                    migrations.append((p, target))
        # promotions (dist > 0) before demotions (dist < 0)
        migrations.sort(key=lambda x: -tier_distance(x[0].tier, x[1]))
        return migrations

    def stats(self) -> Dict[Tier, TierStats]:
        """Current utilisation per tier."""
        accum: Dict[Tier, TierStats] = {
            t: TierStats(
                tier       = t,
                used_mb    = 0.0,
                total_mb   = TIER_CAPACITIES[t].total_mb,
                node_count = 0,
                hot_ratio  = 0.0,
            )
            for t in Tier
        }
        with self._lock:
            for p in self._placements.values():
                st = accum[p.tier]
                st.used_mb    += p.size_bytes / (1024 * 1024)
                st.node_count += 1

        # Update NVMe total from df
        try:
            st = os.statvfs(str(self.nvme_base))
            accum[Tier.NVME].total_mb = int(st.f_blocks * st.f_frsize / (1024 * 1024))
        except OSError:
            pass

        return accum

    def tier_for(self, node_id: str) -> Optional[Tier]:
        with self._lock:
            p = self._placements.get(node_id)
            return p.tier if p else None

    def xmp1_check(self) -> Tuple[bool, str]:
        """
        Best-effort check whether DDR5 is running at XMP1 speed.
        Returns (ok, message).
        """
        try:
            import subprocess
            result = subprocess.run(
                ["sudo", "dmidecode", "-t", "memory"],
                capture_output=True, text=True, timeout=5,
            )
            lines = result.stdout + result.stderr
            # Look for "5200" in Speed lines
            speed_lines = [l for l in lines.splitlines() if "Speed" in l and "MHz" in l]
            for ln in speed_lines:
                if "5200" in ln or "5333" in ln:
                    return True, f"DDR5 running at XMP1 speed ({ln.strip()})"
                if "4800" in ln or "4400" in ln:
                    return False, (
                        f"DDR5 at JEDEC speed ({ln.strip()}) — XMP1 not active. "
                        "Enable XMP1 in BIOS to unlock DDR5-5200 (adds ~10% BW). "
                        "Path: BIOS → AI Tweaker → Ai Overclock Tuner → XMP I"
                    )
            return True, "dmidecode check inconclusive — assuming XMP1 OK"
        except Exception as e:
            return True, f"Could not check XMP1 (need sudo dmidecode): {e}"

    # ── Internal ──────────────────────────────────────────────────────────

    def _publish_state(self) -> None:
        """Write tier stats to /dev/shm for the UI status panel."""
        try:
            with self._lock:
                snap = {
                    t.value: {
                        "used_mb":   sum(
                            p.size_bytes / (1024 * 1024)
                            for p in self._placements.values()
                            if p.tier == t
                        ),
                        "node_count": sum(
                            1 for p in self._placements.values()
                            if p.tier == t
                        ),
                    }
                    for t in Tier
                }
            with open(self.shm_path, "w") as f:
                json.dump({"tiers": snap, "ts": time.time()}, f)
        except OSError:
            pass   # /dev/shm may not be writable in all environments

    def summary(self) -> str:
        """Human-readable tier utilisation summary."""
        lines = ["Tiered Memory Summary", "=" * 48]
        st = self.stats()
        for tier in TIER_ORDER:
            s   = st[tier]
            cap = TIER_CAPACITIES[tier]
            bar_w  = 20
            filled = int(s.utilisation_pct / 100 * bar_w)
            bar    = "█" * filled + "░" * (bar_w - filled)
            lines.append(
                f"  {tier.value.upper():<6}  [{bar}] "
                f"{s.used_mb:7.1f}/{s.total_mb:6d} MB  "
                f"({s.utilisation_pct:4.1f}%)  {s.node_count} nodes"
            )
            if cap.note and s.utilisation_pct > 80:
                lines.append(f"    ⚠ {cap.note}")
        return "\n".join(lines)


# ── Migration planner ─────────────────────────────────────────────────────────

@dataclass
class MigrationPlan:
    promotions: List[Tuple[NodePlacement, Tier]]   # gain capacity
    demotions:  List[Tuple[NodePlacement, Tier]]   # free capacity
    skipped:    List[Tuple[NodePlacement, str]]     # reason: "pinned" | "no space"

def plan_migrations(
    manager:       TieredMemoryManager,
    max_batch_mb:  float = 512.0,   # max transfer per tick
) -> MigrationPlan:
    """
    Given pending migrations, build a concrete plan respecting tier capacity.
    Promotions are preferred; demotions only if target tier has space.
    """
    pending    = manager.pending_migrations()
    stats      = manager.stats()
    plan       = MigrationPlan(promotions=[], demotions=[], skipped=[])
    batch_mb   = 0.0

    for placement, target_tier in pending:
        node_mb = placement.size_bytes / (1024 * 1024)
        if batch_mb + node_mb > max_batch_mb:
            plan.skipped.append((placement, "batch_limit"))
            continue

        t_stat = stats[target_tier]
        cap    = TIER_CAPACITIES[target_tier]

        # Leave headroom in target tier
        headroom_mb = cap.total_mb * 0.05
        if t_stat.free_mb - node_mb < headroom_mb:
            plan.skipped.append((placement, f"no_space_in_{target_tier.value}"))
            continue

        dist = tier_distance(placement.tier, target_tier)
        if dist > 0:
            plan.promotions.append((placement, target_tier))
        else:
            plan.demotions.append((placement, target_tier))

        # Adjust running free estimate
        stats[target_tier].used_mb += node_mb
        stats[placement.tier].used_mb = max(0, stats[placement.tier].used_mb - node_mb)
        batch_mb += node_mb

    return plan
