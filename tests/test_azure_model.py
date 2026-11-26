"""AzureOpenAIModel: request shape, usage, refusals and error mapping, against a fake client.

No network and no credential. The fake client returns real `openai` response types and raises
real `openai` exceptions, so the mapping is tested against what the SDK actually produces.
"""

from __future__ import annotations

import openai
import pytest

try:  # openai 3.x is built on httpx2; 2.x used httpx. The exceptions need the matching types.
    import httpx2 as httpx
except ImportError:  # pragma: no cover - depends on the installed SDK
    import httpx
from openai.types.chat import ChatCompletion

from guardrail_runtime.model import AzureOpenAIModel
from guardrail_runtime.types import ModelUnavailable

REQUEST = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/chat/completions")


def completion(
    content: str | None = "hello",
    finish_reason: str = "stop",
    refusal: str | None = None,
) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content, "refusal": refusal},
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        }
    )


def status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    return cls("error", response=httpx.Response(status, request=REQUEST), body=None)


class FakeCompletions:
    def __init__(self, outcome: ChatCompletion | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def create(self, **kwargs: object) -> ChatCompletion:
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeClient:
    def __init__(self, outcome: ChatCompletion | BaseException) -> None:
        self.completions = FakeCompletions(outcome)
        self.chat = self  # client.chat.completions.create(...)


def model_for(outcome: ChatCompletion | BaseException) -> tuple[AzureOpenAIModel, FakeClient]:
    client = FakeClient(outcome)
    return AzureOpenAIModel("prod-gpt-4o", max_tokens=512, client=client), client


def test_sends_the_prompt_to_the_deployment_with_a_completion_token_cap() -> None:
    model, client = model_for(completion())
    model.complete("summarise the ticket")

    assert client.completions.calls == [
        {
            "model": "prod-gpt-4o",
            "messages": [{"role": "user", "content": "summarise the ticket"}],
            "max_completion_tokens": 512,
        }
    ]


def test_returns_the_text_and_maps_usage_for_the_cost_guard() -> None:
    model, _ = model_for(completion("the answer"))
    text, usage = model.complete("q")

    assert text == "the answer"
    assert (usage.input_tokens, usage.output_tokens, usage.attempts) == (120, 30, 1)


def test_a_refusal_is_not_reported_as_an_empty_answer() -> None:
    model, _ = model_for(completion(content=None, refusal="I can't help with that."))
    with pytest.raises(ModelUnavailable, match="declined"):
        model.complete("q")


def test_a_content_filter_stop_is_not_reported_as_an_answer() -> None:
    model, _ = model_for(completion(content="partial", finish_reason="content_filter"))
    with pytest.raises(ModelUnavailable, match="declined"):
        model.complete("q")


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=REQUEST),
        openai.APITimeoutError(request=REQUEST),
        status_error(openai.RateLimitError, 429),
        status_error(openai.InternalServerError, 500),
        status_error(openai.InternalServerError, 503),
    ],
    ids=["connection", "timeout", "rate-limit", "500", "503"],
)
def test_transient_failures_become_the_retryable_exception(error: BaseException) -> None:
    model, _ = model_for(error)
    with pytest.raises(ModelUnavailable):
        model.complete("q")


@pytest.mark.parametrize(
    ("cls", "status"),
    [(openai.BadRequestError, 400), (openai.AuthenticationError, 401), (openai.NotFoundError, 404)],
)
def test_client_errors_are_not_retried(cls: type[openai.APIStatusError], status: int) -> None:
    # A bad request or a wrong deployment name will fail the same way every time, so it must
    # not be dressed up as transient and burn the retry budget.
    model, _ = model_for(status_error(cls, status))
    with pytest.raises(cls):
        model.complete("q")


def test_missing_configuration_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="AZURE_OPENAI_DEPLOYMENT"):
        AzureOpenAIModel()
    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY"):
        AzureOpenAIModel("prod-gpt-4o")


def test_builds_a_v1_client_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://contoso.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "prod-gpt-4o")

    model = AzureOpenAIModel()

    assert model.deployment == "prod-gpt-4o"
    assert str(model._client.base_url) == "https://contoso.openai.azure.com/openai/v1/"
