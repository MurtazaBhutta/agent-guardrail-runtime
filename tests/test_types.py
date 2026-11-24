"""Usage arithmetic and money. Small surface, and every mistake here is a billing bug."""

from __future__ import annotations

from decimal import Decimal

import pytest

from guardrail_runtime.types import (
    HaltReason,
    Pricing,
    Response,
    RunStatus,
    Usage,
    halt,
)

PRICING = Pricing(cents_per_1k_input=Decimal("0.50"), cents_per_1k_output=Decimal("2.50"))


def test_usage_adds_component_wise() -> None:
    total = Usage(1000, 500, 1) + Usage(200, 100, 1)
    assert total == Usage(1200, 600, 2)


def test_attempts_count_model_calls_not_requests() -> None:
    # Three attempts of one logical request is three model calls, and the tenant pays for
    # all three. If this ever collapses to 1, retries become free to the tenant.
    accumulated = Usage(attempts=1) + Usage(attempts=1) + Usage(1000, 500, 1)
    assert accumulated.attempts == 3


def test_cost_rounds_up_never_down() -> None:
    # 100 input tokens at 0.50c/1k = 0.05c exactly. Rounding down yields 0 and bills nothing
    # for real work - a systematic leak in the direction that costs money.
    assert Usage(input_tokens=100).cost_cents(PRICING) == 1


def test_cost_of_nothing_is_zero() -> None:
    assert Usage().cost_cents(PRICING) == 0


def test_cost_is_exact_for_whole_units() -> None:
    # 2000 in @0.5 = 1.0c; 2000 out @2.5 = 5.0c; total 6c with no rounding involved.
    assert Usage(input_tokens=2000, output_tokens=2000).cost_cents(PRICING) == 6


def test_money_uses_decimal_not_float() -> None:
    assert isinstance(PRICING.cents_per_1k_input, Decimal)


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        (HaltReason.KILL_SWITCH, RunStatus.BLOCKED),
        (HaltReason.INPUT_GUARDRAIL, RunStatus.BLOCKED),
        (HaltReason.OUTPUT_GUARDRAIL, RunStatus.BLOCKED),
        (HaltReason.DAILY_BUDGET, RunStatus.BUDGET_EXCEEDED),
        (HaltReason.APPROVAL_REQUIRED, RunStatus.AWAITING_APPROVAL),
        (HaltReason.MODEL_UNAVAILABLE, RunStatus.FAILED),
    ],
)
def test_every_reason_maps_to_exactly_one_status(reason: HaltReason, status: RunStatus) -> None:
    assert halt(reason).status is status


def test_halt_and_halted_agree_on_status() -> None:
    # Two construction paths, one mapping. If they diverge, the same condition reports
    # different statuses depending on which layer produced it.
    spent = Response(status=RunStatus.COMPLETED, usage=Usage(1000, 500, 1))
    for reason in HaltReason:
        if reason is HaltReason.NONE:
            continue
        assert spent.halted(reason).status is halt(reason).status


def test_halted_preserves_usage_already_spent() -> None:
    # The central rule: blocking an output does not refund the tokens that produced it.
    spent = Response(status=RunStatus.COMPLETED, text="secret", usage=Usage(1000, 500, 1))
    blocked = spent.halted(HaltReason.OUTPUT_GUARDRAIL, "matched")
    assert blocked.usage == Usage(1000, 500, 1)
    assert blocked.text == ""
    assert blocked.reached_model is True


def test_halt_before_the_model_reports_no_spend() -> None:
    blocked = halt(HaltReason.KILL_SWITCH)
    assert blocked.usage == Usage()
    assert blocked.reached_model is False


def test_reached_model_is_driven_by_attempts_not_status() -> None:
    # A failed run that burned three attempts did reach the model. This is what lets the
    # cost guard bill work that produced no usable output.
    failed = halt(HaltReason.MODEL_UNAVAILABLE, usage=Usage(attempts=3))
    assert failed.status is RunStatus.FAILED
    assert failed.reached_model is True
