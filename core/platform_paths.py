"""Portable Arc runtime paths for Linux, macOS, and Windows."""

from __future__ import annotations

import os
import platform
from pathlib import Path


def _default_state_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Arc"
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Arc"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "arc"


STATE_DIR = Path(os.environ.get("ARC_STATE_DIR", _default_state_dir())).expanduser()
DATA_DIR = Path(os.environ.get("ARC_DATA_DIR", STATE_DIR / "data")).expanduser()
RUNTIME_DIR = Path(os.environ.get("ARC_RUNTIME_DIR", STATE_DIR / "runtime")).expanduser()


def runtime_path(name: str) -> str:
    return str(RUNTIME_DIR / name)


def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def default_loci_db_path() -> str:
    configured = os.environ.get("LOCI_DB_PATH")
    if configured:
        return str(Path(configured).expanduser())
    return str(DATA_DIR / "loci" / "context2.db")


def default_glassbox_path() -> str:
    configured = os.environ.get("LOCI_GLASSBOX_PATH")
    if configured:
        return str(Path(configured).expanduser())
    return str(DATA_DIR / "loci" / "glassbox_ledger.jsonl")


def default_shm_path(name: str) -> str:
    if platform.system() == "Linux" and Path("/dev/shm").is_dir():
        return str(Path("/dev/shm") / name)
    return runtime_path(name)
