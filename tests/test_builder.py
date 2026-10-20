import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from builder import build, podman, policy
from tests.test_archives import archive_bytes


@pytest.fixture
def environment():
    return {
        "image": "sha256:" + "a" * 64,
        "r_version": "4.4.3",
        "packages": {"shiny": "1.10.0", "renv": "1.1.5", "jsonlite": "2.0.0"},
    }


@pytest.fixture
def lock():
    return {
        "R": {"Version": "4.4.3"},
        "Packages": {
            "shiny": {
                "Package": "shiny",
                "Version": "1.10.0",
                "Source": "Repository",
                "Repository": "CRAN",
            }
        },
    }


def resolve(tmp_path, lock, environment):
    path = tmp_path / "renv.lock"
    path.write_text(json.dumps(lock))
    return policy.resolve_lock(path, environment)


def test_resolver_exact_version_and_sources(tmp_path, lock, environment):
    resolved = resolve(tmp_path, lock, environment)
    assert resolved["Packages"] == lock["Packages"]
    assert resolved["R"]["Repositories"] == [{"Name": "CRAN", "URL": "https://cloud.r-project.org"}]
    lock["R"]["Version"] = "4.5.2"
    with pytest.raises(policy.BuildError, match="r_unavailable"):
        resolve(tmp_path, lock, environment)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Source", "GitHub"),
        ("Repository", "https://user:secret@host"),
        ("RemoteUrl", "file:///etc/passwd"),
        ("Package", "../shiny"),
        ("Hash", "../cache"),
        ("Version", ["1.0"]),
        ("Requirements", [None]),
    ],
)
def test_resolver_refuses_remote_and_malformed_records(tmp_path, lock, environment, field, value):
    lock["Packages"]["shiny"][field] = value
    with pytest.raises(policy.BuildError, match="lock_invalid"):
        resolve(tmp_path, lock, environment)


def test_absent_package_and_no_lock_are_explicit(tmp_path, lock, environment):
    with pytest.raises(policy.BuildError, match="lock_required"):
        policy.resolve_lock(tmp_path / "renv.lock", environment)
    lock["Packages"]["shiny"]["Version"] = "99.0"
    with pytest.raises(policy.BuildError, match="package_unavailable"):
        resolve(tmp_path, lock, environment)


def test_duplicate_lock_keys_refused(tmp_path, environment):
    path = tmp_path / "renv.lock"
    path.write_text('{"R":{"Version":"4.4.3","Version":"4.5.2"}}')
    with pytest.raises(policy.BuildError, match="lock_invalid"):
        policy.resolve_lock(path, environment)


def test_policy_no_arbitrary_fields_or_mutable_tags(tmp_path, monkeypatch, environment):
    path = tmp_path / "policy.json"
    data = {"schema_version": 1, "environments": {"approved": environment}}
    monkeypatch.setattr(policy, "secure_dir", lambda *_: None)
    original = policy.private_read
    monkeypatch.setattr(policy, "private_read", lambda p: original(p, owner=os.getuid()))
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    assert policy.load_environment(path, "approved") == environment
    environment["image"] = "rocker/shiny:latest"
    path.write_text(json.dumps(data))
    with pytest.raises(policy.BuildError, match="policy"):
        policy.load_environment(path, "approved")
    environment["image"] = "sha256:" + "a" * 64
    environment["volumes"] = ["/"]
    path.write_text(json.dumps(data))
    with pytest.raises(policy.BuildError, match="policy"):
        policy.load_environment(path, "approved")


def test_privileged_files_refuse_links_hardlinks_and_group_writes(tmp_path):
    file = tmp_path / "file"
    file.write_text("{}")
    file.chmod(0o600)
    assert policy.private_read(file, owner=os.getuid()) == b"{}"
    link = tmp_path / "link"
    link.symlink_to(file)
    with pytest.raises(OSError):
        policy.private_read(link, owner=os.getuid())
    os.link(file, tmp_path / "hardlink")
    with pytest.raises(policy.BuildError):
        policy.private_read(file, owner=os.getuid())
    (tmp_path / "hardlink").unlink()
    file.chmod(0o660)
    with pytest.raises(policy.BuildError):
        policy.private_read(file, owner=os.getuid())


def test_snapshot_hash_and_bounds(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"uploaded bytes")
    with pytest.raises(policy.BuildError, match="source_changed"):
        build.snapshot(source, tmp_path / "copy1", "0" * 64)
    monkeypatch.setattr(build, "SOURCE_LIMIT", 2)
    with pytest.raises(policy.BuildError, match="archive"):
        build.snapshot(source, tmp_path / "copy2", "0" * 64)
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        build.snapshot(link, tmp_path / "copy3", "0" * 64)


def test_cache_key_separates_apps_sources_environments_and_helpers(
    lock, environment, monkeypatch, tmp_path
):
    manifest = {"source_hash": "1" * 64, "entrypoint": "."}
    original = build.artifact_key("app1", manifest, environment, lock)
    assert original != build.artifact_key("app2", manifest, environment, lock)
    assert original != build.artifact_key(
        "app1", {**manifest, "entrypoint": "sub"}, environment, lock
    )
    assert original != build.artifact_key("app1", manifest, {**environment, "image": "b"}, lock)
    (tmp_path / "prepare.R").write_text("changed")
    (tmp_path / "launch.R").write_text("changed")
    monkeypatch.setattr(build, "HELPERS", tmp_path)
    assert original != build.artifact_key("app1", manifest, environment, lock)


def test_generated_recipe_never_executes_uploaded_instructions(tmp_path, lock, environment):
    selected = tmp_path / "uploaded"
    selected.mkdir()
    (selected / "app.R").write_text("stop('PRIVATE_UPLOAD')")
    (selected / "Containerfile").write_text("RUN touch /host-pwned")
    (selected / ".containerignore").write_text("*")
    context = tmp_path / "context"
    build.write_context(context, selected, lock, environment, "uuid", "key")
    recipe = (context / "Containerfile").read_text()
    assert "RUN " not in recipe and "ADD " not in recipe and "host-pwned" not in recipe
    assert (context / "ignore").read_text() == ""
    assert (context / "source/app.R").stat().st_mode & 0o222 == 0
    assert (context / "source").stat().st_mode & 0o222 == 0
    (context / "source").chmod(0o755)  # pytest cleanup runs without root.


def test_failed_preflight_does_not_touch_runtime_or_publication(tmp_path, environment, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    source = tmp_path / "input.zip"
    source.write_bytes(archive_bytes([("app.R", "secret"), ("../escape", "bad")]))
    monkeypatch.setattr(podman, "command", lambda *_args, **_kwargs: pytest.fail("runtime called"))
    active = tmp_path / "active-publication"
    active.write_text("keep")
    with pytest.raises(policy.BuildError, match="archive"):
        build.Builder(state)._build(
            source, hashlib.sha256(source.read_bytes()).hexdigest(), "app", "", environment
        )
    assert active.read_text() == "keep"
    assert list(state.iterdir()) == []


def test_durable_artifact_save_is_complete_and_private(tmp_path):
    target = tmp_path / "artifact.json"
    build.save(target, {"image": "sha256:example"})
    assert json.loads(target.read_text()) == {"image": "sha256:example"}
    assert target.stat().st_mode & 0o077 == 0
    assert not list(tmp_path.glob(".tmp-*"))


def test_recovery_does_not_delete_foreign_container(tmp_path, monkeypatch):
    state = tmp_path
    job = {
        "id": "11111111-1111-1111-1111-111111111111",
        "key": "a" * 64,
        "input_image": "sha256:" + "b" * 64,
    }
    (state / "current.json").write_text(json.dumps(job))
    monkeypatch.setattr(build, "private_read", lambda path: path.read_bytes())
    monkeypatch.setattr(podman, "remove_owned", lambda _: policy.fail("ownership"))
    with pytest.raises(policy.BuildError, match="ownership"):
        build.Builder(state).recover()
    assert (state / "current.json").exists()


def test_cleanup_failure_preserves_intent(tmp_path, monkeypatch):
    job = {"id": "11111111-1111-1111-1111-111111111111"}
    (tmp_path / "current.json").write_text("durable intent")
    monkeypatch.setattr(podman, "remove_owned", lambda _: policy.fail("cleanup"))
    with pytest.raises(policy.BuildError, match="cleanup"):
        build.Builder(tmp_path).cleanup(job)
    assert (tmp_path / "current.json").read_text() == "durable intent"


def test_container_ownership_checks_precede_removal(monkeypatch):
    calls = []
    job = {"id": "id", "key": "key", "input_image": "sha256:" + "a" * 64}
    metadata = {"Config": {"Labels": {"org.shinyd.managed": "someone-else"}}, "Image": "a" * 64}

    def run(args, **kwargs):
        calls.append(args)
        return json.dumps([{}] if args[0] == "ps" else [metadata]).encode()

    monkeypatch.setattr(podman, "command", run)
    with pytest.raises(policy.BuildError, match="ownership"):
        podman.remove_owned(job)
    assert all(args[0] != "rm" for args in calls)


def test_cache_hit_requires_managed_image_and_key(tmp_path, monkeypatch):
    (tmp_path / "artifacts").mkdir()
    value = {"cache_key": "a" * 64, "image": "sha256:" + "b" * 64}
    (tmp_path / "artifacts" / (value["cache_key"] + ".json")).write_text(json.dumps(value))
    monkeypatch.setattr(build, "private_read", lambda path: path.read_bytes())
    monkeypatch.setattr(podman, "inspect_image", lambda _: {"Labels": {}})
    with pytest.raises(policy.BuildError, match="ownership"):
        build.Builder(tmp_path).cached(value["cache_key"])
    monkeypatch.setattr(
        podman,
        "inspect_image",
        lambda _: {"Labels": {"org.shinyd.key": value["cache_key"], "org.shinyd.artifact": "v1"}},
    )
    assert build.Builder(tmp_path).cached(value["cache_key"])["cache_hit"] is True


@pytest.mark.parametrize("change", ["profile", "mapping", "caps", "cgroup", "network"])
def test_kernel_protection_checks_fail_closed(monkeypatch, change):
    import types

    value = {
        "Id": "a" * 64,
        "State": {"Running": True, "Pid": 1234},
        "AppArmorProfile": "containers-default-test",
    }
    data = {
        "/proc/1234/status": (
            "NoNewPrivs:\t1\nSeccomp:\t2\n"
            "CapEff:\t0000000000000000\n"
            "CapPrm:\t0000000000000000\n"
            "CapInh:\t0000000000000000\n"
            "CapAmb:\t0000000000000000\n"
            "Uid:\t165534 165534 165534 165534\n"
            "Gid:\t165534 165534 165534 165534\n"
        ),
        "/proc/1234/uid_map": "0 100000 65536",
        "/proc/1234/gid_map": "0 100000 65536",
        "/proc/1234/attr/apparmor/current": "containers-default-test//&crun (mixed)",
        "/sys/kernel/security/apparmor/profiles": (
            "containers-default-test (enforce)\ncrun (complain)\n"
        ),
        "/proc/1234/cgroup": "0::/shinyd-workloads-maintenance.slice/libpod-"
        + "a" * 64
        + ".scope\n",
    }
    monkeypatch.setattr(Path, "read_text", lambda self: data[str(self)])
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self: types.SimpleNamespace(st_ino=1 if str(self) == "/proc/1/ns/net" else 2),
    )
    assert podman.verify_process(value)["maintenance"] is True
    if change == "profile":
        data["/sys/kernel/security/apparmor/profiles"] = "containers-default-test (complain)"
    elif change == "mapping":
        data["/proc/1234/uid_map"] = "0 0 65536"
    elif change == "caps":
        data["/proc/1234/status"] = data["/proc/1234/status"].replace(
            "CapEff:\t0000000000000000", "CapEff:\t0000000000000001"
        )
    elif change == "cgroup":
        data["/proc/1234/cgroup"] = "0::/system.slice/other.scope"
    else:
        monkeypatch.setattr(Path, "stat", lambda self: types.SimpleNamespace(st_ino=1))
    with pytest.raises(policy.BuildError, match="isolation"):
        podman.verify_process(copy.deepcopy(value))


def test_renv_full_description_metadata_is_not_used_as_code(tmp_path, lock, environment):
    lock["Packages"]["shiny"].update(
        {
            "Authors@R": "system('malicious command')",
            "Title": "Full DESCRIPTION record",
            "Repository": "RSPM",
            "OS_type": "unix",
        }
    )
    resolved = resolve(tmp_path, lock, environment)
    assert set(resolved["Packages"]["shiny"]) == {"Package", "Version", "Source", "Repository"}
    assert resolved["R"]["Repositories"][0]["Name"] == "RSPM"


@pytest.mark.parametrize(
    "problem", ["host_memory", "unconfigured_budget", "aggregate_full", "pressure", "disk"]
)
def test_resource_admission_refuses_unsafe_conditions(tmp_path, monkeypatch, problem):
    import types

    gib = 1024**3
    monkeypatch.setattr(podman, "memory_available", lambda: (16 * gib, 8 * gib))
    files = {
        str(podman.WORKLOADS / "memory.max"): str(4 * gib),
        str(podman.WORKLOADS / "memory.current"): str(gib),
        "/proc/pressure/memory": (
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        ),
    }
    monkeypatch.setattr(Path, "read_text", lambda self: files[str(self)])
    monkeypatch.setattr(
        os, "statvfs", lambda _: types.SimpleNamespace(f_bavail=10 * gib, f_frsize=1)
    )
    podman.resources_available(tmp_path, starting=True)
    if problem == "host_memory":
        monkeypatch.setattr(podman, "memory_available", lambda: (16 * gib, gib))
    elif problem == "unconfigured_budget":
        files[str(podman.WORKLOADS / "memory.max")] = "max"
    elif problem == "aggregate_full":
        files[str(podman.WORKLOADS / "memory.current")] = str(4 * gib)
    elif problem == "pressure":
        files["/proc/pressure/memory"] = "full avg10=1.01 avg60=0.00 avg300=0.00 total=0\n"
    else:
        monkeypatch.setattr(
            os, "statvfs", lambda _: types.SimpleNamespace(f_bavail=gib, f_frsize=1)
        )
    with pytest.raises(policy.BuildError, match="admission"):
        podman.resources_available(tmp_path, starting=True)


def test_command_failure_does_not_expose_output_or_host_environment(tmp_path, monkeypatch):
    executable = tmp_path / "runtime"
    executable.write_text(
        "#!/bin/sh\nprintf 'PRIVATE_PACKAGE_OUTPUT'\nprintf 'PRIVATE_STDERR' >&2\nexit 5\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(podman, "PODMAN", str(executable))
    with pytest.raises(policy.BuildError, match="^restore$") as error:
        podman.command(["exec"], code="restore")
    assert "PRIVATE" not in str(error.value)
    executable.write_text("#!/bin/sh\nprintf '%s' \"${BUILDER_TEST_SECRET-unset}\"\n")
    monkeypatch.setenv("BUILDER_TEST_SECRET", "secret")
    assert podman.command(["inspect"]) == b"unset"


def test_command_timeout_and_output_are_bounded(tmp_path, monkeypatch):
    executable = tmp_path / "runtime"
    executable.write_text("#!/bin/sh\nexec /usr/bin/sleep 10\n")
    executable.chmod(0o755)
    monkeypatch.setattr(podman, "PODMAN", str(executable))
    with pytest.raises(policy.BuildError, match="interrupted"):
        podman.command([], timeout=0.01)
    executable.write_text("#!/bin/sh\nexec /usr/bin/head -c 300000 /dev/zero\n")
    with pytest.raises(policy.BuildError, match="assembly"):
        podman.command([])
