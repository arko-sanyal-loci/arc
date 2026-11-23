from __future__ import annotations

from pathlib import Path

from core.loci_memory import LociMemoryBridge


def test_recall_survives_bridge_restart_and_deduplicates_turns(tmp_path: Path) -> None:
    db_path = tmp_path / "context2.db"
    glassbox_path = tmp_path / "glassbox.jsonl"

    first = LociMemoryBridge(str(db_path), str(glassbox_path))
    first.ingest_messages(
        "arc-demo",
        [{"role": "user", "content": "The project workspace is X-Cargo."}],
    )
    first.ingest_messages(
        "arc-demo",
        [{"role": "user", "content": "The project workspace is X-Cargo."}],
    )
    first.close()

    second = LociMemoryBridge(str(db_path), str(glassbox_path))
    recalled = second.recall("arc-demo", "What is the project workspace?", limit=5)

    assert any("X-Cargo" in item["canonical_text"] for item in recalled["selected_items"])
    row_count = second.storage._get_connection().execute(
        "SELECT COUNT(*) FROM memory_item WHERE namespace = 'session'"
    ).fetchone()[0]
    assert row_count == 1
    assert second.status()["glassbox"]["valid"] is True
    second.close()


def test_memory_redacts_credentials_before_persistence(tmp_path: Path) -> None:
    bridge = LociMemoryBridge(
        str(tmp_path / "context2.db"),
        str(tmp_path / "glassbox.jsonl"),
    )
    bridge.ingest_messages(
        "secret-check",
        [{"role": "user", "content": "Use ghp_" + "A" * 36 + " only locally."}],
    )

    row = bridge.storage._get_connection().execute(
        "SELECT canonical_text FROM memory_item"
    ).fetchone()
    assert "ghp_" not in row[0]
    assert "<redacted:github:" in row[0]
    bridge.close()
