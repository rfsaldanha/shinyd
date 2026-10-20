#!/usr/bin/env python3
"""Verify the installed offline builder with trusted fixtures on an empty test installation."""

import argparse
import fcntl
import hashlib
import json
import os
import platform
import pwd
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from runtime_smoke import API_RETRIES, api, command

ROOT = Path(__file__).resolve().parents[1]
POLICY = Path("/etc/shinyd-builder/policy.json")
STATE = Path("/var/lib/shinyd-builder")
RUNTIME_POLICY = Path("/etc/shinyd-runtime/policy.json")


def prepare_panel_python(directory, report):
    """Use the invoking pip to populate an isolated venv, without ensurepip."""
    interpreter = directory / "bin/python"
    steps = (
        ("venv", 60, (sys.executable, "-m", "venv", "--without-pip", str(directory))),
        (
            "dependencies",
            180,
            (
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "--python",
                str(interpreter),
                "install",
                "--no-input",
                "-r",
                str(ROOT / "requirements.lock"),
            ),
        ),
    )
    for stage, timeout, args in steps:
        diagnostic = {"stage": stage, "passed": False}
        report["panel_setup"] = diagnostic
        try:
            result = command(*args, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            diagnostic["error"] = "timeout"
            raise RuntimeError(f"Preparação Python do painel excedeu o prazo: {stage}.") from None
        if result.returncode:
            diagnostic["exit_code"] = result.returncode
            # pip output can contain authenticated index URLs. Never include it.
            raise RuntimeError(f"Preparação Python do painel falhou: {stage}.")
        diagnostic["passed"] = True
    return interpreter


def prepare_environment(directory, reference):
    """Operator fixture preparation, never a recipe submitted by a publisher."""
    pinned = json.loads((ROOT / "experiments/renv-package.json").read_text())
    package = directory / "renv.tar.gz"
    with urllib.request.urlopen(pinned["url"], timeout=45) as response:
        data = response.read(5 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != pinned["sha256"]:
        raise RuntimeError("Checksum do renv não corresponde ao arquivo fixado.")
    package.write_bytes(data)
    command("podman", "pull", reference["image"], timeout=600)
    tag = "localhost/shinyd-builder-base:" + uuid.uuid4().hex
    (directory / "Containerfile").write_text(
        f"FROM {reference['image']}\nCOPY renv.tar.gz /tmp/renv.tar.gz\n"
        "RUN R CMD INSTALL /tmp/renv.tar.gz && rm /tmp/renv.tar.gz\n"
    )
    command(
        "podman",
        "build",
        "--network=none",
        "--pull=never",
        "--tag",
        tag,
        str(directory),
        timeout=300,
    )
    image = json.loads(command("podman", "image", "inspect", tag).stdout)[0]["Id"]
    image = "sha256:" + image.removeprefix("sha256:")
    script = (
        "options(repos=c(CRAN='https://cloud.r-project.org'));"
        "p<-installed.packages();"
        "dir.create('/tmp/project');setwd('/tmp/project');"
        "invisible(capture.output(renv::snapshot(project='/tmp/project',"
        "library=.libPaths()[1],lockfile='/tmp/project/renv.lock',type='all',prompt=FALSE)));l<-jsonlite::read_json('/tmp/project/renv.lock');"
        "cat(jsonlite::toJSON(list(r_version=as.character(getRversion()),"
        "packages=as.list(setNames(p[,'Version'],p[,'Package'])),lock=l),auto_unbox=TRUE))"
    )
    observed = json.loads(
        command(
            "podman",
            "run",
            "--rm",
            "--network=none",
            "--user=65534:65534",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--env=HOME=/tmp",
            "--env=RENV_CONFIG_SANDBOX_ENABLED=FALSE",
            "--env=RENV_CONFIG_AUTOLOADER_ENABLED=FALSE",
            "--tmpfs=/tmp:rw,size=128m,mode=1777",
            "--entrypoint=Rscript",
            image,
            "--vanilla",
            "-e",
            script,
            timeout=90,
        ).stdout
    )
    if observed["r_version"] != reference["r"]:
        raise RuntimeError("Versão R da fixture não confere.")
    return {
        "image": image,
        "r_version": observed["r_version"],
        "packages": observed["packages"],
    }, observed["lock"]


def zip_fixture(path, lock, *, bad_path=False):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project/app.R", (ROOT / "experiments/session-fixture/app.R").read_bytes())
        archive.writestr("project/renv.lock", json.dumps(lock))
        archive.writestr("project/.Rprofile", "stop('PRIVATE_BUILD_SENTINEL')")
        archive.writestr("project/Containerfile", "FROM scratch\nRUN exit 77\n")
        archive.writestr("project/.containerignore", "*\n")
        if bad_path:
            archive.writestr("../escape", "PRIVATE_BUILD_SENTINEL")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(path, checksum, app_id, environment="r-a"):
    result = command(
        "/usr/bin/shinyd-build",
        "--environment",
        environment,
        "--app-id",
        app_id,
        "--sha256",
        checksum,
        str(path),
        check=False,
        timeout=600,
    )
    if "PRIVATE_BUILD_SENTINEL" in result.stdout + result.stderr:
        raise RuntimeError("Conteúdo privado apareceu na saída do builder.")
    try:
        value = json.loads(result.stdout)
    except ValueError:
        # Only platform diagnostics, never print process stdout/stderr blindly.
        raise RuntimeError("Builder não retornou o contrato JSON.") from None
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-package", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not os.environ.get("SUDO_UID"):
        parser.error("Execute com sudo a partir de uma conta comum.")
    os.umask(0o077)
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "Builder offline ZIP/renv, imagens imutáveis e integração com executor",
        "production_ready": False,
        "tests": [],
        "environment": {
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "memory_bytes": int(
                next(
                    line
                    for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemTotal:")
                ).split()[1]
            )
            * 1024,
            "profile": ("Dois apps, builds offline e operações reativas; sem teste de capacidade."),
        },
        "limitations": [
            "Não ativa a publicação pelo painel nem baixa pacotes externos.",
            "Não homologa reboot, capacidade, QoS sob carga ou quotas de disco.",
        ],
    }

    def record(name, passed, detail=None):
        report["tests"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            raise RuntimeError("Verificação falhou: " + name)

    own_policy = own_runtime = None
    previous_limits = None
    created = []
    artifacts = []
    panel_state = None
    panel_jobs = []
    builder_started = False
    ids = [str(uuid.uuid4()) for _ in range(2)]
    with tempfile.TemporaryDirectory(prefix="shinyd-builder-probe-") as temporary:
        temporary = Path(temporary)
        try:
            record(
                "empty_test_installation",
                not POLICY.exists()
                and not RUNTIME_POLICY.exists()
                and not list(Path("/var/lib/shinyd-runtime").glob("*.json"))
                and not (STATE / "current.json").exists()
                and not list((STATE / "artifacts").glob("*.json")),
            )
            package = args.install_package.resolve()
            checksum = hashlib.sha256(package.read_bytes()).hexdigest()
            record(
                "package_checksum",
                checksum == package.with_suffix(".deb.sha256").read_text().split()[0],
                checksum,
            )
            identity = command(
                "dpkg-deb", "--show", "--showformat=${Package} ${Version}", str(package)
            ).stdout
            record("package_identity", identity == "shinyd-builder 0.1.0~builder2", identity)
            command("dpkg", "--install", str(package), timeout=120)
            # Bootstrap before expensive container builds, using disposable state.
            # /var/lib/shinyd belongs to the diagnostic agent (root:root 0750).
            panel_state = Path("/var/lib") / ("shinyd-builder-probe-" + uuid.uuid4().hex)
            control = pwd.getpwnam("shinyd-control")
            panel_state.mkdir(mode=0o700)
            os.chown(panel_state, control.pw_uid, control.pw_gid)
            interpreter = prepare_panel_python(panel_state / "venv", report)
            report["environment"]["podman"] = json.loads(
                command("podman", "version", "--format=json").stdout
            )["Client"]["Version"]
            # Snapshot and restore only these temporary aggregate settings.
            previous_limits = dict(
                line.split("=", 1)
                for line in command(
                    "systemctl",
                    "show",
                    "shinyd-workloads.slice",
                    "--property=MemoryHigh,MemoryMax,MemorySwapMax",
                ).stdout.splitlines()
            )
            command(
                "systemctl",
                "set-property",
                "--runtime",
                "shinyd-workloads.slice",
                "MemoryHigh=3584M",
                "MemoryMax=4G",
                "MemorySwapMax=0",
            )
            environments, locks = {}, []
            references = json.loads((ROOT / "experiments/r-images.json").read_text())["images"]
            for index, reference in enumerate(references):
                directory = temporary / f"base-{index}"
                directory.mkdir()
                environment, lock = prepare_environment(directory, reference)
                environments["r-" + chr(97 + index)] = environment
                locks.append(lock)
            own_policy = json.dumps({"schema_version": 1, "environments": environments}).encode()
            with POLICY.open("xb") as output:
                output.write(own_policy)
            results = []
            for index, lock in enumerate(locks):
                path = temporary / f"app-{index}.zip"
                source_hash = zip_fixture(path, lock)
                value = invoke(path, source_hash, ids[index], "r-" + chr(97 + index))
                record(
                    f"build_{index}_prepared",
                    value.get("passed"),
                    value if not value.get("passed") else None,
                )
                artifact = value["artifact"]
                artifacts.append(artifact["cache_key"])
                results.append(artifact)
                report["artifacts"] = results
                record(
                    f"build_{index}_isolation",
                    artifact["protection"]["maintenance"] and artifact["protection"]["mapped_uid"],
                    artifact["protection"],
                )
                repeated = invoke(path, source_hash, ids[index], "r-" + chr(97 + index))
                record(
                    f"build_{index}_cache",
                    repeated.get("passed")
                    and repeated["artifact"]["cache_hit"]
                    and repeated["artifact"]["image"] == artifact["image"],
                    repeated if not repeated.get("passed") else None,
                )
            record(
                "different_r_and_packages",
                results[0]["r_version"] != results[1]["r_version"]
                and results[0]["packages"] != results[1]["packages"],
                [
                    {
                        "r": item["r_version"],
                        "shiny": item["packages"]["shiny"],
                        "image": item["image"],
                    }
                    for item in results
                ],
            )
            own_runtime = json.dumps(
                {
                    "schema_version": 1,
                    "recipes": {f"built-{i}": value["image"] for i, value in enumerate(results)},
                    "max_instances": 2,
                    "memory_budget_mib": 4096,
                }
            ).encode()
            with RUNTIME_POLICY.open("xb") as output:
                output.write(own_runtime)
            command("systemctl", "reset-failed", "shinyd-runtime.service", check=False)
            command("systemctl", "start", "shinyd-runtime.socket")
            for index in range(2):
                created.append(ids[index])
                value = api(
                    "ensure",
                    ids[index],
                    {"app_id": ids[index], "recipe": f"built-{index}", "priority": "normal"},
                )
                record(f"artifact_{index}_ready", value["state"] == "ready", value)
            # Exercise the actual image over the executor's Unix transport and Shiny WS.
            browser = command(
                sys.executable,
                str(ROOT / "scripts/builder_browser_probe.py"),
                "--instances",
                *ids,
                timeout=120,
            )
            record("browser_reactivity_upload_download", json.loads(browser.stdout)["passed"])
            processes = [
                json.loads(command("podman", "inspect", "shinyd-" + identity).stdout)[0]["State"][
                    "Pid"
                ]
                for identity in ids
            ]
            source = temporary / "app-0.zip"
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            with (STATE / ".lock").open("rb") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = invoke(source, source_hash, ids[0])
                record("concurrent_build_refused", busy.get("error") == "busy")
            mismatch = invoke(source, "0" * 64, ids[0])
            record("source_checksum_mismatch", mismatch.get("error") == "source_changed")
            denied = command(
                "runuser",
                "-u",
                pwd.getpwuid(int(os.environ["SUDO_UID"])).pw_name,
                "--",
                "/usr/bin/shinyd-build",
                check=False,
            )
            record("unprivileged_builder_refused", denied.returncode != 0)
            # Simulate durable state left by an interrupted builder. Do not kill
            # unrelated processes or claim that this is a reboot test.
            orphan = str(uuid.uuid4())
            job = {
                "id": orphan,
                "key": results[0]["cache_key"],
                "input_image": environments["r-a"]["image"],
            }
            (STATE / ("work-" + orphan)).mkdir(mode=0o700)
            with (STATE / "current.json").open("x") as output:
                json.dump(job, output)
            command(
                "podman",
                "run",
                "--detach",
                "--name=shinyd-build-" + orphan,
                "--label=org.shinyd.managed=builder-v1",
                "--label=org.shinyd.build=" + orphan,
                "--label=org.shinyd.key=" + job["key"],
                "--network=none",
                "--userns=auto:size=65536",
                "--user=65534:65534",
                "--read-only",
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                "--image-volume=ignore",
                "--pids-limit=128",
                "--cgroup-parent=shinyd-workloads-maintenance.slice",
                "--entrypoint=/bin/sleep",
                job["input_image"],
                "120",
            )
            maintenance = Path(
                "/sys/fs/cgroup/shinyd.slice/shinyd-workloads.slice/shinyd-workloads-maintenance.slice"
            )
            record(
                "maintenance_weight_and_memory",
                (maintenance / "cpu.weight").read_text().strip() == "50"
                and (maintenance / "memory.max").read_text().strip() == str(1024**3),
            )
            recovered = invoke(source, source_hash, ids[0])
            record(
                "recover_recorded_orphan",
                recovered.get("passed")
                and recovered["artifact"]["cache_hit"]
                and not (STATE / "current.json").exists()
                and command(
                    "podman", "container", "exists", "shinyd-build-" + orphan, check=False
                ).returncode
                == 1,
            )
            snapshot = [(STATE / "artifacts" / (key + ".json")).read_bytes() for key in artifacts]
            failures = [
                ("invalid_zip", locks[0], True, "archive"),
                (
                    "wrong_r",
                    {**locks[0], "R": {**locks[0]["R"], "Version": "0.0.0"}},
                    False,
                    "r_unavailable",
                ),
            ]
            bad_lock = json.loads(json.dumps(locks[0]))
            bad_lock["Packages"]["shiny"]["Version"] = "99.0.0"
            failures.append(("missing_package", bad_lock, False, "package_unavailable"))
            bad_hash = json.loads(json.dumps(locks[0]))
            bad_hash["Packages"]["shiny"]["Hash"] = "0" * 32
            failures.append(("restore_failure", bad_hash, False, "restore"))
            for name, lock, bad_path, expected in failures:
                path = temporary / (name + ".zip")
                sha = zip_fixture(path, lock, bad_path=bad_path)
                result = invoke(path, sha, ids[0])
                record(
                    name, result.get("passed") is False and result.get("error") == expected, result
                )
            record(
                "failure_preserves_artifacts_and_apps",
                all(
                    (STATE / "artifacts" / (key + ".json")).read_bytes() == snapshot[i]
                    for i, key in enumerate(artifacts)
                )
                and all(api("inspect", identity)["state"] == "ready" for identity in ids),
            )
            record(
                "failure_preserves_r_processes",
                all(
                    json.loads(command("podman", "inspect", "shinyd-" + identity).stdout)[0][
                        "State"
                    ]["Pid"]
                    == processes[index]
                    for index, identity in enumerate(ids)
                ),
            )
            record(
                "no_pending_build",
                not (STATE / "current.json").exists() and not list(STATE.glob("work-*")),
            )
            # This separate database never opens the developer's panel state.
            record("empty_api_jobs", not any(p.is_dir() for p in (STATE / "jobs").iterdir()))
            shutil.copyfile(temporary / "app-0.zip", panel_state / "fixture.zip")
            os.chown(panel_state / "fixture.zip", control.pw_uid, control.pw_gid)
            # A developer/runner home can be 0750. Never weaken its permissions
            # to let the service account import the panel or its dependencies.
            project = panel_state / "project"
            project.mkdir()
            shutil.copytree(
                ROOT / "backend", project / "backend", ignore=shutil.ignore_patterns("__pycache__")
            )
            (project / "scripts").mkdir()
            for name in ("bootstrap.py", "builder_panel_probe.py"):
                shutil.copyfile(ROOT / "scripts" / name, project / "scripts" / name)
            for path in panel_state.rglob("*"):
                os.chown(path, control.pw_uid, control.pw_gid, follow_symlinks=False)
            command("systemctl", "start", "shinyd-builder.socket")
            builder_started = True
            probe = [
                "runuser",
                "-u",
                "shinyd-control",
                "--",
                str(interpreter),
                str(project / "scripts/builder_panel_probe.py"),
                "--state",
                str(panel_state),
            ]

            def run_panel(stage, timeout):
                value = command(*probe, "--stage", stage, timeout=timeout, check=False)
                try:
                    result = json.loads(value.stdout)
                except ValueError:
                    result = {"passed": False, "stage": stage, "exit_code": value.returncode}
                if value.returncode or not result.get("passed"):
                    report["panel_diagnostic"] = result
                    raise RuntimeError("Falha no roteiro isolado do painel: " + stage)
                return result

            submitted = run_panel("submit", 90)
            panel_jobs.append(submitted["job_id"])
            record("api_submission_from_control_identity", submitted["passed"])
            command("systemctl", "restart", "shinyd-builder.service")
            completed = run_panel("complete", 660)
            panel_jobs = completed["job_ids"]
            artifacts.append(completed["artifact"]["cache_key"])
            for name in completed["checks"]:
                record(name, completed["passed"])
            report["panel_artifact"] = completed["artifact"]
            report["artifacts"] = results
        except Exception as exc:
            report["error"] = str(exc)
            # Only repository-owned fixtures, never uploads or app environments.
            report["fixture_diagnostics"] = []
            for instance in created:
                inspected = command("podman", "inspect", "shinyd-" + instance, check=False)
                if inspected.returncode == 0:
                    metadata = json.loads(inspected.stdout)[0]
                    diagnostic = {"instance": instance, "state": metadata["State"]}
                    diagnostic["fixture_log"] = command(
                        "podman", "logs", "--tail=10", "shinyd-" + instance, check=False
                    ).stderr[-3000:]
                    if metadata["State"]["Status"] == "created":
                        retry = command("podman", "start", "shinyd-" + instance, check=False)
                        diagnostic["diagnostic_start_code"] = retry.returncode
                        diagnostic["diagnostic_start_error"] = retry.stderr[-3000:]
                    report["fixture_diagnostics"].append(diagnostic)
        finally:
            cleanup_errors = []
            if builder_started:
                # Namespace was verified empty before this fixture used it.
                panel_jobs = [p.name for p in (STATE / "jobs").iterdir() if p.is_dir()]
                for job_id in panel_jobs:
                    command("systemctl", "stop", f"shinyd-build@{job_id}.service", check=False)
                    record_path = STATE / "jobs" / job_id / "record.json"
                    if record_path.exists():
                        value = json.loads(record_path.read_text())
                        if value.get("artifact"):
                            key = value["artifact"]["cache_key"]
                            if key not in artifacts:
                                artifacts.append(key)
                stopped = command(
                    "systemctl",
                    "stop",
                    "shinyd-builder.service",
                    "shinyd-builder.socket",
                    check=False,
                )
                if stopped.returncode:
                    cleanup_errors.append("builder_api_stop")
                elif not (STATE / "current.json").exists():
                    for job_id in panel_jobs:
                        shutil.rmtree(STATE / "jobs" / job_id)
                else:
                    cleanup_errors.append("pending_api_build")
            if panel_state is not None:
                shutil.rmtree(panel_state)
            for instance in created:
                try:
                    api("remove", instance)
                except Exception as exc:
                    cleanup_errors.append("instance:" + instance + ":" + str(exc))
            if own_runtime is not None and not cleanup_errors:
                stopped = command(
                    "systemctl",
                    "stop",
                    "shinyd-runtime.service",
                    "shinyd-runtime.socket",
                    check=False,
                    timeout=90,
                )
                if stopped.returncode:
                    cleanup_errors.append("runtime_stop")
                elif RUNTIME_POLICY.exists() and RUNTIME_POLICY.read_bytes() == own_runtime:
                    RUNTIME_POLICY.unlink()
            if (STATE / "current.json").exists():
                cleanup_errors.append("pending_build")
            if not cleanup_errors:
                if own_policy is not None and POLICY.exists() and POLICY.read_bytes() == own_policy:
                    POLICY.unlink()
                for key in artifacts:
                    (STATE / "artifacts" / (key + ".json")).unlink(missing_ok=True)
                if previous_limits is not None:
                    result = command(
                        "systemctl",
                        "set-property",
                        "--runtime",
                        "shinyd-workloads.slice",
                        *[f"{key}={value}" for key, value in previous_limits.items()],
                        check=False,
                    )
                    if result.returncode:
                        cleanup_errors.append("restore_budget")
            if cleanup_errors:
                report["cleanup_error"] = cleanup_errors
    report["runtime_api_retries"] = API_RETRIES
    report["passed"] = "error" not in report and "cleanup_error" not in report
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
