"""Layer 5: human in the loop.

When a tenant requires approval, no run reaches the model without a valid, single-use token
issued for that tenant. A run without one does not fail - it *pauses*, returning
`AWAITING_APPROVAL` and a resume token for the human to act on.

Position matters: this layer sits outside the cost guard, so a paused run is never debited.
A pause spent no tokens, and charging for it would make the safest configuration the most
expensive one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chain import Handler
from ..context import RunContext
from ..types import HaltReason, Request, Response, halt


@dataclass
class HumanApproval:
    name: str = "hitl"

    def __call__(self, ctx: RunContext, req: Request, next_: Handler) -> Response:
        ctx.enter(self.name)
        if not ctx.config.hitl_required:
            return next_(ctx, req)

        if req.approval_token and ctx.approvals.consume(ctx.tenant_id, req.approval_token):
            return next_(ctx, req)

        # Reached when no token was presented, or one was presented and rejected - expired,
        # already used, or issued to a different tenant. All three are the same outcome: the
        # run is not approved. An invalid token must never fall through to the model, so a
        # fresh approval is issued instead of passing inward.
        token = ctx.approvals.create_pending(ctx.tenant_id, ctx.run_id, req.prompt)
        return ctx.note(
            self.name,
            halt(
                HaltReason.APPROVAL_REQUIRED,
                "run paused pending human approval",
                resume_token=token,
            ),
        )
