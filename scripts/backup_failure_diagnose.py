#!/usr/bin/env python3
"""Read-only inventory after a failed disposable backup probe. Never reads secret values."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
UNITS = [
    "shinyd.target",
    "shinyd-maintenance.timer",
    *[
        f"shinyd-{n}.service"
        for n in (
            "panel",
            "auth",
            "worker",
            "gateway",
            "ingress",
            "maintenance",
            "runtime",
            "builder",
        )
    ],
    *[f"shinyd-{n}.socket" for n in ("panel", "auth", "gateway", "runtime", "builder")],
]


def identity(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("invalid_identifier")
    return value


def read_json(path, *, operator_owned=True):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 4 * 1024**2
            or (operator_owned and (info.st_uid != os.geteuid() or info.st_mode & 0o022))
        ):
            raise ValueError("unsafe_metadata")
        raw = stream.read(4 * 1024**2 + 1)
        if len(raw) > 4 * 1024**2:
            raise ValueError("metadata_limit")
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def command(*args):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"},
    )
    if result.returncode:
        raise RuntimeError("diagnostic_command_failed")
    return result.stdout


def tree_size(path):
    """Count only regular files without following links or reading their contents."""
    total, entries, unsafe = 0, 0, 0
    if not path.exists():
        return {"bytes": 0, "files": 0, "unsafe_entries": 0}
    if path.is_symlink() or not path.is_dir():
        raise ValueError("unsafe_directory")
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            info = (Path(base) / name).lstat()
            entries += 1
            if entries > 100_000:
                raise ValueError("entry_limit")
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                unsafe += 1
    return {"bytes": total, "entries": entries, "unsafe_entries": unsafe}


def reported_instances(report):
    result = set()
    for value in report.get("cleanup_inventory", {}).get("instances", []):
        result.add(identity(value))
    for error in report.get("cleanup_error", []):
        if isinstance(error, str) and error.startswith("instance:"):
            result.add(identity(error.split(":", 2)[1]))
    result.update(identity(v) for v in report.get("activation", {}).get("instances", []))
    for row in report.get("tests", []):
        if row["name"] in {"artifact_0_ready", "artifact_1_ready"}:
            result.add(identity(row["detail"]["id"]))
    return result


def namespace_entries(path, *, jobs=False):
    """Bounded metadata only: do not publish unknown names or read file contents."""
    if not path.exists() and not path.is_symlink():
        return {"present": False}
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        return {"present": True, "directory": False, "symlink": stat.S_ISLNK(info.st_mode)}
    known = {
        "keys",
        "sources",
        "staging",
        "backups",
        ".lock",
        "metadata.sqlite3",
        "metadata.sqlite3-wal",
        "metadata.sqlite3-shm",
        "restore.json",
        "package-test-sentinel",
    }
    entries = []
    for child in sorted(path.iterdir()):
        if len(entries) >= 1000:
            raise ValueError("namespace_entry_limit")
        metadata = child.lstat()
        kind = (
            "directory"
            if stat.S_ISDIR(metadata.st_mode)
            else "file"
            if stat.S_ISREG(metadata.st_mode)
            else "symlink"
            if stat.S_ISLNK(metadata.st_mode)
            else "special"
        )
        name = child.name if child.name in known and not jobs else "unrecognized"
        if jobs:
            if child.name == ".api-lock":
                name = child.name
            else:
                try:
                    name = identity(child.name)
                except ValueError:
                    pass
        entries.append(
            {
                "name": name,
                "kind": kind,
                "bytes": metadata.st_size,
                "uid": metadata.st_uid,
                "gid": metadata.st_gid,
                "mode": oct(stat.S_IMODE(metadata.st_mode)),
                "links": metadata.st_nlink,
            }
        )
    return {"present": True, "directory": True, "entries": entries}


def collect(report, *, root=Path("/"), run=command):
    expected = reported_instances(report)
    if report.get("passed") is not False or not expected:
        raise ValueError("not_a_failed_probe_report")
    datetime.fromisoformat(report["observed_at"])
    value = {
        "schema_version": 1,
        "read_only": True,
        "observed_at": datetime.now(UTC).isoformat(),
        "source_observed_at": report["observed_at"],
        "limitations": [
            "A mensagem da falha anterior não foi preservada pelo roteiro antigo.",
            "A estimativa usa tamanhos descomprimidos; não mede o pico real.",
            "Nenhum serviço, container, imagem ou arquivo é removido ou iniciado.",
        ],
    }
    lib = root / "var/lib"
    value["services"] = run("systemctl", "show", *UNITS, "--property=Id,ActiveState").splitlines()
    containers = json.loads(run("podman", "ps", "--all", "--format=json"))
    value["managed_container_count"] = sum(
        bool((r.get("Labels") or {}).get("org.shinyd.managed"))
        or any(n.startswith("shinyd-") for n in r.get("Names", []))
        for r in containers
    )
    value["backup_interlocks"] = {
        n: (lib / "shinyd-backup" / n).exists() or (lib / "shinyd-backup" / n).is_symlink()
        for n in ("maintenance", "restore-incomplete")
    }
    value["control_config_present"] = (root / "etc/shinyd/control.json").exists()
    value["control_database_present"] = (lib / "shinyd-control/metadata.sqlite3").exists()
    value["remaining_namespaces"] = {
        "control": namespace_entries(lib / "shinyd-control"),
        "builder_jobs": namespace_entries(lib / "shinyd-builder/jobs", jobs=True),
    }
    images, apps = set(), set()
    value["policies"] = {}
    for kind in ("runtime", "builder"):
        path = root / f"etc/shinyd-{kind}/policy.json"
        if not path.exists():
            value["policies"][kind] = {"present": False}
            continue
        policy, checksum = read_json(path)
        selected = (
            list(policy["recipes"].values())
            if kind == "runtime"
            else [v["image"] for v in policy["environments"].values()]
        )
        images.update(selected)
        value["policies"][kind] = {
            "present": True,
            "sha256": checksum,
            "image_count": len(selected),
        }
    records = []
    for path in sorted((lib / "shinyd-runtime").glob("*.json")):
        record, _ = read_json(path)
        identity(path.stem)
        if record["id"] != path.stem or record["desired"] not in {"running", "stopped", "removed"}:
            raise ValueError("invalid_runtime_record")
        app_id = identity(record["spec"]["app_id"])
        known = record["id"] in expected
        if known:
            apps.add(app_id)
        records.append(
            {
                "id": record["id"],
                "app_id": app_id,
                "desired": record["desired"],
                "in_failed_report": known,
            }
        )
    value["runtime_records"] = records
    for a in [*report.get("artifacts", []), report.get("panel_artifact", {})]:
        if a.get("app_id"):
            apps.add(identity(a["app_id"]))
    artifacts = []
    for path in sorted((lib / "shinyd-builder/artifacts").glob("*.json")):
        artifact, _ = read_json(path)
        if not HASH.fullmatch(path.stem) or artifact["cache_key"] != path.stem:
            raise ValueError("invalid_artifact")
        app_id = identity(artifact["app_id"])
        images.update([artifact["image"], artifact["base_image"]])
        artifacts.append(
            {"cache_key": path.stem, "app_id": app_id, "in_failed_report": app_id in apps}
        )
    value["artifacts"] = artifacts
    if len(images) > 512 or any(not IMAGE.fullmatch(v) for v in images):
        raise ValueError("invalid_image_inventory")
    sizes, unavailable = {}, []
    for image in sorted(images):
        try:
            item = json.loads(run("podman", "image", "inspect", image))[0]
            size = item["Size"]
            if (
                type(size) is not int
                or size < 0
                or "sha256:" + item["Id"].removeprefix("sha256:") != image
            ):
                raise ValueError("invalid_image_size")
            sizes[image] = size
        except RuntimeError:
            unavailable.append(image)
    value["images"] = {
        "count": len(images),
        "unavailable": unavailable,
        "uncompressed_bytes": sum(sizes.values()),
        "largest_uncompressed_bytes": max(sizes.values(), default=0),
    }
    data = []
    for path in sorted((lib / "shinyd-data").iterdir()):
        app_id = identity(path.name)
        data.append({"app_id": app_id, "in_failed_report": app_id in apps, **tree_size(path)})
    value["data"] = data
    value["builder_pending_record"] = (lib / "shinyd-builder/current.json").exists()
    value["workload_limits"] = run(
        "systemctl",
        "show",
        "shinyd-workloads.slice",
        "--property=MemoryHigh,MemoryMax,MemorySwapMax",
    ).splitlines()
    value["disk"] = {
        "free_bytes": shutil.disk_usage(lib).free,
        "estimated_peak_bytes": 3 * (sum(sizes.values()) + sum(v["bytes"] for v in data)) + 1024**3,
        "estimate_complete": not unavailable and value["control_database_present"],
    }
    value["disk"]["below_conservative_estimate"] = (
        value["disk"]["free_bytes"] < value["disk"]["estimated_peak_bytes"]
    )
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--namespaces-only",
        action="store_true",
        help="Only inspect remaining control/job entry metadata.",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Execute com sudo para ler apenas os metadados privados do ensaio.")
    if not args.namespaces_only and args.report is None:
        parser.error("Informe --report ou --namespaces-only.")
    try:
        if args.namespaces_only:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "read_only": True,
                        "observed_at": datetime.now(UTC).isoformat(),
                        "remaining_namespaces": {
                            "control": namespace_entries(Path("/var/lib/shinyd-control")),
                            "builder_jobs": namespace_entries(
                                Path("/var/lib/shinyd-builder/jobs"), jobs=True
                            ),
                        },
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        report, checksum = read_json(args.report, operator_owned=False)
        result = collect(report)
        result["source_sha256"] = checksum
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as exc:
        # No arbitrary exception text, file contents, subprocess stderr or environment.
        print(json.dumps({"read_only": True, "diagnostic_error": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
