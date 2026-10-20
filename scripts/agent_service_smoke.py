#!/usr/bin/env python3
"""Install (optionally) and verify only the diagnostic agent system service."""

import argparse
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def command(*args, check=True, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        # Fixed commands in this script never carry passwords or secret values.
        raise RuntimeError(f"{args[0]} falhou: {result.stderr.strip()[:1000]}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-package", type=Path)
    parser.add_argument("--after-reboot-from", help="boot_id do relatório anterior")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Execute com sudo; somente o serviço do agente será administrado.")
    if args.after_reboot_from and not re.fullmatch(
        r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", args.after_reboot_from
    ):
        parser.error("boot_id inválido.")
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "Pacote e serviço systemd do agente de diagnóstico",
        "kernel": platform.release(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "production_ready": False,
        "tests": [],
        "limitations": [
            "Não executa containers nem instala painel, gateway ou worker.",
            "Instalação em Linux limpo exige ambiente de homologação separado.",
        ],
    }
    if not args.after_reboot_from:
        report["limitations"].append(
            "Reboot ainda não homologado; este roteiro não reinicia o host."
        )

    def record(name, passed, detail=None):
        report["tests"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            raise RuntimeError(f"Falha na verificação {name}")

    def status():
        result = command(
            "runuser", "-u", "shinyd-control", "--", "/usr/bin/shinyd-agentctl", "status"
        )
        return json.loads(result.stdout)

    def main_pid():
        return int(command("systemctl", "show", "-p", "MainPID", "--value", "shinyd-agent").stdout)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    restore_socket = False
    try:
        record("systemd_pid1", Path("/proc/1/comm").read_text().strip() == "systemd")
        if args.install_package:
            package = args.install_package.absolute()
            if package.is_symlink() or not package.is_file():
                raise RuntimeError("Informe um arquivo .deb regular.")
            expected = package.with_suffix(".deb.sha256").read_text().split()[0]
            actual = hashlib.sha256(package.read_bytes()).hexdigest()
            record("package_checksum", expected == actual, actual)
            metadata = command(
                "dpkg-deb",
                "--show",
                "--showformat=${Package} ${Version} ${Architecture}",
                str(package),
            ).stdout
            record("package_identity", metadata == "shinyd-agent 0.1.0~agent1 amd64", metadata)
            command("dpkg", "--install", str(package), timeout=90)
        report["package_version"] = command(
            "dpkg-query", "--show", "--showformat=${Version}", "shinyd-agent"
        ).stdout
        if args.after_reboot_from:
            record("boot_changed", report["boot_id"] != args.after_reboot_from)
        # No start command before these checks: a missing auto-start must fail.
        record(
            "socket_enabled_and_active",
            command(
                "systemctl", "is-enabled", "--quiet", "shinyd-agent.socket", check=False
            ).returncode
            == 0
            and command(
                "systemctl", "is-active", "--quiet", "shinyd-agent.socket", check=False
            ).returncode
            == 0,
        )
        account = pwd.getpwnam("shinyd-control")
        record(
            "locked_service_identity",
            account.pw_uid != 0
            and account.pw_shell == "/usr/sbin/nologin"
            and command("passwd", "--status", "shinyd-control").stdout.split()[1] == "L",
        )
        socket_path = Path("/run/shinyd-agent/control.sock")
        metadata = socket_path.lstat()
        record(
            "private_socket_permissions",
            stat.S_ISSOCK(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == 0o660
            and metadata.st_uid == 0
            and metadata.st_gid == grp.getgrnam("shinyd-control").gr_gid,
        )
        observed = status()
        record(
            "authorized_kernel_identity",
            observed["mode"] == "diagnostic" and observed["runtime_enabled"] is False,
        )
        denied = command("/usr/bin/shinyd-agentctl", "status", check=False)
        record("root_not_implicit_caller", denied.returncode == 1 and not denied.stdout)
        pid = main_pid()
        fields = dict(
            line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines()
        )
        record(
            "effective_privilege_restrictions",
            fields["NoNewPrivs"].strip() == "1"
            and int(fields["CapEff"].strip(), 16) == 0
            and fields["Seccomp"].strip() == "2",
        )
        record(
            "private_network_namespace",
            os.readlink(f"/proc/{pid}/ns/net") != os.readlink("/proc/1/ns/net"),
        )
        cgroup = Path(f"/proc/{pid}/cgroup").read_text().strip().split("0::", 1)[1]
        cgroup_path = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
        record(
            "control_resource_guards",
            (cgroup_path / "memory.max").read_text().strip() == str(128 * 1024 * 1024)
            and (cgroup_path / "pids.max").read_text().strip() == "64"
            and "shinyd-control.slice" in cgroup_path.parts,
        )
        # The live service has already passed the checks above. Reset only its
        # start counter before the two deliberate restarts in this bounded test.
        command("systemctl", "reset-failed", "shinyd-agent.service")
        command("systemctl", "restart", "shinyd-agent.service")
        record("agent_restart_recovers", status()["schema_version"] == 1 and main_pid() != pid)
        restore_socket = True
        command("systemctl", "stop", "shinyd-agent.service", "shinyd-agent.socket")
        record("socket_removed_on_stop", not socket_path.exists())
        # Test recovery after /run state is recreated, without rebooting the host.
        command("systemctl", "start", "shinyd-agent.socket")
        record("socket_recreation_recovers", status()["schema_version"] == 1)
        restore_socket = False
        # Allow journald to ingest the bounded test's audit records.
        time.sleep(0.2)
        logs = command(
            "journalctl",
            "--unit=shinyd-agent.service",
            "--since",
            report["observed_at"],
            "--output=cat",
            "--no-pager",
        ).stdout
        events = []
        for line in logs.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "agent.request":
                events.append(event)
        record(
            "audit_records",
            bool(events)
            and all(
                set(event) == {"event", "operation", "caller_uid", "result"} for event in events
            )
            and any(event["result"] == 403 for event in events),
        )
    except KeyboardInterrupt:
        report["error"] = "Homologação interrompida pelo operador."
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        IndexError,
        subprocess.TimeoutExpired,
    ) as exc:
        report["error"] = str(exc)
    finally:
        if restore_socket:
            try:
                command("systemctl", "start", "shinyd-agent.socket")
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                report["recovery_error"] = str(exc)
    report["passed"] = "error" not in report
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
