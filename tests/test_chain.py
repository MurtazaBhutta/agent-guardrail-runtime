"""Chain composition and layer order.

The order is the design, so it gets asserted rather than assumed. If someone reorders
`default_layers` these tests say what breaks and why.
"""

from __future__ import annotations

from conftest import build
from guardrail_runtime.chain import build_chain
from guardrail_runtime.context import RunContext
from guardrail_runtime.runtime import default_layers
from guardrail_runtime.types import HaltReason, Request, Response, RunStatus, halt


def test_default_layer_order_is_exactly_this() -> None:
    # Locked deliberately. Each position is justified in ADR 0001; a change here should be
    # a conscious decision with the ADR updated, not a silent reshuffle.
    assert [layer.name for layer in default_layers()] == [
        "trace",
        "step_recorder",
        "kill_switch",
        "guardrail",
        "hitl",
        "cost_guard",
        "model_retry",
    ]


def test_layers_wrap_outermost_first() -> None:
    calls: list[str] = []

    def make(name: str):
        class Layer:
            def __init__(self) -> None:
                self.name = name

            def __call__(self, ctx, req, next_):
                calls.append(f"enter:{name}")
                response = next_(ctx, req)
                calls.append(f"exit:{name}")
                return response

        return Layer()

    def terminal(ctx, req):
        calls.append("model")
        return Response(status=RunStatus.COMPLETED, text="ok")

    handler = build_chain([make("outer"), make("inner")], terminal)
    handler(None, Request(prompt="x"))

    assert calls == [
        "enter:outer",
        "enter:inner",
        "model",
        "exit:inner",
        "exit:outer",
    ]


def test_each_layer_binds_to_its_own_successor() -> None:
    """Guards the classic loop-closure bug.

    If `build_chain` built its closures inline in the loop, every layer would capture the
    same `handler` variable and end up calling whichever handler was assigned last - the
    chain would silently collapse. Three distinct layers in strict order is the shape that
    only holds if binding captured values rather than the variable.
    """
    seen: list[int] = []

    def make(index: int):
        class Layer:
            name = f"layer{index}"

            def __call__(self, ctx, req, next_):
                seen.append(index)
                return next_(ctx, req)

        return Layer()

    handler = build_chain(
        [make(1), make(2), make(3)],
        lambda ctx, req: Response(status=RunStatus.COMPLETED),
    )
    handler(None, Request(prompt="x"))
    assert seen == [1, 2, 3]


def test_a_halt_short_circuits_everything_inward() -> None:
    harness = build(kill_switch_enabled=True)
    response = harness.run()

    assert response.status is RunStatus.BLOCKED
    # The model was never called, and no inner policy layer was even entered.
    assert harness.model.calls == []
    assert harness.steps.layers_for("run-1") == ["kill_switch", "run"]


def test_an_empty_chain_still_reaches_the_model() -> None:
    def terminal(ctx, req):
        return Response(status=RunStatus.COMPLETED, text="bare")

    assert build_chain([], terminal)(None, Request(prompt="x")).text == "bare"


def test_context_records_entry_before_a_layer_can_halt() -> None:
    """A layer that halts must still appear in the trail.

    Recording entry on the way out would omit the halting layer from exactly the runs where
    knowing which layer stopped things is the entire question.
    """
    ctx = RunContext(
        tenant_id="t",
        run_id="r",
        config=build().config,
        ledger=None,
        steps=None,
        approvals=None,
        traces=None,
        now=lambda: None,
    )
    ctx.enter("kill_switch")
    ctx.note("kill_switch", halt(HaltReason.KILL_SWITCH))
    assert ctx.entered == ["kill_switch"]
    assert ctx.notes["kill_switch"].reason is HaltReason.KILL_SWITCH
