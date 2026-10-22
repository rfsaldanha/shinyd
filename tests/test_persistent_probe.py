import io
import uuid
from contextlib import redirect_stdout
from types import SimpleNamespace

import persistent_browser_probe as probe
import pytest

from control import environment, runtime_client
from control.models import Application


@pytest.mark.parametrize(
    "responses,expected,passed",
    [
        (["data_in_use"], ["data_in_use"], True),
        (["runtime_busy", "data_in_use"], ["runtime_busy", "data_in_use"], True),
        (["runtime_busy"] * 4, ["runtime_busy"] * 4, False),
        (["runtime_busy", "accepted"], ["runtime_busy", "accepted"], False),
        (["admission_denied"], ["admission_denied"], False),
        (["artifact_rejected"], ["unexpected_refusal"], False),
        (["runtime_unavailable"], ["runtime_unavailable"], False),
    ],
)
def test_single_writer_probe_requires_specific_refusal_and_bounds_busy_retries(
    monkeypatch, responses, expected, passed
):
    app_id, contender = str(uuid.uuid4()), str(uuid.uuid4())
    spec = {"app_id": app_id, "data_mode": "persistent", "config_revision": 2}
    publication = SimpleNamespace(runtime_spec=spec)
    secret = "fixture-secret-must-not-appear-in-diagnostics"
    calls, waits, checks = [], [], []
    replies = iter(responses)

    def lookup(*, pk):
        assert pk == app_id
        return SimpleNamespace(active_publication=publication)

    def request(method, identity, received_spec, *, environment):
        assert method == "PUT" and identity == contender and received_spec == spec
        assert environment == {"TOKEN": secret}
        calls.append(identity)
        response = next(replies)
        if response == "runtime_unavailable":
            raise runtime_client.RuntimeUnavailable
        if response != "accepted":
            raise runtime_client.RuntimeRejected(response)
        return {"state": "ready"}

    def admin(code):
        # Execute the exact code sent to shinyd-admin, with only the external
        # runtime and fixture lookup replaced. Do not merely mock its output.
        output = io.StringIO()
        with redirect_stdout(output):
            exec(code, {})
        return output.getvalue()

    def check(name, value):
        assert value, name
        checks.append(name)

    monkeypatch.setattr(Application.objects, "get", lookup)
    monkeypatch.setattr(environment, "reveal", lambda p: {"TOKEN": secret})
    monkeypatch.setattr(runtime_client, "request", request)
    monkeypatch.setattr(probe.time, "sleep", waits.append)
    report = {}
    if passed:
        probe.verify_single_writer(admin, check, report, app_id, contender)
    else:
        with pytest.raises(AssertionError, match="data_second_writer_denied"):
            probe.verify_single_writer(admin, check, report, app_id, contender)
    assert report == {"data_second_writer_outcomes": expected}
    assert secret not in repr(report)
    assert calls == [contender] * len(expected)
    assert waits == [5] * (len(expected) - 1)
    assert checks == (["data_second_writer_denied"] if passed else [])


@pytest.mark.parametrize(
    "output",
    [
        "",
        "REFUSAL=data_in_use\nREFUSAL=accepted\n",
        "REFUSAL=fixture-secret-not-a-runtime-code\n",
    ],
)
def test_single_writer_probe_fails_closed_on_missing_or_invalid_diagnostic(output):
    report, checks = {}, []
    probe.verify_single_writer(
        lambda code: output,
        lambda name, value: checks.append(value),
        report,
        str(uuid.uuid4()),
        str(uuid.uuid4()),
    )
    assert checks == [False]
    assert report == {"data_second_writer_outcomes": ["invalid_probe_result"]}
