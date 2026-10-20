"""Fixed-argument local runtime operations; no application output is returned."""

import json
import os
import re
import selectors
import signal
import subprocess
import time
from pathlib import Path

from .policy import BuildError, fail

PODMAN = "/usr/bin/podman"
MAINTENANCE = "shinyd-workloads-maintenance.slice"
WORKLOADS = Path("/sys/fs/cgroup/shinyd.slice/shinyd-workloads.slice")


def command(args, *, timeout=30, code="assembly", monitor=lambda: None):
    argv = [PODMAN, "--events-backend=none", *args]
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}
    output = bytearray()
    process = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline, next_check = time.monotonic() + timeout, 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    fail("interrupted")
                if time.monotonic() >= next_check:
                    monitor()
                    next_check = time.monotonic() + 1
                for key, _ in selector.select(timeout=0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > 256 * 1024:
                        fail(code)
            if process.wait(timeout=max(0.01, deadline - time.monotonic())):
                fail(code)
        return bytes(output)
    except subprocess.TimeoutExpired:
        fail("interrupted")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        process.stdout.close()


def inspect_image(image):
    try:
        rows = json.loads(command(["image", "inspect", image]))
        if len(rows) != 1:
            fail("assembly")
        return rows[0]
    except (ValueError, TypeError, KeyError):
        fail("assembly")


def inspect_owned(job):
    name = "shinyd-build-" + job["id"]
    rows = json.loads(command(["ps", "--all", "--format=json", "--filter=name=^" + name + "$"]))
    if not rows:
        return None
    values = json.loads(command(["container", "inspect", name]))
    if len(values) != 1:
        fail("ownership")
    value = values[0]
    labels = value["Config"].get("Labels") or {}
    if (
        labels.get("org.shinyd.managed") != "builder-v1"
        or labels.get("org.shinyd.build") != job["id"]
        or labels.get("org.shinyd.key") != job["key"]
        or value["Image"].removeprefix("sha256:") != job["input_image"].removeprefix("sha256:")
        or value["Config"]["User"] != "65534:65534"
        or value["HostConfig"]["NetworkMode"] != "none"
    ):
        fail("ownership")
    return value


def verify_process(value):
    """Observe protections in the kernel before executing the restore helper."""
    try:
        if not value["State"]["Running"] or value["State"]["Pid"] <= 1:
            fail("isolation")
        process = Path(f"/proc/{value['State']['Pid']}")
        status = dict(line.split(":", 1) for line in (process / "status").read_text().splitlines())
        required = {
            "NoNewPrivs": "1",
            "Seccomp": "2",
            "CapEff": "0000000000000000",
            "CapPrm": "0000000000000000",
            "CapInh": "0000000000000000",
            "CapAmb": "0000000000000000",
        }
        if any(status.get(key, "").strip() != expected for key, expected in required.items()):
            fail("isolation")
        for name, field in (("uid_map", "Uid"), ("gid_map", "Gid")):
            mapping = (process / name).read_text().split()
            if len(mapping) != 3:
                fail("isolation")
            inside, outside, size = map(int, mapping)
            if inside != 0 or outside < 65536 or size < 65536:
                fail("isolation")
            if list(map(int, status[field].split())) != [outside + 65534] * 4:
                fail("isolation")
        profile = value["AppArmorProfile"]
        if not re.fullmatch(r"containers-default-[A-Za-z0-9_.-]{1,100}", profile):
            fail("isolation")
        label = (process / "attr/apparmor/current").read_text().strip()
        stack, _, mode = label.rpartition(" (")
        loaded = Path("/sys/kernel/security/apparmor/profiles").read_text().splitlines()
        if (
            mode not in {"enforce)", "mixed)"}
            or profile not in stack.split("//&")
            or profile + " (enforce)" not in loaded
        ):
            fail("isolation")
        group = (process / "cgroup").read_text()
        if not re.fullmatch(r"[a-f0-9]{64}", value["Id"]):
            fail("isolation")
        if f"/{MAINTENANCE}/" not in group or f"/libpod-{value['Id']}.scope" not in group:
            fail("isolation")
        if (process / "ns/net").stat().st_ino == Path("/proc/1/ns/net").stat().st_ino:
            fail("isolation")
        if (process / "cgroup").read_text() != group:
            fail("isolation")
        return {"apparmor": label, "mapped_uid": True, "network": "none", "maintenance": True}
    except (OSError, ValueError, KeyError, TypeError):
        fail("isolation")


def await_protection(job):
    deadline = time.monotonic() + 15
    while True:
        value = inspect_owned(job)
        try:
            return verify_process(value)
        except BuildError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def memory_available():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, raw = line.partition(":")
        if key in {"MemAvailable", "MemTotal"}:
            values[key] = int(raw.split()[0]) * 1024
    return values["MemTotal"], values["MemAvailable"]


def resources_available(state, *, starting=False):
    """Do not change the server's aggregate budget or claim free RAM twice."""
    try:
        total, available = memory_available()
        reserve = max(1024**3, total * 15 // 100)
        needed = 1024**3 if starting else 0
        if available < reserve + needed:
            fail("admission")
        full = next(
            line
            for line in Path("/proc/pressure/memory").read_text().splitlines()
            if line.startswith("full ")
        )
        pressure = float(dict(item.split("=") for item in full.split()[1:])["avg10"])
        if pressure > 1:
            fail("admission")
        maximum = int((WORKLOADS / "memory.max").read_text())
        used = int((WORKLOADS / "memory.current").read_text())
        if used + needed >= maximum:
            fail("admission")
        fs = os.statvfs(state)
        if fs.f_bavail * fs.f_frsize < 6 * 1024**3:
            fail("admission")
        # Podman storage can be on a different filesystem from the workspace.
        fs = os.statvfs("/var/lib/containers")
        if fs.f_bavail * fs.f_frsize < 6 * 1024**3:
            fail("admission")
    except (OSError, ValueError, KeyError, StopIteration):
        fail("admission")


def remove_owned(job):
    value = inspect_owned(job)
    if value is not None:
        command(["rm", "--force", "shinyd-build-" + job["id"]], code="cleanup")
    # Remove only the temporary tag; never prune shared layers or unrelated images.
    tag = "localhost/shinyd-build-input:" + job["id"]
    images = json.loads(command(["image", "list", "--format=json", "--filter=reference=" + tag]))
    if images:
        image = inspect_image(tag)
        labels = image.get("Labels") or image.get("Config", {}).get("Labels") or {}
        if labels.get("org.shinyd.build") != job["id"]:
            fail("ownership")
        command(["rmi", tag], code="cleanup")
