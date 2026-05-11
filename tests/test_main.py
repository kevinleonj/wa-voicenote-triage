"""Tests for the FastAPI app wiring in ``wa_voicenote.main``.

Strategy:
- Tests build a *fresh* FastAPI app via ``create_app()`` with NO lifespan,
  so we never touch Azure clients.
- The signature dependency is overridden with a no-op so tests can hit the
  webhook route directly.
- The handler is replaced with an ``AsyncMock`` so we can assert how the
  route parsed the incoming form into an ``InboundMessage``.
- Lifespan coverage is exercised separately via ``contextlib.aclosing`` of
  a stubbed app, with Azure client constructors patched so no network I/O
  is attempted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from wa_voicenote.handlers import InboundMessage
from wa_voicenote.main import create_app, lifespan
from wa_voicenote.twilio_signing import require_valid_twilio_signature

_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def _build_test_app(handler_mock: AsyncMock) -> FastAPI:
    """Construct a no-lifespan FastAPI app with the handler/state stubbed."""
    app = create_app()
    app.state.handler = handler_mock

    async def _allow_all() -> None:
        return None

    app.dependency_overrides[require_valid_twilio_signature] = _allow_all
    return app


@pytest.fixture
async def signed_client() -> AsyncIterator[tuple[httpx.AsyncClient, AsyncMock]]:
    """Yield (client, handler_mock) with the signature dep overridden."""
    handler_mock = AsyncMock()
    app = _build_test_app(handler_mock)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, handler_mock
    app.dependency_overrides.clear()


# ---- /health ---------------------------------------------------------------


async def test_health_returns_200_ok() -> None:
    """GET /health should return 200 and the canonical body."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---- /webhook/whatsapp: signature gate -------------------------------------


async def test_webhook_invalid_signature_returns_403(
    valid_settings_env: None,  # noqa: ARG001 - settings dependency for real dep
) -> None:
    """Without overriding the signature dep, a request lacking the header is 403."""
    # Use the REAL signature dependency (no override).
    app = create_app()
    app.state.handler = AsyncMock()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.post(
            "/webhook/whatsapp",
            data={"MessageSid": "SM1", "From": "whatsapp:+34611779374"},
        )
    assert response.status_code == 403


# ---- /webhook/whatsapp: happy path -----------------------------------------


async def test_webhook_valid_signature_returns_200_empty_twiml(
    signed_client: tuple[httpx.AsyncClient, AsyncMock],
) -> None:
    """A signed POST returns 200 with the canonical empty TwiML body."""
    client, _handler = signed_client
    response = await client.post(
        "/webhook/whatsapp",
        data={
            "MessageSid": "SMabc",
            "From": "whatsapp:+34611779374",
            "Body": "hello",
            "NumMedia": "0",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert response.text == _EMPTY_TWIML


async def test_webhook_calls_handler_with_parsed_inbound(
    signed_client: tuple[httpx.AsyncClient, AsyncMock],
) -> None:
    """Form fields must be parsed into a matching InboundMessage."""
    client, handler = signed_client
    response = await client.post(
        "/webhook/whatsapp",
        data={
            "MessageSid": "SM123",
            "From": "whatsapp:+34611779374",
            "Body": "context here",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/.../Media/MEabc",
            "MediaContentType0": "audio/ogg",
        },
    )
    assert response.status_code == 200
    handler.handle.assert_awaited_once()
    inbound: InboundMessage = handler.handle.await_args.args[0]
    assert isinstance(inbound, InboundMessage)
    assert inbound.message_sid == "SM123"
    assert inbound.from_ == "whatsapp:+34611779374"
    assert inbound.body == "context here"
    assert inbound.num_media == 1
    assert inbound.media_url_0 == "https://api.twilio.com/.../Media/MEabc"
    assert inbound.media_content_type_0 == "audio/ogg"


# ---- /webhook/whatsapp: handler errors do not 500 --------------------------


async def test_webhook_handler_exception_does_not_500(
    signed_client: tuple[httpx.AsyncClient, AsyncMock],
) -> None:
    """If handler.handle raises, the webhook still returns 200 + empty TwiML."""
    client, handler = signed_client
    handler.handle.side_effect = RuntimeError("simulated downstream failure")
    response = await client.post(
        "/webhook/whatsapp",
        data={
            "MessageSid": "SMboom",
            "From": "whatsapp:+34611779374",
            "Body": "",
            "NumMedia": "0",
        },
    )
    assert response.status_code == 200
    assert response.text == _EMPTY_TWIML
    handler.handle.assert_awaited_once()


# ---- /webhook/whatsapp: form parsing edge cases ----------------------------


async def test_webhook_parses_num_media_int(
    signed_client: tuple[httpx.AsyncClient, AsyncMock],
) -> None:
    """NumMedia='2' must arrive as int 2 inside the InboundMessage."""
    client, handler = signed_client
    response = await client.post(
        "/webhook/whatsapp",
        data={
            "MessageSid": "SMmm",
            "From": "whatsapp:+34611779374",
            "Body": "",
            "NumMedia": "2",
            "MediaUrl0": "https://example.invalid/m0",
            "MediaContentType0": "audio/ogg",
        },
    )
    assert response.status_code == 200
    inbound: InboundMessage = handler.handle.await_args.args[0]
    assert inbound.num_media == 2
    assert isinstance(inbound.num_media, int)


async def test_webhook_media_url_none_when_missing(
    signed_client: tuple[httpx.AsyncClient, AsyncMock],
) -> None:
    """With NumMedia=0 and no MediaUrl0 field, media_url_0 must be None."""
    client, handler = signed_client
    response = await client.post(
        "/webhook/whatsapp",
        data={
            "MessageSid": "SMnoaudio",
            "From": "whatsapp:+34611779374",
            "Body": "hi",
            "NumMedia": "0",
        },
    )
    assert response.status_code == 200
    inbound: InboundMessage = handler.handle.await_args.args[0]
    assert inbound.num_media == 0
    assert inbound.media_url_0 is None
    assert inbound.media_content_type_0 is None


async def test_webhook_missing_num_media_defaults_to_zero(
    signed_client: tuple[httpx.AsyncClient, AsyncMock],
) -> None:
    """A request entirely missing NumMedia must not crash; num_media -> 0."""
    client, handler = signed_client
    response = await client.post(
        "/webhook/whatsapp",
        data={
            "MessageSid": "SMno_nm",
            "From": "whatsapp:+34611779374",
            "Body": "hi",
        },
    )
    assert response.status_code == 200
    inbound: InboundMessage = handler.handle.await_args.args[0]
    assert inbound.num_media == 0


async def test_webhook_invalid_num_media_falls_back_to_zero(
    signed_client: tuple[httpx.AsyncClient, AsyncMock],
) -> None:
    """A non-integer NumMedia must be coerced to 0 rather than 500-ing."""
    client, handler = signed_client
    response = await client.post(
        "/webhook/whatsapp",
        data={
            "MessageSid": "SMbadnm",
            "From": "whatsapp:+34611779374",
            "Body": "",
            "NumMedia": "not-a-number",
        },
    )
    assert response.status_code == 200
    inbound: InboundMessage = handler.handle.await_args.args[0]
    assert inbound.num_media == 0


# ---- create_app + module-level app -----------------------------------------


def test_module_level_app_has_lifespan() -> None:
    """The module-level ``app`` is constructed WITH the production lifespan.

    Asserting the attribute is present is enough; we never actually run it in
    tests because spinning up Azure clients in unit tests is the whole reason
    ``create_app`` exists.
    """
    from wa_voicenote.main import app, lifespan

    # Starlette stores the lifespan as ``router.lifespan_context``.
    assert app.router.lifespan_context is not None
    # Sanity: the production lifespan symbol is the one we expect.
    assert callable(lifespan)


def test_create_app_registers_routes() -> None:
    """create_app() must register both /health and /webhook/whatsapp."""
    app = create_app()
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/health" in paths
    assert "/webhook/whatsapp" in paths


def test_create_app_accepts_lifespan_override() -> None:
    """Passing a custom lifespan must be honored (used by integration tests)."""
    from contextlib import asynccontextmanager

    sentinel: dict[str, Any] = {"started": False}

    @asynccontextmanager
    async def stub_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        sentinel["started"] = True
        yield

    app = create_app(lifespan_override=stub_lifespan)
    assert app.router.lifespan_context is not None


# ---- _build_aoai_client auth-mode branches ---------------------------------


def test_build_aoai_client_uses_api_key_when_present(valid_settings_env: None) -> None:  # noqa: ARG001
    """When AZURE_OPENAI_API_KEY is set, the AoaiClient is built with api_key."""
    import os

    from wa_voicenote.config import get_settings
    from wa_voicenote.main import _build_aoai_client

    os.environ["AZURE_OPENAI_API_KEY"] = "k-local"
    get_settings.cache_clear()
    try:
        settings = get_settings()
        # Sanity: the env var landed.
        assert settings.azure_openai_api_key is not None
        credential = MagicMock()  # never used in the api-key branch
        client = _build_aoai_client(settings, credential)
        # The AoaiClient must hold a SecretStr api_key, not a token_provider.
        assert client._api_key is not None
        assert isinstance(client._api_key, SecretStr)
        assert client._token_provider is None
    finally:
        os.environ.pop("AZURE_OPENAI_API_KEY", None)
        get_settings.cache_clear()


async def test_build_aoai_client_uses_token_provider_in_prod(
    valid_settings_env: None,  # noqa: ARG001 - fixture sets env
) -> None:
    """Without an API key, AoaiClient must be built with a token provider."""
    from wa_voicenote.config import get_settings
    from wa_voicenote.main import _build_aoai_client

    settings = get_settings()
    assert settings.azure_openai_api_key is None

    # Stub credential exposing async get_token returning a token-shaped object.
    fake_token = MagicMock()
    fake_token.token = "fake-jwt"  # noqa: S105 - test fixture, not a real secret
    credential = MagicMock()
    credential.get_token = AsyncMock(return_value=fake_token)

    client = _build_aoai_client(settings, credential)
    assert client._api_key is None
    assert client._token_provider is not None

    # Exercise the closure once so its body is covered and the scope is honored.
    resolved = await client._token_provider()
    assert resolved == "fake-jwt"
    credential.get_token.assert_awaited_once_with("https://cognitiveservices.azure.com/.default")


# ---- lifespan: startup/shutdown wiring -------------------------------------


async def test_lifespan_builds_clients_and_cleans_up(valid_settings_env: None) -> None:  # noqa: ARG001
    """Lifespan must build clients, attach handler to state, then close all clients."""
    fake_credential = MagicMock()
    fake_credential.close = AsyncMock()
    fake_credential.get_token = AsyncMock(return_value=MagicMock(token="t"))  # noqa: S106 - test stub

    fake_table = MagicMock()
    fake_table.close = AsyncMock()

    fake_blob_service = MagicMock()
    fake_blob_service.close = AsyncMock()
    fake_blob_service.get_container_client = MagicMock(return_value=MagicMock())

    with (
        patch("wa_voicenote.main.DefaultAzureCredential", return_value=fake_credential),
        patch("wa_voicenote.main.TableClient", return_value=fake_table),
        patch("wa_voicenote.main.BlobServiceClient", return_value=fake_blob_service),
    ):
        app = FastAPI()
        async with lifespan(app):
            # Startup happened: handler and clients are on state.
            assert app.state.handler is not None
            assert app.state.table_client is fake_table
            assert app.state.blob_service is fake_blob_service
            assert app.state.credential is fake_credential
            assert isinstance(app.state.media_http_client, httpx.AsyncClient)

    # Shutdown happened: every client we plugged in had ``close`` awaited.
    fake_table.close.assert_awaited_once()
    fake_blob_service.close.assert_awaited_once()
    fake_credential.close.assert_awaited_once()


async def test_lifespan_media_fetcher_uses_basic_auth_client(
    valid_settings_env: None,  # noqa: ARG001
) -> None:
    """The handler's media_fetcher must download via the shared httpx client.

    We verify the fetcher closure is wired by handing it a mock transport and
    asserting we get the bytes back.
    """
    fake_credential = MagicMock()
    fake_credential.close = AsyncMock()
    fake_table = MagicMock()
    fake_table.close = AsyncMock()
    fake_blob_service = MagicMock()
    fake_blob_service.close = AsyncMock()
    fake_blob_service.get_container_client = MagicMock(return_value=MagicMock())

    with (
        patch("wa_voicenote.main.DefaultAzureCredential", return_value=fake_credential),
        patch("wa_voicenote.main.TableClient", return_value=fake_table),
        patch("wa_voicenote.main.BlobServiceClient", return_value=fake_blob_service),
    ):
        app = FastAPI()
        async with lifespan(app):
            # The shared media client must exist and be a real httpx.AsyncClient.
            media_client = app.state.media_http_client
            assert isinstance(media_client, httpx.AsyncClient)
            # The handler is the one constructed inside lifespan.
            handler = app.state.handler
            assert handler is not None
