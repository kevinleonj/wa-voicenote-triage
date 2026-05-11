"""Diagnostic endpoint: live health pings.

GET /diag returns JSON with timing for AOAI reachability, Storage table
reachability, and Storage blob container reachability. Protected by a
static bearer token loaded from DIAG_TOKEN. If DIAG_TOKEN is unset, the
endpoint returns 503 (feature disabled — fail-closed).

Designed for Kevin to hit from his phone when something looks weird,
without opening the Azure Portal.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass

import httpx
from azure.core.exceptions import AzureError
from azure.data.tables.aio import TableClient
from azure.storage.blob.aio import ContainerClient
from fastapi import HTTPException, Request, status

_BEARER_PREFIX = "Bearer "
_HTTP_SERVER_ERROR_FLOOR = 500
_ERROR_TRUNCATE = 100


@dataclass(frozen=True)
class PingResult:
    ok: bool
    latency_ms: int
    error: str | None = None


def _verify_bearer(authorization: str | None, expected_token: str | None) -> None:
    """Constant-time compare of bearer token.

    Raises HTTPException(503) when the feature is disabled (token unset).
    Raises HTTPException(401) when the header is missing, the scheme is
    wrong, or the token does not match.
    """
    if expected_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="diag disabled",
        )
    if authorization is None or not authorization.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid scheme",
        )
    presented = authorization[len(_BEARER_PREFIX) :]
    if not hmac.compare_digest(presented, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )


async def ping_aoai(
    endpoint: str,
    deployment: str,
    api_version: str,
    auth_header: dict[str, str],
    timeout_seconds: float,
) -> PingResult:
    """Ping AOAI deployment metadata endpoint. Does not consume tokens."""
    url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}?api-version={api_version}"
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
            response = await client.get(url, headers=auth_header)
        latency_ms = int((time.perf_counter() - start) * 1000)
        ok = response.status_code < _HTTP_SERVER_ERROR_FLOOR
        error = None if ok else f"http_{response.status_code}"
        return PingResult(ok=ok, latency_ms=latency_ms, error=error)
    except httpx.HTTPError as exc:
        return PingResult(
            ok=False,
            latency_ms=int((time.perf_counter() - start) * 1000),
            error=str(exc)[:_ERROR_TRUNCATE],
        )


async def ping_table(table_client: TableClient) -> PingResult:
    """Ping Azure Tables by listing 1 entity (data-plane, no management perms).

    Earlier we used ``get_table_access_policy()`` but that requires
    management-plane access (Owner-level), which the Container App's
    Managed Identity intentionally does not have. ``list_entities(top=1)``
    is a pure data-plane op covered by the ``Storage Table Data Contributor``
    role we already grant.
    """
    start = time.perf_counter()
    try:
        iterator = table_client.list_entities(results_per_page=1)
        # Consume at most one entity. An empty table still returns ok.
        async for _ in iterator:
            break
        return PingResult(
            ok=True,
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
    except AzureError as exc:
        return PingResult(
            ok=False,
            latency_ms=int((time.perf_counter() - start) * 1000),
            error=str(exc)[:_ERROR_TRUNCATE],
        )


async def ping_blob(container_client: ContainerClient) -> PingResult:
    """Ping a blob container by reading its properties."""
    start = time.perf_counter()
    try:
        await container_client.get_container_properties()
        return PingResult(
            ok=True,
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
    except AzureError as exc:
        return PingResult(
            ok=False,
            latency_ms=int((time.perf_counter() - start) * 1000),
            error=str(exc)[:_ERROR_TRUNCATE],
        )


async def build_diag_response(
    request: Request,
    authorization: str | None,
) -> dict[str, object]:
    """Verify auth, run pings, return JSON-ready dict."""
    settings = request.app.state.settings
    expected = settings.diag_token.get_secret_value() if settings.diag_token is not None else None
    _verify_bearer(authorization, expected)

    aoai_auth: dict[str, str] = {}
    if settings.azure_openai_api_key is not None:
        aoai_auth["api-key"] = settings.azure_openai_api_key.get_secret_value()
    else:
        credential = getattr(request.app.state, "credential", None)
        if credential is not None:
            token = await credential.get_token("https://cognitiveservices.azure.com/.default")
            aoai_auth["Authorization"] = f"Bearer {token.token}"

    aoai = await ping_aoai(
        endpoint=settings.azure_openai_endpoint,
        deployment=settings.azure_openai_deployment,
        api_version=settings.azure_openai_api_version,
        auth_header=aoai_auth,
        timeout_seconds=float(settings.http_timeout_seconds),
    )
    table = await ping_table(request.app.state.table_client)
    blob_container = request.app.state.blob_service.get_container_client(
        settings.azure_storage_container,
    )
    blob = await ping_blob(blob_container)

    return {
        "version": "0.1.0",
        "env": settings.env_name,
        "aoai": {
            "ok": aoai.ok,
            "latency_ms": aoai.latency_ms,
            "error": aoai.error,
        },
        "storage_table": {
            "ok": table.ok,
            "latency_ms": table.latency_ms,
            "error": table.error,
        },
        "storage_blob": {
            "ok": blob.ok,
            "latency_ms": blob.latency_ms,
            "error": blob.error,
        },
        "app_insights_configured": (settings.applicationinsights_connection_string is not None),
    }
