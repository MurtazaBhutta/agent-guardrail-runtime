"""The absolute stop."""

from __future__ import annotations

from conftest import build
from guardrail_runtime.types import HaltReason, RunStatus


def test_kill_switch_off_lets_the_run_through() -> None:
    response = build(script=["hello"]).run()
    assert response.status is RunStatus.COMPLETED
    assert response.text == "hello"


def test_kill_switch_blocks_before_the_model() -> None:
    harness = build(kill_switch_enabled=True)
    response = harness.run()

    assert response.status is RunStatus.BLOCKED
    assert response.reason is HaltReason.KILL_SWITCH
    assert harness.model.calls == []


def test_kill_switch_spends_nothing() -> None:
    # The cheapest control has to also be the cheapest to enforce: nothing debited, and the
    # ledger untouched rather than debited zero.
    harness = build(kill_switch_enabled=True)
    response = harness.run()

    assert response.usage.attempts == 0
    assert response.reached_model is False
    assert harness.ledger.entries == []


def test_kill_switch_beats_every_other_policy() -> None:
    # Deliberately contradictory config: a killed tenant with a generous budget, no
    # guardrails and no approval requirement. The kill switch still wins, because it sits
    # outermost of the policy layers.
    harness = build(
        kill_switch_enabled=True,
        daily_budget_cents=1_000_000,
        hitl_required=False,
    )
    assert harness.run().reason is HaltReason.KILL_SWITCH


def test_kill_switch_is_per_tenant() -> None:
    # One tenant halted must not halt another. This is convention-level isolation in the
    # in-memory harness; test_tenant_isolation.py proves the database enforces it.
    from dataclasses import replace

    harness = build(script=["hello"], kill_switch_enabled=True)
    assert harness.run().reason is HaltReason.KILL_SWITCH

    other = replace(harness.config, tenant_id="tenant-b", kill_switch_enabled=False)
    from guardrail_runtime.types import Request

    assert harness.runtime.run(other, Request(prompt="hi")).status is RunStatus.COMPLETED


def test_blocked_run_is_still_traced_and_recorded() -> None:
    # An audit trail that omits blocked runs omits the runs someone will ask about.
    harness = build(kill_switch_enabled=True)
    harness.run()

    assert len(harness.traces.spans) == 1
    assert harness.traces.spans[0].reason is HaltReason.KILL_SWITCH
    assert "kill_switch" in harness.steps.layers_for("run-1")
