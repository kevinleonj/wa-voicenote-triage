"""FastAPI application entrypoint.

Exposes:
- GET /health — liveness probe
- POST /webhook/whatsapp — Twilio inbound webhook

The app constructs all collaborators (StateRepo, BlobRepo, AoaiClient,
TwilioClient) once at startup via the lifespan context manager and
stores them on app.state. Each request gets a handler bound to those
shared clients.

Auth modes:
- Local dev: AZURE_OPENAI_API_KEY set in env -> AoaiClient uses api-key
- Prod: AZURE_OPENAI_API_KEY unset -> AoaiClient uses Managed Identity
  via azure.identity.aio.DefaultAzureCredential, scope
  https://cognitiveservices.azure.com/.default
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Final

import httpx
from azure.data.tables.aio import TableClient
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient
from fastapi import Depends, FastAPI, Header, Request, Response

from wa_voicenote.aoai_client import AoaiClient
from wa_voicenote.blob_repo import BlobRepo
from wa_voicenote.config import get_settings
from wa_voicenote.handlers import InboundMessage, WebhookHandler
from wa_voicenote.observability import configure_observability, get_logger
from wa_voicenote.state_repo import StateRepo
from wa_voicenote.transcoder import transcode_to_wav
from wa_voicenote.twilio_client import TwilioClient
from wa_voicenote.twilio_signing import require_valid_twilio_signature

_EMPTY_TWIML: Final[str] = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
_AOAI_SCOPE: Final[str] = "https://cognitiveservices.azure.com/.default"
_TABLE_ENDPOINT_TPL: Final[str] = "https://{account}.table.core.windows.net"
_BLOB_ENDPOINT_TPL: Final[str] = "https://{account}.blob.core.windows.net"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build shared clients at startup; close them at shutdown.

    All long-lived collaborators are stored on ``app.state`` so request
    handlers can fetch them via ``request.app.state``. Cleanup happens
    in a ``finally`` block so a partial-startup failure still releases
    whatever sessions were opened before the failure.
    """
    settings = get_settings()
    configure_observability(settings, app)
    log = get_logger("startup")

    # Managed Identity in prod (no API key); local dev uses the key for AOAI.
    # Storage (Tables + Blob) always uses Managed Identity because there is no
    # alternative envvar plumbing in this app — local dev relies on
    # ``az login`` credentials via the DefaultAzureCredential chain.
    credential: DefaultAzureCredential = DefaultAzureCredential()

    table_endpoint = _TABLE_ENDPOINT_TPL.format(account=settings.azure_storage_account)
    blob_endpoint = _BLOB_ENDPOINT_TPL.format(account=settings.azure_storage_account)

    table_client = TableClient(
        endpoint=table_endpoint,
        table_name=settings.azure_storage_table,
        credential=credential,
    )
    blob_service = BlobServiceClient(account_url=blob_endpoint, credential=credential)
    container_client = blob_service.get_container_client(settings.azure_storage_container)

    aoai = _build_aoai_client(settings, credential)

    twilio_client = TwilioClient(
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        from_number=settings.twilio_from,
        http_timeout_seconds=float(settings.http_timeout_seconds),
    )

    state_repo = StateRepo(table_client, ring_size=settings.idempotency_ring_size)
    blob_repo = BlobRepo(container_client)

    # Shared HTTP client for Twilio media downloads. Twilio media URLs are
    # authenticated with Account SID + Auth Token via HTTP Basic.
    media_http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(float(settings.http_timeout_seconds)),
        auth=(settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value()),
    )

    async def media_fetcher(url: str) -> bytes:
        response = await media_http_client.get(url, follow_redirects=True)
        response.raise_for_status()
        return response.content

    handler = WebhookHandler(
        settings=settings,
        state_repo=state_repo,
        blob_repo=blob_repo,
        aoai_client=aoai,
        twilio_client=twilio_client,
        media_fetcher=media_fetcher,
        transcoder=transcode_to_wav,
    )

    app.state.handler = handler
    app.state.settings = settings
    app.state.media_http_client = media_http_client
    app.state.table_client = table_client
    app.state.blob_service = blob_service
    app.state.credential = credential

    log.info(
        "startup_complete",
        env=settings.env_name,
        deployment=settings.azure_openai_deployment,
    )

    try:
        yield
    finally:
        await media_http_client.aclose()
        await table_client.close()
        await blob_service.close()
        await credential.close()
        log.info("shutdown_complete")


def _build_aoai_client(
    settings: object,
    credential: DefaultAzureCredential,
) -> AoaiClient:
    """Construct an AoaiClient using API key (local) or token provider (prod).

    Separated from ``lifespan`` so the auth-mode branch is independently
    testable and so the lifespan body stays readable.
    """
    # Local import to avoid circular settings typing at module load.
    from wa_voicenote.config import Settings

    assert isinstance(settings, Settings)  # noqa: S101 - composition-root invariant

    if settings.azure_openai_api_key is not None:
        return AoaiClient(
            endpoint=settings.azure_openai_endpoint,
            deployment=settings.azure_openai_deployment,
            api_version=settings.azure_openai_api_version,
            system_prompt=settings.llm_system_prompt,
            http_timeout_seconds=float(settings.http_timeout_seconds),
            api_key=settings.azure_openai_api_key,
            max_tokens=settings.aoai_max_tokens,
        )

    async def token_provider() -> str:
        token = await credential.get_token(_AOAI_SCOPE)
        return token.token

    return AoaiClient(
        endpoint=settings.azure_openai_endpoint,
        deployment=settings.azure_openai_deployment,
        api_version=settings.azure_openai_api_version,
        system_prompt=settings.llm_system_prompt,
        http_timeout_seconds=float(settings.http_timeout_seconds),
        token_provider=token_provider,
        max_tokens=settings.aoai_max_tokens,
    )


def create_app(lifespan_override: object | None = None) -> FastAPI:
    """Build the FastAPI app with routes registered.

    Separating construction from the module-level ``app`` singleton lets tests
    build a fresh app with no lifespan (or a stubbed one) so they can pin
    ``app.state.handler`` directly without spinning up Azure clients.
    """
    fastapi_kwargs: dict[str, object] = {
        "title": "wa-voicenote-triage",
        "version": "0.1.0",
    }
    if lifespan_override is not None:
        fastapi_kwargs["lifespan"] = lifespan_override
    new_app = FastAPI(**fastapi_kwargs)  # type: ignore[arg-type]
    _register_routes(new_app)
    return new_app


def _register_routes(target: FastAPI) -> None:
    """Attach /health and /webhook/whatsapp to ``target``."""

    @target.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @target.post("/webhook/whatsapp")
    async def webhook_whatsapp(
        request: Request,
        _signature_ok: None = Depends(require_valid_twilio_signature),
    ) -> Response:
        form = await request.form()

        def _get(key: str, default: str = "") -> str:
            value = form.get(key, default)
            return str(value) if value is not None else default

        raw_num_media = _get("NumMedia", "0") or "0"
        try:
            num_media = int(raw_num_media)
        except ValueError:
            num_media = 0
        media_url_0 = _get("MediaUrl0") or None
        media_content_type_0 = _get("MediaContentType0") or None

        inbound = InboundMessage(
            message_sid=_get("MessageSid"),
            from_=_get("From"),
            body=_get("Body"),
            num_media=num_media,
            media_url_0=media_url_0,
            media_content_type_0=media_content_type_0,
        )

        handler: WebhookHandler = request.app.state.handler
        log = get_logger("webhook").bind(
            message_sid=inbound.message_sid,
            from_=inbound.from_,
        )
        try:
            await handler.handle(inbound)
        except Exception:
            # Don't fail the webhook — Twilio retries on 5xx and the
            # idempotency ring in StateRepo absorbs repeats. We still want
            # App Insights to see the exception trace via structlog.
            log.exception("handler_error")

        return Response(content=_EMPTY_TWIML, media_type="application/xml")

    @target.get("/diag")
    async def diag(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        # Local import to avoid loading diag at startup when the feature is off.
        from wa_voicenote.diag import build_diag_response

        return await build_diag_response(request, authorization)


app = FastAPI(
    title="wa-voicenote-triage",
    version="0.1.0",
    lifespan=lifespan,
)
_register_routes(app)
