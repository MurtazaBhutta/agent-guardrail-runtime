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


class AzureOpenAIModel:
    """The same call against an Azure OpenAI deployment. Requires the optional `azure` extra.

    Uses the Azure OpenAI v1 API through the plain `openai` client, so there is no
    `api_version` to pin. The model parameter is the *deployment* name, not a model family.
    Configuration comes from AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and
    AZURE_OPENAI_DEPLOYMENT unless passed in. A `client` can be injected for tests.
    """

    def __init__(
        self,
        deployment: str | None = None,
        max_tokens: int = 4096,
        *,
        endpoint: str | None = None,
        api_key: str | None = None,
        client: object | None = None,
    ) -> None:
        self.deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
        self.max_tokens = max_tokens
        if not self.deployment:
            raise ValueError("set AZURE_OPENAI_DEPLOYMENT to the Azure OpenAI deployment name")
        if client is not None:
            self._client = client
            return

        endpoint = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        api_key = api_key or os.environ.get("AZURE_OPENAI_API_KEY", "")
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", endpoint),
                ("AZURE_OPENAI_API_KEY", api_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Azure OpenAI is not configured: set {', '.join(missing)}")

        import openai

        self._client = openai.OpenAI(base_url=f"{endpoint.rstrip('/')}/openai/v1/", api_key=api_key)

    def complete(self, prompt: str) -> tuple[str, Usage]:
        import openai

        try:
            completion = self._client.chat.completions.create(
                model=self.deployment,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=self.max_tokens,
            )
        except (openai.APIConnectionError, openai.RateLimitError) as exc:
            # APITimeoutError is a subclass of APIConnectionError. Same policy as ClaudeModel:
            # transient failures become the retryable exception.
            raise ModelUnavailable(str(exc)) from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ModelUnavailable(f"status {exc.status_code}") from exc
            raise

        usage = Usage(
            input_tokens=completion.usage.prompt_tokens if completion.usage else 0,
            output_tokens=completion.usage.completion_tokens if completion.usage else 0,
            attempts=1,
        )

        # As with Claude: a refusal or a content-filter stop comes back as a successful
        # response with no usable output, so it is checked before the content is read.
        choice = completion.choices[0]
        if choice.finish_reason == "content_filter" or choice.message.refusal:
            raise ModelUnavailable("model declined the request")

        return choice.message.content or "", usage


def terminal_handler(model: ModelClient) -> object:
    """Wrap a `ModelClient` as the innermost handler of the chain."""

    def handler(ctx: RunContext, req: Request) -> Response:  # noqa: ARG001 - ctx unused here
        text, usage = model.complete(req.prompt)
        return Response(status=RunStatus.COMPLETED, text=text, usage=usage)

    return handler
