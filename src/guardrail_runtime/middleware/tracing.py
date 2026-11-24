"""Layer 1: observability, outermost.

Outermost so that it observes every other layer, including the ones that stop the run. A
trace that only covers successful runs hides precisely the runs someone needs to explain:
the blocked ones, the ones that hit the cap, the ones waiting on a human.

The emit is also wrapped in a `finally`-style guard around an unexpected exception. If
something escapes the chain, a span is still emitted before the exception continues. Traces
that vanish exactly when things break are worse than no traces, because the gap looks like
absence of traffic rather than absence of instrumentation.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chain import Handler
from ..context import RunContext
from ..ports import TraceSpan
from ..types import HaltReason, Request, Response, RunStatus, Usage


@dataclass
class Tracing:
    name: str = "trace"

    def __call__(self, ctx: RunContext, req: Request, next_: Handler) -> Response:
        started = ctx.now()
        try:
            response = next_(ctx, req)
        except BaseException as exc:
            self._emit(ctx, started, Response(status=RunStatus.FAILED, detail=repr(exc)))
            raise
        self._emit(ctx, started, response)
        return response

    def _emit(self, ctx: RunContext, started: object, response: Response) -> None:
        elapsed = ctx.now() - started  # type: ignore[operator]
        ctx.traces.emit(
            TraceSpan(
                tenant_id=ctx.tenant_id,
                run_id=ctx.run_id,
                status=response.status,
                reason=response.reason or HaltReason.NONE,
                duration_ms=int(elapsed.total_seconds() * 1000),
                usage=response.usage or Usage(),
                cost_cents=response.usage.cost_cents(ctx.config.pricing),
            )
        )
