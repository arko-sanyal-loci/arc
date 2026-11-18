"""
core/heartbeat.py — Arc runtime heartbeat: 140ms tick, shm cross-GPU sync

Heartbeat responsibilities:
  1. Emit 140ms tick to all registered Y-lane workers (Y=0 RTX, Y=1 Arc, Y=2 CPU)
  2. Write tick metadata to /dev/shm ring for cross-GPU visibility
  3. Monitor worker silence (missed ticks → alert → escalate to failover)
  4. Trigger engine_sync on every Nth tick (default N=50)
  5. Report heartbeat status to the Arc dashboard endpoint

Design constants (from Glassbox architecture):
  HEARTBEAT_TICK_MS = 140   (cross-GPU coordination interval)
  MISSED_TICKS_WARN = 3     (soft warning)
  MISSED_TICKS_FAIL = 10    (hard failover trigger)
"""

from __future__ import annotations

import asyncio
import json
import mmap
import os
import struct
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Coroutine


# ── Constants ─────────────────────────────────────────────────────────────────

HEARTBEAT_TICK_MS:    int = 140
HEARTBEAT_TICK_S:     float = HEARTBEAT_TICK_MS / 1000.0  # 0.140

MISSED_TICKS_WARN:    int = 3
MISSED_TICKS_FAIL:    int = 10

SHM_PATH:             str = "/dev/shm/arc_heartbeat"
SHM_RING_SIZE:        int = 4096   # bytes, holds ~200 tick records
TICK_RECORD_FMT:      str = "<QHH"  # timestamp_us (8), tick_id (2), lane_mask (2)
TICK_RECORD_SIZE:     int = struct.calcsize(TICK_RECORD_FMT)  # 12 bytes

# Lane mask bits
LANE_RTX   = 0x01   # Y=0  RTX 5060 Ti
LANE_ARC   = 0x02   # Y=1  Arc B70
LANE_CPU   = 0x04   # Y=2  CPU worker


class WorkerStatus(Enum):
    ALIVE    = auto()
    WARN     = auto()   # missed MISSED_TICKS_WARN consecutive ticks
    DEAD     = auto()   # missed MISSED_TICKS_FAIL consecutive ticks


@dataclass
class WorkerState:
    worker_id: str
    lane:      int     # LANE_* bitmask
    last_seen_tick: int = 0
    missed_count:   int = 0
    status: WorkerStatus = WorkerStatus.ALIVE


# ── SHM ring writer ───────────────────────────────────────────────────────────

class SHMRing:
    """
    Circular buffer in /dev/shm for cross-GPU tick visibility.
    Writers: heartbeat emitter (this process)
    Readers: Y=0 RTX worker, Y=1 Arc worker, reconcile_tick, dashboard
    """

    def __init__(self, path: str = SHM_PATH, size: int = SHM_RING_SIZE) -> None:
        self._path = path
        self._size = size
        self._fd = None
        self._mm = None
        self._write_pos = 0

    def open(self) -> None:
        """Create or open the shm file, initialise mmap."""
        existed = os.path.exists(self._path)
        self._fd = open(self._path, "r+b" if existed else "w+b")
        if not existed:
            self._fd.write(b"\x00" * self._size)
            self._fd.flush()
        self._mm = mmap.mmap(self._fd.fileno(), self._size)

    def write_tick(self, tick_id: int, lane_mask: int) -> None:
        """Write one tick record at current ring position, then advance."""
        if self._mm is None:
            return
        ts_us = int(time.time() * 1_000_000)
        record = struct.pack(TICK_RECORD_FMT, ts_us, tick_id & 0xFFFF, lane_mask & 0xFFFF)
        # capacity = number of whole records that fit; use modulo to stay aligned
        capacity = self._size // TICK_RECORD_SIZE
        pos = (self._write_pos % capacity) * TICK_RECORD_SIZE
        self._mm[pos : pos + TICK_RECORD_SIZE] = record
        self._write_pos += 1

    def close(self) -> None:
        if self._mm:
            self._mm.close()
        if self._fd:
            self._fd.close()


# ── Heartbeat engine ──────────────────────────────────────────────────────────

class HeartbeatEngine:
    """
    Async heartbeat emitter.

    Usage:
        hb = HeartbeatEngine()
        hb.register_worker("rtx-worker-0", LANE_RTX)
        hb.register_worker("arc-worker-0", LANE_ARC)
        hb.on_tick(my_tick_handler)
        hb.on_worker_dead(my_failover_handler)
        await hb.run()   # runs until cancelled
    """

    def __init__(
        self,
        tick_ms:        int = HEARTBEAT_TICK_MS,
        sync_every_n:   int = 50,
        shm_path:       str = SHM_PATH,
        status_path:    str = "/dev/shm/arc_status.json",
    ) -> None:
        self._tick_interval = tick_ms / 1000.0
        self._sync_every_n  = sync_every_n
        self._shm           = SHMRing(shm_path)
        self._status_path   = status_path

        self._workers: dict[str, WorkerState] = {}
        self._tick_id: int = 0
        self._running: bool = False

        # Callbacks
        self._tick_handlers:        list[Callable[[int, float], Coroutine]] = []
        self._worker_warn_handlers: list[Callable[[WorkerState], Coroutine]] = []
        self._worker_dead_handlers: list[Callable[[WorkerState], Coroutine]] = []
        self._sync_handlers:        list[Callable[[int], Coroutine]] = []

    # ── Registration ──────────────────────────────────────────────────────────

    def register_worker(self, worker_id: str, lane: int) -> None:
        self._workers[worker_id] = WorkerState(
            worker_id=worker_id,
            lane=lane,
            last_seen_tick=self._tick_id,
        )

    def worker_seen(self, worker_id: str) -> None:
        """Call this when a worker ACKs a tick (via shm or queue)."""
        if worker_id in self._workers:
            w = self._workers[worker_id]
            w.last_seen_tick = self._tick_id
            w.missed_count   = 0
            w.status         = WorkerStatus.ALIVE

    # ── Callback registration ─────────────────────────────────────────────────

    def on_tick(self, fn: Callable) -> None:
        self._tick_handlers.append(fn)

    def on_worker_warn(self, fn: Callable) -> None:
        self._worker_warn_handlers.append(fn)

    def on_worker_dead(self, fn: Callable) -> None:
        self._worker_dead_handlers.append(fn)

    def on_sync(self, fn: Callable) -> None:
        self._sync_handlers.append(fn)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._shm.open()
        self._running = True

        try:
            while self._running:
                tick_start = time.monotonic()
                self._tick_id += 1

                # Build lane mask from currently ALIVE workers
                lane_mask = 0
                for w in self._workers.values():
                    if w.status == WorkerStatus.ALIVE:
                        lane_mask |= w.lane

                # Write to shm ring
                self._shm.write_tick(self._tick_id, lane_mask)

                # Update worker miss counters
                await self._check_workers()

                # Fire tick handlers
                now = time.time()
                for fn in self._tick_handlers:
                    try:
                        await fn(self._tick_id, now)
                    except Exception as e:
                        print(f"[heartbeat] tick handler error: {e}")

                # Fire sync handlers every N ticks
                if self._tick_id % self._sync_every_n == 0:
                    for fn in self._sync_handlers:
                        try:
                            await fn(self._tick_id)
                        except Exception as e:
                            print(f"[heartbeat] sync handler error: {e}")

                # Write status JSON for dashboard
                self._write_status(now, lane_mask)

                # Sleep for remainder of tick interval
                elapsed = time.monotonic() - tick_start
                sleep_s = max(0.0, self._tick_interval - elapsed)
                await asyncio.sleep(sleep_s)

        finally:
            self._running = False
            self._shm.close()

    def stop(self) -> None:
        self._running = False

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _check_workers(self) -> None:
        for w in self._workers.values():
            ticks_since_seen = self._tick_id - w.last_seen_tick
            if ticks_since_seen >= MISSED_TICKS_FAIL and w.status != WorkerStatus.DEAD:
                w.missed_count = ticks_since_seen
                w.status = WorkerStatus.DEAD
                for fn in self._worker_dead_handlers:
                    await fn(w)
            elif ticks_since_seen >= MISSED_TICKS_WARN and w.status == WorkerStatus.ALIVE:
                w.missed_count = ticks_since_seen
                w.status = WorkerStatus.WARN
                for fn in self._worker_warn_handlers:
                    await fn(w)

    def _write_status(self, now: float, lane_mask: int) -> None:
        status = {
            "tick_id":    self._tick_id,
            "timestamp":  now,
            "lane_mask":  lane_mask,
            "workers": {
                wid: {
                    "status":  w.status.name,
                    "missed":  w.missed_count,
                    "lane":    w.lane,
                }
                for wid, w in self._workers.items()
            },
        }
        try:
            Path(self._status_path).write_text(json.dumps(status))
        except OSError:
            pass


# ── Standalone runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    hb = HeartbeatEngine()
    hb.register_worker("rtx-worker-0", LANE_RTX)
    hb.register_worker("arc-worker-0", LANE_ARC)
    hb.register_worker("cpu-worker-0", LANE_CPU)

    async def log_tick(tick_id: int, now: float) -> None:
        if tick_id % 50 == 0:
            print(f"[heartbeat] tick={tick_id}  t={now:.3f}")

    async def on_dead(w: WorkerState) -> None:
        print(f"[heartbeat] DEAD: {w.worker_id}  lane={w.lane}", file=sys.stderr)

    hb.on_tick(log_tick)
    hb.on_worker_dead(on_dead)

    asyncio.run(hb.run())
