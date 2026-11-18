"""
core/gpu_offload.py — Dynamic VRAM offload between Arc B70 (Y=1) and RTX 5060 Ti (Y=0)

Hardware pool:
  Y=1  Arc B70       32 GB GDDR6   608 GB/s   367 AI TOPS  (deep lane — large model weights)
  Y=0  RTX 5060 Ti   16 GB GDDR7   448 GB/s   759 AI TOPS  (fast lane — inference bursts)

Cross-GPU channel: /dev/shm ring at ~7 GB/s sustained
Policy:
  - Idle layers: spill from RTX → Arc (/dev/shm staging)
  - Hot path:    pull from Arc → RTX when RTX head-room < LOW_WATER_MB
  - Sovereignty (heat ≥ 1.0): force Local 70B on Arc; never offload those weights to cloud
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional


# ── Constants ──────────────────────────────────────────────────────────────────

LANE_ARC = 1   # Y=1  Arc B70
LANE_RTX = 0   # Y=0  RTX 5060 Ti

ARC_TOTAL_MB    = 32_768   # 32 GB
RTX_TOTAL_MB    = 16_384   # 16 GB

# Thresholds
RTX_LOW_WATER_MB  = 2_048   # pull from Arc when RTX free < 2 GB
RTX_HIGH_WATER_MB = 4_096   # spill to Arc when RTX free > 4 GB (can reclaim)
ARC_LOW_WATER_MB  = 4_096   # never spill to Arc if Arc free < 4 GB

SHM_OFFLOAD_PATH  = "/dev/shm/arc_offload_ring"
OFFLOAD_STATE_PATH = "/dev/shm/arc_offload_state.json"


class OffloadDir(Enum):
    NONE      = auto()
    RTX_TO_ARC = auto()   # spill: hot → cold storage
    ARC_TO_RTX = auto()   # pull: cold → hot inference


@dataclass
class VRAMStats:
    lane:       int
    total_mb:   int
    used_mb:    int
    free_mb:    int
    timestamp:  float = field(default_factory=time.time)

    @property
    def utilisation(self) -> float:
        return self.used_mb / max(1, self.total_mb)


@dataclass
class OffloadDecision:
    direction:    OffloadDir
    layer_ids:    list[str]        # model layer identifiers to move
    size_mb:      float
    reason:       str
    timestamp:    float = field(default_factory=time.time)


# ── VRAM query ─────────────────────────────────────────────────────────────────

def _nvidia_smi_free_mb(gpu_index: int = 0) -> Optional[int]:
    """Query free VRAM on an NVIDIA GPU via nvidia-smi."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             f"--id={gpu_index}",
             "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode().strip()
        return int(out)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        return None


def _intel_gpu_free_mb() -> Optional[int]:
    """
    Query free VRAM on Intel Arc via intel_gpu_top / sysfs.
    Falls back to a heuristic if the tool is unavailable.
    """
    # Try sysfs (xe driver, Linux 6.8+)
    sysfs_paths = list(Path("/sys/class/drm").glob("card*/device/xe/*/gt/gt0/mem_busy"))
    if sysfs_paths:
        try:
            val = int(sysfs_paths[0].read_text().strip())
            # val is 0-100 utilisation; derive free from total
            used_mb = int(ARC_TOTAL_MB * val / 100)
            return ARC_TOTAL_MB - used_mb
        except (OSError, ValueError):
            pass

    # Fallback: assume 24 GB free if driver not available yet
    return ARC_TOTAL_MB - 8_000  # conservative placeholder


def query_vram(lane: int) -> VRAMStats:
    """Return current VRAM stats for the given lane."""
    if lane == LANE_RTX:
        free = _nvidia_smi_free_mb(gpu_index=0) or (RTX_TOTAL_MB - 4_000)
        used = RTX_TOTAL_MB - free
        return VRAMStats(lane=LANE_RTX, total_mb=RTX_TOTAL_MB, used_mb=used, free_mb=free)
    else:  # LANE_ARC
        free = _intel_gpu_free_mb() or (ARC_TOTAL_MB - 8_000)
        used = ARC_TOTAL_MB - free
        return VRAMStats(lane=LANE_ARC, total_mb=ARC_TOTAL_MB, used_mb=used, free_mb=free)


# ── Offload policy ─────────────────────────────────────────────────────────────

def decide_offload(
    rtx: VRAMStats,
    arc: VRAMStats,
    pending_layers: list[str],
    layer_size_mb: float = 512.0,
    sovereignty_locked: bool = False,
) -> OffloadDecision:
    """
    Pure policy function — no I/O.

    Rules:
      1. If RTX free < LOW_WATER and Arc has room → pull from Arc (ARC_TO_RTX)
      2. If RTX free > HIGH_WATER and Arc free > LOW_WATER → spill (RTX_TO_ARC)
      3. If sovereignty_locked → never move weights off Arc
      4. No pending layers → NONE
    """
    if not pending_layers:
        return OffloadDecision(
            direction=OffloadDir.NONE,
            layer_ids=[],
            size_mb=0.0,
            reason="no pending layers",
        )

    if sovereignty_locked:
        return OffloadDecision(
            direction=OffloadDir.NONE,
            layer_ids=[],
            size_mb=0.0,
            reason="sovereignty lock — weights pinned to Arc",
        )

    # Pull: RTX needs more headroom
    if rtx.free_mb < RTX_LOW_WATER_MB:
        slots = max(1, int(arc.free_mb / layer_size_mb) - 1)
        batch = pending_layers[:slots]
        return OffloadDecision(
            direction=OffloadDir.ARC_TO_RTX,
            layer_ids=batch,
            size_mb=len(batch) * layer_size_mb,
            reason=f"RTX free {rtx.free_mb} MB < low-water {RTX_LOW_WATER_MB} MB",
        )

    # Spill: RTX has lots of room, Arc has room too
    if rtx.free_mb > RTX_HIGH_WATER_MB and arc.free_mb > ARC_LOW_WATER_MB:
        slots = max(1, int((rtx.free_mb - RTX_HIGH_WATER_MB) / layer_size_mb))
        batch = pending_layers[:slots]
        return OffloadDecision(
            direction=OffloadDir.RTX_TO_ARC,
            layer_ids=batch,
            size_mb=len(batch) * layer_size_mb,
            reason=f"RTX free {rtx.free_mb} MB > high-water {RTX_HIGH_WATER_MB} MB; spilling to Arc",
        )

    return OffloadDecision(
        direction=OffloadDir.NONE,
        layer_ids=[],
        size_mb=0.0,
        reason="VRAM within normal bounds",
    )


# ── SHM staging ───────────────────────────────────────────────────────────────

class SHMStager:
    """
    Stages model layer blobs in /dev/shm for cross-GPU transfer.
    Real implementation would use mmap + zero-copy DMA hints.
    This version writes JSON manifests; production would write raw tensors.
    """

    def __init__(self, path: str = SHM_OFFLOAD_PATH) -> None:
        self._path = Path(path)
        self._path.mkdir(exist_ok=True)

    def stage(self, layer_id: str, blob: bytes) -> Path:
        dest = self._path / f"{layer_id}.bin"
        dest.write_bytes(blob)
        return dest

    def unstage(self, layer_id: str) -> Optional[bytes]:
        src = self._path / f"{layer_id}.bin"
        if src.exists():
            data = src.read_bytes()
            src.unlink()
            return data
        return None

    def manifest(self) -> list[str]:
        return [p.stem for p in self._path.glob("*.bin")]


# ── GPUOffloadManager ─────────────────────────────────────────────────────────

class GPUOffloadManager:
    """
    Coordinates dynamic offload between Arc B70 ↔ RTX 5060 Ti.

    Call tick() on each heartbeat sync cycle to apply the policy.
    """

    def __init__(self, shm_path: str = SHM_OFFLOAD_PATH) -> None:
        self._stager     = SHMStager(shm_path)
        self._arc_layers: list[str] = []   # layers currently on Arc
        self._rtx_layers: list[str] = []   # layers currently on RTX
        self._sovereignty_locked = False

    # ── External setters ──────────────────────────────────────────────────────

    def set_model_layers(self, layers: list[str]) -> None:
        """Register all layer IDs for the loaded model."""
        self._rtx_layers = list(layers)
        self._arc_layers = []

    def set_sovereignty_lock(self, locked: bool) -> None:
        """When True, weights stay on Arc (Local 70B sovereignty path)."""
        self._sovereignty_locked = locked

    # ── Main tick ─────────────────────────────────────────────────────────────

    def tick(self) -> OffloadDecision:
        """Query VRAM, decide, execute, publish state."""
        rtx_stats = query_vram(LANE_RTX)
        arc_stats  = query_vram(LANE_ARC)

        # Layers that could move
        pending = self._arc_layers if rtx_stats.free_mb < RTX_LOW_WATER_MB \
                  else self._rtx_layers

        decision = decide_offload(
            rtx=rtx_stats,
            arc=arc_stats,
            pending_layers=pending,
            sovereignty_locked=self._sovereignty_locked,
        )

        self._apply(decision)
        self._publish_state(rtx_stats, arc_stats, decision)
        return decision

    # ── Apply ─────────────────────────────────────────────────────────────────

    def _apply(self, decision: OffloadDecision) -> None:
        if decision.direction == OffloadDir.NONE:
            return

        if decision.direction == OffloadDir.RTX_TO_ARC:
            for lid in decision.layer_ids:
                if lid in self._rtx_layers:
                    self._rtx_layers.remove(lid)
                    self._arc_layers.append(lid)
                    # Production: DMA transfer via /dev/shm ring
                    # self._stager.stage(lid, model_state_dict[lid])

        elif decision.direction == OffloadDir.ARC_TO_RTX:
            for lid in decision.layer_ids:
                if lid in self._arc_layers:
                    self._arc_layers.remove(lid)
                    self._rtx_layers.append(lid)
                    # Production: pull blob from /dev/shm ring
                    # blob = self._stager.unstage(lid)
                    # model_state_dict[lid] = blob

    # ── State publish ─────────────────────────────────────────────────────────

    def _publish_state(
        self,
        rtx: VRAMStats,
        arc: VRAMStats,
        decision: OffloadDecision,
    ) -> None:
        state = {
            "timestamp":           time.time(),
            "rtx": {
                "total_mb": rtx.total_mb,
                "used_mb":  rtx.used_mb,
                "free_mb":  rtx.free_mb,
                "util":     round(rtx.utilisation, 3),
            },
            "arc": {
                "total_mb": arc.total_mb,
                "used_mb":  arc.used_mb,
                "free_mb":  arc.free_mb,
                "util":     round(arc.utilisation, 3),
            },
            "layers_on_rtx":       len(self._rtx_layers),
            "layers_on_arc":       len(self._arc_layers),
            "sovereignty_locked":  self._sovereignty_locked,
            "last_decision": {
                "direction": decision.direction.name,
                "layers":    decision.layer_ids,
                "size_mb":   decision.size_mb,
                "reason":    decision.reason,
            },
        }
        try:
            Path(OFFLOAD_STATE_PATH).write_text(json.dumps(state, indent=2))
        except OSError:
            pass

    @property
    def rtx_layer_count(self) -> int:
        return len(self._rtx_layers)

    @property
    def arc_layer_count(self) -> int:
        return len(self._arc_layers)


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mgr = GPUOffloadManager()
    # Pretend we have 40 model layers
    mgr.set_model_layers([f"layer_{i}" for i in range(40)])

    for _ in range(3):
        decision = mgr.tick()
        print(
            f"[gpu_offload] dir={decision.direction.name}"
            f"  rtx_layers={mgr.rtx_layer_count}"
            f"  arc_layers={mgr.arc_layer_count}"
            f"  reason={decision.reason}"
        )
        time.sleep(0.2)
