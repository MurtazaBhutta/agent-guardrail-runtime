"""Chain composition.

A run is an onion: each middleware may inspect the request, decline to call inward, or
inspect what comes back out. Nothing about a layer's position is implicit - the order is
declared once, in `runtime.py`, and the order is load-bearing. ADR 0001 explains why each
layer sits where it does.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from .context import RunContext
from .types import Request, Response

Handler = Callable[[RunContext, Request], Response]


class Middleware(Protocol):
    """One layer.

    A layer calls `next_(ctx, req)` to continue inward, or returns without calling it to
    halt the run. Halting is a return, never a raise - see `types.py`.
    """

    name: str

    def __call__(self, ctx: RunContext, req: Request, next_: Handler) -> Response: ...


def build_chain(layers: Sequence[Middleware], terminal: Handler) -> Handler:
    """Compose layers around `terminal`. `layers[0]` ends up outermost."""
    handler = terminal
    for layer in reversed(layers):
        handler = _bind(layer, handler)
    return handler


def _bind(layer: Middleware, next_: Handler) -> Handler:
    """Bind one layer to its successor.

    This is a named function rather than a lambda inside the loop on purpose: a closure
    created in a loop captures the *variable*, not its value, so every layer would end up
    pointing at the last handler assigned. Binding through a function call captures the
    values as arguments instead.
    """

    def handler(ctx: RunContext, req: Request) -> Response:
        return layer(ctx, req, next_)

    return handler
