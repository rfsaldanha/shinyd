import argparse
import json
import os
import re
import signal
import sys
import uuid
from pathlib import Path

from .build import Builder
from .podman import MAINTENANCE
from .policy import MESSAGES, BuildError, fail, load_environment


def main():
    parser = argparse.ArgumentParser(description="Preparar ZIP Shiny em ambiente offline aprovado.")
    parser.add_argument("--policy", type=Path, default=Path("/etc/shinyd-builder/policy.json"))
    parser.add_argument("--environment", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--entrypoint", default="")
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0 or f"/{MAINTENANCE}/" not in Path("/proc/self/cgroup").read_text():
            fail("isolation")
        if str(uuid.UUID(args.app_id)) != args.app_id or not re.fullmatch(
            r"[a-f0-9]{64}", args.sha256
        ):
            fail("archive")
        os.umask(0o077)
        signal.signal(signal.SIGTERM, lambda *_: fail("interrupted"))
        signal.signal(signal.SIGINT, lambda *_: fail("interrupted"))
        environment = load_environment(args.policy, args.environment)

        def progress(phase):
            print(json.dumps({"event": "build.phase", "phase": phase}), file=sys.stderr, flush=True)

        result = Builder(progress=progress).build(
            args.archive, args.sha256, args.app_id, args.entrypoint, environment
        )
        print(json.dumps({"passed": True, "artifact": result}, ensure_ascii=False, indent=2))
        return 0
    except (BuildError, OSError, ValueError, KeyError, TypeError):
        exception = sys.exception()
        code = str(exception) if isinstance(exception, BuildError) else "storage"
        code = code if code in MESSAGES else "storage"
        print(
            json.dumps(
                {"passed": False, "error": code, "message": MESSAGES[code]}, ensure_ascii=False
            )
        )
        return 1
