import gzip
import sys
import time

import pytest

from deployment import backup_images as images
from deployment.backup_archive import BackupError


def test_compressed_export_stream_roundtrip_is_private_and_bounded(tmp_path):
    output = tmp_path / "images.tar.gz"
    size = images.compress_command(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'fixture'*10000)"],
        output,
        100_000,
    )
    assert size == 70_000
    assert gzip.decompress(output.read_bytes()) == b"fixture" * 10000
    assert output.stat().st_size < size / 10
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        images.compress_command([sys.executable, "-c", "pass"], output, 100_000)


@pytest.mark.parametrize("failure", ["exit", "timeout", "size", "space"])
def test_export_terminates_safely_on_process_resource_and_disk_failure(
    tmp_path, monkeypatch, failure
):
    program = "import sys;sys.stdout.buffer.write(b'x'*100000)"
    timeout, limit = 5, 200_000
    if failure == "exit":
        program = "import sys;sys.stderr.write('PRIVATE_SENTINEL');sys.exit(7)"
    elif failure == "timeout":
        program, timeout = "import time;time.sleep(60)", 0.1
    elif failure == "size":
        limit = 1000
    else:

        def no_space(*args):
            raise BackupError("Espaço insuficiente")

        monkeypatch.setattr(images.archive, "space", no_space)
    start = time.monotonic()
    with pytest.raises(BackupError) as exc:
        images.compress_command(
            [sys.executable, "-c", program], tmp_path / "partial", limit, timeout=timeout
        )
    assert "PRIVATE_SENTINEL" not in str(exc.value)
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("corruption", [None, "count", "limit", "truncated", "extra_image", "bool"])
def test_bundle_validation_checks_expansion_size_integrity_and_inventory(tmp_path, corruption):
    path = tmp_path / images.BUNDLE
    path.parent.mkdir()
    raw = gzip.compress(b"x" * 10000)
    path.write_bytes(raw[:-1] if corruption == "truncated" else raw)
    manifest = {
        "format": "shinyd-cold-backup-v2",
        "images": ["sha256:" + "a" * 64],
        "image_uncompressed_bytes": 10000,
        "files": {images.BUNDLE: {}},
    }
    limit = 20000
    if corruption == "count":
        manifest["image_uncompressed_bytes"] = 100
    elif corruption == "limit":
        limit = 1000
    elif corruption == "bool":
        manifest["image_uncompressed_bytes"] = True
    elif corruption == "extra_image":
        manifest["files"]["images/" + "b" * 64 + ".tar"] = {}
    if corruption:
        with pytest.raises(BackupError):
            images.validate(tmp_path, manifest, manifest["images"], limit)
    else:
        assert images.validate(tmp_path, manifest, manifest["images"], limit) == [images.BUNDLE]


def test_legacy_image_inventory_remains_readable(tmp_path):
    image = "sha256:" + "a" * 64
    name = "images/" + image[7:] + ".tar"
    manifest = {"format": "shinyd-cold-backup-v1", "images": [image], "files": {name: {}}}
    assert images.validate(tmp_path, manifest, [image], 10000) == [name]
