from types import SimpleNamespace

import pytest
from probe_jobs import JobOutcomeError, wait_job


@pytest.mark.parametrize(
    "expected,observed",
    [("succeeded", "failed"), ("succeeded", "cancelled"), ("failed", "succeeded")],
)
def test_terminal_job_failure_preserves_only_status_and_disposes_browser_handle(expected, observed):
    calls, released = [], []

    def wait(script, *, timeout):
        calls.append(timeout)
        return SimpleNamespace(json_value=lambda: observed, dispose=lambda: released.append(True))

    page = SimpleNamespace(wait_for_function=wait)
    with pytest.raises(JobOutcomeError) as error:
        wait_job(page, timeout=1500, expected=expected)
    assert error.value.expected == expected and error.value.observed == observed
    assert str(error.value) == "unexpected_job_outcome:" + observed
    assert calls == [1500] and released == [True]


def test_pending_job_timeout_is_propagated_without_retry():
    calls = []

    def wait(script, *, timeout):
        calls.append(timeout)
        raise TimeoutError

    with pytest.raises(TimeoutError):
        wait_job(SimpleNamespace(wait_for_function=wait), timeout=100)
    assert calls == [100]
