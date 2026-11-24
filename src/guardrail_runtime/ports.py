"""Storage ports, and in-memory implementations for tests.

Each port is narrow on purpose. `SpendLedger` can debit and total; it cannot list every
tenant's spend, because nothing in the runtime needs that and a method that could would be
a leak waiting to be called.

**The in-memory implementations enforce tenant isolation by convention** - they filter on the
`tenant_id` they were handed. That is exactly the weakness row-level security exists to fix:
convention holds until someone forgets a filter, and then it fails silently, in production,
for a customer. The Postgres implementations in `tenancy/` push the same rule down into
database policy, where forgetting is not possible. See ADR 0002.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .types import HaltReason, RunStatus, Usage


@dataclass(frozen=True)
class StepRecord:
    """One layer's verdict within a run. Written for every layer, including blocking ones.

    Recording only successful steps would produce an audit trail in which blocked runs
    simply do not appear - the exact runs someone will later need to explain.
    """

    tenant_id: str
    run_id: str
    step_index: int
    layer: str
    status: RunStatus
    reason: HaltReason
    detail: str
    usage: Usage


@dataclass(frozen=True)
class TraceSpan:
    """One run, summarised for whatever observability backend is attached."""

    tenant_id: str
    run_id: str
    status: RunStatus
    reason: HaltReason
    duration_ms: int
    usage: Usage
    cost_cents: int


class SpendLedger(Protocol):
    def spend_cents_since(self, tenant_id: str, since: datetime) -> int: ...

    def debit(self, tenant_id: str, run_id: str, cents: int) -> None: ...


class StepStore(Protocol):
    def record(self, step: StepRecord) -> None: ...

    def record_many(self, steps: Sequence[StepRecord]) -> None: ...


class ApprovalStore(Protocol):
    def create_pending(self, tenant_id: str, run_id: str, prompt: str) -> str: ...

    def consume(self, tenant_id: str, token: str) -> bool: ...


class TraceSink(Protocol):
    def emit(self, span: TraceSpan) -> None: ...


# --------------------------------------------------------------------------------------
# In-memory implementations. Test doubles, not production storage.
# --------------------------------------------------------------------------------------


@dataclass
class InMemorySpendLedger:
    entries: list[tuple[str, str, int, datetime]] = field(default_factory=list)
    clock: object = None

    def spend_cents_since(self, tenant_id: str, since: datetime) -> int:
        return sum(c for t, _, c, at in self.entries if t == tenant_id and at >= since)

    def debit(self, tenant_id: str, run_id: str, cents: int) -> None:
        now = self.clock() if callable(self.clock) else datetime.now()
        self.entries.append((tenant_id, run_id, cents, now))


@dataclass
class InMemoryStepStore:
    steps: list[StepRecord] = field(default_factory=list)

    def record(self, step: StepRecord) -> None:
        self.steps.append(step)

    def record_many(self, steps: Sequence[StepRecord]) -> None:
        self.steps.extend(steps)

    def for_run(self, run_id: str) -> list[StepRecord]:
        return [s for s in self.steps if s.run_id == run_id]

    def layers_for(self, run_id: str) -> list[str]:
        return [s.layer for s in self.for_run(run_id)]


@dataclass
class InMemoryApprovalStore:
    pending: dict[str, tuple[str, str, str]] = field(default_factory=dict)

    def create_pending(self, tenant_id: str, run_id: str, prompt: str) -> str:
        # An approval token is a capability: whoever holds it can release a run that a
        # human was supposed to gate. It must not be guessable or derived from the run id.
        token = secrets.token_urlsafe(24)
        self.pending[token] = (tenant_id, run_id, prompt)
        return token

    def consume(self, tenant_id: str, token: str) -> bool:
        record = self.pending.get(token)
        if record is None:
            return False
        # The token is checked against the tenant presenting it. A valid token issued to
        # one tenant must not release another tenant's run.
        if record[0] != tenant_id:
            return False
        # Single use. A reusable approval is a standing approval, which is not what a human
        # granted when they approved one specific run.
        del self.pending[token]
        return True


@dataclass
class RecordingTraceSink:
    spans: list[TraceSpan] = field(default_factory=list)

    def emit(self, span: TraceSpan) -> None:
        self.spans.append(span)
