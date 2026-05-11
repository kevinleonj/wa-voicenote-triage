"""Tests for the Twilio Programmable Messaging client.

Uses ``httpx.MockTransport`` (built-in, no extra dependency) to capture
outbound requests and return canned responses. Each test constructs a
fresh ``TwilioClient`` via the ``_make_client`` helper.
"""

from __future__ import annotations

import base64
import io
import sys
import time
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from wa_voicenote.twilio_client import (
    TwilioClient,
    TwilioHttpError,
    TwilioMessage,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_TEST_ACCOUNT_SID = "AC" + "0" * 32
_TEST_AUTH_TOKEN = "super-secret-token-value-xyz"  # noqa: S105 - test-only sentinel
_TEST_FROM = "whatsapp:+14155238886"
_TEST_TO = "whatsapp:+34611779374"
_TEST_BODY = "hello from c11 tests"


def _twilio_response(
    *,
    status_code: int = 201,
    sid: str = "SM" + "a" * 32,
    status: str = "queued",
    to: str = _TEST_TO,
    from_: str = _TEST_FROM,
    text: str | None = None,
) -> httpx.Response:
    if text is not None:
        return httpx.Response(status_code=status_code, text=text)
    return httpx.Response(
        status_code=status_code,
        json={
            "sid": sid,
            "status": status,
            "to": to,
            "from": from_,
        },
    )


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    account_sid: str = _TEST_ACCOUNT_SID,
    auth_token: SecretStr | None = None,
    from_number: str = _TEST_FROM,
) -> tuple[TwilioClient, httpx.AsyncClient]:
    """Return (client, underlying-mocked-async-http-client).

    The caller can introspect the second value to assert reuse semantics.
    """
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = TwilioClient(
        account_sid=account_sid,
        auth_token=auth_token if auth_token is not None else SecretStr(_TEST_AUTH_TOKEN),
        from_number=from_number,
        http_timeout_seconds=10.0,
        http_client=http_client,
    )
    return client, http_client


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


async def test_send_text_builds_correct_request() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["content_type"] = request.headers.get("content-type", "")
        captured["auth"] = request.headers.get("authorization", "")
        return _twilio_response()

    client, _ = _make_client(handler)
    await client.send_text(_TEST_TO, _TEST_BODY)

    # URL contains AccountSid and Messages.json
    parsed = urlparse(captured["url"])
    assert parsed.path == f"/2010-04-01/Accounts/{_TEST_ACCOUNT_SID}/Messages.json"

    # Form-urlencoded body with From/To/Body
    form = parse_qs(captured["body"])
    assert form["From"] == [_TEST_FROM]
    assert form["To"] == [_TEST_TO]
    assert form["Body"] == [_TEST_BODY]

    # httpx default for ``data=`` is application/x-www-form-urlencoded
    assert "application/x-www-form-urlencoded" in captured["content_type"]

    # Basic auth header is set
    assert captured["auth"].startswith("Basic ")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


async def test_send_text_parses_2xx_response() -> None:
    expected_sid = "SM" + "b" * 32

    def handler(_request: httpx.Request) -> httpx.Response:
        return _twilio_response(
            status_code=201,
            sid=expected_sid,
            status="queued",
            to=_TEST_TO,
            from_=_TEST_FROM,
        )

    client, _ = _make_client(handler)
    msg = await client.send_text(_TEST_TO, _TEST_BODY)

    assert isinstance(msg, TwilioMessage)
    assert msg.sid == expected_sid
    assert msg.status == "queued"
    assert msg.to == _TEST_TO
    assert msg.from_ == _TEST_FROM


# ---------------------------------------------------------------------------
# Error handling: 4xx is NOT retried
# ---------------------------------------------------------------------------


async def test_send_text_raises_on_4xx() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _twilio_response(status_code=400, text='{"code":21211,"message":"invalid To"}')

    client, _ = _make_client(handler)
    with pytest.raises(TwilioHttpError) as exc:
        await client.send_text(_TEST_TO, _TEST_BODY)
    assert exc.value.status_code == 400
    assert "invalid To" in exc.value.body
    # Critical: no retry on 4xx (other than 429)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Retry on 429
# ---------------------------------------------------------------------------


async def test_send_text_retries_on_429_then_succeeds() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return _twilio_response(status_code=429, text="rate limited")
        return _twilio_response()

    client, _ = _make_client(handler)
    msg = await client.send_text(_TEST_TO, _TEST_BODY, backoff_seconds=0.0)

    assert len(calls) == 2
    assert msg.status == "queued"


# ---------------------------------------------------------------------------
# Retry on 5xx
# ---------------------------------------------------------------------------


async def test_send_text_retries_on_500_then_succeeds() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return _twilio_response(status_code=500, text="boom")
        return _twilio_response()

    client, _ = _make_client(handler)
    msg = await client.send_text(_TEST_TO, _TEST_BODY, backoff_seconds=0.0)

    assert len(calls) == 2
    assert msg.sid.startswith("SM")


# ---------------------------------------------------------------------------
# Retry exhaustion
# ---------------------------------------------------------------------------


async def test_send_text_exhausts_retries_then_raises() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _twilio_response(status_code=503, text="unavailable")

    client, _ = _make_client(handler)
    with pytest.raises(TwilioHttpError) as exc:
        await client.send_text(_TEST_TO, _TEST_BODY, max_retries=2, backoff_seconds=0.0)
    assert exc.value.status_code == 503
    # initial attempt + 2 retries = 3 total calls
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Basic auth header content
# ---------------------------------------------------------------------------


async def test_send_text_uses_basic_auth_with_account_sid() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return _twilio_response()

    client, _ = _make_client(handler)
    await client.send_text(_TEST_TO, _TEST_BODY)

    assert captured["auth"].startswith("Basic ")
    encoded = captured["auth"].removeprefix("Basic ").strip()
    decoded = base64.b64decode(encoded).decode("ascii")
    assert decoded == f"{_TEST_ACCOUNT_SID}:{_TEST_AUTH_TOKEN}"


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


async def test_send_text_secret_not_in_url() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return _twilio_response()

    client, _ = _make_client(handler)
    await client.send_text(_TEST_TO, _TEST_BODY)

    assert _TEST_AUTH_TOKEN not in captured["url"]
    assert _TEST_AUTH_TOKEN not in captured["body"]


# ---------------------------------------------------------------------------
# From / To / Body fidelity
# ---------------------------------------------------------------------------


async def test_send_text_uses_configured_from_number() -> None:
    captured: dict[str, dict[str, list[str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["form"] = parse_qs(request.content.decode())
        return _twilio_response()

    custom_from = "whatsapp:+19998887777"
    client, _ = _make_client(handler, from_number=custom_from)
    await client.send_text(_TEST_TO, _TEST_BODY)

    assert captured["form"]["From"] == [custom_from]


async def test_send_text_to_field_passed_through() -> None:
    captured: dict[str, dict[str, list[str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["form"] = parse_qs(request.content.decode())
        return _twilio_response()

    client, _ = _make_client(handler)
    target = "whatsapp:+15551234567"
    await client.send_text(target, _TEST_BODY)

    assert captured["form"]["To"] == [target]


async def test_send_text_body_field_utf8() -> None:
    captured: dict[str, dict[str, list[str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["form"] = parse_qs(request.content.decode())
        return _twilio_response()

    client, _ = _make_client(handler)
    body = "Hola, qué tal — saludos desde Málaga 🎉"
    await client.send_text(_TEST_TO, body)

    assert captured["form"]["Body"] == [body]


# ---------------------------------------------------------------------------
# Backoff timing
# ---------------------------------------------------------------------------


async def test_backoff_between_retries() -> None:
    timestamps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        timestamps.append(time.perf_counter())
        if len(timestamps) == 1:
            return _twilio_response(status_code=429, text="rate limited")
        return _twilio_response()

    client, _ = _make_client(handler)
    # Use a small but observable backoff so the test stays fast.
    backoff = 0.05
    await client.send_text(_TEST_TO, _TEST_BODY, backoff_seconds=backoff)

    assert len(timestamps) == 2
    delta = timestamps[1] - timestamps[0]
    # Linear backoff for attempt index 0 -> sleep = backoff * (0 + 1) = backoff
    assert delta >= backoff


# ---------------------------------------------------------------------------
# External http_client reuse
# ---------------------------------------------------------------------------


async def test_construction_with_external_http_client() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _twilio_response()

    client, http_client = _make_client(handler)
    # If the supplied http_client is used, both calls go through the same
    # MockTransport. If a new transient client were created instead, the
    # MockTransport handler would never run for the second call.
    await client.send_text(_TEST_TO, _TEST_BODY)
    await client.send_text(_TEST_TO, _TEST_BODY)

    assert len(calls) == 2
    # Confirm the client object we passed in is still healthy/usable.
    assert not http_client.is_closed


# ---------------------------------------------------------------------------
# Auth token never leaks to logs / repr / str
# ---------------------------------------------------------------------------


async def test_secret_not_logged() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _twilio_response()

    client, _ = _make_client(handler)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        await client.send_text(_TEST_TO, _TEST_BODY)
        # Also exercise repr/str paths an unwary developer might log.
        sys.stdout.write(repr(client))
        sys.stdout.write(str(client))

    assert _TEST_AUTH_TOKEN not in stdout_buf.getvalue()
    assert _TEST_AUTH_TOKEN not in stderr_buf.getvalue()
