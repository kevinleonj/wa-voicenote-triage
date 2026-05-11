"""Tests for the /diag endpoint and helpers."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from azure.core.exceptions import ServiceRequestError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from wa_voicenote.diag import (
    PingResult,
    _verify_bearer,
    ping_aoai,
    ping_blob,
    ping_table,
)
from wa_voicenote.main import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


_DIAG_TOKEN = "test-diag-token-1234567890"  # noqa: S105 - test fixture, not a real secret
_FAKE_AOAI_KEY = "test-aoai-key"


def _settings_stub(*, diag_token: str | None = _DIAG_TOKEN) -> SimpleNamespace:
    return SimpleNamespace(
        env_name="test",
        diag_token=SecretStr(diag_token) if diag_token is not None else None,
        azure_openai_endpoint="https://test.openai.azure.com/",
        azure_openai_deployment="gpt-audio-mini",
        azure_openai_api_version="2025-04-01-preview",
        azure_openai_api_key=SecretStr(_FAKE_AOAI_KEY),
        azure_storage_container="audio-staging",
        http_timeout_seconds=10,
        applicationinsights_connection_string=None,
    )


def _make_async_iter() -> Any:
    """Return an object whose ``__aiter__`` yields one then stops.

    Used as the return value of ``list_entities`` in the ping_table mock.
    """

    class _Iter:
        async def __aiter__(self) -> Any:
            yield {"PartitionKey": "x", "RowKey": "y"}

    return _Iter()


def _app_with_state(*, diag_token: str | None = _DIAG_TOKEN) -> FastAPI:
    """Build a FastAPI app with no lifespan and mocked state."""
    app = create_app(lifespan_override=None)
    app.state.settings = _settings_stub(diag_token=diag_token)
    app.state.table_client = AsyncMock()
    app.state.table_client.list_entities = lambda **_kw: _make_async_iter()
    app.state.blob_service = AsyncMock()
    container_mock = AsyncMock()
    container_mock.get_container_properties = AsyncMock(return_value={})
    app.state.blob_service.get_container_client = lambda _name: container_mock
    return app


@pytest.fixture
async def client_factory() -> AsyncIterator[Any]:
    """Yield a factory that builds an AsyncClient against a fresh app."""
    clients: list[AsyncClient] = []

    def _factory(*, diag_token: str | None = _DIAG_TOKEN) -> AsyncClient:
        app = _app_with_state(diag_token=diag_token)
        client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        )
        clients.append(client)
        return client

    try:
        yield _factory
    finally:
        for c in clients:
            await c.aclose()


# ---------- _verify_bearer ----------


def test_verify_bearer_503_when_expected_none() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        _verify_bearer("Bearer x", None)
    assert exc_info.value.status_code == 503


def test_verify_bearer_401_when_authorization_missing() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        _verify_bearer(None, "token")
    assert exc_info.value.status_code == 401


def test_verify_bearer_401_when_scheme_wrong() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        _verify_bearer("Basic abc", "token")
    assert exc_info.value.status_code == 401


def test_verify_bearer_401_when_token_wrong() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        _verify_bearer("Bearer wrong", "token")
    assert exc_info.value.status_code == 401


def test_verify_bearer_succeeds_on_match() -> None:
    # No exception means success.
    _verify_bearer("Bearer token", "token")


def test_verify_bearer_uses_constant_time_compare() -> None:
    src = inspect.getsource(_verify_bearer)
    assert "compare_digest" in src


# ---------- ping helpers ----------


@pytest.mark.asyncio
async def test_ping_aoai_returns_ok_on_2xx() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "gpt-audio-mini"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as _:
        # Direct call; ping_aoai constructs its own client. Use patch to swap.
        pass

    with patch("wa_voicenote.diag.httpx.AsyncClient") as mock_async_client:
        instance = AsyncMock()
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = None
        instance.get = AsyncMock(return_value=httpx.Response(200, json={}))
        mock_async_client.return_value = instance

        result = await ping_aoai(
            endpoint="https://x.openai.azure.com/",
            deployment="d",
            api_version="v",
            auth_header={},
            timeout_seconds=5.0,
        )
        assert result.ok is True
        assert result.latency_ms >= 0
        assert result.error is None


@pytest.mark.asyncio
async def test_ping_aoai_returns_not_ok_on_5xx() -> None:
    with patch("wa_voicenote.diag.httpx.AsyncClient") as mock_async_client:
        instance = AsyncMock()
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = None
        instance.get = AsyncMock(return_value=httpx.Response(503))
        mock_async_client.return_value = instance

        result = await ping_aoai(
            endpoint="https://x.openai.azure.com/",
            deployment="d",
            api_version="v",
            auth_header={},
            timeout_seconds=5.0,
        )
        assert result.ok is False
        assert result.error == "http_503"


@pytest.mark.asyncio
async def test_ping_aoai_returns_not_ok_on_http_error() -> None:
    with patch("wa_voicenote.diag.httpx.AsyncClient") as mock_async_client:
        instance = AsyncMock()
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = None
        instance.get = AsyncMock(side_effect=httpx.ConnectError("nope"))
        mock_async_client.return_value = instance

        result = await ping_aoai(
            endpoint="https://x/",
            deployment="d",
            api_version="v",
            auth_header={},
            timeout_seconds=5.0,
        )
        assert result.ok is False
        assert result.error is not None
        assert "nope" in result.error


@pytest.mark.asyncio
async def test_ping_table_ok() -> None:
    table = AsyncMock()
    table.list_entities = lambda **_kw: _make_async_iter()
    result = await ping_table(table)
    assert result.ok is True


@pytest.mark.asyncio
async def test_ping_table_ok_when_empty() -> None:
    table = AsyncMock()

    class _Empty:
        async def __aiter__(self) -> Any:
            return
            yield  # type: ignore[unreachable]

    table.list_entities = lambda **_kw: _Empty()
    result = await ping_table(table)
    assert result.ok is True


@pytest.mark.asyncio
async def test_ping_table_failure() -> None:
    table = AsyncMock()

    def _raise(**_kw: Any) -> Any:
        raise ServiceRequestError("table boom")

    table.list_entities = _raise

    result = await ping_table(table)
    assert result.ok is False
    assert result.error is not None
    assert "table boom" in result.error


@pytest.mark.asyncio
async def test_ping_blob_ok() -> None:
    container = AsyncMock()
    container.get_container_properties = AsyncMock(return_value={})
    result = await ping_blob(container)
    assert result.ok is True


@pytest.mark.asyncio
async def test_ping_blob_failure() -> None:
    container = AsyncMock()
    container.get_container_properties = AsyncMock(
        side_effect=ServiceRequestError("blob boom"),
    )
    result = await ping_blob(container)
    assert result.ok is False


# ---------- /diag endpoint ----------


@pytest.mark.asyncio
async def test_diag_returns_503_when_token_unset(client_factory: Any) -> None:
    client = client_factory(diag_token=None)
    response = await client.get("/diag", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_diag_returns_401_without_authorization(client_factory: Any) -> None:
    client = client_factory()
    response = await client.get("/diag")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_diag_returns_401_with_wrong_token(client_factory: Any) -> None:
    client = client_factory()
    response = await client.get("/diag", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_diag_returns_401_with_wrong_scheme(client_factory: Any) -> None:
    client = client_factory()
    response = await client.get(
        "/diag",
        headers={"Authorization": f"Basic {_DIAG_TOKEN}"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_diag_returns_200_with_valid_token(client_factory: Any) -> None:
    # Patch ping_aoai so we don't hit the network.
    with patch(
        "wa_voicenote.diag.ping_aoai",
        new=AsyncMock(
            return_value=PingResult(ok=True, latency_ms=42),
        ),
    ):
        client = client_factory()
        response = await client.get(
            "/diag",
            headers={"Authorization": f"Bearer {_DIAG_TOKEN}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "0.1.0"
    assert body["env"] == "test"
    assert body["aoai"]["ok"] is True
    assert body["aoai"]["latency_ms"] == 42
    assert "storage_table" in body
    assert "storage_blob" in body
    assert body["app_insights_configured"] is False


@pytest.mark.asyncio
async def test_diag_token_value_never_in_response(client_factory: Any) -> None:
    with patch(
        "wa_voicenote.diag.ping_aoai",
        new=AsyncMock(
            return_value=PingResult(ok=True, latency_ms=1),
        ),
    ):
        client = client_factory()
        response = await client.get(
            "/diag",
            headers={"Authorization": f"Bearer {_DIAG_TOKEN}"},
        )
    assert _DIAG_TOKEN not in response.text


@pytest.mark.asyncio
async def test_build_diag_uses_mi_when_no_api_key() -> None:
    # Drop api key so the MI branch fires; mock credential.
    with patch(
        "wa_voicenote.diag.ping_aoai",
        new=AsyncMock(
            return_value=PingResult(ok=True, latency_ms=1),
        ),
    ):
        app = _app_with_state()
        # Remove api key and add credential
        app.state.settings = SimpleNamespace(
            **{**app.state.settings.__dict__, "azure_openai_api_key": None},
        )
        cred_mock = AsyncMock()
        cred_mock.get_token = AsyncMock(
            return_value=SimpleNamespace(token="fake-bearer-token"),  # noqa: S106
        )
        app.state.credential = cred_mock
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/diag",
                headers={"Authorization": f"Bearer {_DIAG_TOKEN}"},
            )
        assert response.status_code == 200
        cred_mock.get_token.assert_awaited_once()
