import json
import uuid

import backup_failure_diagnose as diagnose
import backup_fixture_recover as recovery
import pytest


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery.os, "sync", lambda: None)
    app, instance, key = str(uuid.uuid4()), str(uuid.uuid4()), "b" * 64
    image = "sha256:" + "a" * 64
    for name in (
        "shinyd-runtime",
        "shinyd-builder/jobs",
        "shinyd-builder/artifacts",
        "shinyd-data",
        "shinyd-control",
    ):
        (tmp_path / "var/lib" / name).mkdir(parents=True, exist_ok=True)
    contents = {
        f"var/lib/shinyd-runtime/{instance}.json": {
            "id": instance,
            "spec": {"app_id": app},
            "desired": "removed",
        },
        f"var/lib/shinyd-builder/artifacts/{key}.json": {
            "cache_key": key,
            "app_id": app,
            "image": image,
            "base_image": image,
        },
        "etc/shinyd-runtime/policy.json": {"recipes": {"fixture": image}},
        "etc/shinyd-builder/policy.json": {"environments": {"r-a": {"image": image}}},
    }
    for name, value in contents.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    data = tmp_path / f"var/lib/shinyd-data/{app}/files/value"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"private fixture data")
    for path in [tmp_path, *[p for p in tmp_path.rglob("*") if p.is_dir()]]:
        path.chmod(0o700)
    report = {
        "passed": False,
        "observed_at": "2026-10-23T11:22:25+00:00",
        "cleanup_error": [f"instance:{instance}:runtime_unavailable"],
    }

    def run(*args):
        if args[:2] == ("systemctl", "list-units") or args[:2] == ("podman", "ps"):
            return "[]"
        if args[:2] == ("systemctl", "show"):
            return "\n".join(f"Id={u}\nActiveState=inactive\n" for u in diagnose.UNITS)
        assert args[:3] == ("podman", "image", "inspect")
        return json.dumps([{"Id": image, "Size": 1000}])

    diagnostic = diagnose.collect(report, root=tmp_path, run=run)
    return report, diagnostic, run, data


def test_quarantine_preserves_identified_files_and_does_not_touch_other_files(tmp_path, fixture):
    report, diagnostic, run, data = fixture
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("keep")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    paths, _ = recovery.prepare(report, diagnostic, root=tmp_path, run=run)
    area, receipt = recovery.quarantine(paths, tmp_path, {"report_sha256": "fixture"})
    assert receipt["status"] == "quarantined"
    assert area.stat().st_mode & 0o077 == 0
    assert not any(p.exists() for p in paths)
    for relative, raw in before.items():
        if relative.as_posix() == "unrelated":
            assert unrelated.read_text() == "keep"
        else:
            assert (area / "originals" / relative).read_bytes() == raw
    assert "private fixture data" not in json.dumps(receipt)
    assert not receipt["images_removed"] and not receipt["resource_limits_changed"]


@pytest.mark.parametrize(
    "change", ["policy", "active", "unknown", "guard", "database", "link", "jobs"]
)
def test_recovery_refuses_changes_live_installation_or_unidentified_files(
    tmp_path, fixture, change
):
    report, diagnostic, run, data = fixture
    if change == "active":
        original = run

        def run(*args):
            return original(*args).replace("ActiveState=inactive", "ActiveState=active", 1)
    elif change == "policy":
        (tmp_path / "etc/shinyd-runtime/policy.json").write_text('{"recipes":{}}')
    elif change == "unknown":
        (tmp_path / "var/lib/shinyd-runtime/unexpected").write_text("keep")
    elif change == "guard":
        marker = tmp_path / "var/lib/shinyd-backup/restore-incomplete"
        marker.parent.mkdir()
        marker.symlink_to(tmp_path / "absent")
    elif change == "database":
        (tmp_path / "var/lib/shinyd-control/metadata.sqlite3").write_text("occupied")
    elif change == "link":
        data.with_name("link").symlink_to(tmp_path / "outside")
    elif change == "jobs":
        (tmp_path / "var/lib/shinyd-builder/jobs/occupied").mkdir()
    with pytest.raises(ValueError):
        recovery.prepare(report, diagnostic, root=tmp_path, run=run)
    assert data.read_bytes() == b"private fixture data"
    assert not list((tmp_path / "var/lib").glob("shinyd-probe-quarantine-*"))


def test_quarantine_rolls_back_completed_moves_if_a_rename_fails(tmp_path, fixture, monkeypatch):
    report, diagnostic, run, data = fixture
    paths, _ = recovery.prepare(report, diagnostic, root=tmp_path, run=run)
    rename = type(data).rename

    def fail_once(self, target):
        if self == paths[1]:
            raise OSError("simulated write error")
        return rename(self, target)

    monkeypatch.setattr(type(data), "rename", fail_once)
    with pytest.raises(OSError):
        recovery.quarantine(paths, tmp_path, {})
    assert all(p.exists() for p in paths)
    journal = next((tmp_path / "var/lib").glob("shinyd-probe-quarantine-*/receipt.json"))
    assert json.loads(journal.read_text())["status"] == "rolled_back"
    assert data.read_bytes() == b"private fixture data"


@pytest.mark.parametrize("partial", [False, True])
def test_quarantine_service_guard_is_retained_only_if_state_is_partial(
    tmp_path, fixture, monkeypatch, partial
):
    report, diagnostic, run, data = fixture
    work = tmp_path / "var/lib/shinyd-backup"
    work.mkdir(mode=0o700)
    paths, _ = recovery.prepare(report, diagnostic, root=tmp_path, run=run)
    operation = recovery.quarantine

    def guarded(paths, root, checksums):
        assert (work / "maintenance").is_file()
        if partial:
            paths[0].rename(work / "partial-record")
            raise RuntimeError("simulated partial quarantine")
        return operation(paths, root, checksums)

    monkeypatch.setattr(recovery, "quarantine", guarded)
    if partial:
        with pytest.raises(RuntimeError):
            recovery.guarded_quarantine(report, diagnostic, paths, tmp_path, {}, run=run)
        assert (work / "maintenance").exists()
    else:
        area, receipt = recovery.guarded_quarantine(
            report, diagnostic, paths, tmp_path, {}, run=run
        )
        assert receipt["status"] == "quarantined" and area.is_dir()
        assert not (work / "maintenance").exists()
