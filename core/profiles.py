"""Read-only interaction profiles used by the local Arc command center."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROFILES_PATH = ROOT / "data" / "member_profiles.json"
EXAMPLE_PATH = ROOT / "config" / "member_profiles.example.json"


def load_profiles() -> list[dict[str, Any]]:
    source = PROFILES_PATH if PROFILES_PATH.exists() else EXAMPLE_PATH
    data = json.loads(source.read_text(encoding="utf-8"))
    profiles = data.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError("profiles must be a list")
    return profiles


def get_profile(profile_id: str | None) -> dict[str, Any] | None:
    if not profile_id:
        return None
    return next((p for p in load_profiles() if p.get("id") == profile_id), None)
