"""Every layer, firing, in one runnable script. No database, no credentials, no network.

    python -m examples.walkthrough

Each scenario changes exactly one thing about the tenant's policy or the model's behaviour,
so the output reads as a table of cause and effect.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

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
from guardrail_runtime.types import ModelUnavailable, Request, Usage

FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0)
BASE = TenantConfig(tenant_id="acme", daily_budget_cents=1000)


def run_scenario(label: str, config: TenantConfig, script: list, prompt: str = "post invoice 4471"):
    ledger = InMemorySpendLedger(clock=lambda: FIXED_NOW)
    steps = InMemoryStepStore()
    traces = RecordingTraceSink()

    runtime = Runtime(
        model=ScriptedModel(script=script),
        ledger=ledger,
        steps=steps,
        approvals=InMemoryApprovalStore(),
        traces=traces,
        # sleep is a no-op so the walkthrough does not pause on the retry scenario
        layers=[
            Tracing(),
            StepRecorder(),
            KillSwitch(),
            Guardrail(),
            HumanApproval(),
            CostGuard(),
            ModelRetry(sleep=lambda _: None),
        ],
        now=lambda: FIXED_NOW,
        run_id_factory=lambda: "run-demo",
    )

    response = runtime.run(config, Request(prompt=prompt))
    span = traces.spans[0]
    trail = " -> ".join(s.layer for s in steps.for_run("run-demo"))

    print(f"\n{label}")
    print(f"  status      {response.status}")
    print(f"  reason      {response.reason}")
    if response.detail:
        print(f"  detail      {response.detail}")
    print(f"  attempts    {response.usage.attempts}")
    print(f"  billed      {span.cost_cents}c")
    print(f"  trail       {trail}")
    return response


def main() -> None:
    print("=" * 78)
    print("guardrail-runtime walkthrough - one policy change per scenario")
    print("=" * 78)

    run_scenario("1. Normal run: every layer permits it", BASE, ["Invoice 4471 posted."])

    run_scenario(
        "2. Kill switch on: stops before anything is computed or spent",
        replace(BASE, kill_switch_enabled=True),
        ["never reached"],
    )

    run_scenario(
        "3. Input guardrail: refused before the model is called",
        replace(BASE, blocked_input_patterns=(r"drop\s+table",)),
        ["never reached"],
        prompt="drop table invoices",
    )

    run_scenario(
        "4. Output guardrail: refused AFTER generation, so still billed",
        replace(BASE, blocked_output_patterns=("sk-live",)),
        ["the key is sk-live-abc123"],
    )

    run_scenario(
        "5. Human in the loop: pauses, and a pause is never billed",
        replace(BASE, hitl_required=True),
        ["never reached"],
    )

    run_scenario(
        "6. Budget already exhausted: refused before the model",
        replace(BASE, daily_budget_cents=0),
        ["never reached"],
    )

    burned = Usage(input_tokens=300)
    run_scenario(
        "7. Two transient failures then success: all three attempts billed",
        replace(BASE, max_attempts=3),
        [
            ModelUnavailable("timeout", usage=burned),
            ModelUnavailable("timeout", usage=burned),
            "Posted.",
        ],
    )

    run_scenario(
        "8. Retries exhausted: FAILED as an outcome, not an exception - and still billed",
        replace(BASE, max_attempts=2),
        [ModelUnavailable("upstream down", usage=burned)] * 2,
    )

    print("\n" + "=" * 78)
    print("Note scenarios 4, 7 and 8: work that produced no usable output still cost money,")
    print("and the ledger reflects spend rather than delivery.")
    print("=" * 78)


if __name__ == "__main__":
    main()
