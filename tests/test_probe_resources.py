import json
from pathlib import Path
from types import SimpleNamespace

import probe_resources as probe
import pytest


@pytest.fixture
def observed(monkeypatch):
    gib = probe.GIB
    files = {
        "/proc/meminfo": f"MemTotal: {16 * gib // 1024} kB\nMemAvailable: {8 * gib // 1024} kB\n",
        "/proc/pressure/memory": "some avg10=2.0\nfull avg10=0.25 avg60=0.5\n",
        str(probe.WORKLOADS / "memory.max"): str(4 * gib),
        str(probe.WORKLOADS / "memory.current"): str(2 * gib),
    }
    monkeypatch.setattr(Path, "read_text", lambda p: files[str(p)])
    monkeypatch.setattr(
        probe.os, "statvfs", lambda p: SimpleNamespace(f_bavail=7 * gib // 4096, f_frsize=4096)
    )
    return files


def test_observations_use_available_disk_memory_reserve_and_aggregate_budget(observed):
    result = probe.snapshot()
    assert result["read_only"] is True
    assert result["disk"][probe.DISKS[0]]["headroom_bytes"] == probe.GIB
    assert result["memory"]["headroom_bytes"] == 7 * probe.GIB - 16 * probe.GIB * 15 // 100
    assert result["memory_pressure"] == {"full_avg10": 0.25, "maximum": 1.0}
    assert result["workloads"]["starting_headroom_bytes"] == probe.GIB
    probe.require_builder_disk(result)


@pytest.mark.parametrize("path", probe.DISKS[:2])
@pytest.mark.parametrize("missing_bytes", [1, 0])
def test_preflight_checks_each_filesystem_without_lowering_the_reserve(
    observed, monkeypatch, path, missing_bytes
):
    def statvfs(p):
        free = 6 * probe.GIB - missing_bytes if str(p) == path else 8 * probe.GIB
        return SimpleNamespace(f_bavail=free, f_frsize=1)

    monkeypatch.setattr(probe.os, "statvfs", statvfs)
    result = probe.snapshot()
    if missing_bytes:
        with pytest.raises(RuntimeError, match="builder_probe_disk_reserve_unavailable"):
            probe.require_builder_disk(result)
    else:
        probe.require_builder_disk(result)


def test_missing_directory_uses_parent_but_permission_failure_is_not_hidden(observed, monkeypatch):
    calls = []

    def statvfs(p):
        calls.append(str(p))
        if str(p) == probe.DISKS[0]:
            raise FileNotFoundError
        if str(p) == probe.DISKS[1]:
            raise PermissionError("PRIVATE_SENTINEL")
        return SimpleNamespace(f_bavail=8 * probe.GIB, f_frsize=1)

    monkeypatch.setattr(probe.os, "statvfs", statvfs)
    result = probe.snapshot()
    assert result["disk"][probe.DISKS[0]]["observed_path"] == "/var/lib"
    assert result["disk"][probe.DISKS[1]] == {"unavailable": True}
    assert calls[:3] == [probe.DISKS[0], "/var/lib", probe.DISKS[1]]
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    with pytest.raises(RuntimeError, match="builder_probe_disk_reserve_unavailable"):
        probe.require_builder_disk(result)


def test_unbounded_workloads_and_invalid_pressure_are_explicit(observed):
    observed[str(probe.WORKLOADS / "memory.max")] = "max"
    observed["/proc/pressure/memory"] = "full avg10=nan"
    result = probe.snapshot()
    assert result["workloads"] == {"current_bytes": 2 * probe.GIB, "maximum_bytes": None}
    assert result["memory_pressure"] == {"unavailable": True}


def test_unreadable_kernel_metadata_does_not_leak_errors(observed, monkeypatch):
    def read(p):
        raise PermissionError("PRIVATE_SENTINEL")

    monkeypatch.setattr(Path, "read_text", read)
    result = probe.snapshot()
    assert all(
        result[k] == {"unavailable": True} for k in ("memory", "memory_pressure", "workloads")
    )
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
