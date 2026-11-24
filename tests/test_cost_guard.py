"""The spend ceiling, including what it deliberately does not promise."""

from __future__ import annotations

from datetime import timedelta

from conftest import FIXED_NOW, build
from guardrail_runtime.types import HaltReason, ModelUnavailable, RunStatus, Usage

# ScriptedModel's default call: 1000 input @0.50c/1k + 500 output @2.50c/1k = 1.75c -> 2c.
COST_OF_ONE_CALL = 2


def test_a_run_within_budget_is_debited_what_it_used() -> None:
    harness = build(script=["ok"], daily_budget_cents=1000)
    harness.run()

    assert len(harness.ledger.entries) == 1
    tenant, run_id, cents, _ = harness.ledger.entries[0]
    assert (tenant, run_id, cents) == ("tenant-a", "run-1", COST_OF_ONE_CALL)


def test_a_tenant_already_at_the_cap_is_stopped_before_the_model() -> None:
    harness = build(daily_budget_cents=100)
    harness.ledger.debit("tenant-a", "earlier-run", 100)

    response = harness.run()
    assert response.status is RunStatus.BUDGET_EXCEEDED
    assert response.reason is HaltReason.DAILY_BUDGET
    assert harness.model.calls == []


def test_spend_accumulates_across_runs_until_it_trips() -> None:
    harness = build(script=["a", "b", "c"], daily_budget_cents=4)
    assert harness.run().status is RunStatus.COMPLETED  # spend 2
    assert harness.run().status is RunStatus.COMPLETED  # spend 4
    assert harness.run().status is RunStatus.BUDGET_EXCEEDED  # 4 >= 4


def test_the_cap_can_be_overshot_by_at_most_one_run() -> None:
    """The documented limitation, pinned as a test rather than left as prose.

    Token counts are not knowable before the call, so the pre-check can only refuse once the
    ledger has *already* reached the cap. A run that starts under the cap is allowed to
    finish and is billed in full. The overshoot is therefore bounded by one run's cost - it
    is never unbounded, and that bound is the design (ADR 0003).
    """
    harness = build(script=["ok"], daily_budget_cents=1)
    response = harness.run()

    assert response.status is RunStatus.COMPLETED
    spent = harness.ledger.spend_cents_since("tenant-a", FIXED_NOW - timedelta(hours=24))
    assert spent == COST_OF_ONE_CALL
    assert spent > 1  # over the cap, by one run, once

    # And the very next run is refused, so the overshoot cannot repeat.
    assert harness.run().status is RunStatus.BUDGET_EXCEEDED


def test_spend_outside_the_window_does_not_count() -> None:
    harness = build(script=["ok"], daily_budget_cents=100)
    harness.ledger.entries.append(
        ("tenant-a", "ancient-run", 500, FIXED_NOW - timedelta(hours=25))
    )
    # 500c of spend exists, but it is 25 hours old and the window is 24.
    assert harness.run().status is RunStatus.COMPLETED


def test_spend_just_inside_the_window_does_count() -> None:
    harness = build(daily_budget_cents=100)
    harness.ledger.entries.append(
        ("tenant-a", "recent-run", 500, FIXED_NOW - timedelta(hours=23))
    )
    assert harness.run().status is RunStatus.BUDGET_EXCEEDED


def test_one_tenants_spend_does_not_consume_anothers_budget() -> None:
    from dataclasses import replace

    from guardrail_runtime.types import Request

    harness = build(script=["ok"], daily_budget_cents=10)
    harness.ledger.debit("tenant-b", "their-run", 1000)

    assert harness.runtime.run(harness.config, Request(prompt="hi")).status is RunStatus.COMPLETED
    tenant_b = replace(harness.config, tenant_id="tenant-b")
    assert harness.runtime.run(tenant_b, Request(prompt="hi")).status is RunStatus.BUDGET_EXCEEDED


def test_every_retry_attempt_is_billed_not_just_the_successful_one() -> None:
    """The reason the cost guard sits outside the retry layer.

    Two attempts burn 300 input tokens each before timing out, then the third succeeds.
    Billing only the success would make a flapping upstream free to the tenant.
    """
    burned = Usage(input_tokens=300, output_tokens=0, attempts=0)
    harness = build(
        script=[
            ModelUnavailable("timeout", usage=burned),
            ModelUnavailable("timeout", usage=burned),
            "finally",
        ],
        daily_budget_cents=1000,
        max_attempts=3,
    )
    response = harness.run()

    assert response.status is RunStatus.COMPLETED
    assert response.usage.attempts == 3
    assert response.usage.input_tokens == 1000 + 600
    # 1600 in @0.50c/1k = 0.80c, 500 out @2.50c/1k = 1.25c -> 2.05c -> 3c.
    assert harness.ledger.entries[0][2] == 3


def test_a_run_that_failed_every_attempt_is_still_billed_for_what_it_burned() -> None:
    burned = Usage(input_tokens=1000, attempts=0)
    harness = build(
        script=[ModelUnavailable("down", usage=burned)] * 2,
        daily_budget_cents=1000,
        max_attempts=2,
    )
    response = harness.run()

    assert response.status is RunStatus.FAILED
    assert response.reached_model is True
    assert harness.ledger.entries[0][2] == 1  # 2000 in @0.50c/1k = 1.0c


def test_a_free_failure_debits_nothing_at_all() -> None:
    # A connection refused before any tokens were generated cost an attempt but no money.
    # The ledger must stay empty rather than gain a zero-cent row.
    harness = build(script=[ModelUnavailable("refused")] * 2, max_attempts=2)
    response = harness.run()

    assert response.status is RunStatus.FAILED
    assert response.usage.attempts == 2
    assert harness.ledger.entries == []
