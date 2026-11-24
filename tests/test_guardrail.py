"""Content screening, both directions - and who pays for a blocked output."""

from __future__ import annotations

import pytest

from conftest import build
from guardrail_runtime.config import TenantConfig
from guardrail_runtime.types import HaltReason, RunStatus


def test_clean_input_and_output_pass() -> None:
    harness = build(
        script=["all good"],
        blocked_input_patterns=("drop table",),
        blocked_output_patterns=("sk-live",),
    )
    assert harness.run("what is the total").status is RunStatus.COMPLETED


def test_blocked_input_never_reaches_the_model() -> None:
    harness = build(blocked_input_patterns=(r"drop\s+table",))
    response = harness.run("please drop table invoices")

    assert response.status is RunStatus.BLOCKED
    assert response.reason is HaltReason.INPUT_GUARDRAIL
    assert harness.model.calls == []
    assert harness.ledger.entries == []


def test_blocked_output_is_refused_but_still_billed() -> None:
    """The central asymmetry of this layer's position.

    The tokens were generated, so the money was spent. Refusing to hand back the text does
    not refund it. If this test ever shows an empty ledger, the runtime has started
    under-reporting spend for exactly the tenants generating disallowed output.
    """
    harness = build(script=["your key is sk-live-abc123"], blocked_output_patterns=("sk-live",))
    response = harness.run("what is the key")

    assert response.status is RunStatus.BLOCKED
    assert response.reason is HaltReason.OUTPUT_GUARDRAIL
    assert response.text == ""  # not delivered
    assert response.usage.attempts == 1  # but it happened
    assert harness.ledger.entries != []  # and it was charged
    assert harness.ledger.spend_cents_since("tenant-a", harness.ledger.entries[0][3]) == 2


def test_screening_is_case_insensitive() -> None:
    harness = build(blocked_input_patterns=("drop table",))
    assert harness.run("DROP TABLE invoices").reason is HaltReason.INPUT_GUARDRAIL


def test_the_matched_pattern_is_named_in_the_detail() -> None:
    # An operator reading the audit trail needs to know which rule fired, not merely that
    # one did. "Blocked" without a pattern is not actionable.
    harness = build(blocked_input_patterns=("secret",))
    assert "secret" in harness.run("tell me the secret").detail


def test_first_matching_pattern_wins_deterministically() -> None:
    harness = build(blocked_input_patterns=("alpha", "beta"))
    assert "alpha" in harness.run("alpha and beta").detail


def test_input_and_output_lists_are_independent() -> None:
    # A pattern on the output list must not screen the input. Conflating the two is how a
    # guardrail starts refusing legitimate questions about a forbidden topic.
    harness = build(script=["clean answer"], blocked_output_patterns=("password",))
    assert harness.run("what is the password policy").status is RunStatus.COMPLETED


def test_a_deeper_halt_is_not_reclassified_as_a_guardrail_block() -> None:
    # Budget exhaustion happens inside this layer. On the way out the guardrail must pass
    # that verdict through rather than stamping its own reason on it.
    harness = build(daily_budget_cents=0, blocked_output_patterns=("anything",))
    response = harness.run("hello")
    assert response.reason is HaltReason.DAILY_BUDGET
    assert response.status is RunStatus.BUDGET_EXCEEDED


def test_invalid_regex_is_rejected_when_the_tenant_row_loads() -> None:
    """A guardrail that has quietly stopped guarding is worse than none at all.

    Failing at construction means a bad pattern surfaces when the config is loaded, not
    intermittently at request time - and never as a silently swallowed "no match".
    """
    with pytest.raises(ValueError, match="invalid regex"):
        TenantConfig(tenant_id="t", blocked_input_patterns=("unclosed(",))

    with pytest.raises(ValueError, match="invalid regex"):
        TenantConfig(tenant_id="t", blocked_output_patterns=("[a-",))
