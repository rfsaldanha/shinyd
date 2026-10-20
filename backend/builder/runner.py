"""Fixed systemd job entry point; reads only root-owned requests."""

import os
import sys
import time
from pathlib import Path

from .api import JOBS
from .build import Builder, save
from .podman import MAINTENANCE
from .policy import MESSAGES, BuildError, fail, private_read, secure_dir, strict_json
from .protocol import TERMINAL, identifier


def run(job_id, *, jobs=JOBS, factory=Builder):
    path = jobs / job_id
    record = strict_json(private_read(path / "record.json"))
    if record["status"] in TERMINAL:
        return
    deadline = time.monotonic() + 900

    def cancelled():
        return (path / "cancel").exists() or time.monotonic() > deadline

    def progress(phase):
        if cancelled():
            fail("interrupted")
        record["phase"] = phase
        save(path / "record.json", record)

    try:
        spec = record["spec"]
        artifact = factory(progress=progress, cancelled=cancelled).build(
            path / "source.zip",
            spec["source_hash"],
            spec["app_id"],
            spec["entrypoint"],
            record["environment"],
        )
        if cancelled():
            fail("interrupted")
        record.update(status="succeeded", phase="prepared", artifact=artifact)
    except Exception as exc:
        code = str(exc) if isinstance(exc, BuildError) and str(exc) in MESSAGES else "storage"
        if code != "busy" and (jobs.parent / "current.json").exists():
            code = "cleanup"
        # Never report cancellation as complete if ownership cleanup failed.
        was_cancelled = (path / "cancel").exists() and code != "cleanup"
        record.update(
            status="cancelled" if was_cancelled else "failed",
            phase="cancelled" if was_cancelled else "failed",
            error=code,
            artifact=None,
        )
    finally:
        (path / "source.zip").unlink(missing_ok=True)
    save(path / "record.json", record)


def main():
    if (
        os.geteuid() != 0
        or len(sys.argv) != 2
        or not identifier(sys.argv[1])
        or f"/{MAINTENANCE}/" not in Path("/proc/self/cgroup").read_text()
    ):
        raise SystemExit("builder_job_invalid")
    os.umask(0o077)
    secure_dir(JOBS / sys.argv[1])
    run(sys.argv[1])
