#!/usr/bin/env python3
"""Prepare a persistent loopback test installation from a successful local backup probe."""

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

from installed_browser_probe import UNITS, require_empty_control
from manual_test_common import (
    CONFIG,
    PACKAGES,
    PRIVATE,
    PUBLIC,
    certificate_pair,
    plan_from_report,
    public_manifest,
    read_file,
    sample_zip,
)
from probe_resources import require_builder_disk, snapshot

ERRORS = {
    "unsafe_file",
    "file_limit",
    "successful_backup_report_required",
    "two_test_environments_required",
    "invalid_observed_environment",
    "unsafe_setup_directory",
    "existing_setup_file_differs",
    "different_setup_source",
    "unrecognized_setup_directory",
    "installed_package_version_differs",
    "public_setup_changed",
    "installed_setup_changed",
    "installed_certificate_changed",
    "services_must_be_stopped",
    "managed_containers_present",
    "installed_control_not_empty",
    "test_namespace_not_empty",
    "builder_jobs_not_empty",
    "backup_in_progress",
    "approved_image_differs",
    "public_setup_directory_occupied",
    "sudo_required",
    "builder_probe_disk_reserve_unavailable",
    *[
        "setup_command_failed:" + name
        for name in ("dpkg-query", "systemctl", "podman", "shinyd-configure")
    ],
}


def command(*args):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=180,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "HOME": "/root"},
    )
    if result.returncode:
        raise RuntimeError("setup_command_failed:" + Path(args[0]).name)
    return result.stdout


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def secure_directory(path, *, root, owner, create=False, mode=0o700):
    parent = path.parent if create and not path.exists() else path
    for candidate in (parent, *parent.parents):
        info = candidate.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise ValueError("unsafe_setup_directory")
        if candidate == root:
            break
    if create:
        path.mkdir(mode=mode, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError("unsafe_setup_directory")


def write_exact(path, data, *, owner, mode=0o600):
    if path.exists() or path.is_symlink():
        if read_file(path, owners={owner}) != data or stat.S_IMODE(path.lstat().st_mode) != mode:
            raise ValueError("existing_setup_file_differs")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(plan, *, root=Path("/"), run=command, owner=0):
    def at(path):
        return root / str(path).lstrip("/")

    private, public = at(PRIVATE), at(PUBLIC)
    source = private / "source.json"
    owned = source.exists() or source.is_symlink()
    if owned:
        secure_directory(private, root=root, owner=owner)
        if read_file(source, owners={owner}, private=True) != encoded(plan):
            raise ValueError("different_setup_source")
    elif private.exists() or private.is_symlink():
        raise ValueError("unrecognized_setup_directory")

    for package, version in PACKAGES.items():
        if run("dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", package).strip() != (
            "installed " + version
        ):
            raise ValueError("installed_package_version_differs")

    if owned and (public / "setup.json").exists():
        # A completed setup never reconfigures or restarts an installation in use.
        secure_directory(public, root=root, owner=owner)
        pem = read_file(public / "cert.pem", owners={owner})
        expected = public_manifest(plan, pem)
        if json.loads(read_file(public / "setup.json", owners={owner})) != expected:
            raise ValueError("public_setup_changed")
        for path, value in (
            ("/etc/shinyd/control.json", plan["config"]),
            ("/etc/shinyd-builder/policy.json", plan["builder"]),
            ("/etc/shinyd-runtime/policy.json", plan["runtime"]),
        ):
            if json.loads(read_file(at(path), owners={owner})) != value:
                raise ValueError("installed_setup_changed")
        if read_file(at("/etc/shinyd-tls/cert.pem"), owners={owner}) != pem:
            raise ValueError("installed_certificate_changed")
        return expected

    states = run(
        "systemctl",
        "show",
        *UNITS,
        "shinyd-runtime.service",
        "shinyd-runtime.socket",
        "shinyd-builder.service",
        "shinyd-builder.socket",
        "--property=ActiveState",
        "--value",
    ).split()
    if not states or any(s not in {"inactive", "failed"} for s in states):
        raise ValueError("services_must_be_stopped")
    containers = json.loads(run("podman", "ps", "--all", "--format=json"))
    if any(
        (c.get("Labels") or {}).get("org.shinyd.managed")
        or any(n.startswith("shinyd-") for n in c.get("Names", []))
        for c in containers
    ):
        raise ValueError("managed_containers_present")
    if not owned:
        require_empty_control(
            at("/var/lib/shinyd-control"),
            [
                at(p)
                for p in (
                    "/etc/shinyd/control.json",
                    "/etc/shinyd-builder/policy.json",
                    "/etc/shinyd-runtime/policy.json",
                    "/etc/shinyd-gateway/key",
                    "/etc/shinyd-tls/cert.pem",
                    "/etc/shinyd-tls/key.pem",
                    "/etc/shinyd-tls/Caddyfile",
                )
            ],
        )
        for path in ("/var/lib/shinyd-data", "/var/lib/shinyd-builder/artifacts"):
            directory = at(path)
            if directory.is_symlink() or (directory.exists() and any(directory.iterdir())):
                raise ValueError("test_namespace_not_empty")
        if (
            list(at("/var/lib/shinyd-runtime").glob("*.json"))
            or at("/var/lib/shinyd-builder/current.json").exists()
        ):
            raise ValueError("test_namespace_not_empty")
        jobs = at("/var/lib/shinyd-builder/jobs")
        for path in jobs.iterdir():
            if path.name != ".api-lock" or read_file(path, owners={owner}, private=True) != b"":
                raise ValueError("builder_jobs_not_empty")
    if (
        at("/var/lib/shinyd-backup/maintenance").exists()
        or at("/var/lib/shinyd-backup/restore-incomplete").exists()
    ):
        raise ValueError("backup_in_progress")
    for environment in plan["builder"]["environments"].values():
        image = environment["image"]
        metadata = json.loads(run("podman", "image", "inspect", image))[0]
        if "sha256:" + metadata["Id"].removeprefix("sha256:") != image or (
            metadata.get("Config", {}).get("OnBuild")
        ):
            raise ValueError("approved_image_differs")
    if not owned and (public.exists() or public.is_symlink()):
        raise ValueError("public_setup_directory_occupied")
    for path in ("/etc/shinyd", "/etc/shinyd-builder", "/etc/shinyd-runtime", "/etc/shinyd-tls"):
        secure_directory(at(path), root=root, owner=owner)
    secure_directory(private, root=root, owner=owner, create=True)
    write_exact(source, encoded(plan), owner=owner)
    cert, key = private / "cert.pem", private / "key.pem"
    if not cert.exists() and not key.exists():
        pem, secret = certificate_pair()
        write_exact(key, secret, owner=owner)
        write_exact(cert, pem, owner=owner)
    pem = read_file(cert, owners={owner}, private=True)
    read_file(key, owners={owner}, private=True)
    write_exact(at("/etc/shinyd-builder/policy.json"), encoded(plan["builder"]), owner=owner)
    write_exact(at("/etc/shinyd-runtime/policy.json"), encoded(plan["runtime"]), owner=owner)
    run(
        "shinyd-configure",
        "--panel-host",
        CONFIG["panel_host"],
        "--app-domain",
        CONFIG["app_domain"],
        "--bind",
        CONFIG["bind"],
        "--https-port",
        str(CONFIG["https_port"]),
        "--certificate",
        str(cert),
        "--private-key",
        str(key),
    )
    run(
        "systemctl",
        "set-property",
        "shinyd-workloads.slice",
        "MemoryHigh=3584M",
        "MemoryMax=4G",
        "MemorySwapMax=0",
    )
    secure_directory(public, root=root, owner=owner, create=True, mode=0o755)
    write_exact(public / "cert.pem", pem, owner=owner, mode=0o644)
    for name, environment in plan["builder"]["environments"].items():
        for revision in ("v1", "v2"):
            write_exact(
                public / f"demo-{name}-{revision}.zip",
                sample_zip(environment, revision),
                owner=owner,
                mode=0o644,
            )
    result = public_manifest(plan, pem)
    write_exact(public / "setup.json", encoded(result), owner=owner, mode=0o644)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        owners = {os.geteuid(), int(os.environ.get("SUDO_UID", os.geteuid()))}
        plan = plan_from_report(read_file(args.report, owners=owners))
        if not args.apply:
            print(json.dumps({"apply": False, "plan": plan}, ensure_ascii=False, indent=2))
            return
        if os.geteuid() != 0:
            raise ValueError("sudo_required")
        # Check before any mutation. Completed setups can be inspected again while running.
        if not (PUBLIC / "setup.json").exists():
            require_builder_disk(snapshot())
            with socket.socket() as listener:
                listener.bind((CONFIG["bind"], CONFIG["https_port"]))
        result = prepare(plan)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("Configuração preservada em /usr/local/share/shinyd-manual-test.", file=sys.stderr)
        print("Crie sua conta: sudo shinyd-admin createsuperuser", file=sys.stderr)
        print("Inicie: sudo systemctl enable --now shinyd.target", file=sys.stderr)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        reason = str(exc) if str(exc) in ERRORS else "validation_or_io_failed"
        print(json.dumps({"configured": False, "error": reason}))
        print(
            "Preparação não concluída. Confira o relatório aprovado, versões dos pacotes, "
            "imagens locais, serviços parados e namespace vazio. Arquivos existentes foram "
            "preservados; consulte docs/TESTE_MANUAL.md para retomar.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
