"""Read-only GPU telemetry for the Arc status overlay.

Deliberately does not initialise, clock, reset, or otherwise modify a GPU.
Unavailable measurements are returned as ``None`` rather than estimated.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any


NVIDIA_FIELDS = (
    "index,name,temperature.gpu,utilization.gpu,utilization.memory,"
    "memory.total,memory.used,power.draw,power.limit,clocks.sm,clocks.mem,pstate"
).split(",")


def _number(value: str) -> float | int | None:
    value = value.strip()
    if value in {"", "N/A", "[Not Supported]"}:
        return None
    try:
        parsed = float(value)
        return int(parsed) if parsed.is_integer() else parsed
    except ValueError:
        return None


def _nvidia() -> list[dict[str, Any]]:
    try:
        output = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={','.join(NVIDIA_FIELDS)}",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []

    devices: list[dict[str, Any]] = []
    for row in output.splitlines():
        values = [item.strip() for item in row.split(",")]
        if len(values) != len(NVIDIA_FIELDS):
            continue
        data = dict(zip(NVIDIA_FIELDS, values))
        total = _number(data["memory.total"])
        used = _number(data["memory.used"])
        power = _number(data["power.draw"])
        power_limit = _number(data["power.limit"])
        alerts: list[dict[str, str]] = []
        temp = _number(data["temperature.gpu"])
        if isinstance(temp, (int, float)) and temp >= 83:
            alerts.append({"level": "critical", "kind": "thermal", "message": f"{temp}°C: thermal ceiling"})
        elif isinstance(temp, (int, float)) and temp >= 78:
            alerts.append({"level": "warning", "kind": "thermal", "message": f"{temp}°C: elevated temperature"})
        if isinstance(total, (int, float)) and isinstance(used, (int, float)) and total > 0 and used / total >= 0.90:
            alerts.append({"level": "warning", "kind": "vram", "message": f"{used}/{total} MiB VRAM in use"})
        if isinstance(power, (int, float)) and isinstance(power_limit, (int, float)) and power_limit > 0 and power / power_limit >= 0.95:
            alerts.append({"level": "warning", "kind": "power", "message": "power limit reached"})
        util = _number(data["utilization.gpu"])
        if isinstance(util, (int, float)) and util >= 95 and not alerts:
            alerts.append({"level": "info", "kind": "compute", "message": "compute saturation"})
        devices.append({
            "vendor": "NVIDIA", "index": _number(data["index"]), "name": data["name"],
            "temperature_c": temp, "gpu_util_pct": util,
            "memory_util_pct": _number(data["utilization.memory"]),
            "memory_total_mb": total, "memory_used_mb": used,
            "power_draw_w": power, "power_limit_w": power_limit,
            "sm_clock_mhz": _number(data["clocks.sm"]),
            "memory_clock_mhz": _number(data["clocks.mem"]), "pstate": data["pstate"],
            "alerts": alerts,
        })
    return devices


def _intel_devices() -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for device in Path("/sys/class/drm").glob("card*/device"):
        try:
            if device.joinpath("vendor").read_text().strip().lower() != "0x8086":
                continue
        except OSError:
            continue
        temperatures = []
        for temp in device.glob("hwmon/hwmon*/temp*_input"):
            try:
                temperatures.append(int(temp.read_text().strip()) / 1000)
            except (OSError, ValueError):
                pass
        name = "Intel GPU"
        try:
            name = device.joinpath("uevent").read_text().split("PCI_ID=", 1)[1].splitlines()[0]
        except (OSError, IndexError):
            pass
        devices.append({
            "vendor": "Intel", "name": name,
            "temperature_c": max(temperatures) if temperatures else None,
            "gpu_util_pct": None, "memory_util_pct": None,
            "memory_total_mb": None, "memory_used_mb": None,
            "power_draw_w": None, "power_limit_w": None,
            "sm_clock_mhz": None, "memory_clock_mhz": None, "pstate": None,
            "alerts": [],
        })
    return devices


def collect_gpu_telemetry() -> dict[str, Any]:
    """Return current host telemetry without fabricating absent readings."""
    devices = _nvidia() + _intel_devices()
    return {
        "timestamp": time.time(),
        "available": bool(devices),
        "devices": devices,
        "bottlenecks": [alert for device in devices for alert in device["alerts"]],
    }
