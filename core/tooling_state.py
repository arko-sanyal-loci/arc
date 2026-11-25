"""Shared state paths, tooling token, and hash-chained audit log for Arc tooling.

Nothing here stores provider secrets in the repository. State lives under
``$ARC_STATE_DIR`` (default ``~/.local/state/arc``) with 0700/0600 modes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def state_dir() -> Path:
    path = Path(os.environ.get("ARC_STATE_DIR", "~/.local/state/arc")).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def config_dir() -> Path:
    path = Path(os.environ.get("ARC_CONFIG_DIR", "~/.config/arc")).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


# ── Tooling token ────────────────────────────────────────────────────────────

def tooling_token() -> str:
    """Return the local tooling bearer token, creating it (0600) on first use."""
    env = os.environ.get("ARC_TOOLING_TOKEN")
    if env:
        return env
    path = config_dir() / "tooling-token"
    with _lock:
        if not path.exists():
            _write_private(path, secrets.token_urlsafe(32) + "\n")
    return path.read_text(encoding="utf-8").strip()


def token_ok(headers: Any) -> bool:
    supplied = ""
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    supplied = supplied or (headers.get("x-arc-tooling-token") or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, tooling_token())


# ── Audit log ────────────────────────────────────────────────────────────────

_SECRET_KEYS = ("token", "secret", "password", "authorization", "cookie", "code_verifier", "refresh")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in _SECRET_KEYS) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    return value


def audit(event: str, **fields: Any) -> dict:
    """Append a hash-chained record; each line commits to the previous one."""
    path = state_dir() / "tooling-audit.jsonl"
    with _lock:
        prev = "0" * 64
        if path.exists() and path.stat().st_size:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 8192))
                tail = handle.read().splitlines()
            if tail:
                try:
                    prev = json.loads(tail[-1]).get("hash", prev)
                except ValueError:
                    prev = "corrupt"
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            **_redact(fields),
            "prev": prev,
        }
        record["hash"] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def verify_audit_chain() -> dict:
    path = state_dir() / "tooling-audit.jsonl"
    if not path.exists():
        return {"records": 0, "intact": True}
    prev = "0" * 64
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        claimed = record.pop("hash", "")
        if record.get("prev") != prev:
            return {"records": count, "intact": False, "broken_at": count}
        if hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest() != claimed:
            return {"records": count, "intact": False, "broken_at": count}
        prev = claimed
        count += 1
    return {"records": count, "intact": True, "head": prev}
