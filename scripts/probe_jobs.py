"""Observe terminal browser job states without waiting for an impossible success."""


class JobOutcomeError(RuntimeError):
    def __init__(self, expected, observed):
        self.expected = expected
        self.observed = observed
        super().__init__("unexpected_job_outcome:" + observed)


def wait_job(page, *, timeout, expected="succeeded"):
    if expected not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("invalid_expected_job_status")
    handle = page.wait_for_function(
        """() => {
            const text = document.querySelector('#job-status h2')?.textContent.trim();
            if (text === 'Concluído') return 'succeeded';
            if (text === 'Falhou') return 'failed';
            if (text === 'Cancelado') return 'cancelled';
            return false;
        }""",
        timeout=timeout,
    )
    try:
        outcome = handle.json_value()
    finally:
        handle.dispose()
    if outcome != expected:
        raise JobOutcomeError(expected, outcome)
