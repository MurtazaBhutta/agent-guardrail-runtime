"""The model call the chain wraps, behind an interface.

The runtime's job is to constrain *whatever* is at the centre, so the centre is a two-method
port. Tests drive a scripted double, which is what makes the retry, budget and guardrail
behaviour exhaustively testable without a network or a credential.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from .context import RunContext
from .types import ModelUnavailable, Request, Response, RunStatus, Usage

DEFAULT_MODEL = os.environ.get("GUARDRAIL_MODEL", "claude-opus-5")


class ModelClient(Protocol):
    def complete(self, prompt: str) -> tuple[str, Usage]: ...


@dataclass
class ScriptedModel:
    """Deterministic test double.

    Each entry in `script` is consumed by one call: a string is returned as the completion,
    an exception instance is raised. That makes "fails twice then succeeds" a one-line
    fixture, which is the only honest way to test a retry policy.
    """

    script: list[str | BaseException] = field(default_factory=list)
    usage_per_call: Usage = field(
        default_factory=lambda: Usage(input_tokens=1000, output_tokens=500, attempts=1)
    )
    calls: list[str] = field(default_factory=list)

    def complete(self, prompt: str) -> tuple[str, Usage]:
        self.calls.append(prompt)
        if not self.script:
            raise AssertionError(
                f"ScriptedModel called {len(self.calls)} time(s) but the script is exhausted; "
                "the runtime made more model calls than the test expected"
            )
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome, self.usage_per_call


class ClaudeModel:
    """The real call. Requires the optional `claude` extra and a credential."""

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 4096) -> None:
        import anthropic

        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic()

    def complete(self, prompt: str) -> tuple[str, Usage]:
        import anthropic

        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError) as exc:
            # Both are transient by nature, so they become the retryable exception rather
            # than a terminal failure. A 400 or 404 is not retryable and is left to raise.
            raise ModelUnavailable(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ModelUnavailable(f"status {exc.status_code}") from exc
            raise

        usage = Usage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            attempts=1,
        )

        # stop_reason is checked before content is read. A refusal returns HTTP 200 with no
        # usable output, and reading content first would misreport it as an empty answer.
        if message.stop_reason == "refusal":
            raise ModelUnavailable("model declined the request")

        text = "".join(block.text for block in message.content if block.type == "text")
        return text, usage


def terminal_handler(model: ModelClient) -> object:
    """Wrap a `ModelClient` as the innermost handler of the chain."""

    def handler(ctx: RunContext, req: Request) -> Response:  # noqa: ARG001 - ctx unused here
        text, usage = model.complete(req.prompt)
        return Response(status=RunStatus.COMPLETED, text=text, usage=usage)

    return handler
