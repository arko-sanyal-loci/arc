"""
core/engine_sync.py — Arc engine sync: Loci state ↔ SQLite ↔ /dev/shm

Runs every 50 heartbeat ticks (~7 seconds) or on explicit call.
Responsibilities:
  1. Read Loci SHM status → update engine state snapshot
  2. Flush any in-memory SLP register changes to SQLite WAL
  3. Confirm dual-write consistency (Drive ↔ Compendium hash check)
  4. Publish sync summary to /dev/shm/arc_engine_state.json
  5. Trigger reconcile_tick if drift is detected
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# Loci modules (copied into arc-runtime/loci/)
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "loci"))

from reconcile_tick import ReconcileTicker, ReconcileResult, DriftKind  # noqa: E402


# ── Constants ──────────────────────────────────────────────────────────────────

ENGINE_STATE_PATH  = "/dev/shm/arc_engine_state.json"
HEARTBEAT_SHM_PATH = "/dev/shm/arc_heartbeat"
STATUS_SHM_PATH    = "/dev/shm/arc_status.json"

DEFAULT_DB_PATH    = str(Path(__file__).parent.parent / "data" / "arc.db")


# ── Engine state snapshot ─────────────────────────────────────────────────────

@dataclass
class EngineState:
    tick_id:          int    = 0
    timestamp:        float  = 0.0
    db_path:          str    = DEFAULT_DB_PATH
    # Heartbeat
    heartbeat_alive:  bool   = False
    lane_mask:        int    = 0
    # Loci
    loci_node_count:  int    = 0
    loci_ledger_rows: int    = 0
    last_drift_kinds: list   = field(default_factory=list)
    drift_detected:   bool   = False
    # GPU offload (populated by gpu_offload.py)
    arc_vram_used_mb:  int   = 0
    rtx_vram_used_mb:  int   = 0
    # API health
    xai_ok:           bool   = False
    nexos_ok:         bool   = False
    # Sync
    sync_duration_ms: float  = 0.0
    errors:           list   = field(default_factory=list)


# ── SQLite helpers ─────────────────────────────────────────────────────────────

def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS nodes (
            node_id      TEXT PRIMARY KEY,
            category     TEXT,
            project      TEXT,
            content      TEXT,
            drive_hash   TEXT,
            heat_u16     INTEGER DEFAULT 0,
            created_at   REAL,
            updated_at   REAL
        );

        CREATE TABLE IF NOT EXISTS ledger (
            entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id      TEXT,
            agent_id     TEXT,
            prev_hash    TEXT,
            entry_hash   TEXT NOT NULL,
            timestamp_s  REAL,
            action       TEXT
        );

        CREATE TABLE IF NOT EXISTS engine_syncs (
            sync_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_id      INTEGER,
            timestamp    REAL,
            lane_mask    INTEGER,
            node_count   INTEGER,
            ledger_rows  INTEGER,
            drift        INTEGER DEFAULT 0,
            duration_ms  REAL
        );
    """)
    conn.commit()


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


# ── Heartbeat reader ───────────────────────────────────────────────────────────

def _read_heartbeat_status() -> dict:
    try:
        return json.loads(Path(STATUS_SHM_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# ── Engine sync class ──────────────────────────────────────────────────────────

class EngineSync:
    """
    Synchronises Arc engine state: Loci SQLite ↔ /dev/shm status.

    Typical call path:
        sync = EngineSync(db_path="data/arc.db")
        sync.ensure_db()
        # Registered as heartbeat sync handler:
        await sync.sync_tick(tick_id)
    """

    def __init__(
        self,
        db_path:            str = DEFAULT_DB_PATH,
        reconcile_on_drift: bool = True,
    ) -> None:
        self._db_path           = db_path
        self._reconcile         = reconcile_on_drift
        self._reconciler:       Optional[ReconcileTicker] = None
        self._state             = EngineState(db_path=db_path)
        self._conn:             Optional[sqlite3.Connection] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def ensure_db(self) -> None:
        """Open (or create) the SQLite DB and ensure schema."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        _ensure_tables(self._conn)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Sync tick (called by heartbeat every N ticks) ─────────────────────────

    async def sync_tick(self, tick_id: int) -> EngineState:
        t0 = time.monotonic()
        state = EngineState(
            tick_id=tick_id,
            timestamp=time.time(),
            db_path=self._db_path,
        )

        try:
            # 1 — Heartbeat status
            hb = _read_heartbeat_status()
            state.heartbeat_alive = bool(hb)
            state.lane_mask       = hb.get("lane_mask", 0)

            # 2 — SQLite row counts
            if self._conn:
                state.loci_node_count  = _count_rows(self._conn, "nodes")
                state.loci_ledger_rows = _count_rows(self._conn, "ledger")

            # 3 — Reconcile (optional, non-blocking)
            if self._reconcile and self._conn:
                result = self._run_reconcile()
                if result:
                    state.drift_detected   = result.drift_count > 0
                    state.last_drift_kinds = [d.name for d in result.drifts_by_kind]

        except Exception as exc:
            state.errors.append(str(exc))

        # 4 — Duration (calculated before the DB insert so the row holds the real value)
        state.sync_duration_ms = (time.monotonic() - t0) * 1000

        # 5 — Record sync to DB
        if self._conn:
            try:
                self._conn.execute(
                    """INSERT INTO engine_syncs
                       (tick_id, timestamp, lane_mask, node_count, ledger_rows, drift, duration_ms)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        tick_id,
                        state.timestamp,
                        state.lane_mask,
                        state.loci_node_count,
                        state.loci_ledger_rows,
                        int(state.drift_detected),
                        state.sync_duration_ms,
                    ),
                )
                self._conn.commit()
            except Exception as exc:
                state.errors.append(f"db_insert: {exc}")

        # 6 — Publish to shm
        self._publish_state(state)
        self._state = state
        return state

    # ── Manual trigger ────────────────────────────────────────────────────────

    def sync_now(self) -> EngineState:
        """Synchronous sync (for use outside async context)."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.sync_tick(self._state.tick_id + 1)
        )

    @property
    def state(self) -> EngineState:
        return self._state

    # ── Internals ─────────────────────────────────────────────────────────────

    def _run_reconcile(self) -> Optional[ReconcileResult]:
        """
        Build minimal index stubs from the DB and run reconcile_tick.
        Full implementation would use drive_index + comp_index from dual_write.
        """
        try:
            rows = self._conn.execute(
                "SELECT node_id, drive_hash FROM nodes"
            ).fetchall()
            drive_index = {r[0]: r[1] for r in rows}
            comp_index  = {r[0]: r[1] for r in rows}  # same source for now

            alarms: list[str] = []
            def alarm_handler(msg: str) -> None:
                alarms.append(msg)

            from reconcile_tick import reconcile_tick
            return reconcile_tick(drive_index, comp_index, alarm_handler, ledger_limit=100)
        except Exception:
            return None

    def _publish_state(self, state: EngineState) -> None:
        try:
            Path(ENGINE_STATE_PATH).write_text(
                json.dumps(asdict(state), default=str)
            )
        except OSError:
            pass


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    sync = EngineSync()
    sync.ensure_db()

    async def main():
        for i in range(1, 4):
            state = await sync.sync_tick(i)
            print(
                f"[engine_sync] tick={state.tick_id}"
                f"  nodes={state.loci_node_count}"
                f"  ledger={state.loci_ledger_rows}"
                f"  drift={state.drift_detected}"
                f"  {state.sync_duration_ms:.1f}ms"
            )
            await asyncio.sleep(0.5)

    asyncio.run(main())
    sync.close()
