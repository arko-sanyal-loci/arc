from __future__ import annotations

from pathlib import Path

import pytest

from core import workspace


def test_workspace_operations_are_rooted_and_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_WORKSPACE_ROOTS", str(tmp_path))
    workspace.write_file("hello.txt", "hello")

    assert '"hello"' in workspace.read_file("hello.txt")
    assert '"hello.txt"' in workspace.list_files("", depth=1)

    with pytest.raises(workspace.WorkspaceError, match="traversal"):
        workspace.read_file("../outside.txt")
    with pytest.raises(workspace.WorkspaceError, match="at most"):
        workspace.write_file("large.txt", "x" * 200_001)


def test_workspace_checks_allow_only_named_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_WORKSPACE_ROOTS", str(tmp_path))
    workspace.write_file("module.py", "value = 1\n")

    result = workspace.run_check("module.py", "python-compile")
    assert '"passed": true' in result

    with pytest.raises(workspace.WorkspaceError, match="unsupported check"):
        workspace.run_check("module.py", "echo")
