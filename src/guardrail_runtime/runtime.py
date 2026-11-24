"""Assembling the runtime.

The default layer order lives here, in one place, as a list you can read top to bottom. That
is the point: the safety posture of the platform should be legible in a single expression
rather than distributed across decorators and call sites.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .chain import Handler, Middleware, build_chain
from .config import TenantConfig
from .context import RunContext
from .middleware import (
    CostGuard,
    Guardrail,
    HumanApproval,
    KillSwitch,
    ModelRetry,
    StepRecorder,
    Tracing,
)
from .model import ModelClient, terminal_handler
from .ports import ApprovalStore, SpendLedger, StepStore, TraceSink
from .types import Request, Response


def default_layers() -> list[Middleware]:
    """Outermost first.

    Reading order is enforcement order:

      1. trace         - sees everything, including halts and escapes
      2. step_recorder - persists the trail, including halts and escapes
      3. kill_switch   - absolute stop, before anything is computed
      4. guardrail     - input screening in, output screening out
      5. hitl          - pause for approval before any spend
      6. cost_guard    - budget check in, debit of all attempts out
      7. model_retry   - retries only the model call
    """
    return [
        Tracing(),
        StepRecorder(),
        KillSwitch(),
        Guardrail(),
        HumanApproval(),
        CostGuard(),
        ModelRetry(),
    ]


@dataclass
class Runtime:
    """Binds stores and a model client once, then runs many tenants' requests through them."""

    model: ModelClient
    ledger: SpendLedger
    steps: StepStore
    approvals: ApprovalStore
    traces: TraceSink
    layers: Sequence[Middleware] = field(default_factory=default_layers)
    now: Callable[[], datetime] = datetime.now
    run_id_factory: Callable[[], str] = lambda: str(uuid.uuid4())  # noqa: E731

    def __post_init__(self) -> None:
        self._handler: Handler = build_chain(self.layers, terminal_handler(self.model))

    def run(self, config: TenantConfig, request: Request) -> Response:
        """Execute one request under one tenant's policy.

        The context is built per run and never reused, so no tenant's config, notes, or run
        id can leak into another's request through shared state.
        """
        ctx = RunContext(
            tenant_id=config.tenant_id,
            run_id=self.run_id_factory(),
            config=config,
            ledger=self.ledger,
            steps=self.steps,
            approvals=self.approvals,
            traces=self.traces,
            now=self.now,
        )
        return self._handler(ctx, request)
