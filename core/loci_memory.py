"""Arc adapter for the canonical LOCI memory engine.

Arc keeps its engine-sync database for compatibility, but conversation memory
must use the canonical LOCI StorageEngine so recall, heat, and continuity are
shared with the rest of the workstation.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any


_ARC_ROOT = Path(__file__).resolve().parents[1]
_LOCI_ROOT = Path(
    os.environ.get(
        "LOCI_ENGINE_ROOT",
        str(_ARC_ROOT.parent / "loci-memory-engine"),
    )
).expanduser()
if str(_LOCI_ROOT) not in sys.path:
    sys.path.insert(0, str(_LOCI_ROOT))

from loci_engine.glassbox import (  # noqa: E402
    glassbox_learn,
    glassbox_verify_chain,
)
from loci_engine.session_continuity import (  # noqa: E402
    end_session,
    start_session,
)
from loci_engine.session_ingester import SessionIngester  # noqa: E402
from loci_engine.storage import StorageEngine  # noqa: E402
from core.platform_paths import default_glassbox_path, default_loci_db_path  # noqa: E402


_SAFE_SESSION_ID = re.compile(r"[^A-Za-z0-9_.-]+")


class LociMemoryBridge:
    """Persist and recall Arc turns through canonical LOCI APIs."""

    def __init__(
        self,
        db_path: str | None = None,
        glassbox_path: str | None = None,
        actor_id: str = "arc",
    ):
        self.db_path = str(
            Path(
                db_path
                or os.environ.get(
                    "LOCI_DB_PATH",
                    default_loci_db_path(),
                )
            ).expanduser()
        )
        self.glassbox_path = str(
            Path(
                glassbox_path
                or os.environ.get(
                    "LOCI_GLASSBOX_PATH",
                    default_glassbox_path(),
                )
            ).expanduser()
        )
        Path(self.glassbox_path).parent.mkdir(parents=True, exist_ok=True)
        self.storage = StorageEngine(self.db_path)
        self.actor_id = actor_id
        self._sessions: dict[str, dict[str, Any]] = {}

        # glassbox.py reads this value at import time, so update the module
        # value explicitly for tests and for callers supplying a custom path.
        import loci_engine.glassbox as glassbox_module

        glassbox_module.GLASSBOX_PATH = self.glassbox_path
        glassbox_module.GLASSBOX_LOCK = self.glassbox_path + ".lock"

    @staticmethod
    def normalize_session_id(session_id: str) -> str:
        normalized = _SAFE_SESSION_ID.sub("-", session_id.strip())[:96].strip("-")
        if not normalized:
            raise ValueError("session_id must contain at least one safe character")
        return normalized

    @staticmethod
    def derive_session_id(messages: list[dict[str, Any]]) -> str:
        first_user = next(
            (
                str(message.get("content", "")).strip()
                for message in messages
                if message.get("role") == "user" and str(message.get("content", "")).strip()
            ),
            "",
        )
        if not first_user:
            return "arc-anonymous"
        digest = hashlib.sha256(first_user.encode("utf-8")).hexdigest()[:24]
        return f"arc-{digest}"

    def _session(self, session_id: str) -> tuple[SessionIngester, str]:
        external_id = self.normalize_session_id(session_id)
        existing = self._sessions.get(external_id)
        if existing:
            return existing["ingester"], existing["internal_id"]

        prior = start_session(
            self.storage,
            agent_id=self.actor_id,
            metadata={"arc_session_id": external_id},
        )
        ingester = SessionIngester(
            self.storage,
            session_id=external_id,
            actor_id=self.actor_id,
            decay_on_init=False,
        )
        self._sessions[external_id] = {
            "ingester": ingester,
            "internal_id": prior["session_id"],
        }
        return ingester, prior["session_id"]

    def ingest_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        position: int,
    ) -> str | None:
        if role not in {"user", "assistant"}:
            return None
        if not content or not content.strip():
            return None

        ingester, _internal_id = self._session(session_id)
        safe_content = content.strip()
        turn_id = (
            f"{ingester.session_id}_turn_{position:04d}_"
            f"{role}_{hashlib.sha256(safe_content.encode('utf-8')).hexdigest()[:12]}"
        )
        with self.storage._get_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM memory_item WHERE id = ?", (turn_id,)
            ).fetchone()
        if exists:
            return turn_id

        self.storage.ingest_session_turn(
            turn_id=turn_id,
            role=role,
            content=safe_content,
            namespace="session",
            heat=0.75 if role == "user" else 0.60,
            era="near",
            source_actor=self.actor_id,
        )
        glassbox_learn(
            author_id=self.actor_id,
            role_tag=role,
            content=safe_content,
            category="observation",
            metadata={"arc_session_id": ingester.session_id, "turn_id": turn_id},
        )
        return turn_id

    def ingest_messages(self, session_id: str, messages: list[dict[str, Any]]) -> list[str]:
        stored: list[str] = []
        for position, message in enumerate(messages):
            turn_id = self.ingest_turn(
                session_id=session_id,
                role=str(message.get("role", "")),
                content=str(message.get("content", "")),
                position=position,
            )
            if turn_id:
                stored.append(turn_id)
        return stored

    def recall(self, session_id: str, query: str, limit: int = 10) -> dict[str, Any]:
        ingester, internal_id = self._session(session_id)
        result = ingester.recall_for_query(
            query=query,
            namespace="session",
            source_actor=self.actor_id,
            limit=max(1, min(limit * 5, 100)),
        )
        selected = result.get("selected_items", [])[:limit]
        for item in selected:
            heat = float(item.get("effective_heat", item.get("heat", 0.0)))
            tier = "kernel" if heat >= 0.50 else "hop" if heat >= 0.167 else "offload"
            from loci_engine.session_continuity import record_recall

            record_recall(self.storage, internal_id, item["id"], tier, heat)
        return {**result, "selected_items": selected, "session_id": internal_id}

    def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Search shared canonical memory without creating a chat session."""
        ingester = SessionIngester(
            self.storage,
            session_id="arc-memory-search",
            actor_id=self.actor_id,
            decay_on_init=False,
        )
        result = ingester.recall_for_query(
            query=query,
            namespace=None,
            source_actor=self.actor_id,
            limit=max(1, min(limit * 5, 100)),
        )
        return {**result, "selected_items": result.get("selected_items", [])[:limit]}

    def ingest_document(self, content: str, category: str, project: str) -> str:
        item_id = (
            "arc-document-"
            + hashlib.sha256(f"{category}\0{project}\0{content}".encode("utf-8")).hexdigest()
        )
        with self.storage._get_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM memory_item WHERE id = ?", (item_id,)
            ).fetchone()
        if not exists:
            self.storage.insert_fact(
                item_id=item_id,
                namespace=f"document:{project}",
                canonical_text=f"[{category}] {content}",
                source_actor=self.actor_id,
                source_trust=1.0,
                heat=0.70,
                era="mid",
            )
            glassbox_learn(
                author_id=self.actor_id,
                role_tag="document",
                content=content,
                category="discovery",
                metadata={"category": category, "project": project, "item_id": item_id},
            )
        return item_id

    def close(self) -> None:
        for session in self._sessions.values():
            end_session(self.storage, session["internal_id"])
        self._sessions.clear()

    def status(self) -> dict[str, Any]:
        chain = glassbox_verify_chain()
        return {
            "db_path": self.db_path,
            "glassbox_path": self.glassbox_path,
            "active_arc_sessions": len(self._sessions),
            "glassbox": chain,
        }
