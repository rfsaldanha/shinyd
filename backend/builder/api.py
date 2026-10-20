"""Root-owned asynchronous builds, served only on an authenticated Unix socket."""

import fcntl
import hashlib
import os
import pwd
import socket
import socketserver
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .build import SOURCE_LIMIT, STATE, save
from .policy import BuildError, canonical, fail, load_catalog, private_read, secure_dir, strict_json
from .protocol import TERMINAL, identifier, validate_spec

POLICY = Path("/etc/shinyd-builder/policy.json")
JOBS = STATE / "jobs"


def unit(job_id):
    return f"shinyd-build@{job_id}.service"


def systemctl(*arguments):
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
        if result.returncode:
            fail("storage")
        return result.stdout.decode().strip()
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        fail("storage")


def view(record):
    return {k: record[k] for k in ("id", "spec", "status", "phase", "error", "artifact")}


class Jobs:
    def __init__(self, root=JOBS, policy=POLICY, controller=systemctl):
        self.root, self.policy, self.controller = root, policy, controller
        self.lock = threading.Lock()

    def read(self, job_id):
        path = self.root / job_id / "record.json"
        if not path.exists():
            return None
        return strict_json(private_read(path))

    def get(self, job_id):
        with self.lock:
            return self._get(job_id)

    def _get(self, job_id):
        record = self.read(job_id)
        if record is None:
            return None
        if record["status"] not in TERMINAL:
            state = self.controller("show", unit(job_id), "--property=ActiveState", "--value")
            # Child writes its terminal record before exiting. Re-read after the
            # systemd query to avoid overwriting a completion racing this request.
            record = self.read(job_id)
            if state in {"inactive", "failed"} and record["status"] not in TERMINAL:
                cancelled = (self.root / job_id / "cancel").exists()
                record.update(
                    status="cancelled" if cancelled else "failed",
                    phase="cancelled" if cancelled else "failed",
                    error="cleanup"
                    if (self.root.parent / "current.json").exists()
                    else "interrupted",
                )
                if record["error"] == "cleanup":
                    record.update(status="failed", phase="failed")
                save(self.root / job_id / "record.json", record)
                (self.root / job_id / "source.zip").unlink(missing_ok=True)
        return view(record)

    def _new_directory(self, job_id):
        path = self.root / job_id
        if path.is_dir() and not (path / "record.json").exists():
            # Interrupted upload never became an accepted job. These are the
            # only files created before that durable acceptance point.
            (path / "source.zip").unlink(missing_ok=True)
            for temporary in path.glob(".tmp-*"):
                temporary.unlink()
            path.rmdir()  # Unexpected contents fail closed rather than get deleted.
        # Bounded history, no hidden root queue. Retention UI is a later milestone.
        if sum(1 for item in self.root.iterdir() if identifier(item.name)) >= 1000:
            fail("admission")
        path.mkdir(mode=0o700)
        return path

    def submit(self, job_id, spec, stream, length):
        validate_spec(spec)
        if not 0 < length <= SOURCE_LIMIT:
            fail("archive")
        if not self.lock.acquire(blocking=False):
            fail("busy")
        path = None
        accepted = False
        try:
            existing = self._get(job_id)
            if existing:
                if existing["spec"] not in (None, spec):
                    fail("ownership")
                return existing
            for other in self.root.iterdir():
                if identifier(other.name):
                    record = self._get(other.name)
                    if record and record["status"] not in TERMINAL:
                        fail("busy")
            catalog = load_catalog(self.policy)
            if spec["environment"] not in catalog:
                fail("policy")
            environment = catalog[spec["environment"]]
            # Check before receiving a potentially large upload; the builder
            # repeats full memory/disk admission before invoking Podman.
            fs = os.statvfs(self.root)
            if fs.f_bavail * fs.f_frsize < length + 6 * 1024**3:
                fail("admission")
            path = self._new_directory(job_id)
            remaining, sha, deadline = length, hashlib.sha256(), time.monotonic() + 60
            with (path / "source.zip").open("xb") as output:
                while remaining:
                    if time.monotonic() > deadline:
                        fail("interrupted")
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        fail("archive")
                    output.write(chunk)
                    sha.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if sha.hexdigest() != spec["source_hash"]:
                fail("source_changed")
            record = {
                "id": job_id,
                "spec": spec,
                "environment": environment,
                "status": "running",
                "phase": "accepted",
                "error": "",
                "artifact": None,
            }
            save(path / "record.json", record)
            accepted = True
            # Only a UUID is passed to a fixed, installed systemd template.
            # A lost response is reconciled by GET, never by a new build ID.
            self.controller("start", unit(job_id))
            return view(record)
        finally:
            if path is not None and not accepted:
                (path / "source.zip").unlink(missing_ok=True)
                path.rmdir()
            self.lock.release()

    def cancel(self, job_id):
        with self.lock:
            record = self._get(job_id)
            if record and record["status"] in TERMINAL:
                return record
            if record is None:
                path = self._new_directory(job_id)
                record = {
                    "id": job_id,
                    "spec": None,
                    "status": "cancelled",
                    "phase": "cancelled",
                    "error": "",
                    "artifact": None,
                }
                # A tombstone prevents a delayed worker submitting after cancel.
                save(path / "record.json", record)
                return record
            save(self.root / job_id / "cancel", {"cancel": True})
            return record


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # No paths, uploaded headers, application output or exception text.

    def send_json(self, status, payload):
        raw = canonical(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def handle_expect_100(self):
        self.send_json(400, {"error": "archive"})
        return False

    def dispatch(self):
        try:
            if (
                self.headers.get("Host") != "builder"
                or "?" in self.path
                or "%" in self.path
                or self.headers.get("Transfer-Encoding")
                or self.headers.get("Upgrade")
                or self.headers.get("Expect")
                or sum(len(k) + len(v) for k, v in self.headers.items()) > 8192
                or any(len(self.headers.get_all(k)) != 1 for k in self.headers)
            ):
                fail("archive")
            length = int(self.headers.get("Content-Length", "0"))
            if self.command == "GET" and self.path == "/v1/environments" and length == 0:
                catalog = load_catalog(self.server.jobs.policy)
                return self.send_json(
                    200,
                    {
                        "environments": [
                            {"id": name, "r_version": value["r_version"]}
                            for name, value in sorted(catalog.items())
                        ]
                    },
                )
            parts = self.path.split("/")
            if len(parts) not in {4, 5} or parts[1:3] != ["v1", "builds"]:
                return self.send_json(404, {"error": "not_found"})
            job_id = parts[3]
            if not identifier(job_id):
                fail("archive")
            if self.command == "GET" and len(parts) == 4 and length == 0:
                result = self.server.jobs.get(job_id)
                return (
                    self.send_json(200, result)
                    if result
                    else self.send_json(404, {"error": "not_found"})
                )
            if self.command == "POST" and parts[4:] == ["cancel"] and length == 0:
                return self.send_json(200, self.server.jobs.cancel(job_id))
            if (
                self.command == "PUT"
                and len(parts) == 4
                and self.headers.get("Content-Type") == "application/zip"
            ):
                spec = strict_json(self.headers.get("X-Shinyd-Build", ""))
                result = self.server.jobs.submit(job_id, spec, self.rfile, length)
                return self.send_json(200, result)
            fail("archive")
        except BuildError as exc:
            code = str(exc)
            status = 429 if code in {"busy", "admission"} else 400 if code == "archive" else 409
            self.send_json(status, {"error": code})
        except (ValueError, TypeError, KeyError, RecursionError):
            self.send_json(400, {"error": "archive"})
        except OSError:
            self.send_json(503, {"error": "storage"})

    do_GET = do_PUT = do_POST = dispatch


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, address, jobs, uid, *, activated=None):
        self.jobs, self.uid = jobs, uid
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler, bind_and_activate=activated is None)
        if activated is not None:
            self.socket.close()
            self.socket = activated

    def verify_request(self, request, _):
        _, uid, _ = struct.unpack(
            "3i", request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        request.settimeout(3)
        if uid != self.uid:
            request.sendall(
                b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            return False
        return True

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, *_):
        pass


def main():
    if os.geteuid() != 0 or os.environ.get("LISTEN_PID") != str(os.getpid()):
        raise SystemExit("builder_socket_activation_required")
    if os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("builder_socket_activation_required")
    os.umask(0o077)
    secure_dir(JOBS)
    # One daemon owns admission; its child builds also take the existing flock.
    with (JOBS / ".api-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        listener = socket.socket(fileno=3)
        if (
            listener.family != socket.AF_UNIX
            or listener.getsockname() != "/run/shinyd-builder/control.sock"
        ):
            raise SystemExit("builder_socket_invalid")
        Server(
            None, Jobs(), pwd.getpwnam("shinyd-control").pw_uid, activated=listener
        ).serve_forever()
