"""Tests for the Azure OpenAI gpt-audio client.

Uses ``httpx.MockTransport`` (built-in, no extra dependency) to capture
outbound requests and return canned responses. Each test constructs a
fresh AoaiClient via the ``_make_client`` helper.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from wa_voicenote.aoai_client import (
    AoaiClient,
    AoaiHttpError,
    AoaiParseError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_CONTENT = json.dumps(
    {
        "transcript": "hello world",
        "summary": "a greeting",
        "suggested_reply": "hi back",
    }
)

_DEFAULT_USAGE = {"prompt_tokens": 49, "completion_tokens": 69, "total_tokens": 118}
_DEFAULT_MODEL = "gpt-audio-mini-2025-12-15"
_DEFAULT_API_KEY = SecretStr("test-key")
_SENTINEL: Any = object()


def _aoai_response(
    content: str,
    *,
    status_code: int = 200,
    usage: dict[str, int] | None = None,
    model: str = _DEFAULT_MODEL,
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": usage if usage is not None else _DEFAULT_USAGE,
            "model": model,
        },
    )


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    deployment: str = "gpt-audio-mini",
    api_version: str = "2025-04-01-preview",
    system_prompt: str = "test prompt",
    endpoint: str = "https://test.openai.azure.com/",
    api_key: SecretStr | None = _SENTINEL,
    token_provider: Any = None,
) -> AoaiClient:
    if api_key is _SENTINEL:
        api_key = _DEFAULT_API_KEY
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return AoaiClient(
        endpoint=endpoint,
        deployment=deployment,
        api_version=api_version,
        system_prompt=system_prompt,
        http_timeout_seconds=10.0,
        api_key=api_key,
        token_provider=token_provider,
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_construction_requires_exactly_one_auth_neither() -> None:
    with pytest.raises(ValueError, match="exactly one of api_key"):
        AoaiClient(
            endpoint="https://x/",
            deployment="d",
            api_version="v",
            system_prompt="p",
            http_timeout_seconds=10.0,
        )


def test_construction_requires_exactly_one_auth_both() -> None:
    async def provider() -> str:
        return "tok"

    with pytest.raises(ValueError, match="exactly one of api_key"):
        AoaiClient(
            endpoint="https://x/",
            deployment="d",
            api_version="v",
            system_prompt="p",
            http_timeout_seconds=10.0,
            api_key=SecretStr("k"),
            token_provider=provider,
        )


# ---------------------------------------------------------------------------
# URL / payload shape
# ---------------------------------------------------------------------------


async def test_builds_correct_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return _aoai_response(_VALID_CONTENT)

    client = _make_client(handler)
    await client.process(b"RIFF....fakewavbytes")

    assert "gpt-audio-mini" in captured["url"]
    assert "api-version=2025-04-01-preview" in captured["url"]

    body = captured["body"]
    assert body["modalities"] == ["text"]
    assert body["max_tokens"] == 200

    messages = body["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "test prompt"

    user_msg = messages[1]
    assert user_msg["role"] == "user"
    user_content = user_msg["content"]
    assert isinstance(user_content, list)
    audio_block = user_content[-1]
    assert audio_block["type"] == "input_audio"
    assert audio_block["input_audio"]["format"] == "wav"
    decoded = base64.b64decode(audio_block["input_audio"]["data"])
    assert decoded == b"RIFF....fakewavbytes"


async def test_url_includes_deployment_and_api_version() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return _aoai_response(_VALID_CONTENT)

    client = _make_client(
        handler,
        deployment="my-custom-deploy",
        api_version="2099-01-01-preview",
    )
    await client.process(b"abc")

    parsed = urlparse(captured["url"])
    assert "/openai/deployments/my-custom-deploy/chat/completions" in parsed.path
    qs = parse_qs(parsed.query)
    assert qs["api-version"] == ["2099-01-01-preview"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_api_key_header_in_local_mode() -> None:
    captured: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return _aoai_response(_VALID_CONTENT)

    client = _make_client(handler, api_key=SecretStr("local-secret"))
    await client.process(b"abc")

    assert captured["headers"]["api-key"] == "local-secret"
    assert "authorization" not in captured["headers"]


async def test_bearer_token_in_prod_mode() -> None:
    captured: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return _aoai_response(_VALID_CONTENT)

    async def provider() -> str:
        return "fake-token"

    client = _make_client(handler, api_key=None, token_provider=provider)
    await client.process(b"abc")

    assert captured["headers"]["authorization"] == "Bearer fake-token"
    assert "api-key" not in captured["headers"]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


async def test_parses_valid_json_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _aoai_response(_VALID_CONTENT)

    client = _make_client(handler)
    result = await client.process(b"abc")
    assert result.transcript == "hello world"
    assert result.summary == "a greeting"
    assert result.suggested_reply == "hi back"


async def test_retries_once_on_non_json_then_success() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return _aoai_response("sorry, here is some prose")
        return _aoai_response(_VALID_CONTENT)

    client = _make_client(handler, system_prompt="base prompt")
    result = await client.process(b"abc")

    assert len(calls) == 2
    assert result.transcript == "hello world"
    # Second call's system prompt must include the stricter suffix
    second_system = calls[1]["messages"][0]["content"]
    assert second_system.startswith("base prompt")
    assert "raw JSON only" in second_system


async def test_raises_after_second_non_json() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _aoai_response("still no JSON here")

    client = _make_client(handler)
    with pytest.raises(AoaiParseError):
        await client.process(b"abc")
    assert len(calls) == 2


async def test_raises_on_http_500_no_retry() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, text="internal server error")

    client = _make_client(handler)
    with pytest.raises(AoaiHttpError) as exc:
        await client.process(b"abc")
    assert exc.value.status_code == 500
    assert "internal server error" in exc.value.body
    assert len(calls) == 1


async def test_raises_on_empty_choices() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [], "usage": _DEFAULT_USAGE, "model": _DEFAULT_MODEL}
        )

    client = _make_client(handler)
    with pytest.raises(AoaiParseError):
        await client.process(b"abc")


async def test_raises_on_missing_keys_in_content_json() -> None:
    bad_content = json.dumps({"transcript": "x", "summary": "y"})  # missing reply

    def handler(_request: httpx.Request) -> httpx.Response:
        return _aoai_response(bad_content)

    client = _make_client(handler)
    with pytest.raises(AoaiParseError):
        await client.process(b"abc")


# ---------------------------------------------------------------------------
# Content shape: context handling
# ---------------------------------------------------------------------------


async def test_context_added_before_audio() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _aoai_response(_VALID_CONTENT)

    client = _make_client(handler)
    await client.process(b"abc", context="my note")

    user_content = captured["body"]["messages"][1]["content"]
    assert len(user_content) == 2
    assert user_content[0]["type"] == "text"
    assert "my note" in user_content[0]["text"]
    assert user_content[1]["type"] == "input_audio"


async def test_no_context_means_audio_only() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _aoai_response(_VALID_CONTENT)

    client = _make_client(handler)
    await client.process(b"abc", context=None)

    user_content = captured["body"]["messages"][1]["content"]
    assert len(user_content) == 1
    assert user_content[0]["type"] == "input_audio"


# ---------------------------------------------------------------------------
# Prompt / payload fidelity
# ---------------------------------------------------------------------------


async def test_system_prompt_verbatim() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _aoai_response(_VALID_CONTENT)

    prompt = "You are a strict JSON-emitting assistant. Output JSON only."
    client = _make_client(handler, system_prompt=prompt)
    await client.process(b"abc")

    assert captured["body"]["messages"][0]["content"] == prompt


async def test_audio_base64_roundtrip() -> None:
    captured: dict[str, Any] = {}
    original = b"\x00\x01\x02\x03binary-blob\xff\xfe"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _aoai_response(_VALID_CONTENT)

    client = _make_client(handler)
    await client.process(original)

    audio_block = captured["body"]["messages"][1]["content"][-1]
    decoded = base64.b64decode(audio_block["input_audio"]["data"])
    assert decoded == original


# ---------------------------------------------------------------------------
# Result fields
# ---------------------------------------------------------------------------


async def test_latency_ms_populated() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _aoai_response(_VALID_CONTENT)

    client = _make_client(handler)
    result = await client.process(b"abc")
    assert result.latency_ms >= 0


async def test_model_field_populated() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _aoai_response(_VALID_CONTENT, model="gpt-audio-mini-9999-99-99")

    client = _make_client(handler)
    result = await client.process(b"abc")
    assert result.model == "gpt-audio-mini-9999-99-99"


async def test_token_counts_populated() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _aoai_response(
            _VALID_CONTENT,
            usage={"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        )

    client = _make_client(handler)
    result = await client.process(b"abc")
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 22
