#!/usr/bin/env python3
"""Exercise the real agent binary and socket activation without system privileges."""

import argparse
import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", type=Path, default=ROOT / "dist/shinyd-agent")
    parser.add_argument("--client", type=Path, default=ROOT / "dist/shinyd-agentctl")
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error("Execute como usuário comum.")
    result = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "Agente real com socket herdado; usuário de desenvolvimento",
        "production_ready": False,
        "tests": [],
        "limitations": [
            "Não instala o pacote nem homologa isolamento do serviço systemd ou reboot.",
            "Não executa containers; operações de runtime permanecem indisponíveis.",
        ],
    }

    def record(name, passed):
        result["tests"].append({"name": name, "passed": bool(passed)})
        if not passed:
            raise RuntimeError(f"Falha: {name}")

    process = None
    with tempfile.TemporaryDirectory(prefix="shinyd-agent-probe-") as temporary:
        root = Path(temporary)
        path = str(root / "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(32)
        log_path = root / "agent.log"
        log = log_path.open("wb")

        def start(allowed_user):
            # Tiny exec wrapper only sets the child's own LISTEN_PID and fd 3.
            # No shell, preexec_fn, root or host service changes.
            wrapper = (
                "import os,sys; fd=int(sys.argv[1]); os.dup2(fd,3); "
                "os.set_inheritable(3,True); "
                "os.environ.update(LISTEN_PID=str(os.getpid()),LISTEN_FDS='1',"
                "LISTEN_FDNAMES='control'); os.execv(sys.argv[2],sys.argv[2:])"
            )
            return subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    wrapper,
                    str(listener.fileno()),
                    str(args.agent.resolve()),
                    "--allowed-user",
                    allowed_user,
                    "--socket-path",
                    path,
                    "--data-dir",
                    temporary,
                ],
                pass_fds=(listener.fileno(),),
                stdout=log,
                stderr=log,
            )

        def status():
            return subprocess.run(
                [
                    str(args.client.resolve()),
                    "--socket",
                    path,
                    "--expected-server-uid",
                    str(os.getuid()),
                    "status",
                ],
                capture_output=True,
                text=True,
                timeout=6,
            )

        def stop():
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

        try:
            process = start(pwd.getpwuid(os.getuid()).pw_name)
            first = status()
            record("inherited_unix_socket_and_real_doctor", first.returncode == 0)
            observed = json.loads(first.stdout)
            record(
                "read_only_capability",
                observed["capabilities"] == ["status"] and observed["runtime_enabled"] is False,
            )
            # Test a real request whose contents must not reach audit.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(path)
                client.sendall(
                    b"GET /v1/status?command=SECRET_SENTINEL HTTP/1.1\r\n"
                    b"Host: agent\r\nAuthorization: SECRET_SENTINEL\r\n\r\n"
                )
                response = client.recv(4096)
                record("arguments_rejected", b" 400 " in response.split(b"\r\n")[0])
            stop()
            record(
                "graceful_stop_keeps_activation_socket",
                process.returncode == 0 and Path(path).exists(),
            )
            process = start(pwd.getpwuid(os.getuid()).pw_name)
            record("service_restart_reuses_socket", status().returncode == 0)
            stop()
            # Same actual peer, different configured principal: credentials cannot
            # be impersonated by request headers. nobody exists on Ubuntu.
            process = start("nobody")
            record("unauthorized_uid_rejected", status().returncode != 0)
            stop()
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            record(
                "audit_allowlist",
                bool(records)
                and all(
                    set(event) == {"event", "operation", "caller_uid", "result"}
                    for event in records
                )
                and "SECRET_SENTINEL" not in log_path.read_text()
                and any(event["result"] == 403 for event in records),
            )
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            stop()
            listener.close()
            log.close()
    result["passed"] = "error" not in result
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
