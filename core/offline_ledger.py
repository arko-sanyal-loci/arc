"""Append-only, offline activity and evidence ledger for Arc's command center.

No network calls are made here. Liability entries are claims with provenance,
not legal or accounting determinations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


DB_PATH = Path(os.environ.get(
    "ARC_OFFLINE_CACHE_DB", str(Path(__file__).resolve().parents[1] / "data" / "arc_offline_cache.sqlite")
))
LIABILITY_STATES = {"proposed", "documented", "disputed", "settled", "closed"}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(DB_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(DB_PATH, 0o600)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(DB_PATH) + suffix)
        if sidecar.exists():
            os.chmod(sidecar, 0o600)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS work_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            work_ref TEXT NOT NULL,
            occurred_at REAL NOT NULL,
            actor TEXT NOT NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            evidence_uri TEXT NOT NULL DEFAULT '',
            evidence_sha256 TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS work_events_time_idx ON work_events(occurred_at DESC);
        CREATE TABLE IF NOT EXISTS liability_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            liability_ref TEXT NOT NULL,
            occurred_at REAL NOT NULL,
            creditor TEXT NOT NULL DEFAULT '',
            debtor TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL,
            amount_minor INTEGER,
            currency TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            evidence_uri TEXT NOT NULL DEFAULT '',
            evidence_sha256 TEXT NOT NULL DEFAULT '',
            source_note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS liability_events_ref_idx ON liability_events(liability_ref, seq DESC);
        CREATE TRIGGER IF NOT EXISTS work_events_no_update BEFORE UPDATE ON work_events
            BEGIN SELECT RAISE(ABORT, 'work ledger is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS work_events_no_delete BEFORE DELETE ON work_events
            BEGIN SELECT RAISE(ABORT, 'work ledger is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS liability_events_no_update BEFORE UPDATE ON liability_events
            BEGIN SELECT RAISE(ABORT, 'liability ledger is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS liability_events_no_delete BEFORE DELETE ON liability_events
            BEGIN SELECT RAISE(ABORT, 'liability ledger is append-only'); END;
    """)
    return conn


def record_work(*, actor: str, event_type: str, title: str, detail: str = "",
                project: str = "Arc", evidence_uri: str = "",
                metadata: dict[str, Any] | None = None, work_ref: str | None = None) -> dict[str, Any]:
    now = time.time()
    ref = work_ref or str(uuid.uuid4())
    metadata = metadata or {}
    # An evidence URI is a locator, not the evidence content; never mislabel its hash as a file digest.
    evidence_hash = ""
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO work_events(event_id,work_ref,occurred_at,actor,event_type,title,detail,project,evidence_uri,evidence_sha256,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), ref, now, actor, event_type, title, detail, project,
             evidence_uri, evidence_hash, json.dumps(metadata, sort_keys=True)),
        )
        return {"seq": cursor.lastrowid, "work_ref": ref, "occurred_at": now}


def record_liability(*, liability_ref: str, creditor: str, debtor: str,
                     description: str, state: str, amount_minor: int | None = None,
                     currency: str = "", evidence_uri: str = "",
                     evidence_sha256: str = "", source_note: str = "") -> dict[str, Any]:
    if state not in LIABILITY_STATES:
        raise ValueError(f"state must be one of {sorted(LIABILITY_STATES)}")
    if not liability_ref.strip() or not description.strip():
        raise ValueError("liability_ref and description are required")
    if any(len(value) > maxlen for value, maxlen in ((liability_ref, 120), (creditor, 200), (debtor, 200), (description, 2000), (source_note, 4000), (evidence_uri, 2000))):
        raise ValueError("liability field exceeds its maximum length")
    if amount_minor is not None and not currency:
        raise ValueError("currency is required when an amount is recorded")
    if amount_minor is not None and amount_minor < 0:
        raise ValueError("amount_minor cannot be negative")
    if currency and (len(currency) != 3 or not currency.isalpha()):
        raise ValueError("currency must be a 3-letter code")
    if evidence_sha256 and (len(evidence_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in evidence_sha256)):
        raise ValueError("evidence_sha256 must be a SHA-256 hex digest")
    now = time.time()
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO liability_events(event_id,liability_ref,occurred_at,creditor,debtor,description,amount_minor,currency,state,evidence_uri,evidence_sha256,source_note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), liability_ref, now, creditor, debtor, description,
             amount_minor, currency.upper(), state, evidence_uri, evidence_sha256.lower(), source_note),
        )
        return {"seq": cursor.lastrowid, "liability_ref": liability_ref, "occurred_at": now}


def recent_work(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM work_events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        result.append(item)
    return result


def current_liabilities(limit: int = 100) -> dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute("""
            SELECT e.* FROM liability_events e
            JOIN (SELECT liability_ref, MAX(seq) AS seq FROM liability_events GROUP BY liability_ref) latest
            ON latest.seq=e.seq ORDER BY e.seq DESC LIMIT ?
        """, (limit,)).fetchall()
        count = conn.execute("SELECT COUNT(*) FROM liability_events").fetchone()[0]
    return {"items": [dict(row) for row in rows], "event_count": count,
            "note": "Evidence-linked records only; no amount or legal status is inferred."}


def cache_status() -> dict[str, Any]:
    with _connect() as conn:
        work_count = conn.execute("SELECT COUNT(*) FROM work_events").fetchone()[0]
        liability_count = conn.execute("SELECT COUNT(*) FROM liability_events").fetchone()[0]
    return {"path": str(DB_PATH), "available": DB_PATH.exists(), "offline": True,
            "work_events": work_count, "liability_events": liability_count,
            "updated_at": time.time()}
