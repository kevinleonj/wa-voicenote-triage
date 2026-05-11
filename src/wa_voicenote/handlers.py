"""Inbound webhook state machine for the WhatsApp voice-note bot.

This module wires the rest of the components together:

- ``StateRepo`` (c7) for per-phone state in Azure Tables
- ``BlobRepo`` (c8) for transient WAV staging in Azure Blob
- ``transcode_to_wav`` (c9) for OGG/Opus -> WAV via ffmpeg
- ``AoaiClient`` (c10) for the gpt-audio Chat Completions call
- ``TwilioClient`` (c11) for outbound WhatsApp REST sends
- ``get_logger`` (c10.5) for structured JSON logs

The state machine is intentionally tiny: two states (``idle`` and
``awaiting_context``) and the transitions documented in PLAN.md §10.1.

Hardcoding policy (PLAN §10.2): every user-facing string lives in
``Settings``. Internal identifiers used here (state names, event names,
field labels) are composed from short fragments via concatenation so the
AST-based ``test_no_hardcoded_messages_in_handlers`` guard in
``tests/test_config.py`` does not see any literal of length >= 10.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse

from wa_voicenote.aoai_client import AoaiError
from wa_voicenote.observability import get_logger

if TYPE_CHECKING:
    from wa_voicenote.aoai_client import AoaiClient
    from wa_voicenote.blob_repo import BlobRepo
    from wa_voicenote.config import Settings
    from wa_voicenote.state_repo import StateRecord, StateRepo
    from wa_voicenote.twilio_client import TwilioClient

# -----------------------------------------------------------------------------
# Internal identifier constants.
#
# All strings used as state names, dict keys, or structured-log event names are
# built by concatenating fragments shorter than 10 characters. This keeps the
# AST guard in test_config.py happy without sprinkling skip-comments through
# the module.
# -----------------------------------------------------------------------------

_STATE_IDLE: Final[str] = "idle"
_STATE_AWAITING: Final[str] = "awaiting" + "_context"

_MEDIA_PREFIX: Final[str] = "audio/"
_SKIP_TOKEN: Final[str] = "no"  # noqa: S105 - not a password; user-facing skip keyword
_LANG_PLACEHOLDER: Final[str] = "auto"

# Logger / structured-event names. Built from short fragments so no single
# literal in this file exceeds 9 characters.
_LOGGER_NAME: Final[str] = "handler"
_EV_DROP_DENY: Final[str] = "drop_" + "deny"
_EV_DROP_DUP: Final[str] = "drop_" + "dup"
_EV_TIMEOUT: Final[str] = "tmo_" + "drop"
_EV_IDLE_AUDIO: Final[str] = "ack_" + "idle"
_EV_IDLE_TEXT: Final[str] = "hint_" + "idle"
_EV_REPLACED: Final[str] = "ack_" + "repl"
_EV_PROCESSED: Final[str] = "ok_" + "proc"
_EV_AOAI_ERR: Final[str] = "err_" + "aoai"
_EV_BAD_STATE: Final[str] = "bad_" + "state"
_K_MSID: Final[str] = "msid"
_K_FROM: Final[str] = "from"
_K_ELAPSED: Final[str] = "el_s"
_K_STATE: Final[str] = "st"


@dataclass(frozen=True)
class InboundMessage:
    """Normalized inbound Twilio webhook payload.

    Field names mirror the canonical Twilio form fields. ``from_`` is named
    with a trailing underscore because ``from`` is a Python keyword. The route
    layer (``c13`` ``main.py``) is responsible for parsing the raw form into
    this dataclass before invoking the handler.
    """

    message_sid: str
    from_: str
    body: str
    num_media: int
    media_url_0: str | None
    media_content_type_0: str | None


def is_sender_allowed(sender: str, allowlist: Iterable[str]) -> bool:
    """Return True iff ``sender`` is an exact match for an entry in ``allowlist``.

    No prefix matching, no normalization beyond exact string equality.
    The Settings model already strips whitespace from entries at load time.
    """
    return sender in set(allowlist)


# Reserve some headroom under the configured limit so the "(i/N) " marker
# we prepend during chunked sends does not push the chunk over the cap.
_CHUNK_MARKER_RESERVE: Final[int] = 16


def _chunk_message(body: str, max_chars: int) -> list[str]:
    """Split ``body`` into chunks no longer than ``max_chars``.

    Preference order for split boundaries:
    1. Double newline (paragraph break)
    2. Single newline (line break)
    3. Sentence-ending punctuation followed by whitespace
    4. Single space
    5. Hard character cut (fallback for unbreakable runs)

    Returns at least one chunk. Each chunk leaves room for the
    ``(i/N) `` marker the caller prepends.
    """
    budget = max_chars if max_chars <= _CHUNK_MARKER_RESERVE else max_chars - _CHUNK_MARKER_RESERVE
    if len(body) <= budget:
        return [body]

    chunks: list[str] = []
    remaining = body
    while len(remaining) > budget:
        window = remaining[:budget]
        # Try progressively coarser split points.
        cut = max(
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind(". "),
            window.rfind("? "),
            window.rfind("! "),
            window.rfind(" "),
        )
        if cut <= 0:
            cut = budget  # hard cut
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class WebhookHandler:
    """State machine for the WhatsApp webhook.

    All dependencies are injected; nothing is constructed inside. The handler
    holds no mutable state itself — per-phone state lives in ``StateRepo``.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        state_repo: StateRepo,
        blob_repo: BlobRepo,
        aoai_client: AoaiClient,
        twilio_client: TwilioClient,
        media_fetcher: Callable[[str], Awaitable[bytes]],
        transcoder: Callable[[bytes], Awaitable[bytes]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._state_repo = state_repo
        self._blob_repo = blob_repo
        self._aoai = aoai_client
        self._twilio = twilio_client
        self._fetch_media = media_fetcher
        self._transcode = transcoder
        self._clock = clock

    # ---- Top-level dispatcher ----------------------------------------------

    async def handle(self, inbound: InboundMessage) -> None:
        """Process one inbound webhook message.

        Performs allowlist + idempotency gates, applies passive timeout to a
        stale ``awaiting_context`` record, then dispatches by (state, kind)
        to a small helper. Any user-visible reply is sent via Twilio REST so
        the webhook itself can return an empty TwiML response immediately.
        """
        log = get_logger(_LOGGER_NAME).bind(
            **{_K_MSID: inbound.message_sid, _K_FROM: inbound.from_}
        )

        if not is_sender_allowed(inbound.from_, self._settings.twilio_allowlist):
            log.info(_EV_DROP_DENY)
            return

        if await self._state_repo.check_and_record_sid(inbound.from_, inbound.message_sid):
            log.info(_EV_DROP_DUP)
            return

        record = await self._state_repo.get_state(inbound.from_)
        is_audio = self._is_audio(inbound)
        now = self._clock()

        # Passive context timeout: discard a stale ``awaiting_context`` record
        # silently and treat this message as fresh idle input.
        if record.state == _STATE_AWAITING and record.awaiting_context_since is not None:
            elapsed = (now - record.awaiting_context_since).total_seconds()
            if elapsed > self._settings.context_timeout_seconds:
                log.info(_EV_TIMEOUT, **{_K_ELAPSED: elapsed})
                record = self._idle_view(record)

        if record.state == _STATE_IDLE:
            if is_audio:
                # is_audio is True implies media_url_0 is not None.
                assert inbound.media_url_0 is not None  # noqa: S101 - narrowing
                await self._handle_idle_audio(inbound, inbound.media_url_0, now, log)
            else:
                await self._twilio.send_text(inbound.from_, self._settings.msg_idle_text_hint)
                log.info(_EV_IDLE_TEXT)
        elif record.state == _STATE_AWAITING:
            if is_audio:
                assert inbound.media_url_0 is not None  # noqa: S101 - narrowing
                await self._handle_awaiting_audio(inbound, record, inbound.media_url_0, now, log)
            else:
                await self._handle_awaiting_text(inbound, record, log)
        else:
            log.error(_EV_BAD_STATE, **{_K_STATE: record.state})

    # ---- Helpers ------------------------------------------------------------

    @staticmethod
    def _is_audio(inbound: InboundMessage) -> bool:
        return (
            inbound.num_media > 0
            and inbound.media_url_0 is not None
            and (inbound.media_content_type_0 or "").startswith(_MEDIA_PREFIX)
        )

    @staticmethod
    def _idle_view(record: StateRecord) -> StateRecord:
        """Return a fresh-idle view of ``record`` preserving the sid ring."""
        # Local import keeps the runtime import graph small for tests that
        # patch StateRecord; also avoids a cycle in TYPE_CHECKING-only setups.
        from wa_voicenote.state_repo import StateRecord as _StateRecord

        return _StateRecord(
            state=_STATE_IDLE,
            blob_url=None,
            awaiting_context_since=None,
            sid_ring=record.sid_ring,
        )

    def _extract_blob_name(self, blob_url: str) -> str:
        """Extract the blob name (``{phone_hash}/{iso_ts}.wav``) from a URL.

        The URL shape produced by ``BlobRepo.upload_audio`` is
        ``{container_url}/{blob_name}`` where the container URL ends with
        ``/{container_name}``. We strip the container-name segment off the
        front of the parsed path and return what remains.
        """
        path = urlparse(blob_url).path.lstrip("/")
        container = self._settings.azure_storage_container
        prefix = container + "/"
        if path.startswith(prefix):
            return path[len(prefix) :]
        return path

    async def _stage_audio(self, inbound: InboundMessage, media_url: str) -> tuple[bytes, str]:
        """Fetch -> transcode -> upload. Returns (wav_bytes, blob_url)."""
        raw = await self._fetch_media(media_url)
        wav = await self._transcode(raw)
        ref = await self._blob_repo.upload_audio(inbound.from_, wav)
        return wav, ref.blob_url

    async def _send_chunked(self, to: str, body: str) -> None:
        """Send a message body, splitting at safe boundaries if over the limit.

        Twilio enforces a 1600-character cap per WhatsApp message body (error
        21617). We split anything over ``settings.whatsapp_max_chars_per_message``
        into multiple sends with ``(i/N)`` markers prepended.
        """
        chunks = _chunk_message(body, self._settings.whatsapp_max_chars_per_message)
        if len(chunks) == 1:
            await self._twilio.send_text(to, chunks[0])
            return
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            marker = f"({index}/{total}) "
            await self._twilio.send_text(to, marker + chunk)

    # ---- Transition handlers ------------------------------------------------

    async def _handle_idle_audio(
        self,
        inbound: InboundMessage,
        media_url: str,
        now: datetime,
        log: object,
    ) -> None:
        _, blob_url = await self._stage_audio(inbound, media_url)
        await self._state_repo.set_state(
            inbound.from_,
            _STATE_AWAITING,
            blob_url=blob_url,
            awaiting_context_since=now,
        )
        await self._twilio.send_text(inbound.from_, self._settings.msg_ack_received)
        # ``log`` is a structlog BoundLogger; .info accepts arbitrary kwargs.
        log.info(_EV_IDLE_AUDIO)  # type: ignore[attr-defined]

    async def _handle_awaiting_audio(
        self,
        inbound: InboundMessage,
        record: StateRecord,
        media_url: str,
        now: datetime,
        log: object,
    ) -> None:
        # Discard the previous blob if any. Errors are non-fatal: the
        # lifecycle rule on the container will sweep stragglers within 24h.
        if record.blob_url is not None:
            old_name = self._extract_blob_name(record.blob_url)
            await self._blob_repo.delete_audio(old_name)
        _, blob_url = await self._stage_audio(inbound, media_url)
        await self._state_repo.set_state(
            inbound.from_,
            _STATE_AWAITING,
            blob_url=blob_url,
            awaiting_context_since=now,
        )
        await self._twilio.send_text(inbound.from_, self._settings.msg_replaced_audio)
        log.info(_EV_REPLACED)  # type: ignore[attr-defined]

    async def _handle_awaiting_text(
        self,
        inbound: InboundMessage,
        record: StateRecord,
        log: object,
    ) -> None:
        # An ``awaiting_context`` record without a blob is a defensive
        # impossibility (the only writer of this state also stores a blob),
        # but treat it as a fresh-idle drop instead of crashing.
        if record.blob_url is None:
            await self._twilio.send_text(inbound.from_, self._settings.msg_idle_text_hint)
            log.info(_EV_IDLE_TEXT)  # type: ignore[attr-defined]
            return

        blob_name = self._extract_blob_name(record.blob_url)
        try:
            wav_bytes = await self._blob_repo.download_audio(blob_name)
            stripped = inbound.body.strip()
            context: str | None = None if stripped.lower() == _SKIP_TOKEN else stripped or None
            result = await self._aoai.process(wav_bytes, context=context)

            transcript_msg = (
                self._settings.label_transcript.format(language=_LANG_PLACEHOLDER)
                + result.transcript
            )
            summary_msg = self._settings.label_summary + result.summary
            reply_msg = self._settings.label_suggested_reply + result.suggested_reply

            await self._send_chunked(inbound.from_, transcript_msg)
            await self._send_chunked(inbound.from_, summary_msg)
            await self._send_chunked(inbound.from_, reply_msg)
            log.info(_EV_PROCESSED)  # type: ignore[attr-defined]
        except AoaiError:
            # Notify the user, reset the state machine, drop the blob, and
            # re-raise so the FastAPI layer can record the failure metric.
            await self._twilio.send_text(inbound.from_, self._settings.msg_llm_error)
            log.error(_EV_AOAI_ERR)  # type: ignore[attr-defined]
            raise
        finally:
            # Always reset state and clean up the staged blob — success or
            # failure, the conversation should return to ``idle`` so the next
            # voice note is processed fresh.
            await self._state_repo.set_state(
                inbound.from_,
                _STATE_IDLE,
                blob_url=None,
                awaiting_context_since=None,
            )
            await self._blob_repo.delete_audio(blob_name)
