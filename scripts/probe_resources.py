#!/usr/bin/env python3
"""Read-only resource observations for the disposable builder probe."""

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

GIB = 1024**3
WORKLOADS = Path("/sys/fs/cgroup/shinyd.slice/shinyd-workloads.slice")
DISKS = ("/var/lib/shinyd-builder", "/var/lib/containers", "/var/lib/shinyd-backup", "/tmp")


def disk_observation(path):
    # Before installation the directory may not exist yet. Observe its nearest
    # existing parent, and identify that fallback explicitly in the report.
    observed = Path(path)
    try:
        while True:
            try:
                info = os.statvfs(observed)
                break
            except FileNotFoundError:
                if observed == observed.parent:
                    raise
                observed = observed.parent
        free = info.f_bavail * info.f_frsize
        result = {"observed_path": str(observed), "available_bytes": free}
        if path in DISKS[:2]:
            result.update(minimum_bytes=6 * GIB, headroom_bytes=free - 6 * GIB)
        return result
    except OSError:
        return {"unavailable": True}


def snapshot():
    result = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "disk": {path: disk_observation(path) for path in DISKS},
        "limitations": [
            "Observação atual; não identifica retrospectivamente o recurso que recusou um build.",
            "Os 6 GiB são a reserva mínima do builder, não o espaço total exigido pelo ensaio.",
            "Não mede o pico de consumo nem garante admissão futura ou capacidade.",
        ],
    }
    try:
        fields = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        total, available = (
            int(fields[key].split()[0]) * 1024 for key in ("MemTotal", "MemAvailable")
        )
        reserve = max(GIB, total * 15 // 100)
        result["memory"] = {
            "total_bytes": total,
            "available_bytes": available,
            "reserve_bytes": reserve,
            "starting_bytes": GIB,
            "headroom_bytes": available - reserve - GIB,
        }
    except (OSError, ValueError, KeyError):
        result["memory"] = {"unavailable": True}
    try:
        full = next(
            line
            for line in Path("/proc/pressure/memory").read_text().splitlines()
            if line.startswith("full ")
        )
        value = float(dict(item.split("=") for item in full.split()[1:])["avg10"])
        if not math.isfinite(value) or value < 0:
            raise ValueError
        result["memory_pressure"] = {"full_avg10": value, "maximum": 1.0}
    except (OSError, ValueError, KeyError, StopIteration):
        result["memory_pressure"] = {"unavailable": True}
    try:
        maximum = (WORKLOADS / "memory.max").read_text().strip()
        used = int((WORKLOADS / "memory.current").read_text())
        result["workloads"] = {"current_bytes": used, "maximum_bytes": None}
        if maximum != "max":
            result["workloads"].update(
                maximum_bytes=int(maximum), starting_headroom_bytes=int(maximum) - used - GIB
            )
    except (OSError, ValueError):
        result["workloads"] = {"unavailable": True}
    return result


def require_builder_disk(observation):
    for path in DISKS[:2]:
        disk = observation["disk"][path]
        if disk.get("unavailable") or disk["headroom_bytes"] < 0:
            raise RuntimeError("builder_probe_disk_reserve_unavailable")


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
