import json
import os
import stat
from pathlib import Path

import container_smoke
import container_subids as subids
import pytest


@pytest.fixture
def host_files(tmp_path, monkeypatch):
    (tmp_path / "nsswitch.conf").write_text("passwd: files\ngroup: files\n")
    (tmp_path / "subuid").write_bytes(b"# keep this comment\nalice:100000:65536\n")
    (tmp_path / "subgid").write_bytes(b"bob:165536:65536")  # No trailing newline.
    for path in tmp_path.iterdir():
        path.chmod(0o644)
    monkeypatch.setattr(subids.pwd, "getpwall", lambda: [])
    monkeypatch.setattr(subids.grp, "getgrall", lambda: [])
    return tmp_path


def test_plan_avoids_both_id_spaces_and_visible_accounts():
    contents = {"subuid": b"alice:100000:65536\n", "subgid": b"bob:165536:65536\n"}
    plan = subids.plan_reservation(contents, [300000], [1600000])
    allocated = plan["ranges"][0]
    start, end = allocated["start"], allocated["start"] + allocated["count"]
    assert start % subids.BLOCK == 0
    assert allocated["count"] == 16 * subids.BLOCK
    assert start >= 300001
    assert end <= 1600000
    assert plan["additions"]["subuid"] == plan["additions"]["subgid"]


def test_inspection_makes_no_changes(host_files):
    before = {p.name: p.read_bytes() for p in host_files.iterdir()}
    plan, contents = subids.inspect_reservation(host_files)
    assert plan["action"] == "reserve"
    assert plan["ranges"] == [{"start": 262144, "count": subids.POOL_SIZE}]
    assert contents["subgid"] == before["subgid"]
    assert {p.name: p.read_bytes() for p in host_files.iterdir()} == before


def test_apply_preserves_existing_entries_metadata_and_is_idempotent(host_files):
    before = {name: (host_files / name).read_bytes() for name in ("subuid", "subgid")}
    (host_files / "subgid").chmod(0o640)
    result = subids.reserve(host_files)
    backup = Path(result["backup_directory"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    for name, data in before.items():
        assert (backup / name).read_bytes() == data
        assert (host_files / name).read_bytes().startswith(data)
        assert (host_files / name).read_text().splitlines()[-1] == "containers:262144:1048576"
    assert stat.S_IMODE((host_files / "subgid").stat().st_mode) == 0o640
    assert not list(host_files.glob("*.lock"))
    second = subids.reserve(host_files)
    assert second["action"] == "unchanged"
    assert second["applied"] is False
    assert len(list(host_files.glob("shinyd-subids-backup-*"))) == 1


def test_second_write_failure_restores_first_and_keeps_backup(host_files, monkeypatch):
    original = {name: (host_files / name).read_bytes() for name in ("subuid", "subgid")}
    real_replace = subids.atomic_replace

    def fail_gid(path, data):
        if path.name == "subgid":
            raise OSError("simulated write failure")
        real_replace(path, data)

    monkeypatch.setattr(subids, "atomic_replace", fail_gid)
    with pytest.raises(RuntimeError, match="originais em"):
        subids.reserve(host_files)
    assert {name: (host_files / name).read_bytes() for name in original} == original
    assert len(list(host_files.glob("shinyd-subids-backup-*"))) == 1
    assert not list(host_files.glob("*.lock"))


def test_foreign_lock_is_preserved_and_own_locks_are_released(host_files):
    lock = host_files / "subgid.lock"
    lock.write_bytes(b"12345\0")
    with pytest.raises(RuntimeError, match="Cadastro ocupado"):
        subids.reserve(host_files)
    assert lock.read_bytes() == b"12345\0"
    assert sorted(p.name for p in host_files.glob("*.lock")) == ["subgid.lock"]
    assert not list(host_files.glob("shinyd-subids-backup-*"))


def test_external_subid_provider_is_refused(host_files):
    (host_files / "nsswitch.conf").write_text("subid: sss\n")
    with pytest.raises(ValueError, match="externo"):
        subids.reserve(host_files)
    assert b"containers" not in (host_files / "subuid").read_bytes()
    assert not list(host_files.glob("*.lock"))


@pytest.mark.parametrize("kind", ["symlink", "writable", "hardlink"])
def test_unsafe_file_is_refused(host_files, kind):
    path = host_files / "subuid"
    if kind == "symlink":
        path.rename(host_files / "original")
        path.symlink_to(host_files / "original")
    elif kind == "hardlink":
        os.link(path, host_files / "another-link")
    else:
        path.chmod(0o666)
    with pytest.raises((OSError, ValueError)):
        subids.reserve(host_files)
    assert not list(host_files.glob("shinyd-subids-backup-*"))
    assert not list(host_files.glob("*.lock"))


@pytest.mark.parametrize(
    ("uid_data", "gid_data", "error"),
    [
        (b"containers:262144:65536\n", b"", "incompletas"),
        (b"containers:262144:65536\n", b"containers:327680:65536\n", "diferentes"),
        (b"containers:262144:100\n", b"containers:262144:100\n", "menor"),
        (
            b"containers:262144:65536\nalice:300000:1000\n",
            b"containers:262144:65536\n",
            "sobreposta",
        ),
        (b"alice:0:65536\n", b"", "inválida"),
        (b"alice:4294967294:2\n", b"", "inválida"),
        (b"alice:100000:0\n", b"", "inválida"),
        (b"alice:bad:65536\n", b"", "inválida"),
    ],
)
def test_invalid_existing_reservations_are_not_changed(uid_data, gid_data, error):
    with pytest.raises(ValueError, match=error):
        subids.plan_reservation({"subuid": uid_data, "subgid": gid_data}, [], [])


def test_existing_pool_must_not_overlap_real_account():
    contents = dict.fromkeys(("subuid", "subgid"), b"containers:262144:65536\n")
    with pytest.raises(ValueError, match="sobreposta"):
        subids.plan_reservation(contents, [300000], [])


def test_existing_small_valid_pool_is_not_expanded():
    contents = dict.fromkeys(("subuid", "subgid"), b"containers:262144:65536\n")
    assert subids.plan_reservation(contents, [], []) == {
        "action": "unchanged",
        "ranges": [{"start": 262144, "count": 65536}],
        "additions": {},
    }


def test_exhausted_id_space_is_reported():
    contents = {"subuid": b"alice:1:4294967294\n", "subgid": b""}
    with pytest.raises(ValueError, match="faixa livre"):
        subids.plan_reservation(contents, [], [])


def test_smoke_reports_missing_reservation_before_running_commands(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["container_smoke.py", "--rootful"])
    monkeypatch.setattr(container_smoke.os, "geteuid", lambda: 0)
    monkeypatch.setattr(container_smoke.shutil, "which", lambda name: "/usr/bin/podman")
    monkeypatch.setattr(
        container_smoke,
        "inspect_reservation",
        lambda: ({"action": "reserve", "ranges": []}, {}),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("A preflight failure must not create units, pull images or run containers")

    monkeypatch.setattr(container_smoke, "command", forbidden)
    monkeypatch.setattr(container_smoke, "reserve", forbidden)
    assert container_smoke.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["smoke_passed"] is False
    assert report["tests"][0]["name"] == "subid_reservation"
    assert "--prepare-subids" in report["error"]


def test_preparation_requires_explicit_flag_and_rootful_mode(monkeypatch):
    monkeypatch.setattr("sys.argv", ["container_smoke.py", "--prepare-subids"])
    monkeypatch.setattr(container_smoke.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as exc:
        container_smoke.main()
    assert exc.value.code == 2
