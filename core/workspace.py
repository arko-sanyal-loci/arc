"""Bounded local workspace operations for Arc coding tools."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


class WorkspaceError(ValueError):
    """Raised when a workspace request is invalid or outside its roots."""


def _roots() -> list[Path]:
    configured = os.environ.get("ARC_WORKSPACE_ROOTS", "")
    values = configured.split(os.pathsep) if configured else [str(Path(__file__).resolve().parents[1])]
    roots = [Path(value).expanduser().resolve() for value in values if value.strip()]
    if not roots:
        raise WorkspaceError("no workspace roots configured")
    return roots


def _resolve(relative_path: str) -> tuple[Path, Path]:
    if not isinstance(relative_path, str) or relative_path.startswith(("/", "\\")):
        raise WorkspaceError("path must be relative to a configured workspace root")
    candidate = Path(relative_path)
    if any(part == ".." for part in candidate.parts):
        raise WorkspaceError("parent traversal is not allowed")
    for root in _roots():
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return root, resolved
    raise WorkspaceError("path is outside configured workspace roots")


def list_files(relative_path: str = "", depth: int = 2) -> str:
    if not 0 <= depth <= 5:
        raise WorkspaceError("depth must be between 0 and 5")
    root, target = _resolve(relative_path)
    if not target.exists():
        raise WorkspaceError("path does not exist")
    if target.is_file():
        items = [{"path": str(target.relative_to(root)), "type": "file"}]
    else:
        items = []
        for path in sorted(target.rglob("*")):
            if len(path.relative_to(target).parts) > depth + 1:
                continue
            items.append({
                "path": str(path.relative_to(root)),
                "type": "directory" if path.is_dir() else "file",
            })
    return json.dumps({"root": str(root), "items": items[:500]})


def read_file(relative_path: str, max_bytes: int = 200_000) -> str:
    if not 1 <= max_bytes <= 1_000_000:
        raise WorkspaceError("max_bytes must be between 1 and 1000000")
    root, target = _resolve(relative_path)
    if not target.is_file():
        raise WorkspaceError("path is not a file")
    data = target.read_bytes()
    if len(data) > max_bytes:
        raise WorkspaceError("file exceeds max_bytes")
    return json.dumps({
        "path": str(target.relative_to(root)),
        "content": data.decode("utf-8"),
        "bytes": len(data),
    })


def write_file(relative_path: str, content: str) -> str:
    if not isinstance(content, str) or len(content.encode("utf-8")) > 200_000:
        raise WorkspaceError("content must be UTF-8 and at most 200000 bytes")
    root, target = _resolve(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return json.dumps({
        "path": str(target.relative_to(root)),
        "bytes": len(content.encode("utf-8")),
        "status": "written",
    })


def run_check(relative_path: str, check: str = "git-diff-check") -> str:
    root, target = _resolve(relative_path)
    if not target.exists():
        raise WorkspaceError("path does not exist")
    allowed = {
        "git-diff-check": ["git", "diff", "--check", "--"],
        "python-compile": [sys.executable, "-m", "py_compile"],
        "bash-syntax": ["bash", "-n"],
        "node-syntax": ["node", "--check"],
    }
    if check not in allowed:
        raise WorkspaceError(f"unsupported check: {check}")
    command = [*allowed[check], str(target.relative_to(root))]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"check failed to start: {type(exc).__name__}") from exc
    return json.dumps({
        "check": check,
        "path": str(target.relative_to(root)),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-20_000:],
        "stderr": completed.stderr[-20_000:],
        "passed": completed.returncode == 0,
    })
