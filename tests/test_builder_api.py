import hashlib
import io
import json
import os
import threading
import uuid

import pytest

from builder import api, policy, runner
from builder.build import save
from control.agent_client import UnixConnection


@pytest.fixture
def service(tmp_path, monkeypatch):
    original = policy.private_read
    monkeypatch.setattr(api, "private_read", lambda p: original(p, owner=os.getuid()))
    monkeypatch.setattr(runner, "private_read", lambda p: original(p, owner=os.getuid()))
    environment = {"image": "sha256:" + "a" * 64, "r_version": "4.4.3", "packages": {}}
    monkeypatch.setattr(api, "load_catalog", lambda _: {"approved": environment})
    root = tmp_path / "jobs"
    root.mkdir(mode=0o700)
    calls = []

    def controller(*args):
        calls.append(args)
        return "active"

    return api.Jobs(root, controller=controller), calls


def spec(content=b"fixture"):
    return {
        "app_id": str(uuid.uuid4()),
        "source_hash": hashlib.sha256(content).hexdigest(),
        "environment": "approved",
        "entrypoint": ".",
    }


def test_durable_submission_retry_conflict_and_single_build(service):
    jobs, calls = service
    identity, value = str(uuid.uuid4()), spec()
    first = jobs.submit(identity, value, io.BytesIO(b"fixture"), 7)
    assert first["status"] == "running"
    assert jobs.submit(identity, value, io.BytesIO(b""), 7) == first
    assert calls.count(("start", api.unit(identity))) == 1
    with pytest.raises(policy.BuildError, match="ownership"):
        jobs.submit(identity, spec(), io.BytesIO(b"fixture"), 7)
    with pytest.raises(policy.BuildError, match="busy"):
        jobs.submit(str(uuid.uuid4()), spec(), io.BytesIO(b"fixture"), 7)
    assert jobs.read(identity)["environment"]["image"].startswith("sha256:")


def test_upload_failures_leave_no_record_or_source(service):
    jobs, _ = service
    for data, length, expected in [(b"short", 7, "archive"), (b"invalid", 7, "source_changed")]:
        with pytest.raises(policy.BuildError, match=expected):
            jobs.submit(str(uuid.uuid4()), spec(), io.BytesIO(data), length)
        assert not list(jobs.root.iterdir())


@pytest.mark.parametrize(
    "field,value",
    [
        ("archive", "/etc/shadow"),
        ("command", "sh"),
        ("image", "root-image"),
        ("app_id", "../root"),
        ("environment", "../policy"),
        ("entrypoint", "../app"),
    ],
)
def test_no_privileged_paths_or_commands(service, field, value):
    jobs, calls = service
    request = {**spec(), field: value}
    with pytest.raises(policy.BuildError, match="archive"):
        jobs.submit(str(uuid.uuid4()), request, io.BytesIO(b"fixture"), 7)
    assert calls == []


def test_cancel_before_submit_is_a_durable_tombstone(service):
    jobs, calls = service
    identity = str(uuid.uuid4())
    jobs.cancel(identity)
    assert jobs.submit(identity, spec(), io.BytesIO(b"fixture"), 7)["status"] == "cancelled"
    assert not calls


def test_interrupted_upload_can_resume_same_id(service):
    jobs, _ = service
    identity = str(uuid.uuid4())
    path = jobs.root / identity
    path.mkdir()
    (path / "source.zip").write_bytes(b"partial upload")
    assert jobs.submit(identity, spec(), io.BytesIO(b"fixture"), 7)["status"] == "running"


def test_interrupted_child_with_cleanup_intent_is_not_reported_cancelled(service):
    jobs, _ = service
    identity = str(uuid.uuid4())
    jobs.submit(identity, spec(), io.BytesIO(b"fixture"), 7)
    jobs.cancel(identity)
    save(jobs.root.parent / "current.json", {"pending": True})
    jobs.controller = lambda *_: "failed"
    result = jobs.get(identity)
    assert result["status"] == "failed" and result["error"] == "cleanup"


def test_crashed_child_is_terminal_and_never_restarted(service):
    jobs, calls = service
    identity, value = str(uuid.uuid4()), spec()
    jobs.submit(identity, value, io.BytesIO(b"fixture"), 7)
    jobs.controller = lambda *_: "failed"
    assert jobs.get(identity)["status"] == "failed"
    assert not (jobs.root / identity / "source.zip").exists()
    assert jobs.submit(identity, value, io.BytesIO(b"fixture"), 7)["error"] == "interrupted"
    assert calls.count(("start", api.unit(identity))) == 1


def test_completion_racing_systemd_query_is_preserved(service):
    jobs, _ = service
    identity = str(uuid.uuid4())
    jobs.submit(identity, spec(), io.BytesIO(b"fixture"), 7)

    def completed(*_):
        record = jobs.read(identity)
        record.update(status="succeeded", phase="prepared", artifact={"image": "immutable"})
        save(jobs.root / identity / "record.json", record)
        return "inactive"

    jobs.controller = completed
    assert jobs.get(identity)["status"] == "succeeded"


def test_runner_cancel_and_safe_failure(service):
    jobs, _ = service
    identity = str(uuid.uuid4())
    jobs.submit(identity, spec(), io.BytesIO(b"fixture"), 7)
    jobs.cancel(identity)

    class CancelledBuild:
        def __init__(self, progress, cancelled):
            assert cancelled()
            self.progress = progress

        def build(self, *_):
            self.progress("validating")

    runner.run(identity, jobs=jobs.root, factory=CancelledBuild)
    assert jobs.get(identity)["status"] == "cancelled"
    assert not (jobs.root / identity / "source.zip").exists()

    identity = str(uuid.uuid4())
    jobs.submit(identity, spec(), io.BytesIO(b"fixture"), 7)

    class BrokenBuild:
        def __init__(self, **_):
            pass

        def build(self, *_):
            raise RuntimeError("PRIVATE_SENTINEL")

    runner.run(identity, jobs=jobs.root, factory=BrokenBuild)
    assert "PRIVATE_SENTINEL" not in json.dumps(jobs.read(identity))
    assert jobs.get(identity)["error"] == "storage"


def test_real_unix_peer_identity_and_typed_transport(service, tmp_path):
    jobs, _ = service
    path = str(tmp_path / "api.sock")
    server = api.Server(path, jobs, os.getuid())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(method, path, body=None, headers=None):
        connection = UnixConnection(str(tmp_path / "api.sock"), os.getuid())
        connection.host = "builder"
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        assert call("GET", "/v1/environments")[0] == 200
        identity = str(uuid.uuid4())
        status, raw = call(
            "PUT",
            "/v1/builds/" + identity,
            b"fixture",
            {
                "Content-Type": "application/zip",
                "X-Shinyd-Build": json.dumps(spec()),
            },
        )
        assert status == 200 and json.loads(raw)["id"] == identity
        assert call("GET", "/v1/builds/" + identity + "?path=/etc/shadow")[0] == 400
        server.uid = os.getuid() + 1
        assert call("GET", "/v1/environments")[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
