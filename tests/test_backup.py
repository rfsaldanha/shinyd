import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from django.contrib.sessions.models import Session
from django.db import connection
from django.utils import timezone

from control.environment import snapshot
from control.models import AppInstance, Job, Publication, RecoveryState, Version
from control.services import set_environment
from deployment import backup, backup_images, backup_state
from deployment import backup_archive as archive
from deployment.backup_archive import BackupError
from tests.test_deployment import CONFIG, pem

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def tree(transactional_db, app, accounts, local_state, settings):
    root = local_state / "snapshot"
    for name in sorted(archive.DIRECTORIES):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "control/keys/secrets.key").write_bytes(settings.SHINYD_MASTER_KEY_FILE.read_bytes())
    (root / "control/keys/django.key").write_bytes(b"d" * 64)
    (root / "control/keys/gateway.key").write_bytes(b"e" * 64)
    for name, value in {
        "control": CONFIG,
        "runtime": {
            "schema_version": 1,
            "recipes": {},
            "builder_artifacts": True,
            "max_instances": 5,
            "memory_budget_mib": 4096,
        },
        "builder": {"schema_version": 1, "environments": {"r-a": {"image": IMAGE}}},
    }.items():
        (root / f"config/{name}.json").write_text(json.dumps(value))
    cert, key = pem(["panel.example.org", "*.apps.example.org"])
    (root / "config/cert.pem").write_bytes(cert)
    (root / "config/tls.key").write_bytes(key)
    source = b"fixture source ZIP bytes, not executed"
    source_path = f"sources/{uuid.uuid4()}.zip"
    (root / "control" / source_path).write_bytes(source)
    artifact = {
        "cache_key": "b" * 64,
        "schema_version": 1,
        "image": IMAGE,
        "base_image": IMAGE,
        "app_id": str(app.pk),
        "source_hash": hashlib.sha256(source).hexdigest(),
    }
    (root / "builder/artifacts" / (artifact["cache_key"] + ".json")).write_text(
        json.dumps(artifact)
    )
    version = Version.objects.create(
        app=app,
        created_by=accounts["admin"],
        source_path=source_path,
        source_hash=artifact["source_hash"],
        status="prepared",
        image_digest=IMAGE,
        build_manifest=artifact,
    )
    set_environment(accounts["admin"], app.pk, 0, "API_TOKEN", "private-backup-sentinel", True)
    app.refresh_from_db()
    app.last_version = version
    app.data_enabled = True
    app.save()
    publication = Publication.objects.create(
        app=app, version=version, config_revision=1, created_by=accounts["admin"], status="retired"
    )
    publication.environment_snapshot = snapshot(publication)
    publication.save()
    AppInstance.objects.create(
        app=app,
        publication=publication,
        host="fixture.apps.example.org",
        target="unix:/retired.sock",
        enabled=False,
    )
    RecoveryState.objects.update_or_create(
        app=app, defaults={"enabled": True, "state": "open", "attempts": 3}
    )
    Session.objects.create(
        session_key="old-session", session_data="invalid", expire_date=timezone.now()
    )
    data = root / f"data/{app.pk}/files"
    data.mkdir(parents=True)
    (data / "fixture.txt").write_bytes(b"persistent-UTF8-\xc3\xa7\n")
    (data / "empty").mkdir()
    (root / "images" / (IMAGE[7:] + ".tar")).write_bytes(b"image fixture, never loaded")
    with sqlite3.connect(root / "control/metadata.sqlite3") as output:
        connection.connection.backup(output)
    return root


def metadata():
    return {"id": str(uuid.uuid4()), "images": [IMAGE], "compatibility": {"test": True}}


def test_archive_roundtrip_and_database_reset_preserve_app_data_and_secrets(tree, tmp_path):
    key = (tree / "control/keys/secrets.key").read_bytes()
    assert backup_state.inspect_snapshot(tree) == [IMAGE]
    packed = tmp_path / "backup.tar"
    manifest = archive.pack(tree, packed, metadata())
    restored = tmp_path / "restored"
    assert archive.unpack(packed, restored) == manifest
    for name, value in archive.inventory(restored).items():
        assert {k: v for k, v in value.items() if k != "mode"} == {
            k: v for k, v in manifest["files"][name].items() if k != "mode"
        }
    backup_state.reset_access(restored, manifest["id"])
    assert backup_state.inspect_snapshot(restored) == [
        IMAGE
    ]  # decrypts both snapshots and variables
    assert (restored / "control/keys/secrets.key").read_bytes() == key
    assert (restored / "control/keys/django.key").read_bytes() != b"d" * 64
    assert (restored / "control/keys/gateway.key").read_bytes() != b"e" * 64
    with sqlite3.connect(restored / "control/metadata.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM django_session").fetchone() == (0,)
        assert db.execute("SELECT enabled,attempts FROM control_recoverystate").fetchone() == (0, 0)
        assert (
            db.execute("SELECT desired_state,last_version_id FROM control_application").fetchone()[
                0
            ]
            == "stopped"
        )
        assert db.execute("SELECT action FROM control_auditevent ORDER BY id DESC").fetchone() == (
            "server.restored",
        )
    for path in (tree / "data").rglob("*"):
        if path.is_file():
            assert (restored / path.relative_to(tree)).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory_link"])
def test_publisher_data_cannot_escape_or_copy_host_keys(tree, tmp_path, kind):
    data = next((tree / "data").glob("*/files"))
    target = data / "escape"
    sentinel = tmp_path / "host-secret"
    sentinel.write_bytes(b"not in backup")
    if kind == "symlink":
        target.symlink_to(sentinel)
    elif kind == "directory_link":
        target.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(sentinel, target)
    else:
        os.mkfifo(target)
    with pytest.raises(BackupError):
        archive.inventory(tree)
    assert sentinel.read_bytes() == b"not in backup"


@pytest.mark.parametrize(
    "corruption", ["../outside", "/tmp/outside", "duplicate", "symlink", "hash", "missing", "size"]
)
def test_restore_refuses_unsafe_or_incomplete_archive(tree, tmp_path, corruption):
    good, bad = tmp_path / "good.tar", tmp_path / "bad.tar"
    archive.pack(tree, good, metadata())
    with tarfile.open(good) as src, tarfile.open(bad, "w") as dst:
        for item in src:
            content = src.extractfile(item).read() if item.isfile() else None
            if item.name == "control/keys/secrets.key":
                if corruption == "missing":
                    continue
                if corruption == "hash":
                    content = b"x" * item.size
                elif corruption == "size":
                    item.size += 1
                    content += b"x"
                elif corruption == "symlink":
                    item.type, item.linkname, item.size, content = (
                        tarfile.SYMTYPE,
                        "/etc/passwd",
                        0,
                        None,
                    )
                elif corruption != "duplicate":
                    item.name = corruption
            dst.addfile(item, io.BytesIO(content) if content is not None else None)
            if item.name == "control/keys/secrets.key" and corruption == "duplicate":
                dst.addfile(item, io.BytesIO(content))
    with pytest.raises(BackupError):
        archive.unpack(bad, tmp_path / "unpacked")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize(
    "change", ["active", "job", "migration", "missing_key", "wrong_key", "missing_image", "source"]
)
def test_snapshot_validation_fails_closed(tree, change):
    with sqlite3.connect(tree / "control/metadata.sqlite3") as db:
        if change == "active":
            db.execute("UPDATE control_application SET desired_state='running'")
        elif change == "job":
            # Reuse the ORM to construct a complete queued job, then replace the snapshot.
            version = Version.objects.first()
            Job.objects.create(
                app=version.app, version=version, actor=version.created_by, kind="prepare_version"
            )
            connection.connection.backup(db)
        elif change == "migration":
            db.execute("DELETE FROM django_migrations WHERE name='0012_app_recovery'")
        elif change == "source":
            next((tree / "control/sources").iterdir()).write_bytes(b"changed source")
        elif change == "missing_image":
            next((tree / "builder/artifacts").iterdir()).unlink()
        elif change == "missing_key":
            (tree / "control/keys/secrets.key").unlink()
        else:
            (tree / "control/keys/secrets.key").write_bytes(b"z" * 32)
    with pytest.raises((BackupError, FileNotFoundError, InvalidTag)):
        backup_state.inspect_snapshot(tree)


@pytest.fixture
def age_binary(monkeypatch):
    binary = shutil.which("age") or str(
        Path(__file__).parents[1] / "var/age-toolchain/root/usr/bin/age"
    )
    assert Path(binary).is_file(), "Install age to run the encryption/recovery tests."
    monkeypatch.setattr(archive, "AGE", binary)
    return Path(binary)


def identity(directory, binary):
    key = directory / (uuid.uuid4().hex + ".key")
    subprocess.run(
        [str(binary.with_name("age-keygen")), "-o", str(key)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    public = (
        subprocess.check_output([str(binary.with_name("age-keygen")), "-y", str(key)])
        .decode()
        .strip()
    )
    return key, public


def test_real_age_roundtrip_wrong_key_corruption_and_truncation(tmp_path, age_binary):
    key, public = identity(tmp_path, age_binary)
    wrong, _ = identity(tmp_path, age_binary)
    plain, encrypted = tmp_path / "plain", tmp_path / "encrypted"
    plain.write_bytes(b"private-backup-sentinel" * 10000)
    archive.age(plain, encrypted, recipient=public)
    assert b"private-backup-sentinel" not in encrypted.read_bytes()
    recovered = tmp_path / "recovered"
    archive.age(encrypted, recovered, identity=key)
    assert recovered.read_bytes() == plain.read_bytes()
    with pytest.raises(BackupError):
        archive.age(encrypted, tmp_path / "wrong-output", identity=wrong)
    for index, data in enumerate(
        (encrypted.read_bytes()[:-1], encrypted.read_bytes()[:-20] + b"x" * 20)
    ):
        bad = tmp_path / f"bad-{index}"
        bad.write_bytes(data)
        with pytest.raises(BackupError):
            archive.age(bad, tmp_path / f"bad-output-{index}", identity=key)


def test_failed_decryption_never_parses_or_installs(tmp_path, age_binary, monkeypatch):
    layout = backup.Layout(tmp_path / "host")
    layout.work.mkdir(parents=True)
    bad = tmp_path / "backup.age"
    bad.write_bytes(b"not an age stream")
    key, _ = identity(tmp_path, age_binary)
    monkeypatch.setattr(backup, "safe_directory", lambda *a, **k: None)
    monkeypatch.setattr(backup, "quiescent", lambda *a: None)
    monkeypatch.setattr(backup, "empty_destination", lambda *a: None)
    monkeypatch.setattr(backup, "install", lambda *a: pytest.fail("must not install"))
    monkeypatch.setattr(archive, "unpack", lambda *a: pytest.fail("must not parse plaintext"))
    args = argparse.Namespace(operation="restore", max_gib=1, file=bad, identity=key)
    with pytest.raises(BackupError):
        backup.execute(args, layout)
    assert list(layout.work.iterdir()) == [layout.work / ".lock"]


def test_quiescence_refuses_running_units_or_containers(tmp_path, monkeypatch):
    layout = backup.Layout(tmp_path)
    monkeypatch.setattr(backup, "command", lambda *a: b"active\n" * len(backup.UNITS))
    with pytest.raises(BackupError, match="serviços"):
        backup.quiescent(layout)

    def running(*args):
        if args[:2] == ("systemctl", "show"):
            return b"inactive\n" * len(backup.UNITS)
        if args[0] == "podman":
            return b'[{"Labels":{"org.shinyd.managed":"executor-v1"},"Names":[]}]'
        return b"[]"

    monkeypatch.setattr(backup, "command", running)
    with pytest.raises(BackupError, match="containers"):
        backup.quiescent(layout)


def test_guard_stays_after_interrupted_restore_and_cannot_be_resumed(tmp_path, monkeypatch):
    layout = backup.Layout(tmp_path)
    layout.work.mkdir(parents=True)
    monkeypatch.setattr(backup, "safe_directory", lambda *a, **k: None)
    with backup.maintenance(layout):
        assert (layout.work / "maintenance").exists()
        (layout.work / "restore-incomplete").write_text("interrupted fixture")
    with pytest.raises(BackupError):
        backup.execute(argparse.Namespace(operation="resume", max_gib=1), layout)
    assert (layout.work / "maintenance").exists()


def test_admin_readiness_permissions_and_counts(client, app, accounts, local_state):
    assert client.get("/server/backups/").status_code == 302
    for role in ("publisher", "viewer", "outsider"):
        client.force_login(accounts[role])
        assert client.get("/server/backups/").status_code == 403
    client.force_login(accounts["admin"])
    app.desired_state = "running"
    app.save()
    page = client.get("/server/backups/")
    assert page.context["active_apps"] == 1
    assert page.context["pending_jobs"] == 0
    assert "ainda não estão disponíveis" in page.content.decode()
    assert client.post("/server/backups/").status_code == 405


@pytest.fixture
def destination(tmp_path, monkeypatch):
    layout = backup.Layout(tmp_path / "host")
    for path in (
        layout.control,
        layout.builder / "artifacts",
        layout.builder / "jobs",
        layout.runtime,
        layout.data,
        layout.work,
        *[p.parent for p in layout.config.values()],
        layout.root / "etc/shinyd-gateway",
    ):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Unit test filesystem is unprivileged; the installed smoke checks real owners.
    monkeypatch.setattr(backup, "safe_directory", lambda *a, **k: None)
    monkeypatch.setattr(
        backup.pwd, "getpwnam", lambda n: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    )
    monkeypatch.setattr(backup.grp, "getgrnam", lambda n: SimpleNamespace(gr_gid=os.getgid()))
    monkeypatch.setattr(backup.os, "chown", lambda *a: None)
    monkeypatch.setattr(backup.os, "fchown", lambda *a: None)
    monkeypatch.setattr(backup.os, "sync", lambda: None)
    return layout


def test_install_into_empty_namespace_then_refuse_second_restore(tree, destination):
    manifest = {**metadata(), "files": archive.inventory(tree)}
    backup_state.reset_access(tree, manifest["id"])
    backup.install(tree, manifest, destination)
    assert (
        json.loads((destination.control / "restore.json").read_text())["backup_id"]
        == manifest["id"]
    )
    assert (destination.root / "etc/shinyd-gateway/key").read_bytes() == (
        tree / "control/keys/gateway.key"
    ).read_bytes()
    assert (destination.control / "keys/secrets.key").stat().st_mode & 0o777 == 0o600
    data = next(destination.data.glob("*/files/fixture.txt"))
    assert data.read_bytes() == b"persistent-UTF8-\xc3\xa7\n"
    before = (destination.control / "metadata.sqlite3").read_bytes()
    with pytest.raises(BackupError, match="vazia"):
        backup.install(tree, manifest, destination)
    assert before == (destination.control / "metadata.sqlite3").read_bytes()
    assert not (destination.work / "restore-incomplete").exists()


def test_install_cleans_only_new_paths_on_partial_write_failure(tree, destination, monkeypatch):
    manifest = {**metadata(), "files": archive.inventory(tree)}
    copy = archive.copy_file

    def failing(source, target, *args, **kwargs):
        if target == destination.control / "metadata.sqlite3":
            with target.open("xb") as output:
                kwargs["created"].append(target)
                output.write(b"simulated interrupted copy")
            raise OSError("simulated disk failure")
        return copy(source, target, *args, **kwargs)

    monkeypatch.setattr(archive, "copy_file", failing)
    with pytest.raises(OSError):
        backup.install(tree, manifest, destination)
    backup.empty_destination(destination)
    assert not (destination.work / "restore-incomplete").exists()
    assert (destination.builder / "artifacts").is_dir()
    assert (tree / "control/metadata.sqlite3").is_file()


def test_restore_refuses_existing_data_and_symlinks(destination, tmp_path):
    sentinel = destination.data / "important.txt"
    sentinel.write_bytes(b"preserve me")
    with pytest.raises(BackupError):
        backup.empty_destination(destination)
    assert sentinel.read_bytes() == b"preserve me"
    sentinel.unlink()
    (destination.data / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BackupError, match="links"):
        backup.empty_destination(destination)


def test_archive_limits_and_free_space_fail_closed(tree, tmp_path, monkeypatch):
    with pytest.raises(BackupError, match="limite"):
        archive.inventory(tree, limit=100)
    monkeypatch.setattr(archive, "ENTRIES", 2)
    with pytest.raises(BackupError, match="limite"):
        archive.inventory(tree)
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda p: SimpleNamespace(free=100))
    with pytest.raises(BackupError, match="Espaço"):
        archive.copy_file(tree / "control/metadata.sqlite3", tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


def test_expired_tls_can_be_recovered_but_configuration_requires_renewal(tree):
    from deployment.cli import verify_certificate

    cert, key = pem(["panel.example.org", "*.apps.example.org"], expired=True)
    (tree / "config/cert.pem").write_bytes(cert)
    (tree / "config/tls.key").write_bytes(key)
    assert backup_state.inspect_snapshot(tree) == [IMAGE]
    with pytest.raises(ValueError, match="validade"):
        verify_certificate(cert, key, CONFIG)


def test_create_verify_restore_orchestration_with_real_age_and_sqlite(
    tree, destination, tmp_path, age_binary, monkeypatch
):
    manifest = {**metadata(), "files": archive.inventory(tree)}
    backup.install(tree, manifest, destination)
    key, public = identity(tmp_path, age_binary)
    output = tmp_path / "copy.age"
    monkeypatch.setattr(backup, "compatibility", lambda: {"test": True})
    monkeypatch.setattr(backup, "quiescent", lambda *a: None)
    loaded = []

    def podman(*args, **kwargs):
        if args[:3] == ("podman", "image", "inspect"):
            return json.dumps([{"Id": IMAGE[7:], "Size": 64}]).encode()
        if args[:2] == ("podman", "load"):
            loaded.append(gzip.decompress(Path(args[-1]).read_bytes()))
        else:
            pytest.fail("Unexpected command")
        return b""

    monkeypatch.setattr(backup, "command", podman)

    def export(images, root, limit):
        assert images == [IMAGE]
        raw = b"image transported unchanged"
        (root / backup_images.BUNDLE).write_bytes(gzip.compress(raw))
        return len(raw)

    monkeypatch.setattr(backup_images, "export", export)
    result = backup.execute(
        argparse.Namespace(operation="create", file=output, recipient=public, max_gib=1),
        destination,
    )
    verified = backup.execute(
        argparse.Namespace(operation="verify", file=output, identity=key, max_gib=1), destination
    )
    assert result["backup_id"] == verified["backup_id"]
    assert not loaded
    # Simulate a different empty host; preserve the source installation throughout.
    other = backup.Layout(tmp_path / "other-host")
    for path in (
        other.control,
        other.builder / "artifacts",
        other.builder / "jobs",
        other.runtime,
        other.data,
        other.work,
        *[p.parent for p in other.config.values()],
        other.root / "etc/shinyd-gateway",
    ):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    args = argparse.Namespace(operation="restore", file=output, identity=key, max_gib=1)
    monkeypatch.setattr(backup, "compatibility", lambda: {"test": "incompatible"})
    with pytest.raises(BackupError, match="mesmas versões"):
        backup.execute(args, other)
    assert not loaded and not (other.control / "metadata.sqlite3").exists()
    monkeypatch.setattr(backup, "compatibility", lambda: {"test": True})
    restored = backup.execute(args, other)
    assert restored["backup_id"] == result["backup_id"]
    assert loaded == [b"image transported unchanged"]
    assert (
        next(other.data.glob("*/files/fixture.txt")).read_bytes() == b"persistent-UTF8-\xc3\xa7\n"
    )
    assert (other.control / "keys/secrets.key").read_bytes() == (
        destination.control / "keys/secrets.key"
    ).read_bytes()
    assert (destination.control / "metadata.sqlite3").is_file()
    assert list(other.work.iterdir()) == [other.work / ".lock"]
