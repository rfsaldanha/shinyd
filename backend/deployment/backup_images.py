"""One compressed image archive: shared layers are exported once by Podman."""

import gzip
import os
import selectors
import subprocess
import time
import zlib

from deployment import backup_archive as archive
from deployment.backup_archive import BackupError

BUNDLE = "images/bundle.tar.gz"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "HOME": "/root"}


class BoundedOutput:
    def __init__(self, stream, path, limit):
        self.stream, self.path, self.limit = stream, path, limit
        self.total = 0

    def write(self, block):
        self.total += len(block)
        if self.total > self.limit:
            raise BackupError("Imagens excedem o limite do backup.")
        archive.space(self.path.parent, len(block))
        return self.stream.write(block)


def compress_command(args, output, limit, *, timeout=1800, env=None):
    """Bound bytes, disk and wall time; never retain subprocess output or stderr."""
    deadline = time.monotonic() + timeout
    with output.open("xb") as raw:
        os.fchmod(raw.fileno(), 0o600)
        with subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env or ENV
        ) as process:
            try:
                total = 0
                with (
                    selectors.DefaultSelector() as selector,
                    gzip.GzipFile(
                        filename="",
                        mode="wb",
                        compresslevel=1,
                        mtime=0,
                        fileobj=BoundedOutput(raw, output, limit),
                    ) as compressed,
                ):
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise BackupError("Exportação das imagens excedeu o tempo permitido.")
                        if not selector.select(min(remaining, 1)):
                            continue
                        block = os.read(process.stdout.fileno(), 1024**2)
                        if not block:
                            break
                        total += len(block)
                        if total > limit:
                            raise BackupError("Imagens excedem o limite do backup.")
                        compressed.write(block)
                if process.wait(timeout=max(0.01, deadline - time.monotonic())):
                    raise BackupError(
                        "Exportação das imagens falhou; confira Podman e espaço livre."
                    )
                raw.flush()
                os.fsync(raw.fileno())
                return total
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


def export(images, root, limit):
    return compress_command(
        ["podman", "save", "--quiet", "--multi-image-archive", "--format=docker-archive", *images],
        root / BUNDLE,
        limit,
        env={**ENV, "TMPDIR": str(root.parent)},
    )


def validate(root, manifest, images, limit):
    expected = {"images/" + v[7:] + ".tar" for v in images}
    if manifest["format"] == "shinyd-cold-backup-v2":
        expected = {BUNDLE}
        count = manifest.get("image_uncompressed_bytes")
        if type(count) is not int or not 0 < count <= limit:
            raise BackupError("Tamanho descomprimido das imagens inválido.")
        total = 0
        try:
            with archive.regular(root / BUNDLE) as raw, gzip.GzipFile(fileobj=raw) as source:
                while block := source.read(min(1024**2, count - total + 1)):
                    total += len(block)
                    if total > count:
                        raise BackupError("Imagens excedem o limite do backup.")
        except (OSError, EOFError, zlib.error):
            raise BackupError("Arquivo comprimido de imagens inválido.") from None
        if total != count:
            raise BackupError("Arquivo comprimido de imagens incompleto.")
    if images != manifest["images"] or expected != {
        n for n in manifest["files"] if n.startswith("images/")
    }:
        raise BackupError("Inventário de imagens incompleto ou divergente.")
    return sorted(expected)
