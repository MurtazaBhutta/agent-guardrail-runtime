"""Layer 6: the spend ceiling.

Two halves, one on each side of the model call:

* **before** - if the tenant's rolling-window spend already meets the cap, the run stops.
* **after**  - debit what was actually used, including every retry attempt.

The retry point is the one that is easy to get wrong. This layer sits **outside**
`model_retry`, so the usage it sees is the accumulated total across every attempt, not one
nominal call. A request that succeeded on its third try made three model calls, and whatever
each of them consumed is billed - including tokens burned by an attempt that then timed out.
Bill only the successful call and a flapping upstream becomes free to the tenant and
expensive to you.

Honest limitation, stated rather than hidden: the pre-check refuses only when the ledger has
*already* reached the cap, because token counts are not knowable before the call. A single
run can therefore overshoot by up to one run's cost. The overshoot is bounded by one run,
never unbounded, and that bound is the design - see ADR 0003.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..chain import Handler
from ..context import RunContext
from ..types import HaltReason, Request, Response, halt

BUDGET_WINDOW = timedelta(hours=24)


@dataclass
class CostGuard:
    name: str = "cost_guard"

    def __call__(self, ctx: RunContext, req: Request, next_: Handler) -> Response:
        ctx.enter(self.name)
        budget = ctx.config.daily_budget_cents
        spent = ctx.ledger.spend_cents_since(ctx.tenant_id, ctx.now() - BUDGET_WINDOW)

        if spent >= budget:
            return ctx.note(
                self.name,
                halt(
                    HaltReason.DAILY_BUDGET,
                    f"spent {spent}c of {budget}c cap in the last {BUDGET_WINDOW}",
                ),
            )

        response = next_(ctx, req)

        # Debit on *any* outcome that reached the model, not only success. A run that failed
        # after three attempts, or whose output was refused by the guardrail one layer out,
        # still consumed tokens. Debiting only successes is how a spend cap ends up leaking.
        if response.reached_model:
            cost = response.usage.cost_cents(ctx.config.pricing)
            if cost > 0:
                ctx.ledger.debit(ctx.tenant_id, ctx.run_id, cost)

        return response
