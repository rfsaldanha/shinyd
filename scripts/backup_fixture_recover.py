#!/usr/bin/env python3
"""Quarantine only identified remnants of an interrupted disposable probe."""

import argparse
import fcntl
import json
import os
import stat
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

import backup_failure_diagnose as diagnose

SAFE_ERRORS = {
    "unsafe_parent_directory",
    "inventory_changed",
    "installation_not_quiescent_or_empty",
    "active_build",
    "unidentified_or_unsafe_fixture",
    "remaining_control_or_jobs",
    "remaining_configuration",
    "unexpected_namespace_entry",
    "missing_fixture_policy",
    "quarantine_requires_same_filesystem",
    "quarantine_partial_preserved",
    "diagnostic_does_not_match_report",
    "unsafe_lock",
    "unsafe_builder_api_lock",
    "builder_api_lock_busy",
}


def safe_directory(path, root):
    for item in (path, *path.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("unsafe_parent_directory")
        if item == root:
            break


@contextmanager
def builder_api_lock(jobs, root):
    """Preserve the builder daemon's empty lock, holding its actual inode while used."""
    safe_directory(jobs, root)
    path = jobs / ".api-lock"
    if not path.exists() and not path.is_symlink():
        yield None
        return
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        validate_api_lock(info)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("builder_api_lock_busy") from None
        yield info


def validate_api_lock(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size != 0
        or info.st_uid != os.geteuid()
        or info.st_gid != os.getegid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("unsafe_builder_api_lock")


def prepare(
    report,
    diagnostic,
    *,
    root=Path("/"),
    run=diagnose.command,
    own_marker=False,
    held_api_lock=None,
):
    current = diagnose.collect(report, root=root, run=run)
    for key in ("runtime_records", "artifacts", "data", "policies"):
        if current[key] != diagnostic[key]:
            raise ValueError("inventory_changed")
    states = [s for s in current["services"] if s.startswith("ActiveState=")]
    units = {s[3:] for s in current["services"] if s.startswith("Id=")}
    if (
        len(states) != len(diagnose.UNITS)
        or units != set(diagnose.UNITS)
        or any(s not in {"ActiveState=inactive", "ActiveState=failed"} for s in states)
        or current["managed_container_count"]
        or current["backup_interlocks"]["restore-incomplete"]
        or (current["backup_interlocks"]["maintenance"] and not own_marker)
        or current["control_config_present"]
        or current["control_database_present"]
        or current["builder_pending_record"]
    ):
        raise ValueError("installation_not_quiescent_or_empty")
    builds = json.loads(
        run("systemctl", "list-units", "--all", "--output=json", "shinyd-build@*.service")
    )
    if any(v["active"] not in {"inactive", "failed"} for v in builds):
        raise ValueError("active_build")
    records, artifacts, data = (current[k] for k in ("runtime_records", "artifacts", "data"))
    if (
        not records
        or any(not v["in_failed_report"] or v["desired"] != "removed" for v in records)
        or any(not v["in_failed_report"] for v in artifacts)
        or any(not v["in_failed_report"] or v["unsafe_entries"] for v in data)
    ):
        raise ValueError("unidentified_or_unsafe_fixture")
    lib = root / "var/lib"
    # The old probe deleted its control state and build jobs before cleanup failed.
    # The builder intentionally leaves its daemon lock after exiting.
    for name in ("shinyd-control", "shinyd-builder/jobs"):
        directory = lib / name
        allowed_entries = {".api-lock"} if name == "shinyd-builder/jobs" else set()
        if directory.is_symlink() or (
            directory.exists() and any(p.name not in allowed_entries for p in directory.iterdir())
        ):
            raise ValueError("remaining_control_or_jobs")
    # Called directly in tests/dry checks too; never accept a name alone as a safe lock.
    if held_api_lock is None:
        with builder_api_lock(lib / "shinyd-builder/jobs", root):
            pass
    else:
        actual = (lib / "shinyd-builder/jobs/.api-lock").lstat()
        validate_api_lock(actual)
        if (actual.st_dev, actual.st_ino) != (held_api_lock.st_dev, held_api_lock.st_ino):
            raise ValueError("unsafe_builder_api_lock")
    for name in (
        "etc/shinyd/control.json",
        "etc/shinyd-gateway/key",
        "etc/shinyd-tls/cert.pem",
        "etc/shinyd-tls/key.pem",
        "etc/shinyd-tls/Caddyfile",
    ):
        if (root / name).exists() or (root / name).is_symlink():
            raise ValueError("remaining_configuration")
    allowed = {
        "shinyd-runtime": {".lock", *[v["id"] + ".json" for v in records]},
        "shinyd-builder": {".lock", "jobs", "artifacts"},
        "shinyd-builder/artifacts": {v["cache_key"] + ".json" for v in artifacts},
        "shinyd-data": {v["app_id"] for v in data},
    }
    for name, entries in allowed.items():
        path = lib / name
        safe_directory(path, root)
        if set(p.name for p in path.iterdir()) - entries:
            raise ValueError("unexpected_namespace_entry")
    paths = [
        *[lib / "shinyd-runtime" / (v["id"] + ".json") for v in records],
        *[lib / "shinyd-builder/artifacts" / (v["cache_key"] + ".json") for v in artifacts],
        *[lib / "shinyd-data" / v["app_id"] for v in data],
    ]
    for kind in ("runtime", "builder"):
        if not current["policies"][kind]["present"]:
            raise ValueError("missing_fixture_policy")
        paths.append(root / f"etc/shinyd-{kind}/policy.json")
    for path in paths:
        safe_directory(path.parent, root)
        if path.lstat().st_dev != lib.stat().st_dev:
            raise ValueError("quarantine_requires_same_filesystem")
    return paths, current


def quarantine(paths, root, checksums):
    area = Path(tempfile.mkdtemp(prefix="shinyd-probe-quarantine-", dir=root / "var/lib"))
    area.chmod(0o700)
    moved = []
    receipt = {
        "schema_version": 1,
        "status": "prepared",
        **checksums,
        "paths": [p.relative_to(root).as_posix() for p in paths],
        "images_removed": False,
        "resource_limits_changed": False,
    }
    journal = area / "receipt.json"

    def save():
        journal.write_text(json.dumps(receipt, indent=2) + "\n")
        journal.chmod(0o600)
        os.sync()

    save()
    try:
        for origin in paths:
            target = area / "originals" / origin.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            origin.rename(target)
            moved.append((origin, target))
        receipt["status"] = "quarantined"
        save()
    except BaseException:
        for origin, target in reversed(moved):
            if origin.exists() or origin.is_symlink():
                raise RuntimeError("quarantine_partial_preserved") from None
            target.rename(origin)
        receipt["status"] = "rolled_back"
        save()
        raise
    return area, receipt


def guarded_quarantine(
    report, diagnostic, paths, root, checksums, *, run=diagnose.command, held_api_lock=None
):
    marker = root / "var/lib/shinyd-backup/maintenance"
    with marker.open("x") as guard:
        guard.write("fixture-quarantine\n")
        guard.flush()
        os.fsync(guard.fileno())
    completed = False
    try:
        # Guard new service starts, then recheck state before the first move.
        paths, _ = prepare(
            report, diagnostic, root=root, run=run, own_marker=True, held_api_lock=held_api_lock
        )
        result = quarantine(paths, root, checksums)
        completed = True
        return result
    finally:
        if completed or all(p.exists() for p in paths):
            marker.unlink()
            os.sync()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="Move identified files to private quarantine."
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Execute com sudo; a operação confere o namespace privado do ensaio.")
    os.umask(0o077)
    try:
        report, report_hash = diagnose.read_json(args.report, operator_owned=False)
        diagnostic, diagnostic_hash = diagnose.read_json(args.diagnostic, operator_owned=False)
        if diagnostic.get("source_sha256") != report_hash or not diagnostic.get("read_only"):
            raise ValueError("diagnostic_does_not_match_report")
        with ExitStack() as stack:
            for name in ("shinyd-backup", "shinyd-builder", "shinyd-runtime"):
                path = Path("/var/lib") / name
                safe_directory(path, Path("/"))
                fd = os.open(path / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                stack.enter_context(os.fdopen(fd, "wb"))
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0:
                    raise ValueError("unsafe_lock")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held_api_lock = stack.enter_context(
                builder_api_lock(Path("/var/lib/shinyd-builder/jobs"), Path("/"))
            )
            paths, _ = prepare(report, diagnostic, held_api_lock=held_api_lock)
            result = {"passed": True, "applied": args.apply, "identified_paths": len(paths)}
            result["preserved_builder_api_lock"] = held_api_lock is not None
            if args.apply:
                area, receipt = guarded_quarantine(
                    report,
                    diagnostic,
                    paths,
                    Path("/"),
                    {"report_sha256": report_hash, "diagnostic_sha256": diagnostic_hash},
                    held_api_lock=held_api_lock,
                )
                result.update(quarantine=str(area), **receipt)
            print(json.dumps(result, indent=2))
    except Exception as exc:
        result = {
            "passed": False,
            "recovery_error": type(exc).__name__,
            "reason": str(exc) if str(exc) in SAFE_ERRORS else "recovery_failed",
        }
        if result["reason"] == "remaining_control_or_jobs":
            try:
                result["remaining_namespaces"] = {
                    "control": diagnose.namespace_entries(Path("/var/lib/shinyd-control")),
                    "builder_jobs": diagnose.namespace_entries(
                        Path("/var/lib/shinyd-builder/jobs"), jobs=True
                    ),
                }
            except Exception:
                result["namespace_details_unavailable"] = True
        print(json.dumps(result, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
