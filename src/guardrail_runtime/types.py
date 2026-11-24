"""The vocabulary the whole runtime shares.

One decision shapes everything else here: **a halt is a return value, not an exception.**

Being stopped by the kill switch, refused by a guardrail, paused for human approval, or cut
off by a spend cap are all *normal, expected outcomes* of running an agent in production.
Each one has to be recorded, attributed to a tenant, counted, and handed back to the caller
with a reason it can act on. Raising would turn each of those into a stack trace at the call
site and push the classification work onto every caller.

Exceptions stay for what they are actually for: the model being unreachable, the database
being down - conditions the runtime cannot describe as an outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum


class RunStatus(StrEnum):
    """How a run ended. Exactly one of these is always true of a finished run."""

    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    FAILED = "FAILED"


class HaltReason(StrEnum):
    """Why a run stopped short. A closed set, so it can be alerted and reported on.

    Free-text reasons cannot be counted, and a reason nobody can count is a reason nobody
    acts on.
    """

    NONE = "NONE"
    KILL_SWITCH = "KILL_SWITCH"
    INPUT_GUARDRAIL = "INPUT_GUARDRAIL"
    OUTPUT_GUARDRAIL = "OUTPUT_GUARDRAIL"
    DAILY_BUDGET = "DAILY_BUDGET"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"


@dataclass(frozen=True)
class Usage:
    """Token usage. `attempts` counts model calls, not logical requests.

    The distinction matters for billing: a request that succeeded on its third retry cost
    three model calls, and the tenant is charged for all three.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            attempts=self.attempts + other.attempts,
        )

    def cost_cents(self, pricing: Pricing) -> int:
        """Cost in whole cents, always rounded **up**.

        Rounding down would under-bill by a fraction of a cent on every call, which across
        a high-volume tenant is a systematic leak in the direction that costs money. Under-
        billing is the unsafe direction, so the runtime never does it.
        """
        exact = (
            Decimal(self.input_tokens) / 1000 * pricing.cents_per_1k_input
            + Decimal(self.output_tokens) / 1000 * pricing.cents_per_1k_output
        )
        return int(exact.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class Pricing:
    """Cents per thousand tokens, as exact decimals.

    Money is never a float here. Binary floats cannot represent 0.15 exactly, and a spend
    cap built on accumulated float error is a spend cap that drifts.
    """

    cents_per_1k_input: Decimal = Decimal("0.50")
    cents_per_1k_output: Decimal = Decimal("2.50")


@dataclass(frozen=True)
class Request:
    """One inbound agent request."""

    prompt: str
    approval_token: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    """The outcome of a run, whether it reached the model or not."""

    status: RunStatus
    text: str = ""
    reason: HaltReason = HaltReason.NONE
    detail: str = ""
    usage: Usage = field(default_factory=Usage)
    resume_token: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.COMPLETED

    @property
    def reached_model(self) -> bool:
        """True when at least one model call was made - i.e. when money was spent."""
        return self.usage.attempts > 0

    def halted(self, reason: HaltReason, detail: str = "") -> Response:
        """Return a halted copy that preserves usage already incurred.

        Preserving usage is the whole point. An output blocked by a guardrail still cost
        the tokens that produced it, and a response that dropped its usage on the way out
        would silently under-report spend.
        """
        return Response(
            status=_STATUS_FOR_REASON.get(reason, RunStatus.BLOCKED),
            text="",
            reason=reason,
            detail=detail,
            usage=self.usage,
            resume_token=self.resume_token,
        )


# One mapping, used by both `Response.halted` and `halt`, so a reason can never end up with
# two different statuses depending on which construction path a layer happened to use.
_STATUS_FOR_REASON: dict[HaltReason, RunStatus] = {
    HaltReason.DAILY_BUDGET: RunStatus.BUDGET_EXCEEDED,
    HaltReason.APPROVAL_REQUIRED: RunStatus.AWAITING_APPROVAL,
    HaltReason.MODEL_UNAVAILABLE: RunStatus.FAILED,
}


def halt(
    reason: HaltReason,
    detail: str = "",
    usage: Usage | None = None,
    resume_token: str | None = None,
) -> Response:
    """Build a halted response for a layer that stops a run before it has one.

    `usage` defaults to nothing spent, which is right for the layers that halt before the
    model is reached. `model_retry` passes its accumulated attempts instead, because those
    attempts happened.
    """
    return Response(
        status=_STATUS_FOR_REASON.get(reason, RunStatus.BLOCKED),
        text="",
        reason=reason,
        detail=detail,
        usage=usage if usage is not None else Usage(),
        resume_token=resume_token,
    )


class ModelUnavailable(RuntimeError):
    """The model could not be reached. Transient by assumption, so retryable.

    This is an exception rather than a HaltReason because it is not an outcome the runtime
    can describe - it is the absence of one. `model_retry` converts exhausted retries into
    a FAILED response, which *is* an outcome.

    It carries `usage` because a failed call is not always a free call. A request that timed
    out or was cut off after the model had already generated tokens consumed those tokens,
    and the provider bills for them. Dropping that usage on the floor would make an
    unreliable upstream look free in the ledger, and the tenants hitting the most failures
    would be the ones under-billed.
    """

    def __init__(self, message: str = "", usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage if usage is not None else Usage()
