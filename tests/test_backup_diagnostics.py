import json
import os
import uuid
from types import SimpleNamespace

import backup_failure_diagnose as diagnose
import probe_cleanup
import pytest
from backup_browser_probe import backup_command, failure_detail, platform_errors, refused_command


@pytest.mark.parametrize("marker", [None, "maintenance", "restore-incomplete", "dangling"])
def test_runtime_cleanup_reopens_socket_but_respects_interrupted_backup(
    tmp_path, monkeypatch, marker
):
    monkeypatch.setattr(probe_cleanup, "BACKUP_STATE", tmp_path)
    policy = tmp_path / "policy.json"
    policy.write_bytes(b"known fixture policy")
    if marker == "dangling":
        (tmp_path / "maintenance").symlink_to(tmp_path / "absent")
    elif marker:
        (tmp_path / marker).write_text("interrupted")
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        assert kwargs == {"check": False}
        return SimpleNamespace(returncode=0)

    if marker:
        with pytest.raises(RuntimeError, match="backup_interrupted_preserved"):
            probe_cleanup.prepare_runtime_cleanup(policy, policy.read_bytes(), run)
        assert calls == []
    else:
        probe_cleanup.prepare_runtime_cleanup(policy, policy.read_bytes(), run)
        assert calls == [
            ("systemctl", "start", "shinyd-runtime.socket"),
            ("systemctl", "is-active", "--quiet", "shinyd-runtime.socket"),
        ]


def test_cleanup_refuses_changed_policy_and_inactive_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_cleanup, "BACKUP_STATE", tmp_path)
    path = tmp_path / "policy"
    path.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="policy_mismatch"):
        probe_cleanup.prepare_runtime_cleanup(
            path, b"original", lambda *a, **k: pytest.fail("must not start")
        )
    with pytest.raises(RuntimeError, match="socket_unavailable"):
        probe_cleanup.prepare_runtime_cleanup(
            path, b"changed", lambda *a, **k: SimpleNamespace(returncode=3)
        )


@pytest.mark.parametrize(
    "stderr,reason",
    [
        (
            "Espaço insuficiente; mantenha ao menos 1 GiB livre além da operação.\n",
            "insufficient_space",
        ),
        ("private-token-in-an-unexpected-traceback", "unknown_failure"),
    ],
)
def test_backup_reports_only_allowlisted_errors(stderr, reason):
    report = {}
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stderr=stderr, stdout="private-secret")

    with pytest.raises(RuntimeError, match="backup_command_failed:" + reason):
        backup_command(run, report, "shinyd-backup", "create", "private-path")
    detail = report["backup_failure"]
    assert {k: detail[k] for k in ("operation", "exit_code", "reason")} == {
        "operation": "create",
        "exit_code": 1,
        "reason": reason,
    }
    if reason == "unknown_failure":
        assert "platform_error" not in detail
    else:
        assert detail["platform_error"]["message"] == stderr.strip()
    assert "private" not in repr(report)
    assert calls[0][1] == {"timeout": 1800, "check": False}


def test_all_literal_backup_errors_are_identified_without_exposing_dynamic_output():
    errors = platform_errors()
    for message, definitions in errors.items():
        detail = failure_detail(SimpleNamespace(returncode=1, stderr=message + "\n"), "create")
        assert detail["reason"] != "unknown_failure"
        assert detail["platform_error"] == {"definitions": definitions, "message": message}
        private = message + "\nPRIVATE_TOKEN"
        detail = failure_detail(SimpleNamespace(returncode=1, stderr=private), "create")
        assert detail["reason"] == "unknown_failure" and "platform_error" not in detail
        assert "PRIVATE_TOKEN" not in json.dumps(detail)
    assert {v["module"] for locations in errors.values() for v in locations} == {
        "backup.py",
        "backup_archive.py",
        "backup_images.py",
        "backup_state.py",
    }


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (1, "Links e arquivos especiais não são aceitos no backup.", True),
        (1, "Diretório inseguro para manutenção privilegiada.", False),
        (1, "PRIVATE_TOKEN", False),
        (0, "", False),
    ],
)
def test_negative_backup_check_requires_the_intended_refusal(code, message, expected):
    report = {}

    def run(*args, **kwargs):
        return SimpleNamespace(returncode=code, stderr=message, stdout="PRIVATE_TOKEN")

    assert refused_command(run, report, "unsafe_entry", "shinyd-backup", "create") is expected
    assert ("backup_failure" in report) is not expected
    assert "PRIVATE_TOKEN" not in json.dumps(report)


def test_read_only_diagnostic_keeps_data_and_excludes_secrets(tmp_path):
    app, instance, key, image = str(uuid.uuid4()), str(uuid.uuid4()), "b" * 64, "sha256:" + "a" * 64
    private = "PRIVATE_DIAGNOSTIC_SENTINEL"
    paths = {
        "var/lib/shinyd-runtime/" + instance + ".json": {
            "id": instance,
            "desired": "removed",
            "spec": {"app_id": app, "secret": private},
        },
        "etc/shinyd-runtime/policy.json": {"recipes": {"built": image}},
        "etc/shinyd-builder/policy.json": {"environments": {"r-a": {"image": image}}},
        "var/lib/shinyd-builder/artifacts/" + key + ".json": {
            "cache_key": key,
            "app_id": app,
            "image": image,
            "base_image": image,
            "secret": private,
        },
    }
    for name, value in paths.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    data = tmp_path / "var/lib/shinyd-data" / app / "files"
    data.mkdir(parents=True)
    (data / "secret.txt").write_text(private)
    secret_key = tmp_path / "var/lib/shinyd-control/keys/secrets.key"
    secret_key.parent.mkdir(parents=True)
    secret_key.write_text(private)
    (data / "escape").symlink_to(secret_key)
    before = {
        str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and not p.is_symlink()
    }
    calls = []

    def run(*args):
        calls.append(args)
        if args[:2] == ("systemctl", "show"):
            return "ActiveState=inactive\n"
        if args[:2] == ("podman", "ps"):
            return "[]"
        assert args == ("podman", "image", "inspect", image)
        return json.dumps([{"Id": image[7:], "Size": 10_000, "Env": [private]}])

    report = {
        "passed": False,
        "observed_at": "2026-10-23T11:22:25+00:00",
        "cleanup_error": [f"instance:{instance}:runtime_unavailable"],
    }
    result = diagnose.collect(report, root=tmp_path, run=run)
    assert result["runtime_records"][0]["in_failed_report"]
    assert result["data"][0]["in_failed_report"]
    assert result["data"][0]["unsafe_entries"] == 1
    assert result["images"]["count"] == 1 and result["images"]["uncompressed_bytes"] == 10_000
    assert result["disk"]["estimate_complete"] is False
    assert private not in json.dumps(result) and "secret.txt" not in json.dumps(result)
    assert all(p.read_bytes() == before[str(p)] for p in tmp_path.rglob("*") if str(p) in before)
    assert (data / "escape").is_symlink()
    assert all(args[1] in {"show", "ps", "image"} for args in calls)


def test_diagnostic_rejects_links_and_nonregular_metadata(tmp_path):
    path = tmp_path / "secret"
    path.write_text('{"secret":"private"}')
    (tmp_path / "link").symlink_to(path)
    with pytest.raises(OSError):
        diagnose.read_json(tmp_path / "link")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="unsafe_metadata"):
        diagnose.read_json(fifo)


def test_namespace_diagnostic_lists_only_fixed_names_and_metadata(tmp_path):
    secret = tmp_path / "PRIVATE_FILENAME_TOKEN"
    secret.write_text("PRIVATE_CONTENT_TOKEN")
    (tmp_path / "package-test-sentinel").touch()
    (tmp_path / "keys").mkdir()
    (tmp_path / "sources").symlink_to(tmp_path / "keys")
    result = diagnose.namespace_entries(tmp_path)
    assert {row["name"] for row in result["entries"]} == {
        "unrecognized",
        "package-test-sentinel",
        "keys",
        "sources",
    }
    assert next(r for r in result["entries"] if r["name"] == "sources")["kind"] == "symlink"
    assert "PRIVATE" not in json.dumps(result)
    assert secret.read_text() == "PRIVATE_CONTENT_TOKEN"
    assert diagnose.namespace_entries(tmp_path / "sources")["directory"] is False
    assert diagnose.namespace_entries(tmp_path / "absent") == {"present": False}
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    identity = str(uuid.uuid4())
    (jobs / identity).mkdir()
    assert diagnose.namespace_entries(jobs, jobs=True)["entries"][0]["name"] == identity
