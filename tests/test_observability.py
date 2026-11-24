"""Tracing and the step trail.

Both exist to answer questions after the fact, so the tests are mostly about the runs that
are easiest to lose: the blocked ones, the paused ones, and the ones that threw.
"""

from __future__ import annotations

from conftest import build
from guardrail_runtime.types import HaltReason, ModelUnavailable, RunStatus


def test_a_successful_run_records_every_layer_that_permitted_it() -> None:
    # The full trail, not just the outcome. This is what lets someone answer "was the
    # guardrail even in force for this run?" months later.
    harness = build(script=["ok"])
    harness.run()

    assert harness.steps.layers_for("run-1") == [
        "kill_switch",
        "guardrail",
        "hitl",
        "cost_guard",
        "model_retry",
        "run",
    ]
    assert all(s.status is RunStatus.COMPLETED for s in harness.steps.for_run("run-1"))


def test_the_trail_stops_at_the_layer_that_halted() -> None:
    harness = build(daily_budget_cents=0)
    harness.run()

    layers = harness.steps.layers_for("run-1")
    assert layers == ["kill_switch", "guardrail", "hitl", "cost_guard", "run"]
    assert "model_retry" not in layers

    halting = next(s for s in harness.steps.for_run("run-1") if s.layer == "cost_guard")
    assert halting.status is RunStatus.BUDGET_EXCEEDED
    assert halting.reason is HaltReason.DAILY_BUDGET


def test_the_trail_always_ends_with_a_run_record() -> None:
    # So a query can answer "how did this end" without reconstructing it from layer rows.
    for kwargs in ({}, {"kill_switch_enabled": True}, {"hitl_required": True}):
        harness = build(script=["ok"], **kwargs)
        harness.run()
        assert harness.steps.for_run("run-1")[-1].layer == "run"


def test_step_indexes_are_contiguous_and_ordered() -> None:
    harness = build(script=["ok"])
    harness.run()
    indexes = [s.step_index for s in harness.steps.for_run("run-1")]
    assert indexes == list(range(len(indexes)))


def test_every_step_is_attributed_to_the_tenant() -> None:
    harness = build(script=["ok"])
    harness.run()
    assert {s.tenant_id for s in harness.steps.for_run("run-1")} == {"tenant-a"}


def test_the_trail_is_written_in_one_call_per_run() -> None:
    """Batching, asserted.

    Seven round trips per request is a cost paid on every single call, and the trail is only
    ever read as a unit. If this becomes one write per layer, latency regresses everywhere.
    """
    harness = build(script=["ok"])
    calls: list[int] = []
    original = harness.steps.record_many

    def counting(steps):
        calls.append(len(steps))
        return original(steps)

    harness.steps.record_many = counting  # type: ignore[method-assign]
    harness.run()

    assert len(calls) == 1
    assert calls[0] == 6


def test_a_trace_span_is_emitted_for_every_outcome() -> None:
    for kwargs, expected in (
        ({}, RunStatus.COMPLETED),
        ({"kill_switch_enabled": True}, RunStatus.BLOCKED),
        ({"hitl_required": True}, RunStatus.AWAITING_APPROVAL),
        ({"daily_budget_cents": 0}, RunStatus.BUDGET_EXCEEDED),
    ):
        harness = build(script=["ok"], **kwargs)
        harness.run()
        assert len(harness.traces.spans) == 1
        assert harness.traces.spans[0].status is expected


def test_the_span_carries_cost_not_just_tokens() -> None:
    # An observability backend that only sees tokens cannot alert on spend without knowing
    # this tenant's pricing, which it does not have.
    harness = build(script=["ok"])
    harness.run()
    span = harness.traces.spans[0]
    assert span.usage.input_tokens == 1000
    assert span.cost_cents == 2


def test_a_blocked_output_is_traced_with_its_spend_intact() -> None:
    harness = build(script=["sk-live-123"], blocked_output_patterns=("sk-live",))
    harness.run()
    span = harness.traces.spans[0]
    assert span.reason is HaltReason.OUTPUT_GUARDRAIL
    assert span.cost_cents == 2


def test_an_escaping_exception_is_still_traced_and_recorded() -> None:
    """The gap that looks like silence.

    If instrumentation is skipped when something escapes the chain, the runs you most need to
    see are missing - and a missing run reads as no traffic, not as a crash.
    """
    harness = build(script=[RuntimeError("boom")])
    try:
        harness.run()
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError to propagate")

    assert len(harness.traces.spans) == 1
    assert harness.traces.spans[0].status is RunStatus.FAILED
    assert harness.steps.for_run("run-1")[-1].layer == "run"
    assert "boom" in harness.steps.for_run("run-1")[-1].detail


def test_failed_retries_are_traced_once_for_the_run_not_once_per_attempt() -> None:
    # One span per run keeps trace volume proportional to traffic rather than to how badly
    # the upstream is behaving.
    harness = build(script=[ModelUnavailable("down")] * 3, max_attempts=3)
    harness.run()
    assert len(harness.traces.spans) == 1
    assert harness.traces.spans[0].usage.attempts == 3
