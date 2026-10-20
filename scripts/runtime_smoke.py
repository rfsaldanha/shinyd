#!/usr/bin/env python3
"""Bounded rootful executor verification using repository fixtures, never uploads."""

import argparse
import grp
import hashlib
import json
import os
import pwd
import secrets
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from container_subids import inspect_reservation

ROOT = Path(__file__).resolve().parents[1]
POLICY = Path("/etc/shinyd-runtime/policy.json")
STATE = Path("/var/lib/shinyd-runtime")


def command(*args, input=None, check=True, timeout=60):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        operation = args[1] if args[0] in {"systemctl", "podman"} else ""
        raise RuntimeError(
            f"Comando de homologação falhou: {Path(args[0]).name} {operation} "
            f"(código {result.returncode})"
        )
    return result


def api(action, instance, spec=None, check=True):
    result = command(
        "runuser",
        "-u",
        "shinyd-control",
        "--",
        "/usr/bin/shinyd-runtimectl",
        action,
        instance,
        input=json.dumps(spec) if spec is not None else None,
        check=check,
        timeout=70,
    )
    return json.loads(result.stdout) if check else result


def stop_process(process):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-package", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or not os.environ.get("SUDO_UID"):
        parser.error("Use sudo a partir de uma conta comum; Chrome não executa como root.")
    browser_uid = int(os.environ["SUDO_UID"])
    browser_user = pwd.getpwuid(browser_uid)
    if browser_uid == 0:
        parser.error("A conta do navegador não pode ser root.")
    os.umask(0o077)
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "Executor rootful, rede none, relay Unix e Shiny real",
        "production_ready": False,
        "tests": [],
        "limitations": [
            "Não publica ZIPs nem homologa TLS, builder, dados persistentes ou QoS sob carga.",
            "Reinício do executor encerra sessões; não há drenagem nem acesso externo dos apps.",
            "Não testa reboot; mantém imagens em cache e pacote instalado, executor desabilitado.",
        ],
    }
    ids = [str(uuid.uuid4()) for _ in range(4)]
    policy_bytes = None
    proxy = browser = None
    touched_service = False
    created = []

    def record(name, passed, detail=None):
        report["tests"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            raise RuntimeError(f"Falha na verificação {name}")

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="shinyd-runtime-probe-") as temporary:
        temporary = Path(temporary)
        temporary.chmod(0o755)
        try:
            # Never interrupt an operator's installation or rewrite its policy.
            record(
                "empty_test_installation", not POLICY.exists() and not list(STATE.glob("*.json"))
            )
            reservation, _ = inspect_reservation()
            record("subids_prepared", reservation["action"] == "unchanged")
            if args.install_package:
                package = args.install_package.resolve(strict=True)
                digest = hashlib.sha256(package.read_bytes()).hexdigest()
                record(
                    "package_checksum",
                    digest == package.with_suffix(".deb.sha256").read_text().split()[0],
                    digest,
                )
                metadata = command(
                    "dpkg-deb", "--show", "--showformat=${Package} ${Version}", str(package)
                ).stdout
                record("package_identity", metadata == "shinyd-runtime 0.1.0~runtime1", metadata)
                command("dpkg", "--install", str(package), timeout=90)
            record(
                "execution_disabled_after_install",
                command(
                    "systemctl", "is-enabled", "--quiet", "shinyd-runtime.socket", check=False
                ).returncode
                != 0,
            )
            image = json.loads((ROOT / "experiments/r-images.json").read_text())["images"][0][
                "image"
            ]
            command("podman", "pull", image, timeout=600)
            shutil.copyfile(ROOT / "experiments/session-fixture/app.R", temporary / "app.R")
            recipe = temporary / "Containerfile"
            recipe.write_text(
                f"FROM {image}\nCOPY --chmod=0444 app.R /app/app.R\nWORKDIR /app\n"
                + "ENTRYPOINT "
                + json.dumps(
                    [
                        "Rscript",
                        "--vanilla",
                        "-e",
                        "shiny::runApp('/app',host='0.0.0.0',port=3838,launch.browser=FALSE)",
                    ]
                )
                + "\n"
            )
            tag = "localhost/shinyd-runtime-probe:" + uuid.uuid4().hex
            command(
                "podman",
                "build",
                "--network=none",
                "--pull=never",
                "--tag",
                tag,
                str(temporary),
                timeout=300,
            )
            image_id = json.loads(command("podman", "image", "inspect", tag).stdout)[0]["Id"]
            image_id = "sha256:" + image_id.removeprefix("sha256:")
            recipe.write_text(
                f"FROM {image}\nENTRYPOINT "
                + json.dumps(
                    ["Rscript", "--vanilla", "-e", "cat('PRIVATE_PROBE_OUTPUT');quit(status=21)"]
                )
                + "\n"
            )
            bad_tag = tag + "-bad"
            command(
                "podman",
                "build",
                "--network=none",
                "--pull=never",
                "--tag",
                bad_tag,
                str(temporary),
                timeout=300,
            )
            bad_id = json.loads(command("podman", "image", "inspect", bad_tag).stdout)[0]["Id"]
            policy_bytes = json.dumps(
                {
                    "schema_version": 1,
                    "recipes": {
                        "probe": image_id,
                        "failed": "sha256:" + bad_id.removeprefix("sha256:"),
                    },
                    "max_instances": 2,
                    "memory_budget_mib": 1024,
                }
            ).encode()
            with POLICY.open("xb") as file:
                file.write(policy_bytes)
            touched_service = True
            # A newly installed service may not yet be loaded by systemd.
            command("systemctl", "reset-failed", "shinyd-runtime.service", check=False)
            command("systemctl", "start", "shinyd-runtime.socket")
            views, container_ids, pids = [], [], []
            for index in range(2):
                spec = {"app_id": ids[index], "recipe": "probe", "priority": "normal"}
                created.append(ids[index])
                value = api("ensure", ids[index], spec)
                record(f"instance_{index}_ready", value["state"] == "ready", value)
                views.append(value)
                inspect = json.loads(command("podman", "inspect", "shinyd-" + ids[index]).stdout)[0]
                container_ids.append(inspect["Id"])
                pids.append(inspect["State"]["Pid"])
                again = api("ensure", ids[index], spec)
                current = json.loads(command("podman", "inspect", "shinyd-" + ids[index]).stdout)[0]
                record(
                    f"instance_{index}_idempotent",
                    again["state"] == "ready" and current["Id"] == container_ids[-1],
                )
            paths = [
                Path("/sys/fs/cgroup")
                / Path(f"/proc/{pid}/cgroup").read_text().strip().split("0::", 1)[1].lstrip("/")
                for pid in pids
            ]
            record(
                "aggregate_memory_and_app_hierarchy",
                all((path / "memory.max").read_text().strip() == "max" for path in paths)
                and all(
                    f"app{ids[index].replace('-', '')}.slice" in str(paths[index])
                    for index in range(2)
                )
                and Path("/sys/fs/cgroup/shinyd.slice/shinyd-workloads.slice/memory.max")
                .read_text()
                .strip()
                == str(1024 * 1024 * 1024),
            )
            invalid = {"app_id": ids[2], "recipe": "probe", "priority": "high", "mounts": ["/"]}
            record(
                "untyped_arguments_denied",
                api("ensure", ids[2], invalid, check=False).returncode != 0,
            )
            record(
                "server_admission_limit",
                api(
                    "ensure",
                    ids[2],
                    {k: invalid[k] for k in ("app_id", "recipe", "priority")},
                    check=False,
                ).returncode
                != 0,
            )
            record(
                "unauthorized_uid",
                command("/usr/bin/shinyd-runtimectl", "inspect", ids[0], check=False).returncode
                != 0,
            )
            namespaces = [os.readlink(f"/proc/{pid}/ns/net") for pid in pids]
            record(
                "distinct_network_namespaces",
                len(set(namespaces + [os.readlink("/proc/1/ns/net")])) == 3,
            )
            # Listener only on the host. The container's 127.0.0.1 must not reach it.
            with socket.socket() as host_listener:
                host_listener.bind(("127.0.0.1", 0))
                host_listener.listen(2)
                host_port = host_listener.getsockname()[1]
                script = (
                    "stopifnot(identical(list.files('/sys/class/net'),'lo'));"
                    "stopifnot(!file.exists('/run/shinyd-runtime/control.sock'));"
                    "stopifnot(!file.exists('/var/lib/shinyd-runtime'));"
                    "stopifnot(!file.exists('/run/podman/podman.sock'));"
                    f"x<-try(socketConnection('127.0.0.1',{host_port},open='r+',timeout=1),silent=TRUE);"
                    "stopifnot(inherits(x,'try-error'));"
                    "x<-try(socketConnection('169.254.169.254',80,open='r+',timeout=1),silent=TRUE);"
                    "stopifnot(inherits(x,'try-error'));cat('network-isolated')"
                )
                result = command(
                    "podman", "exec", "shinyd-" + ids[0], "Rscript", "--vanilla", "-e", script
                )
                record(
                    "host_metadata_and_control_blocked", result.stdout.strip() == "network-isolated"
                )
            with socket.socket() as free:
                free.bind(("127.0.0.1", 0))
                port = free.getsockname()[1]
            config = temporary / "gateway.json"
            config.write_text(
                json.dumps(
                    {
                        "token": secrets.token_urlsafe(32),
                        "routes": [
                            {
                                "host": f"{chr(97 + i)}.localhost:{port}",
                                "id": ids[i],
                                "target": views[i]["target"],
                            }
                            for i in range(2)
                        ],
                    }
                )
            )
            os.chown(config, browser_uid, grp.getgrnam("shinyd-gateway").gr_gid)
            config.chmod(0o640)
            proxy_binary = temporary / "proxy"
            shutil.copyfile(ROOT / "dist/shinyd-proxy-probe", proxy_binary)
            proxy_binary.chmod(0o755)
            proxy = subprocess.Popen(
                [
                    "runuser",
                    "-u",
                    "shinyd-control",
                    "-g",
                    "shinyd-gateway",
                    "--",
                    str(proxy_binary),
                    "--config",
                    str(config),
                    "--bind",
                    f"127.0.0.1:{port}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            browser = subprocess.Popen(
                [
                    "runuser",
                    "-u",
                    browser_user.pw_name,
                    "--",
                    sys.executable,
                    str(ROOT / "scripts/runtime_browser_probe.py"),
                    "--config",
                    str(config),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(browser.stdout, selectors.EVENT_READ)
                for stage in ("stop_first", "restart_executor", "passed"):
                    if not selector.select(timeout=60):
                        raise RuntimeError("Navegador excedeu o prazo.")
                    line = browser.stdout.readline()
                    if not line:
                        raise RuntimeError("Navegador encerrou antes de concluir a verificação.")
                    result = json.loads(line)
                    record("browser_" + stage, result.get("stage") == stage, result.get("observed"))
                    if stage == "stop_first":
                        record("stop_one_instance", api("stop", ids[0])["state"] == "stopped")
                        browser.stdin.write("stopped\n")
                        browser.stdin.flush()
                    elif stage == "restart_executor":
                        command("systemctl", "restart", "shinyd-runtime.service", timeout=80)
                        value = api("inspect", ids[1])
                        current = json.loads(
                            command("podman", "inspect", "shinyd-" + ids[1]).stdout
                        )[0]
                        record(
                            "restart_without_duplicate",
                            value["state"] == "ready" and current["Id"] == container_ids[1],
                        )
                        browser.stdin.write("restarted\n")
                        browser.stdin.flush()
            browser.wait(timeout=15)
            created.append(ids[3])
            failed = {"app_id": ids[3], "recipe": "failed", "priority": "low"}
            for _ in range(3):
                value = api("ensure", ids[3], failed)
                if value["state"] != "failed":
                    raise RuntimeError("Inicialização malsucedida não foi identificada.")
            record(
                "failed_start_is_bounded",
                api("ensure", ids[3], failed, check=False).returncode != 0,
            )
            record("failure_preserves_other_app", api("inspect", ids[1])["state"] == "ready")
            logs = command(
                "journalctl",
                "--unit=shinyd-runtime",
                "--since",
                report["observed_at"],
                "--output=cat",
                "--no-pager",
            ).stdout
            record("no_container_output_in_audit", "PRIVATE_PROBE_OUTPUT" not in logs)
            report["fixture_image"] = image_id
        except KeyboardInterrupt:
            report["error"] = "Homologação interrompida."
        except Exception as exc:
            report["error"] = str(exc)
        finally:
            stop_process(browser)
            stop_process(proxy)
            if touched_service:
                if "error" in report:
                    # This dedicated service logs fixed errors and audit metadata only.
                    # Never collect application/container output into the report.
                    report["executor_diagnostics"] = command(
                        "journalctl",
                        "--unit=shinyd-runtime",
                        "--since",
                        report["observed_at"],
                        "--output=cat",
                        "--no-pager",
                        "--lines=40",
                        check=False,
                    ).stdout
                cleanup_errors = []
                for instance in created:
                    try:
                        api("remove", instance)
                    except Exception:
                        cleanup_errors.append(instance)
                if cleanup_errors:
                    report["cleanup_error"] = {"instances": cleanup_errors}
                else:
                    command(
                        "systemctl",
                        "stop",
                        "shinyd-runtime.service",
                        "shinyd-runtime.socket",
                        check=False,
                        timeout=80,
                    )
                    if POLICY.exists() and POLICY.read_bytes() == policy_bytes:
                        POLICY.unlink()
    report["passed"] = "error" not in report and "cleanup_error" not in report
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
