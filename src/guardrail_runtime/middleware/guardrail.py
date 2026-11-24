"""Layer 4: content screening, on the way in and on the way out.

Screening only the input is half a guardrail. The model can produce something disallowed
from an entirely innocuous prompt, so the response is screened on the way back out too.

Position matters. This layer sits **outside** the cost guard, which means an output blocked
here has already been debited. That is correct and deliberate: the tokens were generated, so
the money was spent. Refusing to deliver the text does not refund it, and a runtime that
skipped the debit would under-report spend for exactly the tenants behaving worst. See
ADR 0001.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..chain import Handler
from ..context import RunContext
from ..types import HaltReason, Request, Response, halt


def _first_match(patterns: tuple[str, ...], text: str) -> str | None:
    """Return the first pattern that matches, or None.

    Patterns are compiled at config construction, so an invalid regex fails when the tenant
    row is loaded rather than silently matching nothing at run time. A guardrail that
    quietly stops guarding is worse than one that is absent, because it still reads as
    protection.
    """
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None


@dataclass
class Guardrail:
    name: str = "guardrail"

    def __call__(self, ctx: RunContext, req: Request, next_: Handler) -> Response:
        ctx.enter(self.name)
        hit = _first_match(ctx.config.blocked_input_patterns, req.prompt)
        if hit is not None:
            return ctx.note(
                self.name, halt(HaltReason.INPUT_GUARDRAIL, f"input matched pattern {hit!r}")
            )

        response = next_(ctx, req)
        if not response.ok:
            # Already halted deeper in. Pass it through unchanged rather than re-classifying
            # someone else's verdict.
            return response

        hit = _first_match(ctx.config.blocked_output_patterns, response.text)
        if hit is not None:
            # `halted()` preserves usage, so the spend survives the block.
            return ctx.note(
                self.name,
                response.halted(HaltReason.OUTPUT_GUARDRAIL, f"output matched pattern {hit!r}"),
            )
        return response
