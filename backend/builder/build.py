"""Single-build, durable offline preparation without changing a live publication."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from control.archives import ArchiveError, inspect_archive

from . import podman
from .policy import (
    IMAGE,
    BuildError,
    canonical,
    digest,
    fail,
    private_read,
    resolve_lock,
    secure_dir,
    strict_json,
)

STATE = Path("/var/lib/shinyd-builder")
HELPERS = Path(__file__).parent
RECIPE = 1
SOURCE_LIMIT = 500 * 1024**2
EXTRACT_LIMIT = 2 * 1024**3


def save(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        fail("storage")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def snapshot(source, target, expected_hash):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    total, sha, deadline = 0, hashlib.sha256(), time.monotonic() + 60
    with os.fdopen(fd, "rb") as original, target.open("xb") as output:
        info = os.fstat(original.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= SOURCE_LIMIT:
            fail("archive")
        while chunk := original.read(1024 * 1024):
            total += len(chunk)
            if total > SOURCE_LIMIT or time.monotonic() > deadline:
                fail("archive")
            sha.update(chunk)
            output.write(chunk)
    if sha.hexdigest() != expected_hash:
        fail("source_changed")


def artifact_key(app_id, manifest, environment, lock):
    helpers = {
        name: hashlib.sha256((HELPERS / name).read_bytes()).hexdigest()
        for name in ("prepare.R", "launch.R")
    }
    return digest(
        {
            "recipe": RECIPE,
            "app_id": app_id,
            "source": manifest["source_hash"],
            "entrypoint": manifest["entrypoint"],
            "environment": environment,
            "lock": lock,
            "helpers": helpers,
        }
    )


def write_context(context, selected, lock, environment, job_id, key):
    context.mkdir(mode=0o700)
    selected.rename(context / "source")
    for path in (context / "source").rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    (context / "source").chmod(0o555)
    (context / "library").mkdir(mode=0o755)
    (context / "lock.json").write_bytes(canonical(lock))
    for name in ("prepare.R", "launch.R"):
        shutil.copyfile(HELPERS / name, context / name)
    # No RUN, ADD, uploaded instructions, uploaded ignorefile, or remote context.
    (context / "ignore").write_text("")
    recipe = f"""FROM {environment["image"]}
USER 0
COPY --chown=0:0 source/ /app/
COPY --chown=0:0 --chmod=0444 prepare.R launch.R lock.json /opt/shinyd/
COPY --chown=65534:65534 library/ /opt/shinyd/library/
USER 65534:65534
WORKDIR /app
ENV HOME=/tmp RENV_CONFIG_AUTOLOADER_ENABLED=FALSE
ENV RENV_CONFIG_CACHE_ENABLED=FALSE RENV_CONFIG_CACHE_SYMLINKS=FALSE
ENV RENV_CONFIG_SANDBOX_ENABLED=FALSE RENV_CONFIG_PAK_ENABLED=FALSE
ENV RENV_PATHS_ROOT=/tmp/renv R_LIBS_USER=/opt/shinyd/library
ENTRYPOINT ["Rscript", "--vanilla", "/opt/shinyd/launch.R"]
CMD []
LABEL org.shinyd.managed=builder-v1 org.shinyd.build={job_id} org.shinyd.key={key}
"""
    (context / "Containerfile").write_text(recipe)


class Builder:
    def __init__(self, state=STATE, progress=lambda phase: None):
        self.state, self.progress = state, progress

    def cleanup(self, job):
        podman.remove_owned(job)
        directory = self.state / ("work-" + job["id"])
        # Source directories are read-only even on the host. Root owns them;
        # no container ever mounts or writes this staging tree.
        shutil.rmtree(directory, ignore_errors=False) if directory.exists() else None
        (self.state / "current.json").unlink(missing_ok=True)

    def recover(self):
        path = self.state / "current.json"
        if not path.exists():
            return
        try:
            job = strict_json(private_read(path))
            if (
                set(job) != {"id", "key", "input_image"}
                or str(uuid.UUID(job["id"])) != job["id"]
                or not re.fullmatch(r"[a-f0-9]{64}", job["key"])
                or (job["input_image"] and not IMAGE.fullmatch(job["input_image"]))
            ):
                fail("ownership")
        except (ValueError, TypeError, KeyError):
            fail("ownership")
        self.progress("recovering")
        self.cleanup(job)

    def cached(self, key):
        path = self.state / "artifacts" / (key + ".json")
        if not path.exists():
            return None
        value = strict_json(private_read(path))
        if value.get("cache_key") != key or not IMAGE.fullmatch(value.get("image", "")):
            fail("ownership")
        try:
            metadata = podman.inspect_image(value["image"])
        except BuildError:
            return None  # A pruned cache is reconstructed; it is not an active publication.
        labels = metadata.get("Labels") or metadata.get("Config", {}).get("Labels") or {}
        if labels.get("org.shinyd.key") != key or labels.get("org.shinyd.artifact") != "v1":
            fail("ownership")
        return {**value, "cache_hit": True}

    def build(self, source, expected_hash, app_id, entrypoint, environment):
        secure_dir(self.state)
        secure_dir(self.state / "artifacts")
        fd = os.open(
            self.state / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        with os.fdopen(fd, "rb") as lockfile:
            info = os.fstat(lockfile.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
                fail("ownership")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail("busy")
            self.recover()
            return self._build(source, expected_hash, app_id, entrypoint, environment)

    def _build(self, source, expected_hash, app_id, entrypoint, environment):
        job_id = str(uuid.uuid4())
        work = self.state / ("work-" + job_id)
        work.mkdir(mode=0o700)
        active = None
        try:
            self.progress("validating")
            snapshot(source, work / "source.zip", expected_hash)
            extracted = work / "extracted"
            extracted.mkdir(mode=0o700)
            try:
                manifest = inspect_archive(
                    work / "source.zip",
                    extracted,
                    max_bytes=EXTRACT_LIMIT,
                    max_entries=50000,
                    entrypoint=entrypoint,
                )
            except (ArchiveError, ValueError, RecursionError):
                fail("archive")
            selected = extracted / manifest["entrypoint"]
            lock = resolve_lock(selected / "renv.lock", environment)
            key = artifact_key(app_id, manifest, environment, lock)
            cached = self.cached(key)
            if cached is not None:
                self.progress("cached")
                return cached
            self.progress("admission")
            podman.resources_available(self.state, starting=True)
            info = json.loads(podman.command(["info", "--format=json"]))
            host = info["host"]
            if (
                host["security"]["rootless"]
                or not host["security"]["apparmorEnabled"]
                or host["cgroupVersion"] != "v2"
                or host["cgroupManager"] != "systemd"
                or not {"cpu", "memory", "pids"} <= set(host["cgroupControllers"])
            ):
                fail("isolation")
            base = podman.inspect_image(environment["image"])
            if base["Id"].removeprefix("sha256:") != environment["image"].removeprefix("sha256:"):
                fail("ownership")
            context = work / "context"
            write_context(context, selected, lock, environment, job_id, key)
            active = {"id": job_id, "key": key, "input_image": ""}
            save(self.state / "current.json", active)

            def monitor():
                podman.resources_available(self.state)

            self.progress("assembling")
            tag = "localhost/shinyd-build-input:" + job_id
            podman.command(
                [
                    "build",
                    "--network=none",
                    "--pull=never",
                    "--layers=false",
                    "--force-rm",
                    "--http-proxy=false",
                    "--ignorefile=" + str(context / "ignore"),
                    "--file=" + str(context / "Containerfile"),
                    "--tag=" + tag,
                    str(context),
                ],
                timeout=180,
                monitor=monitor,
            )
            image = podman.inspect_image(tag)
            active["input_image"] = "sha256:" + image["Id"].removeprefix("sha256:")
            if not IMAGE.fullmatch(active["input_image"]):
                fail("assembly")
            save(self.state / "current.json", active)
            name = "shinyd-build-" + job_id
            podman.command(
                [
                    "run",
                    "--detach",
                    "--pull=never",
                    "--name=" + name,
                    "--network=none",
                    "--userns=auto:size=65536",
                    "--user=65534:65534",
                    "--image-volume=ignore",
                    "--cap-drop=all",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=128",
                    "--cgroups=enabled",
                    "--cgroupns=private",
                    "--cgroup-parent=" + podman.MAINTENANCE,
                    "--restart=no",
                    "--log-driver=none",
                    "--tmpfs=/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                    "--ulimit=fsize=536870912:536870912",
                    "--entrypoint=/bin/sleep",
                    active["input_image"],
                    "600",
                ],
                monitor=monitor,
            )
            protection = podman.await_protection(active)
            self.progress("restoring")
            # stdout from package code is discarded in the container too. It can
            # contain credentials bundled in the upload; the API exposes stages only.
            podman.command(
                [
                    "exec",
                    name,
                    "/bin/sh",
                    "-c",
                    "exec Rscript --vanilla /opt/shinyd/prepare.R >/dev/null 2>&1",
                ],
                timeout=180,
                code="restore",
                monitor=monitor,
            )
            podman.verify_process(podman.inspect_owned(active))
            self.progress("sealing")
            podman.command(["stop", "--time=3", name])
            # Do not retain the sleep override or /tmp. The final process is fixed.
            raw = podman.command(
                [
                    "commit",
                    "--format=oci",
                    "--include-volumes=false",
                    '--change=ENTRYPOINT ["Rscript","--vanilla","/opt/shinyd/launch.R"]',
                    "--change=CMD []",
                    "--change=LABEL org.shinyd.artifact=v1",
                    name,
                ],
                timeout=180,
                monitor=monitor,
            )
            image_id = "sha256:" + raw.decode().strip().removeprefix("sha256:")
            if not IMAGE.fullmatch(image_id):
                fail("assembly")
            result = {
                "schema_version": 1,
                "recipe_version": RECIPE,
                "created_at": datetime.now(UTC).isoformat(),
                "app_id": app_id,
                "cache_key": key,
                "source_hash": expected_hash,
                "entrypoint": manifest["entrypoint"],
                "base_image": environment["image"],
                "image": image_id,
                "r_version": environment["r_version"],
                "lock_hash": digest(lock),
                "packages": {name: value["Version"] for name, value in lock["Packages"].items()},
                "protection": protection,
                "cache_hit": False,
            }
            # Image remains local and immutable. Publication/activation is a
            # separate operation; this code never edits executor policy or app data.
            save(self.state / "artifacts" / (key + ".json"), result)
            self.progress("prepared")
            return result
        finally:
            if active is not None:
                try:
                    self.cleanup(active)
                except Exception:
                    fail("cleanup")  # Keep durable intent; next invocation reconciles it.
            elif work.exists():
                shutil.rmtree(work)
