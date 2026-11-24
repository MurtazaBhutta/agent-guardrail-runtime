"""Layer 3: the absolute stop.

Cheapest and most absolute control in the runtime. It reads one boolean and, if set, the run
ends before any pattern is compiled, any ledger is read, or any model is called.

It exists because "stop this tenant's agent now" is a thing someone needs at 2am, and the
answer cannot be "ship a deploy". Flipping a row in `tenant_config` is the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chain import Handler
from ..context import RunContext
from ..types import HaltReason, Request, Response, halt


@dataclass
class KillSwitch:
    name: str = "kill_switch"

    def __call__(self, ctx: RunContext, req: Request, next_: Handler) -> Response:
        ctx.enter(self.name)
        if ctx.config.kill_switch_enabled:
            return ctx.note(
                self.name,
                halt(HaltReason.KILL_SWITCH, f"tenant {ctx.tenant_id} is halted by kill switch"),
            )
        return next_(ctx, req)
