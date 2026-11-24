"""Retries: how many, how long between, and what they must not re-run."""

from __future__ import annotations

from conftest import build
from guardrail_runtime.types import HaltReason, ModelUnavailable, RunStatus, Usage


def test_first_attempt_success_makes_no_further_calls() -> None:
    harness = build(script=["ok"])
    assert harness.run().status is RunStatus.COMPLETED
    assert len(harness.model.calls) == 1
    assert harness.sleeps == []


def test_a_transient_failure_is_retried_and_succeeds() -> None:
    harness = build(script=[ModelUnavailable("blip"), "recovered"], max_attempts=3)
    response = harness.run()

    assert response.status is RunStatus.COMPLETED
    assert response.text == "recovered"
    assert len(harness.model.calls) == 2


def test_exhausted_retries_become_a_failed_outcome_not_an_exception() -> None:
    # The chain converts an exhausted retry budget into a FAILED response so it can be
    # traced, recorded and attributed like every other outcome.
    harness = build(script=[ModelUnavailable("down")] * 3, max_attempts=3)
    response = harness.run()

    assert response.status is RunStatus.FAILED
    assert response.reason is HaltReason.MODEL_UNAVAILABLE
    assert len(harness.model.calls) == 3


def test_max_attempts_is_a_total_not_an_extra_allowance() -> None:
    # max_attempts=1 means one call and no retry. Off-by-one here silently doubles the
    # traffic a tenant sends to the provider.
    harness = build(script=[ModelUnavailable("down")] * 2, max_attempts=1)
    harness.run()
    assert len(harness.model.calls) == 1
    assert harness.sleeps == []


def test_backoff_is_exponential_and_skipped_after_the_last_attempt() -> None:
    # Three attempts means two waits, not three: sleeping after the final failure delays the
    # caller's error for no benefit.
    harness = build(script=[ModelUnavailable("down")] * 3, max_attempts=3)
    harness.run()
    assert harness.sleeps == [0.05, 0.10]


def test_the_last_error_is_reported_in_the_detail() -> None:
    harness = build(
        script=[ModelUnavailable("first"), ModelUnavailable("second")], max_attempts=2
    )
    detail = harness.run().detail
    assert "second" in detail
    assert "2 attempt" in detail


def test_retries_do_not_re_request_human_approval() -> None:
    """The reason retries are innermost.

    If retries wrapped the approval layer, a flapping model would ask a human to approve the
    same run once per attempt. The token is consumed once and the retries happen inside it.
    """
    harness = build(
        script=[ModelUnavailable("blip"), "posted"], hitl_required=True, max_attempts=3
    )
    token = harness.run("post this").resume_token

    response = harness.run("post this", approval_token=token)
    assert response.status is RunStatus.COMPLETED
    assert len(harness.model.calls) == 2
    assert harness.approvals.pending == {}  # consumed exactly once


def test_retries_do_not_re_screen_the_input() -> None:
    # The guardrail sits outside the retry loop, so a retried run is screened once. Screening
    # per attempt would be wasted work on every retry of every request.
    harness = build(
        script=[ModelUnavailable("blip"), "clean"],
        blocked_input_patterns=("forbidden",),
        max_attempts=2,
    )
    assert harness.run("perfectly fine").status is RunStatus.COMPLETED
    assert harness.steps.layers_for("run-1").count("guardrail") == 1


def test_failed_attempts_accumulate_their_usage() -> None:
    burned = Usage(input_tokens=100, attempts=0)
    harness = build(
        script=[ModelUnavailable("a", usage=burned), ModelUnavailable("b", usage=burned)],
        max_attempts=2,
    )
    response = harness.run()
    assert response.usage.attempts == 2
    assert response.usage.input_tokens == 200


def test_a_non_transient_error_is_not_retried() -> None:
    # ValueError is not ModelUnavailable. Retrying a deterministic error just multiplies it,
    # so it escapes the chain - after the tracer and recorder have written the run.
    harness = build(script=[ValueError("bad request")], max_attempts=3)
    try:
        harness.run()
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError to propagate")

    assert len(harness.model.calls) == 1
    assert len(harness.traces.spans) == 1
    assert harness.traces.spans[0].status is RunStatus.FAILED
