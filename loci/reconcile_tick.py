"""
reconcile_tick.py — Drive ↔ Compendium drift detection

Runs on a periodic tick (default every N=50 dual-writes or 5 minutes).
Compares node IDs and provenance hashes between:
  - Google Drive folder listing
  - SQLite Compendium nodes table

Raises alarms (without auto-correcting) when drift is detected.
Auto-correction is intentionally NOT here: drift means something broke,
and human or supervisor review should decide the resolution.

Drift categories:
  DRIVE_ONLY    — file in Drive but no matching Compendium node
  COMPENDIUM_ONLY — node in Compendium but no matching Drive file
  HASH_MISMATCH — both have the entry but provenance hashes differ
  LEDGER_GAP    — gap in the ledger chain (prev_hash mismatch)
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol


# ── Drift categories ──────────────────────────────────────────────────────────

class DriftKind(Enum):
    DRIVE_ONLY        = auto()   # in Drive, missing from Compendium
    COMPENDIUM_ONLY   = auto()   # in Compendium, missing from Drive
    HASH_MISMATCH     = auto()   # entry exists in both, hashes differ
    LEDGER_GAP        = auto()   # chain break in ledger.jsonl


@dataclass
class DriftRecord:
    kind:        DriftKind
    commit_id:   str            # the dual-write commit_id
    category:    str
    detail:      str            # human-readable explanation
    drive_hash:  str | None = None
    comp_hash:   str | None = None


# ── Protocols for Drive and Compendium readers ───────────────────────────────

class DriveIndex(Protocol):
    def list_entries(self) -> list[dict]:
        """
        Return list of dicts with at minimum:
            {commit_id: str, category: str, provenance_hash: str}
        """
        ...


class CompendiumIndex(Protocol):
    def list_nodes(self) -> list[dict]:
        """
        Return list of dicts with at minimum:
            {commit_id: str, category: str, provenance_hash: str}
        """
        ...

    def list_ledger(self, limit: int = 1000) -> list[dict]:
        """
        Return most recent N ledger entries ordered by timestamp ascending.
        Each entry: {commit_id, timestamp_s, prev_hash, provenance_hash}
        """
        ...


# ── Reconcile result ──────────────────────────────────────────────────────────

@dataclass
class ReconcileResult:
    tick_id:          str           # unique ID for this reconcile run
    timestamp_s:      int
    total_drive:      int
    total_compendium: int
    drifts:           list[DriftRecord] = field(default_factory=list)
    alarm_raised:     bool = False

    @property
    def clean(self) -> bool:
        return len(self.drifts) == 0

    def summary(self) -> str:
        if self.clean:
            return (
                f"[reconcile {self.tick_id}] CLEAN — "
                f"Drive={self.total_drive} Comp={self.total_compendium}"
            )
        counts = {}
        for d in self.drifts:
            counts[d.kind.name] = counts.get(d.kind.name, 0) + 1
        counts_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return (
            f"[reconcile {self.tick_id}] ALARM — "
            f"{counts_str} | "
            f"Drive={self.total_drive} Comp={self.total_compendium}"
        )


# ── Alarm callbacks ───────────────────────────────────────────────────────────

class AlarmHandler(Protocol):
    def on_drift(self, result: ReconcileResult) -> None:
        """Called when drift is detected. Should page/log/notify."""
        ...


class LoggingAlarmHandler:
    """Default no-op handler that just prints to stdout."""
    def on_drift(self, result: ReconcileResult) -> None:
        print(result.summary())
        for d in result.drifts:
            print(f"  [{d.kind.name}] commit_id={d.commit_id} cat={d.category} — {d.detail}")


# ── Ledger chain validation ───────────────────────────────────────────────────

def _validate_ledger_chain(entries: list[dict]) -> list[DriftRecord]:
    """
    Walk the ledger and flag any broken prev_hash links.
    Returns list of DriftRecord with kind=LEDGER_GAP.
    """
    drifts: list[DriftRecord] = []
    if not entries:
        return drifts

    prev = None
    for entry in entries:
        if prev is not None:
            expected_prev = prev.get("provenance_hash", "")
            actual_prev   = entry.get("prev_hash", "")
            if actual_prev != expected_prev:
                drifts.append(DriftRecord(
                    kind=DriftKind.LEDGER_GAP,
                    commit_id=entry.get("commit_id", "?"),
                    category=entry.get("category", "?"),
                    detail=(
                        f"prev_hash mismatch: "
                        f"expected={expected_prev[:16]}… "
                        f"got={actual_prev[:16]}…"
                    ),
                ))
        prev = entry

    return drifts


# ── Main reconcile function ───────────────────────────────────────────────────

def reconcile_tick(
    drive_index:   DriveIndex,
    comp_index:    CompendiumIndex,
    alarm_handler: AlarmHandler | None = None,
    ledger_limit:  int = 500,
) -> ReconcileResult:
    """
    Run one reconcile tick.

    1. Fetch Drive and Compendium entry lists
    2. Build lookup dicts keyed by commit_id
    3. Find DRIVE_ONLY, COMPENDIUM_ONLY, HASH_MISMATCH
    4. Validate ledger chain for LEDGER_GAP
    5. Raise alarm if any drift found

    Args:
        drive_index:   DriveIndex implementation
        comp_index:    CompendiumIndex implementation
        alarm_handler: AlarmHandler; defaults to LoggingAlarmHandler
        ledger_limit:  How many recent ledger entries to chain-check

    Returns:
        ReconcileResult
    """
    if alarm_handler is None:
        alarm_handler = LoggingAlarmHandler()

    tick_id = hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:12]
    now_s = int(time.time())

    # 1. Fetch entries
    drive_entries = drive_index.list_entries()
    comp_entries  = comp_index.list_nodes()

    # 2. Build lookup dicts
    drive_map: dict[str, dict] = {e["commit_id"]: e for e in drive_entries}
    comp_map:  dict[str, dict] = {e["commit_id"]: e for e in comp_entries}

    drifts: list[DriftRecord] = []

    # 3a. DRIVE_ONLY
    for cid, de in drive_map.items():
        if cid not in comp_map:
            drifts.append(DriftRecord(
                kind=DriftKind.DRIVE_ONLY,
                commit_id=cid,
                category=de.get("category", "?"),
                detail="Present in Drive but missing from Compendium.",
                drive_hash=de.get("provenance_hash"),
            ))

    # 3b. COMPENDIUM_ONLY
    for cid, ce in comp_map.items():
        if cid not in drive_map:
            drifts.append(DriftRecord(
                kind=DriftKind.COMPENDIUM_ONLY,
                commit_id=cid,
                category=ce.get("category", "?"),
                detail="Present in Compendium but missing from Drive.",
                comp_hash=ce.get("provenance_hash"),
            ))

    # 3c. HASH_MISMATCH
    for cid in set(drive_map) & set(comp_map):
        dh = drive_map[cid].get("provenance_hash", "")
        ch = comp_map[cid].get("provenance_hash", "")
        if dh and ch and dh != ch:
            drifts.append(DriftRecord(
                kind=DriftKind.HASH_MISMATCH,
                commit_id=cid,
                category=drive_map[cid].get("category", "?"),
                detail=(
                    f"Hash mismatch: "
                    f"Drive={dh[:16]}… "
                    f"Comp={ch[:16]}…"
                ),
                drive_hash=dh,
                comp_hash=ch,
            ))

    # 4. Ledger chain validation
    ledger_entries = comp_index.list_ledger(limit=ledger_limit)
    drifts.extend(_validate_ledger_chain(ledger_entries))

    result = ReconcileResult(
        tick_id=tick_id,
        timestamp_s=now_s,
        total_drive=len(drive_map),
        total_compendium=len(comp_map),
        drifts=drifts,
        alarm_raised=bool(drifts),
    )

    if drifts:
        alarm_handler.on_drift(result)

    return result


# ── Scheduled ticker ──────────────────────────────────────────────────────────

class ReconcileTicker:
    """
    Wraps reconcile_tick with a write-count + wall-clock trigger.

    Usage:
        ticker = ReconcileTicker(drive, comp, every_n_writes=50, every_seconds=300)
        # after each dual_write:
        ticker.on_write()
    """

    def __init__(
        self,
        drive_index:    DriveIndex,
        comp_index:     CompendiumIndex,
        alarm_handler:  AlarmHandler | None = None,
        every_n_writes: int = 50,
        every_seconds:  int = 300,
    ) -> None:
        self._drive = drive_index
        self._comp  = comp_index
        self._alarm = alarm_handler
        self._every_n = every_n_writes
        self._every_s = every_seconds
        self._write_count = 0
        self._last_tick_s = time.time()

    def on_write(self) -> ReconcileResult | None:
        """
        Call after every dual_write.
        Triggers a reconcile if either threshold is crossed.
        Returns ReconcileResult if a tick ran, else None.
        """
        self._write_count += 1
        now = time.time()
        elapsed = now - self._last_tick_s

        if self._write_count >= self._every_n or elapsed >= self._every_s:
            self._write_count = 0
            self._last_tick_s = now
            return reconcile_tick(self._drive, self._comp, self._alarm)

        return None
