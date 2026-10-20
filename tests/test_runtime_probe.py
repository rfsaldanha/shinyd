from types import SimpleNamespace

import pytest
import runtime_smoke


def test_probe_retries_only_bounded_429_with_same_identity(monkeypatch):
    calls, pauses = [], []

    def command(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=1 if len(calls) < 3 else 0,
            stderr="executor recusou a operação (HTTP 429)",
            stdout='{"state":"removed"}',
        )

    monkeypatch.setattr(runtime_smoke, "command", command)
    monkeypatch.setattr(runtime_smoke.time, "sleep", pauses.append)
    monkeypatch.setattr(runtime_smoke, "API_RETRIES", [])
    assert runtime_smoke.api("remove", "test-id") == {"state": "removed"}
    assert len(calls) == 3 and all(call == calls[0] for call in calls)
    assert pauses == [5, 5]
    assert len(runtime_smoke.API_RETRIES) == 2


def test_probe_never_retries_negative_tests_or_other_failures(monkeypatch):
    failure = SimpleNamespace(returncode=1, stderr="PRIVATE_SENTINEL", stdout="")
    monkeypatch.setattr(runtime_smoke, "command", lambda *a, **k: failure)
    monkeypatch.setattr(runtime_smoke.time, "sleep", lambda _: pytest.fail("unexpected retry"))
    assert runtime_smoke.api("ensure", "test-id", check=False) is failure
    with pytest.raises(RuntimeError, match="runtime_unavailable") as exc:
        runtime_smoke.api("remove", "test-id")
    assert "PRIVATE_SENTINEL" not in str(exc.value)


def test_probe_stops_after_four_busy_responses(monkeypatch):
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=1,
            stderr="executor recusou a operação (HTTP 429)",
            stdout="",
        )

    monkeypatch.setattr(runtime_smoke, "command", command)
    monkeypatch.setattr(runtime_smoke.time, "sleep", lambda _: None)
    monkeypatch.setattr(runtime_smoke, "API_RETRIES", [])
    with pytest.raises(RuntimeError, match="HTTP 429"):
        runtime_smoke.api("remove", "test-id")
    assert len(calls) == 4
