"""Layer 2: the durable step trail.

Every layer that reaches a verdict appends a note to the context; this layer persists the
whole trail once, at the end of the run. One write per run rather than one per layer - seven
round trips per request is a cost you pay on every single call, and the trail is only ever
read as a unit.

A final `run` record is always appended, so a query can answer "how did this run end" without
reconstructing it from the layer notes.

Like the tracer, it writes the trail even when an exception escapes. An audit trail missing
its failures is not an audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chain import Handler
from ..context import RunContext
from ..ports import StepRecord
from ..types import HaltReason, Request, Response, RunStatus, Usage


@dataclass
class StepRecorder:
    name: str = "step_recorder"

    def __call__(self, ctx: RunContext, req: Request, next_: Handler) -> Response:
        try:
            response = next_(ctx, req)
        except BaseException as exc:
            ctx.steps.record_many(
                self._trail(ctx, Response(status=RunStatus.FAILED, detail=repr(exc)))
            )
            raise
        ctx.steps.record_many(self._trail(ctx, response))
        return response

    def _trail(self, ctx: RunContext, final: Response) -> list[StepRecord]:
        # One record per policy layer reached, in order. `status` is that layer's verdict:
        # COMPLETED means the layer permitted the run to continue inward; anything else is
        # the layer's own halt. A successful run therefore leaves a full trail showing every
        # control that allowed it, which is what makes the table answerable after the fact.
        records = []
        for index, layer in enumerate(ctx.entered):
            verdict = ctx.notes.get(layer)
            records.append(
                StepRecord(
                    tenant_id=ctx.tenant_id,
                    run_id=ctx.run_id,
                    step_index=index,
                    layer=layer,
                    status=verdict.status if verdict else RunStatus.COMPLETED,
                    reason=(verdict.reason if verdict else HaltReason.NONE) or HaltReason.NONE,
                    detail=verdict.detail if verdict else "",
                    usage=(verdict.usage if verdict else Usage()) or Usage(),
                )
            )
        records.append(
            StepRecord(
                tenant_id=ctx.tenant_id,
                run_id=ctx.run_id,
                step_index=len(records),
                layer="run",
                status=final.status,
                reason=final.reason or HaltReason.NONE,
                detail=final.detail,
                usage=final.usage or Usage(),
            )
        )
        return records
