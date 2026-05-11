"""Azure OpenAI gpt-audio Chat Completions client.

Sends a multimodal Chat Completions request with one input_audio block
and a system prompt that asks the model to return a JSON object with
keys transcript, summary, suggested_reply. Retries once with a stricter
prompt on non-JSON response; raises AoaiParseError after the second
failure.

Auth: API key (local dev) or Managed Identity bearer token (prod). The
caller injects exactly one via api_key or token_provider.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

import httpx
from pydantic import SecretStr

_MAX_TOKENS = 200
_HTTP_ERROR_THRESHOLD = 400


@dataclass(frozen=True)
class AoaiResult:
    """Result of a successful gpt-audio Chat Completions call."""

    transcript: str
    summary: str
    suggested_reply: str
    model: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int


class AoaiError(Exception):
    """Base AOAI failure."""


class AoaiHttpError(AoaiError):
    """HTTP non-2xx from AOAI. Carries status_code and response body."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"AOAI HTTP {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


class AoaiParseError(AoaiError):
    """Model returned non-JSON content (or missing required keys) after retry."""


_STRICTER_SUFFIX = (
    "\nIMPORTANT: Respond with raw JSON only. No prose, no markdown fences, no commentary."
)


class AoaiClient:
    """Typed wrapper around AOAI /chat/completions for gpt-audio.

    The class is auth-agnostic: callers inject exactly one of
    ``api_key`` (for local dev) or ``token_provider`` (for prod
    Managed Identity). All endpoint/deployment/version/prompt values
    are caller-injected from Settings; nothing is hardcoded.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        api_version: str,
        system_prompt: str,
        http_timeout_seconds: float,
        api_key: SecretStr | None = None,
        token_provider: Callable[[], Awaitable[str]] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if (api_key is None) == (token_provider is None):
            raise ValueError(
                "Provide exactly one of api_key (local dev) or "
                "token_provider (prod Managed Identity)."
            )
        self._endpoint = endpoint.rstrip("/")
        self._deployment = deployment
        self._api_version = api_version
        self._system_prompt = system_prompt
        self._timeout = http_timeout_seconds
        self._api_key = api_key
        self._token_provider = token_provider
        self._http = http_client  # if None, create a transient client per request

    # ---- URL / auth -------------------------------------------------------

    def _url(self) -> str:
        return (
            f"{self._endpoint}/openai/deployments/{self._deployment}"
            f"/chat/completions?api-version={self._api_version}"
        )

    async def _auth_headers(self) -> dict[str, str]:
        if self._api_key is not None:
            return {"api-key": self._api_key.get_secret_value()}
        # Constructor invariant: exactly one of api_key / token_provider is set.
        token_provider = self._token_provider
        assert token_provider is not None  # noqa: S101 - invariant
        token = await token_provider()
        return {"Authorization": f"Bearer {token}"}

    # ---- Body construction ------------------------------------------------

    def _build_body(
        self,
        wav_b64: str,
        context: str | None,
        system_prompt: str,
    ) -> dict[str, object]:
        user_content: list[dict[str, object]] = []
        if context:
            user_content.append(
                {
                    "type": "text",
                    "text": f"Additional context from the user: {context}",
                }
            )
        user_content.append(
            {
                "type": "input_audio",
                "input_audio": {"data": wav_b64, "format": "wav"},
            }
        )
        return {
            "modalities": ["text"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": _MAX_TOKENS,
        }

    # ---- Transport --------------------------------------------------------

    async def _send(self, body: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
        if self._http is not None:
            response = await self._http.post(self._url(), json=body, headers=headers)
        else:
            timeout = httpx.Timeout(self._timeout)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(self._url(), json=body, headers=headers)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            raise AoaiHttpError(response.status_code, response.text)
        result = response.json()
        if not isinstance(result, dict):
            raise AoaiParseError(f"Expected dict response, got {type(result).__name__}")
        return cast(dict[str, object], result)

    # ---- Response parsing -------------------------------------------------

    @staticmethod
    def _parse_choice(data: dict[str, object]) -> tuple[str, str, str]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AoaiParseError("Response missing choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise AoaiParseError("Choice is not an object")
        message = first.get("message")
        if not isinstance(message, dict):
            raise AoaiParseError("Choice message missing")
        content = message.get("content")
        if not isinstance(content, str):
            raise AoaiParseError("Choice content not a string")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AoaiParseError(f"Content not JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise AoaiParseError("Parsed content not a JSON object")
        transcript = parsed.get("transcript")
        summary = parsed.get("summary")
        suggested_reply = parsed.get("suggested_reply")
        if not (
            isinstance(transcript, str)
            and isinstance(summary, str)
            and isinstance(suggested_reply, str)
        ):
            raise AoaiParseError("Response JSON missing one of transcript/summary/suggested_reply")
        return transcript, summary, suggested_reply

    # ---- Public entrypoint ------------------------------------------------

    async def process(
        self,
        wav_bytes: bytes,
        context: str | None = None,
    ) -> AoaiResult:
        """Send a voice note to AOAI and return the parsed structured result.

        On parse failure (non-JSON or missing keys), retries exactly once
        with a stricter system prompt suffix. HTTP errors are not retried.
        """
        wav_b64 = base64.b64encode(wav_bytes).decode("ascii")
        headers = await self._auth_headers()
        headers["Content-Type"] = "application/json"

        start = time.perf_counter()
        body = self._build_body(wav_b64, context, self._system_prompt)
        try:
            data = await self._send(body, headers)
            transcript, summary, suggested_reply = self._parse_choice(data)
        except AoaiParseError:
            body = self._build_body(wav_b64, context, self._system_prompt + _STRICTER_SUFFIX)
            data = await self._send(body, headers)
            transcript, summary, suggested_reply = self._parse_choice(data)
        latency_ms = int((time.perf_counter() - start) * 1000)

        usage_obj = data.get("usage")
        usage: dict[str, object] = usage_obj if isinstance(usage_obj, dict) else {}
        prompt_tokens_raw = usage.get("prompt_tokens", 0)
        completion_tokens_raw = usage.get("completion_tokens", 0)
        prompt_tokens = int(prompt_tokens_raw) if isinstance(prompt_tokens_raw, (int, float)) else 0
        completion_tokens = (
            int(completion_tokens_raw) if isinstance(completion_tokens_raw, (int, float)) else 0
        )
        model_raw = data.get("model", "unknown")
        model = model_raw if isinstance(model_raw, str) else "unknown"

        return AoaiResult(
            transcript=transcript,
            summary=summary,
            suggested_reply=suggested_reply,
            model=model,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
