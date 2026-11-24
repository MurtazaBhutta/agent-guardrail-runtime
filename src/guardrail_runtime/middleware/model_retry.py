"""Layer 7: retries, innermost.

Innermost on purpose. A retry re-runs *only* the model call - it does not re-screen the
input, does not re-request human approval, and does not re-read the budget. Placing retries
further out would re-run those layers, which for the approval layer would mean asking a human
to approve the same run several times.

Usage accumulates across attempts and is handed outward as one total, so the cost guard bills
every attempt. Failed attempts contribute an attempt count with zero tokens: the call cost a
try but produced nothing to charge for.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..chain import Handler
from ..context import RunContext
from ..types import HaltReason, ModelUnavailable, Request, Response, Usage, halt


@dataclass
class ModelRetry:
    name: str = "model_retry"
    base_delay_seconds: float = 0.05
    # Injected so tests exercise the backoff schedule without waiting for it. A retry policy
    # tested with real sleeps is a retry policy nobody runs often enough to trust.
    sleep: Callable[[float], None] = field(default=time.sleep)

    def __call__(self, ctx: RunContext, req: Request, next_: Handler) -> Response:
        ctx.enter(self.name)
        attempts_used = Usage()
        last_error = ""
        max_attempts = ctx.config.max_attempts

        for attempt in range(1, max_attempts + 1):
            try:
                response = next_(ctx, req)
            except ModelUnavailable as exc:
                last_error = str(exc)
                # Count the attempt, and carry whatever that attempt consumed before it
                # died. A call cut off after generating tokens still cost those tokens.
                attempts_used = attempts_used + Usage(attempts=1) + exc.usage
                if attempt < max_attempts:
                    self.sleep(self.base_delay_seconds * (2 ** (attempt - 1)))
                continue
            return replace(response, usage=attempts_used + response.usage)

        return ctx.note(
            self.name,
            halt(
                HaltReason.MODEL_UNAVAILABLE,
                f"{max_attempts} attempt(s) failed; last error: {last_error}",
                usage=attempts_used,
            ),
        )
