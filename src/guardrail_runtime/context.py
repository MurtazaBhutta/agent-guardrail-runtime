"""Everything one run needs, assembled once and threaded through the chain."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .config import TenantConfig
from .ports import ApprovalStore, SpendLedger, StepStore, TraceSink
from .types import Response


@dataclass
class RunContext:
    """Per-run state. One instance per request, never shared across tenants.

    `now` is injected rather than called directly so that budget windows and durations are
    deterministic in tests. A spend cap whose window depends on the wall clock is a spend
    cap that cannot be tested at its boundary, and the boundary is the only interesting part.
    """

    tenant_id: str
    run_id: str
    config: TenantConfig
    ledger: SpendLedger
    steps: StepStore
    approvals: ApprovalStore
    traces: TraceSink
    now: Callable[[], datetime]

    # Policy layers reached, in order. Recorded on entry rather than on exit so that a layer
    # which halts still appears in the trail - the trail has to show where a run stopped,
    # and a layer that only registered on the way out would be missing from exactly the runs
    # that need explaining.
    entered: list[str] = field(default_factory=list)

    # Halt verdicts, keyed by layer. The step recorder joins these onto `entered` and
    # persists the whole trail in one write at the end of the run.
    notes: dict[str, Response] = field(default_factory=dict)

    def enter(self, layer: str) -> None:
        """Mark a policy layer as reached."""
        self.entered.append(layer)

    def note(self, layer: str, response: Response) -> Response:
        """Record a layer's halt and return it unchanged, so callers can `return ctx.note(...)`."""
        self.notes[layer] = response
        return response
