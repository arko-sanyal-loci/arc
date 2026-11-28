"""Fresh, read-only host orientation for every Arc inference request.

This is operational context (interfaces and local hardware), not a claim that
Arc has human perception or awareness. Missing measurements stay unavailable.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any


def _ip_json(*args: str) -> Any:
    try:
        raw = subprocess.check_output(
            ["ip", "-j", *args], stderr=subprocess.DEVNULL, text=True, timeout=1.5
        )
        return json.loads(raw)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            json.JSONDecodeError, OSError):
        return None


def _interfaces() -> dict[str, Any]:
    rows = _ip_json("address", "show")
    if not isinstance(rows, list):
        return {"available": False, "interfaces": []}
    interfaces = []
    for row in rows:
        addresses = [
            {"address": info.get("local"), "family": info.get("family"),
             "scope": info.get("scope")}
            for info in row.get("addr_info", [])
            if info.get("local") and info.get("scope") != "host"
        ]
        interfaces.append({
            "name": row.get("ifname", "unknown"),
            "state": row.get("operstate", "unknown"),
            "addresses": addresses,
        })
    return {"available": True, "interfaces": interfaces}


def _default_routes() -> dict[str, Any]:
    rows = _ip_json("route", "show", "default")
    if not isinstance(rows, list):
        return {"available": False, "routes": []}
    return {"available": True, "routes": [
        {key: row[key] for key in ("dst", "gateway", "dev", "metric", "protocol") if key in row}
        for row in rows
    ]}


def _memory_total_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def collect_orientation() -> dict[str, Any]:
    """Poll local IP/interface, route, CPU, memory, disk, and GPU state now."""
    from core.gpu_telemetry import collect_gpu_telemetry

    try:
        disk = shutil.disk_usage(Path.cwd())
        disk_state = {"total_gb": round(disk.total / 1e9, 1),
                      "free_gb": round(disk.free / 1e9, 1)}
    except OSError:
        disk_state = {"available": False}
    return {
        "timestamp": time.time(),
        "source": "read-only local host snapshot",
        "host": {
            "hostname": socket.gethostname(), "system": platform.system(),
            "release": platform.release(), "architecture": platform.machine(),
            "processor": platform.processor() or None,
            "logical_cpus": os.cpu_count(), "memory_total_mb": _memory_total_mb(),
            "working_directory_disk": disk_state,
        },
        "network": {"interfaces": _interfaces(), "default_routes": _default_routes()},
        "gpu": collect_gpu_telemetry(),
    }


def orientation_system_message(profile: dict[str, Any] | None = None) -> str:
    """Create the per-inference grounded orientation instruction."""
    snapshot = collect_orientation()
    host = snapshot["host"]
    network = snapshot["network"]
    interfaces = network["interfaces"].get("interfaces", [])
    # Never send private IPs, gateway addresses, or the host name to model
    # providers. Keep detailed values in the localhost-only dashboard only.
    scoped_counts: dict[str, int] = {}
    for interface in interfaces:
        for address in interface.get("addresses", []):
            scope = address.get("scope", "unknown")
            scoped_counts[scope] = scoped_counts.get(scope, 0) + 1
    model_snapshot = {
        "timestamp": snapshot["timestamp"],
        "host": {key: host.get(key) for key in
                 ("system", "release", "architecture", "logical_cpus", "memory_total_mb")},
        "network": {
            "interface_count": len(interfaces),
            "interface_states": [item.get("state", "unknown") for item in interfaces],
            "address_scope_counts": scoped_counts,
            "default_route_count": len(network["default_routes"].get("routes", [])),
        },
        "gpu": {
            "available": snapshot["gpu"].get("available", False),
            "devices": [{key: device.get(key) for key in
                         ("vendor", "name", "temperature_c", "gpu_util_pct", "memory_used_mb", "memory_total_mb")}
                        for device in snapshot["gpu"].get("devices", [])],
        },
    }
    profile_context = None
    if profile:
        profile_context = {
            "id": profile.get("id"), "name": profile.get("name"),
            "role": profile.get("role"),
            "identity_basis": "selected interaction profile; not authenticated",
        }
    instruction = (
        "Arc operational orientation snapshot (refresh for this inference):\n"
        + json.dumps({"spatial_context": model_snapshot, "interaction_profile": profile_context},
                     separators=(",", ":"), ensure_ascii=True)
        + "\nThis is a privacy-filtered summary; exact host names, IP addresses, and gateways remain local. "
          "Treat these readings as local operational context, not human perception. "
          "A missing or null value is unknown. Do not infer people, location, or external "
          "network state from this snapshot. The selected interaction profile is user supplied, "
          "not verified identity. Arc is the sole director for this inference; do not claim "
          "another model or agent made the routing decision."
    )
    return instruction


def orient_messages(messages: list[Any], profile: dict[str, Any] | None = None) -> list[Any]:
    """Prepend a grounded orientation system message to each inference call."""
    from core.api_provision import ChatMessage

    instruction = orientation_system_message(profile)
    normalized = [ChatMessage(
        role=m.role if hasattr(m, "role") else m.get("role", "user"),
        content=(m.content if hasattr(m, "content") else str(m.get("content", ""))),
    ) for m in messages]
    for item in normalized:
        if item.role == "system":
            item.content = item.content + "\n\n" + instruction
            return normalized
    return [ChatMessage(role="system", content=instruction), *normalized]
