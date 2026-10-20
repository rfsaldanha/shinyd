import io
import json
import stat
import struct
import zipfile

import pytest

from control.archives import ArchiveError, inspect_archive


def archive_bytes(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return stream.getvalue()


def inspect(tmp_path, entries, **kwargs):
    source = tmp_path / "input.zip"
    source.write_bytes(archive_bytes(entries))
    dest = tmp_path / "out"
    dest.mkdir(exist_ok=True)
    return inspect_archive(
        source,
        dest,
        max_bytes=kwargs.pop("max_bytes", 1024 * 1024),
        max_entries=kwargs.pop("max_entries", 100),
        **kwargs,
    )


@pytest.mark.parametrize(
    "name",
    [
        "../app.R",
        "/app.R",
        "dir/../../app.R",
        "C:/app.R",
        "dir\\app.R",
        "./app.R",
        "x//app.R",
    ],
)
def test_rejects_traversal(tmp_path, name):
    with pytest.raises(ArchiveError):
        inspect(tmp_path, [(name, "code")])
    assert not (tmp_path.parent / "app.R").exists()


def test_symlink_is_rejected(tmp_path):
    link = zipfile.ZipInfo("app.R")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ArchiveError, match="Links"):
        inspect(tmp_path, [(link, "/etc/passwd")])


@pytest.mark.parametrize(
    "entries",
    [
        [("app.R", "1"), ("APP.r", "2")],
        [("dir", "file"), ("dir/app.R", "1")],
    ],
)
def test_collisions(tmp_path, entries):
    with pytest.raises(ArchiveError):
        inspect(tmp_path, entries)


def test_expansion_limit_and_cancel(tmp_path):
    with pytest.raises(ArchiveError, match="limite"):
        inspect(tmp_path, [("app.R", "0" * 10000)], max_bytes=100)
    with pytest.raises(ArchiveError, match="cancelada"):
        inspect(tmp_path, [("app.R", "code")], cancelled=lambda: True)


def test_entrypoint_ambiguity_and_exact_lock(tmp_path):
    with pytest.raises(ArchiveError, match="mais de um"):
        inspect(tmp_path, [("a/app.R", "a"), ("b/app.R", "b")])
    # A fresh private extraction directory for the next inspection.
    import shutil

    shutil.rmtree(tmp_path / "out")
    manifest = inspect(
        tmp_path,
        [
            ("a/app.R", "a"),
            ("b/app.R", "b"),
            ("a/renv.lock", json.dumps({"R": {"Version": "4.3.3"}, "Packages": {}})),
        ],
        entrypoint="a",
    )
    assert manifest["r_version"] == "4.3.3"
    assert manifest["file_count"] == 3
    assert manifest["has_lockfile"]


def test_no_lock_records_warning(tmp_path):
    result = inspect(tmp_path, [("ui.R", "ui"), ("server.R", "server")])
    assert result["entrypoint"] == "."
    assert result["warnings"]


def test_forged_entry_count_rejected_before_zipfile(tmp_path):
    source = tmp_path / "forged.zip"
    data = bytearray(archive_bytes([("app.R", "x"), ("extra.txt", "y")]))
    index = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, index + 8, 1, 1)
    source.write_bytes(data)
    with pytest.raises(ArchiveError, match="Contagem"):
        inspect_archive(source, tmp_path / "out", max_bytes=1000, max_entries=10)


def test_max_entries(tmp_path):
    with pytest.raises(ArchiveError, match="arquivos demais"):
        inspect(tmp_path, [("app.R", "x"), ("b", "y")], max_entries=1)
