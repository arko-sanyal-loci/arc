"""
dual_write.py — Atomic dual-write to Google Drive + Locus Compendium.

Every committed package writes to BOTH places atomically:
  - Drive: Markdown doc per thread in /Glassbox/<project>/ (human-readable)
  - Compendium: SLP 20-byte register + full JSON node + hash chain entry (agent-readable)

If one write succeeds and the other fails, both are rolled back.
A single commit spans both writes — atomic or it didn't happen.

The hash chain binds both writes to the same provenance:
  hash_entry = SHA256(timestamp ‖ author_id ‖ role ‖ content)

Usage:
    result = dual_write(package, drive_client, compendium_db)
    if not result.ok:
        # both rolled back, inspect result.error
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from .slp_encoding import SLPRegister, sha_lo32

logger = logging.getLogger(__name__)


# ── Package definition ────────────────────────────────────────────────────────

@dataclass
class Package:
    """A committed ingest record ready for dual-write."""
    commit_id: str          # unique commit identifier (UUID or hash string)
    category: str           # human-readable category
    project: str            # Glassbox project name (used as Drive folder)
    source_id: str          # original source (e.g. Gmail thread ID)
    author_id: str          # sender/author identifier
    role: str               # "sender" | "receiver" | "system"
    content: str            # normalised text content
    slp: SLPRegister        # pre-packed SLP register from glassbox_ingest
    metadata: dict = field(default_factory=dict)
    timestamp_s: int = field(default_factory=lambda: int(time.time()))

    def provenance_hash(self) -> str:
        """
        SHA256(timestamp ‖ author_id ‖ role ‖ content)
        Binds both Drive and Compendium writes to the same hash chain entry.
        """
        raw = (
            struct.pack("<I", self.timestamp_s)
            + self.author_id.encode()
            + self.role.encode()
            + self.content.encode()
        )
        return hashlib.sha256(raw).hexdigest()


# ── Backend protocols ─────────────────────────────────────────────────────────

class DriveClient(Protocol):
    """Thin protocol — wrap your actual Drive connector here."""
    def write_doc(self, folder: str, filename: str, content_md: str) -> str:
        """Write a Markdown doc; return the Drive file ID."""
        ...

    def delete_doc(self, file_id: str) -> None:
        """Delete a Drive doc (used for rollback)."""
        ...


class CompendiumDB(Protocol):
    """Thin protocol — wraps SQLite WAL + ledger.jsonl."""
    def insert_node(self, node: dict) -> None:
        """Insert a node record (raises on failure)."""
        ...

    def insert_ledger(self, entry: dict) -> None:
        """Append a hash-chain entry to ledger.jsonl."""
        ...

    def delete_node(self, commit_id: str) -> None:
        """Remove a node (used for rollback)."""
        ...

    def delete_ledger(self, commit_id: str) -> None:
        """Remove a ledger entry (used for rollback)."""
        ...


# ── Write result ──────────────────────────────────────────────────────────────

@dataclass
class DualWriteResult:
    ok: bool
    commit_id: str
    drive_file_id: Optional[str] = None
    provenance_hash: Optional[str] = None
    error: Optional[str] = None
    rolled_back: bool = False


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_drive_doc(pkg: Package, prov_hash: str) -> str:
    """Format a Package as a Markdown doc for Google Drive."""
    meta_lines = "\n".join(f"- **{k}:** {v}" for k, v in pkg.metadata.items())
    return f"""# {pkg.category} — {pkg.source_id}

**Project:** {pkg.project}
**Commit:** `{pkg.commit_id}`
**Provenance:** `{prov_hash}`
**Author:** {pkg.author_id} ({pkg.role})
**Timestamp:** {pkg.timestamp_s}
**Heat:** {pkg.slp.heat:.4f}
**Flags:** {pkg.slp.class_flags.name}

## Content

{pkg.content}

## Metadata

{meta_lines or "_(none)_"}
"""


def _format_compendium_node(pkg: Package, prov_hash: str) -> dict:
    """Format a Package as a Compendium node (JSON)."""
    return {
        "commit_id": pkg.commit_id,
        "category": pkg.category,
        "category_id": pkg.slp.category_id,
        "project": pkg.project,
        "source_id": pkg.source_id,
        "author_id": pkg.author_id,
        "role": pkg.role,
        "content": pkg.content,
        "timestamp_s": pkg.timestamp_s,
        "heat": pkg.slp.heat,
        "heat_u16": int((pkg.slp.heat - 0.05) / 0.9499 * 65535),
        "class_flags": int(pkg.slp.class_flags),
        "routing_hint": int(pkg.slp.routing_hint),
        "content_sha_lo32": pkg.slp.content_sha_lo32,
        "slp_bytes": pkg.slp.pack().hex(),
        "provenance_hash": prov_hash,
        "metadata": pkg.metadata,
    }


def _format_ledger_entry(pkg: Package, prov_hash: str) -> dict:
    """Format a hash-chain ledger entry."""
    return {
        "commit_id": pkg.commit_id,
        "provenance_hash": prov_hash,
        "timestamp_s": pkg.timestamp_s,
        "author_id": pkg.author_id,
        "role": pkg.role,
        "category": pkg.category,
        "source_id": pkg.source_id,
        "content_sha": hashlib.sha256(pkg.content.encode()).hexdigest(),
    }


# ── Core dual-write ───────────────────────────────────────────────────────────

def dual_write(
    pkg: Package,
    drive: DriveClient,
    compendium: CompendiumDB,
) -> DualWriteResult:
    """
    Atomically write pkg to both Drive and Compendium.
    Rollback both on any failure.

    Returns DualWriteResult — check .ok before proceeding.
    """
    prov_hash = pkg.provenance_hash()
    drive_file_id: Optional[str] = None
    node_written = False
    ledger_written = False

    try:
        # ── Phase 1: Drive write ──────────────────────────────────────────
        folder = f"/Glassbox/{pkg.project}"
        filename = f"{pkg.commit_id}_{pkg.source_id}.md"
        doc_md = _format_drive_doc(pkg, prov_hash)

        drive_file_id = drive.write_doc(folder, filename, doc_md)
        logger.debug("dual_write Drive OK file_id=%s commit=%s", drive_file_id, pkg.commit_id)

        # ── Phase 2: Compendium node write ────────────────────────────────
        node = _format_compendium_node(pkg, prov_hash)
        compendium.insert_node(node)
        node_written = True
        logger.debug("dual_write Compendium node OK commit=%s", pkg.commit_id)

        # ── Phase 3: Ledger entry ─────────────────────────────────────────
        ledger_entry = _format_ledger_entry(pkg, prov_hash)
        compendium.insert_ledger(ledger_entry)
        ledger_written = True
        logger.debug("dual_write Ledger OK commit=%s prov=%s", pkg.commit_id, prov_hash)

        return DualWriteResult(
            ok=True,
            commit_id=pkg.commit_id,
            drive_file_id=drive_file_id,
            provenance_hash=prov_hash,
        )

    except Exception as exc:
        logger.error("dual_write FAILED commit=%s error=%s — rolling back", pkg.commit_id, exc)

        # ── Rollback: undo in reverse order ──────────────────────────────
        _rollback(
            drive=drive,
            compendium=compendium,
            commit_id=pkg.commit_id,
            drive_file_id=drive_file_id,
            node_written=node_written,
            ledger_written=ledger_written,
        )

        return DualWriteResult(
            ok=False,
            commit_id=pkg.commit_id,
            error=str(exc),
            rolled_back=True,
        )


def _rollback(
    drive: DriveClient,
    compendium: CompendiumDB,
    commit_id: str,
    drive_file_id: Optional[str],
    node_written: bool,
    ledger_written: bool,
) -> None:
    """
    Best-effort rollback. Logs but does not re-raise on rollback failure —
    the caller already has the original error.
    """
    if ledger_written:
        try:
            compendium.delete_ledger(commit_id)
            logger.info("rollback: ledger entry removed commit=%s", commit_id)
        except Exception as e:
            logger.error("rollback: FAILED to remove ledger entry commit=%s: %s", commit_id, e)

    if node_written:
        try:
            compendium.delete_node(commit_id)
            logger.info("rollback: compendium node removed commit=%s", commit_id)
        except Exception as e:
            logger.error("rollback: FAILED to remove compendium node commit=%s: %s", commit_id, e)

    if drive_file_id is not None:
        try:
            drive.delete_doc(drive_file_id)
            logger.info("rollback: Drive doc removed file_id=%s", drive_file_id)
        except Exception as e:
            logger.error("rollback: FAILED to remove Drive doc file_id=%s: %s", drive_file_id, e)


# ── SQLite WAL implementation (concrete CompendiumDB) ────────────────────────

class SQLiteCompendium:
    """
    Concrete CompendiumDB backed by SQLite WAL + ledger.jsonl.
    Thread-safe via WAL mode; suitable for single-process multi-thread use.
    """

    def __init__(self, db_path: str, ledger_path: str):
        self.db_path = db_path
        self.ledger_path = Path(ledger_path)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._bootstrap()

    def _bootstrap(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                commit_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                project TEXT NOT NULL,
                source_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp_s INTEGER NOT NULL,
                heat REAL NOT NULL,
                class_flags INTEGER NOT NULL,
                routing_hint INTEGER NOT NULL,
                content_sha_lo32 INTEGER NOT NULL,
                slp_bytes TEXT NOT NULL,
                provenance_hash TEXT NOT NULL,
                metadata TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_nodes_category ON nodes(category_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project);
            CREATE INDEX IF NOT EXISTS idx_nodes_heat ON nodes(heat);
        """)
        self._conn.commit()
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def insert_node(self, node: dict) -> None:
        self._conn.execute(
            """INSERT INTO nodes VALUES (
               :commit_id,:category,:category_id,:project,:source_id,
               :author_id,:role,:content,:timestamp_s,:heat,
               :class_flags,:routing_hint,:content_sha_lo32,:slp_bytes,
               :provenance_hash,:metadata
            )""",
            {**node, "metadata": json.dumps(node.get("metadata", {}))},
        )
        self._conn.commit()

    def insert_ledger(self, entry: dict) -> None:
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def delete_node(self, commit_id: str) -> None:
        self._conn.execute("DELETE FROM nodes WHERE commit_id = ?", (commit_id,))
        self._conn.commit()

    def delete_ledger(self, commit_id: str) -> None:
        """Remove the last ledger entry matching commit_id (append-only, so we rewrite)."""
        if not self.ledger_path.exists():
            return
        lines = self.ledger_path.read_text().splitlines()
        kept = [l for l in lines if json.loads(l).get("commit_id") != commit_id]
        self.ledger_path.write_text("\n".join(kept) + ("\n" if kept else ""))

    def close(self) -> None:
        self._conn.close()
