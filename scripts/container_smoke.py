#!/usr/bin/env python3
"""Bounded Podman smoke test. Prints JSON; never installs or changes host policy."""

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

IMAGE = "docker.io/library/alpine:3.22"


def command(*args, timeout=30, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]}: {result.stderr.strip()[:1500]}")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rootful",
        action="store_true",
        help="Exige root; testa AppArmor e slice transitória do sistema",
    )
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    if args.rootful != (os.geteuid() == 0):
        parser.error("Use sem sudo para rootless; com sudo, informe --rootful.")
    podman = shutil.which("podman")
    if not podman:
        parser.error("Podman não encontrado.")
    prefix = "shinycheck" + uuid.uuid4().hex[:10]
    container = prefix + "-probe"
    unit = prefix + "-anchor.service"
    slice_name = prefix + "-apps.slice"
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "rootful-smoke" if args.rootful else "rootless-smoke",
        "kernel": platform.release(),
        "tests": [],
        "production_ready": False,
        "limitations": [
            "Não testa ainda Shiny, build, rede de atendimento, QoS sob disputa ou reboot.",
            "A imagem permanece em cache; somente containers/unidades do teste são removidos.",
        ],
    }
    created_unit = False
    created_container = False

    def record(name, passed, detail):
        report["tests"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            raise RuntimeError(f"Falha no teste {name}: {detail}")

    try:
        info = json.loads(command(podman, "info", "--format", "json"))
        host = info["host"]
        report["podman_version"] = info["version"]["Version"]
        report["controllers"] = host.get("cgroupControllers", [])
        report["apparmor_reported"] = host["security"].get("apparmorEnabled")
        record("cgroup_v2", host["cgroupVersion"] == "v2", host["cgroupVersion"])
        record("systemd_manager", host["cgroupManager"] == "systemd", host["cgroupManager"])
        if args.rootful:
            record("cpu_available", "cpu" in report["controllers"], report["controllers"])
            created_unit = True
            command(
                "systemd-run",
                "--quiet",
                "--unit=" + unit,
                "--slice=" + slice_name,
                "--property=RuntimeMaxSec=180",
                "/usr/bin/sleep",
                "180",
            )
            command(
                "systemctl",
                "set-property",
                "--runtime",
                slice_name,
                "CPUWeight=400",
                "MemoryMax=192M",
                "TasksMax=96",
            )
        command(podman, "pull", IMAGE, timeout=180)
        image = json.loads(command(podman, "image", "inspect", IMAGE))[0]
        report["image_digest"] = image["Digest"]
        image_id = image["Id"]
        options = [
            podman,
            "run",
            "--detach",
            "--name",
            container,
            "--label",
            "org.shinyd.test=" + prefix,
            "--network=none",
            "--read-only",
            "--user=65534:65534",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=128m",
            "--memory-swap=128m",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
        ]
        if args.rootful:
            options.extend(["--userns=auto:size=65536", "--cgroup-parent=" + slice_name])
        options.extend([image_id, "sleep", "150"])
        created_container = True
        command(*options)
        inspect = json.loads(command(podman, "inspect", container))[0]
        pid = inspect["State"]["Pid"]
        # Known test script only; no uploaded app is executed by this harness.
        script = r"""
set -eu
test "$(id -u)" = 65534
test ! -e /run/podman/podman.sock
test ! -e /var/run/docker.sock
test ! -e /var/lib/shinyd
test -z "$(ip route)"
if touch /shinyd-write-probe 2>/dev/null; then exit 21; fi
touch /tmp/probe
grep -q 'NoNewPrivs:[[:space:]]*1' /proc/self/status
grep -q 'CapEff:[[:space:]]*0000000000000000' /proc/self/status
grep -q 'Seccomp:[[:space:]]*2' /proc/self/status
printf 'isolation-ok'
"""
        output = command(podman, "exec", container, "sh", "-ec", script)
        record(
            "isolation",
            output == "isolation-ok",
            "UID não-root, raiz somente leitura, tmpfs, rede desligada, caps=0, seccomp e NNP",
        )
        namespace_map = command(podman, "exec", container, "cat", "/proc/self/uid_map")
        report["uid_map"] = namespace_map
        if args.rootful:
            entries = [list(map(int, line.split())) for line in namespace_map.splitlines()]
            record(
                "user_namespace_remapped",
                all(line[1] != 0 for line in entries),
                "UIDs internos não mapeiam para root do host",
            )
        cgroup = Path(f"/proc/{pid}/cgroup").read_text().strip()
        path = cgroup.split("0::", 1)[1]
        root = Path("/sys/fs/cgroup") / path.lstrip("/")
        record(
            "memory_limit",
            (root / "memory.max").read_text().strip() == str(128 * 1024 * 1024),
            "memory.max=128 MiB no kernel",
        )
        record(
            "pids_limit", (root / "pids.max").read_text().strip() == "64", "pids.max=64 no kernel"
        )
        report["cgroup_path"] = path
        profile = command(podman, "exec", container, "cat", "/proc/self/attr/current")
        report["apparmor_profile"] = profile
        if args.rootful:
            record("apparmor_enforced", "(enforce)" in profile, profile)
            record("slice_membership", slice_name in root.parts, path)
            parent = root
            while parent.name != slice_name and parent != parent.parent:
                parent = parent.parent
            record(
                "parent_weight",
                (parent / "cpu.weight").read_text().strip() == "400",
                "CPUWeight=400 presente no pai agregado",
            )
        else:
            report["limitations"].append(
                "Rootless não homologa delegação de CPU, AppArmor ou slices do sistema."
            )
    except KeyboardInterrupt:
        report["error"] = "Teste interrompido pelo operador."
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        report["error"] = str(exc)
    finally:
        if created_container:
            try:
                command(podman, "rm", "--force", "--ignore", container)
            except Exception as exc:
                report["cleanup_error"] = str(exc)
        if created_unit:
            try:
                command("systemctl", "stop", unit, slice_name, prefix + ".slice", check=False)
                command("systemctl", "revert", slice_name, check=False)
                command("systemctl", "reset-failed", unit, check=False)
            except Exception as exc:
                report["cleanup_error"] = str(exc)
    report["smoke_passed"] = (
        bool(report["tests"]) and "error" not in report and "cleanup_error" not in report
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["smoke_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
