"""Operator-only cold backup/restore. No HTTP access to host paths or recovery keys."""

import argparse
import contextlib
import fcntl
import grp
import json
import os
import platform
import pwd
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm

from deployment import backup_archive as archive
from deployment import backup_images
from deployment.backup_archive import BackupError
from deployment.backup_state import inspect_snapshot, reset_access
from deployment.config import caddyfile

UNITS = [
    "shinyd.target",
    "shinyd-maintenance.timer",
    *[
        f"shinyd-{n}.service"
        for n in (
            "panel",
            "auth",
            "worker",
            "gateway",
            "ingress",
            "maintenance",
            "runtime",
            "builder",
        )
    ],
    *[f"shinyd-{n}.socket" for n in ("panel", "auth", "gateway", "runtime", "builder")],
]
PACKAGES = ("shinyd-control", "shinyd-runtime", "shinyd-builder", "shinyd-agent", "podman", "crun")


class Layout:
    def __init__(self, root=Path("/")):
        self.root = root
        self.work = root / "var/lib/shinyd-backup"
        self.control = root / "var/lib/shinyd-control"
        self.builder = root / "var/lib/shinyd-builder"
        self.runtime = root / "var/lib/shinyd-runtime"
        self.data = root / "var/lib/shinyd-data"
        self.config = {
            "control.json": root / "etc/shinyd/control.json",
            "builder.json": root / "etc/shinyd-builder/policy.json",
            "runtime.json": root / "etc/shinyd-runtime/policy.json",
            "cert.pem": root / "etc/shinyd-tls/cert.pem",
            "tls.key": root / "etc/shinyd-tls/key.pem",
        }


def command(*args, timeout=120, temporary=None):
    result = subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        env={**backup_images.ENV, **({"TMPDIR": str(temporary)} if temporary else {})},
    )
    if result.returncode:
        raise BackupError("Comando de manutenção falhou; consulte os pré-requisitos do guia.")
    return result.stdout


def compatibility():
    return {
        "architecture": platform.machine(),
        "os": {
            k: v for k, v in platform.freedesktop_os_release().items() if k in {"ID", "VERSION_ID"}
        },
        "packages": {
            name: command("dpkg-query", "-W", "--showformat=${Version}", name).decode()
            for name in PACKAGES
        },
    }


def safe_directory(path, *, owner=0):
    # No symlink in any ancestor; only the final control tree may be service-owned.
    for item in (path, *path.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, owner} or info.st_mode & 0o022:
            raise BackupError("Diretório inseguro para manutenção privilegiada.")


@contextlib.contextmanager
def maintenance(layout):
    safe_directory(layout.work)
    fd = os.open(layout.work / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as lock:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise BackupError("Arquivo de trava inseguro.")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("Já há uma operação de backup em andamento.") from None
        if (layout.work / "restore-incomplete").exists():
            raise BackupError("Restauração interrompida; preserve o estado e consulte o guia.")
        marker = layout.work / "maintenance"
        if marker.exists():
            raise BackupError("Manutenção anterior interrompida; consulte o comando resume.")
        marker.write_text("shinyd-cold-backup-v2\n")
        try:
            yield
        finally:
            if not (layout.work / "restore-incomplete").exists():
                marker.unlink(missing_ok=True)


def quiescent(layout):
    states = command("systemctl", "show", *UNITS, "--property=ActiveState", "--value").decode()
    if len(states.split()) != len(UNITS) or any(
        s not in {"inactive", "failed"} for s in states.split()
    ):
        raise BackupError("Pare os serviços, sockets e timer do shinyd antes da manutenção.")
    builds = json.loads(
        command("systemctl", "list-units", "--all", "--output=json", "shinyd-build@*.service")
    )
    if any(v["active"] not in {"inactive", "failed"} for v in builds):
        raise BackupError("Há uma tarefa de build ativa.")
    containers = json.loads(command("podman", "ps", "--all", "--format=json"))
    if any(
        (v.get("Labels") or {}).get("org.shinyd.managed")
        or any(n.startswith("shinyd-") for n in v.get("Names", []))
        for v in containers
    ):
        raise BackupError("Retire os containers do shinyd antes da manutenção.")
    if (layout.builder / "current.json").exists() or list(layout.builder.glob("work-*")):
        raise BackupError("Build interrompido ainda precisa de limpeza pelo builder.")
    for path in (layout.builder / "jobs").glob("*/record.json"):
        with archive.regular(path) as source:
            record = json.load(source)
        if record["status"] not in {"succeeded", "failed", "cancelled"}:
            raise BackupError("Há tarefa pendente no builder.")


def copy_tree(source, target, limit):
    info = source.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise BackupError("Links e arquivos especiais não são aceitos no backup.")
    target.mkdir(mode=0o700)
    count, total = 0, 0
    for base, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            origin = Path(base) / name
            destination = target / origin.relative_to(source)
            info = origin.lstat()
            count += 1
            if count > archive.ENTRIES or info.st_dev != source.stat().st_dev:
                raise BackupError("Árvore excessiva ou ponto de montagem dentro dos dados.")
            if stat.S_ISDIR(info.st_mode):
                destination.mkdir(mode=0o700)
            else:
                total += info.st_size
                if total > limit:
                    raise BackupError("Dados excedem o limite do backup.")
                archive.copy_file(origin, destination, limit)
    # File modes of app data are kept; private staging directories remain searchable.
    return total


def snapshot(layout, root, limit):
    safe_directory(layout.control / "keys", owner=pwd.getpwnam("shinyd-control").pw_uid)
    for path in (layout.runtime, layout.builder, layout.data):
        safe_directory(path)
    for name in ("control", "control/keys", "config", "builder", "runtime", "images"):
        (root / name).mkdir(mode=0o700)
    for name in ("django", "gateway", "secrets"):
        archive.copy_file(layout.control / f"keys/{name}.key", root / f"control/keys/{name}.key")
    for name, path in layout.config.items():
        safe_directory(path.parent)
        archive.copy_file(path, root / "config" / name)
    safe_directory(layout.control, owner=pwd.getpwnam("shinyd-control").pw_uid)
    # SQLite's backup API also handles a cleanly stopped database with a remaining WAL.
    with archive.regular(layout.control / "metadata.sqlite3"):
        with contextlib.closing(
            sqlite3.connect((layout.control / "metadata.sqlite3").as_uri() + "?mode=ro", uri=True)
        ) as src:
            size = (
                src.execute("PRAGMA page_count").fetchone()[0]
                * src.execute("PRAGMA page_size").fetchone()[0]
            )
            if size > limit:
                raise BackupError("Banco excede o limite do backup.")
            archive.space(root, size)
            with contextlib.closing(sqlite3.connect(root / "control/metadata.sqlite3")) as dst:
                src.backup(dst)
                # The backup inherits WAL from production. Keep the private copy
                # self-contained before validation opens it, without changing the source.
                if dst.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise BackupError("Não foi possível consolidar a cópia do banco de metadados.")
    for source, target in (
        (layout.control / "sources", root / "control/sources"),
        (layout.data, root / "data"),
        (layout.builder / "artifacts", root / "builder/artifacts"),
    ):
        copy_tree(source, target, limit)
    for path in layout.runtime.glob("*.json"):
        archive.copy_file(path, root / "runtime" / path.name, limit)
    # Reject unsafe app-created entries before exporting large images.
    files = archive.inventory(root, limit)
    total = sum(v["size"] for v in files.values())
    images = inspect_snapshot(root)
    for image in images:
        metadata = json.loads(command("podman", "image", "inspect", image))[0]
        if "sha256:" + metadata["Id"].removeprefix("sha256:") != image:
            raise BackupError("Imagem local não corresponde ao ID publicado.")
    # Size sums repeat shared layers. Enforce limits on the actual exported stream.
    expanded = backup_images.export(images, root, limit - total)
    return images, expanded


def empty_destination(layout):
    paths = [
        *layout.config.values(),
        layout.root / "etc/shinyd-gateway/key",
        layout.root / "etc/shinyd-tls/Caddyfile",
    ]
    if any(p.exists() or p.is_symlink() for p in paths):
        raise BackupError("Restaure somente em instalação vazia; configurações já existem.")
    for root in (layout.control, layout.builder, layout.runtime, layout.data):
        safe_directory(
            root, owner=pwd.getpwnam("shinyd-control").pw_uid if root == layout.control else 0
        )
        for base, dirs, files in os.walk(root, followlinks=False):
            if any((Path(base) / n).is_symlink() for n in dirs):
                raise BackupError("Destino contém links.")
            for name in files:
                path = Path(base) / name
                if path in {layout.builder / ".lock", layout.runtime / ".lock"}:
                    with archive.regular(path):
                        pass
                    continue
                raise BackupError("Restaure somente em instalação vazia; dados já existem.")


def install(root, manifest, layout):
    """Copy only into empty paths. Failure removes only paths created by this transaction."""
    empty_destination(layout)
    control = pwd.getpwnam("shinyd-control")
    created = []
    marker = layout.work / "restore-incomplete"
    marker.write_text(manifest["id"] + "\n")
    os.sync()  # Durable inhibition of service start before the first installed file.
    try:
        for name, record in sorted(manifest["files"].items()):
            if name == "images" or name.startswith("images/"):
                continue
            if name == "config":
                continue
            parts = name.split("/")
            uid, gid, mode = 0, 0, 0o700 if record["kind"] == "directory" else 0o600
            if parts[0] == "config":
                destination = layout.config[parts[1]]
                group = {
                    "control.json": "shinyd-config",
                    "cert.pem": "shinyd-ingress",
                    "tls.key": "shinyd-ingress",
                }.get(parts[1])
                if group:
                    gid, mode = grp.getgrnam(group).gr_gid, 0o640
            else:
                destination = getattr(layout, parts[0]).joinpath(*parts[1:])
                if parts[0] == "control":
                    uid, gid = control.pw_uid, control.pw_gid
                elif parts[0] == "data" and len(parts) >= 3:
                    uid = gid = 65534
                    mode = (
                        0o700 if record["kind"] == "directory" else 0o600 | (record["mode"] & 0o100)
                    )
            if record["kind"] == "directory":
                if destination.exists():
                    safe_directory(destination, owner=uid)
                    continue
                destination.mkdir(mode=0o700)
                created.append(destination)
            else:
                safe_directory(destination.parent, owner=uid)
                archive.copy_file(root / name, destination, created=created)
            os.chown(destination, uid, gid)
            destination.chmod(mode)
        config = json.loads((root / "config/control.json").read_bytes())
        extra = {
            layout.root / "etc/shinyd-gateway/key": (
                (root / "control/keys/gateway.key").read_bytes(),
                0,
                grp.getgrnam("shinyd-gateway").gr_gid,
                0o640,
            ),
            layout.root / "etc/shinyd-tls/Caddyfile": (
                caddyfile(config).encode(),
                0,
                grp.getgrnam("shinyd-ingress").gr_gid,
                0o640,
            ),
            layout.control / "restore.json": (
                json.dumps(
                    {
                        "backup_id": manifest["id"],
                        "restored_at": datetime.now(UTC).isoformat(),
                        "apps_stopped": True,
                    }
                ).encode(),
                control.pw_uid,
                control.pw_gid,
                0o600,
            ),
        }
        for path, (data, uid, gid, mode) in extra.items():
            safe_directory(path.parent, owner=uid)
            with path.open("xb") as output:
                created.append(path)
                output.write(data)
                os.fchown(output.fileno(), uid, gid)
                os.fchmod(output.fileno(), mode)
        os.sync()
    except BaseException:
        for path in reversed(created):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        os.sync()
        marker.unlink()
        raise
    marker.unlink()
    os.sync()


def execute(args, layout):
    limit = args.max_gib * 1024**3
    if args.operation == "resume":
        # A killed process releases flock. Never remove the incomplete-restore interlock.
        safe_directory(layout.work)
        fd = os.open(layout.work / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as lock:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise BackupError("Arquivo de trava inseguro.")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (layout.work / "restore-incomplete").exists():
                raise BackupError(
                    "Restauração interrompida exige conferência manual; consulte o guia."
                )
            (layout.work / "maintenance").unlink(missing_ok=True)
        return {"operation": "resume", "services_started": False}
    with maintenance(layout):
        quiescent(layout)
        with tempfile.TemporaryDirectory(prefix="work-", dir=layout.work) as temporary:
            work = Path(temporary)
            root, tar = work / "snapshot", work / "backup.tar"
            if args.operation == "create":
                safe_directory(args.file.parent)
                if args.file.exists() or args.file.is_symlink():
                    raise BackupError("O arquivo de destino já existe; nada será sobrescrito.")
                if any(
                    args.file.is_relative_to(p)
                    for p in (
                        layout.work,
                        layout.control,
                        layout.builder,
                        layout.runtime,
                        layout.data,
                    )
                ):
                    raise BackupError("Guarde o backup fora dos diretórios da instalação.")
                root.mkdir(mode=0o700)
                images, expanded = snapshot(layout, root, limit)
                metadata = {
                    "format": "shinyd-cold-backup-v2",
                    "id": str(uuid.uuid4()),
                    "created_at": datetime.now(UTC).isoformat(),
                    "compatibility": compatibility(),
                    "images": images,
                    "image_uncompressed_bytes": expanded,
                }
                manifest = archive.pack(root, tar, metadata, limit)
                shutil.rmtree(root)  # All content now belongs to the complete private TAR.
                archive.space(args.file.parent, tar.stat().st_size + 1024**2)
                # Encrypt beside destination, then link atomically without replacing a file.
                with tempfile.TemporaryDirectory(
                    prefix=".shinyd-backup-", dir=args.file.parent
                ) as out:
                    encrypted = Path(out) / "backup.age"
                    archive.age(tar, encrypted, recipient=args.recipient, limit=limit + 1024**3)
                    quiescent(layout)
                    os.link(encrypted, args.file)
                    os.sync()
            else:
                if args.operation == "restore":
                    empty_destination(layout)
                archive.space(work, 2 * args.file.stat().st_size)
                archive.age(args.file, tar, identity=args.identity, limit=limit)
                manifest = archive.unpack(tar, root, limit)
                tar.unlink()  # Release authenticated transport before loading images.
                if str(uuid.UUID(manifest["id"])) != manifest["id"]:
                    raise BackupError("Identificador do backup inválido.")
                if args.operation == "restore" and manifest["compatibility"] != compatibility():
                    raise BackupError(
                        "Instale as mesmas versões de pacotes, sistema e arquitetura."
                    )
                images = inspect_snapshot(root)
                image_files = backup_images.validate(root, manifest, images, limit)
                if args.operation == "restore":
                    reset_access(root, manifest["id"])
                    for name in image_files:
                        # Podman may unpack image layers in TMPDIR. Keep them private,
                        # budgeted and removable with this operation's staging area.
                        archive.space(work, manifest.get("image_uncompressed_bytes", 0))
                        command(
                            "podman",
                            "load",
                            "--quiet",
                            "--input",
                            str(root / name),
                            timeout=1800,
                            temporary=work,
                        )
                    for image in images:
                        observed = json.loads(command("podman", "image", "inspect", image))[0]
                        if "sha256:" + observed["Id"].removeprefix("sha256:") != image:
                            raise BackupError("Imagem restaurada diverge do ID registrado.")
                    quiescent(layout)
                    install(root, manifest, layout)
            return {
                "operation": args.operation,
                "backup_id": manifest["id"],
                "files": len(manifest["files"]),
                "images": len(images),
                "services_started": False,
                "required_compatibility": manifest["compatibility"],
            }


def main():
    parser = argparse.ArgumentParser(description="Backup criptografado em janela de manutenção.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("create", "verify", "restore", "resume"):
        action = sub.add_parser(name)
        action.add_argument("--max-gib", type=int, default=64)
        if name != "resume":
            action.add_argument("file", type=Path)
            if name == "create":
                action.add_argument("--recipient", required=True)
            else:
                action.add_argument("--identity", required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Execute com sudo; a interface não recebe chaves de recuperação.")
    if not 1 <= args.max_gib <= 1024:
        parser.error("--max-gib deve estar entre 1 e 1024.")
    if args.operation != "resume":
        args.file = Path(os.path.abspath(args.file))
    os.umask(0o077)
    try:
        print(json.dumps(execute(args, Layout()), sort_keys=True))
    except BackupError as exc:
        raise SystemExit(str(exc)) from None
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        InvalidTag,
        UnsupportedAlgorithm,
        x509.ExtensionNotFound,
        RecursionError,
        subprocess.SubprocessError,
        archive.tarfile.TarError,
    ):
        raise SystemExit(
            "Backup/restauração não concluído. Confira o guia, arquivos e chaves; "
            "os serviços não foram iniciados."
        ) from None


if __name__ == "__main__":
    main()
