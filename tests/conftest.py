"""Test harness.

Three things are pinned so that every assertion is about behaviour rather than timing: the
clock, the run ids, and sleep. `ModelRetry`'s backoff is injected with a recorder, so tests
assert the backoff *schedule* without ever waiting for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pytest

from guardrail_runtime.config import TenantConfig
from guardrail_runtime.middleware import (
    CostGuard,
    Guardrail,
    HumanApproval,
    KillSwitch,
    ModelRetry,
    StepRecorder,
    Tracing,
)
from guardrail_runtime.model import ScriptedModel
from guardrail_runtime.ports import (
    InMemoryApprovalStore,
    InMemorySpendLedger,
    InMemoryStepStore,
    RecordingTraceSink,
)
from guardrail_runtime.runtime import Runtime
from guardrail_runtime.types import Request, Response

FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0)


@dataclass
class Harness:
    runtime: Runtime
    config: TenantConfig
    model: ScriptedModel
    ledger: InMemorySpendLedger
    steps: InMemoryStepStore
    approvals: InMemoryApprovalStore
    traces: RecordingTraceSink
    sleeps: list[float] = field(default_factory=list)

    def run(self, prompt: str = "hello", approval_token: str | None = None) -> Response:
        return self.runtime.run(
            self.config, Request(prompt=prompt, approval_token=approval_token)
        )


def build(script: list[str | BaseException] | None = None, **config_kwargs) -> Harness:
    """Assemble a runtime over in-memory stores with a scripted model."""
    model = ScriptedModel(script=list(script) if script is not None else ["ok"])
    ledger = InMemorySpendLedger(clock=lambda: FIXED_NOW)
    steps = InMemoryStepStore()
    approvals = InMemoryApprovalStore()
    traces = RecordingTraceSink()
    sleeps: list[float] = []

    layers = [
        Tracing(),
        StepRecorder(),
        KillSwitch(),
        Guardrail(),
        HumanApproval(),
        CostGuard(),
        ModelRetry(sleep=sleeps.append),
    ]

    counter = {"n": 0}

    def next_run_id() -> str:
        counter["n"] += 1
        return f"run-{counter['n']}"

    runtime = Runtime(
        model=model,
        ledger=ledger,
        steps=steps,
        approvals=approvals,
        traces=traces,
        layers=layers,
        now=lambda: FIXED_NOW,
        run_id_factory=next_run_id,
    )

    return Harness(
        runtime=runtime,
        config=TenantConfig(tenant_id="tenant-a", **config_kwargs),
        model=model,
        ledger=ledger,
        steps=steps,
        approvals=approvals,
        traces=traces,
        sleeps=sleeps,
    )


@pytest.fixture
def harness() -> Harness:
    return build()
