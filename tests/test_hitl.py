"""Human in the loop: pausing, resuming, and the ways a token must not work."""

from __future__ import annotations

from dataclasses import replace

from conftest import build
from guardrail_runtime.types import HaltReason, Request, RunStatus


def test_approval_not_required_passes_straight_through() -> None:
    assert build(script=["done"]).run().status is RunStatus.COMPLETED


def test_a_run_needing_approval_pauses_rather_than_fails() -> None:
    # AWAITING_APPROVAL is not an error. The run is resumable, and the distinction matters
    # to whatever is driving it: a failure gets retried, a pause gets escalated to a person.
    harness = build(hitl_required=True)
    response = harness.run("post this invoice")

    assert response.status is RunStatus.AWAITING_APPROVAL
    assert response.reason is HaltReason.APPROVAL_REQUIRED
    assert response.resume_token
    assert harness.model.calls == []


def test_a_pause_is_never_billed() -> None:
    # Otherwise the safest configuration becomes the most expensive one, and tenants are
    # financially discouraged from requiring human oversight.
    harness = build(hitl_required=True)
    response = harness.run()

    assert response.usage.attempts == 0
    assert harness.ledger.entries == []


def test_a_valid_token_releases_the_run() -> None:
    harness = build(script=["posted"], hitl_required=True)
    token = harness.run("post this").resume_token

    resumed = harness.run("post this", approval_token=token)
    assert resumed.status is RunStatus.COMPLETED
    assert resumed.text == "posted"


def test_an_approval_token_is_single_use() -> None:
    """A reusable token is a standing approval.

    That is not what a human granted when they approved one specific run, and it turns one
    click into unlimited future authorisations.
    """
    harness = build(script=["posted", "posted again"], hitl_required=True)
    token = harness.run("post this").resume_token

    assert harness.run("post this", approval_token=token).status is RunStatus.COMPLETED
    second = harness.run("post this", approval_token=token)
    assert second.status is RunStatus.AWAITING_APPROVAL
    assert second.resume_token != token


def test_a_token_issued_to_one_tenant_cannot_release_another() -> None:
    """Cross-tenant capability check.

    An approval token is a capability. If tenant B could present tenant A's token, approval
    would be a shared secret across the platform rather than a per-tenant control.
    """
    harness = build(script=["posted"], hitl_required=True)
    token_for_a = harness.run("post this").resume_token

    tenant_b = replace(harness.config, tenant_id="tenant-b")
    response = harness.runtime.run(
        tenant_b, Request(prompt="post this", approval_token=token_for_a)
    )

    assert response.status is RunStatus.AWAITING_APPROVAL
    assert harness.model.calls == []


def test_an_unknown_token_does_not_fall_through_to_the_model() -> None:
    # The dangerous failure mode: treating "token present but invalid" as approved.
    harness = build(hitl_required=True)
    response = harness.run("post this", approval_token="not-a-real-token")

    assert response.status is RunStatus.AWAITING_APPROVAL
    assert harness.model.calls == []


def test_a_rejected_token_yields_a_fresh_pending_approval() -> None:
    harness = build(hitl_required=True)
    response = harness.run("post this", approval_token="bogus")
    assert response.resume_token
    assert response.resume_token != "bogus"


def test_tokens_are_not_derived_from_the_run_id() -> None:
    # Run ids appear in logs and traces. A token derivable from one would be guessable by
    # anyone who can read a log line.
    harness = build(hitl_required=True)
    response = harness.run()
    assert "run-1" not in (response.resume_token or "")
    assert len(response.resume_token or "") >= 20
