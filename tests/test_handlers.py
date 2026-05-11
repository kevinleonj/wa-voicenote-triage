"""Tests for the full WebhookHandler state machine (PLAN §3, c12).

The c6 allowlist guard (``is_sender_allowed``) is exercised in
``tests/test_allowlist.py``. This module covers every transition of the
``idle`` <-> ``awaiting_context`` state machine, the passive
``CONTEXT_TIMEOUT_SECONDS`` drop, idempotency, and the error-recovery path
for AOAI failures.

All Azure / Twilio / network dependencies are mocked via ``AsyncMock``.
A small factory ``_build_handler`` keeps the setup boilerplate localized.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from wa_voicenote.aoai_client import AoaiError, AoaiResult
from wa_voicenote.blob_repo import BlobRef
from wa_voicenote.handlers import InboundMessage, WebhookHandler
from wa_voicenote.state_repo import StateRecord
from wa_voicenote.twilio_client import TwilioMessage

# -----------------------------------------------------------------------------
# Fixtures and helpers
# -----------------------------------------------------------------------------

_ALLOWED_FROM = "whatsapp:+34611779374"
_DENIED_FROM = "whatsapp:+999000"
_CONTAINER = "audio-staging"
_PHONE_HASH = "abcdef0123456789"
_BLOB_NAME = f"{_PHONE_HASH}/2026-05-10T12:00:00+00:00.wav"
_OLD_BLOB_NAME = f"{_PHONE_HASH}/2026-05-10T11:00:00+00:00.wav"
_BLOB_URL = f"https://stwavoicenote.blob.core.windows.net/{_CONTAINER}/{_BLOB_NAME}"
_OLD_BLOB_URL = f"https://stwavoicenote.blob.core.windows.net/{_CONTAINER}/{_OLD_BLOB_NAME}"
_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


def _make_settings(
    *,
    timeout_s: int = 120,
    container: str = _CONTAINER,
) -> Any:
    """Return a duck-typed Settings-like object with the fields the handler reads."""

    class _StubSettings:
        twilio_allowlist: list[str] = [_ALLOWED_FROM]  # noqa: RUF012 - test stub, not shared
        azure_storage_container = container
        context_timeout_seconds = timeout_s
        whatsapp_max_chars_per_message = 1500
        msg_ack_received = "ACK_RECEIVED_TEMPLATE_VALUE"
        msg_replaced_audio = "REPLACED_AUDIO_TEMPLATE_VALUE"
        msg_idle_text_hint = "IDLE_HINT_TEMPLATE_VALUE"
        msg_llm_error = "LLM_ERROR_TEMPLATE_VALUE"
        label_transcript = "T({language}):"
        label_summary = "S:"
        label_suggested_reply = "R:"

    return _StubSettings()


def _make_record(
    state: str = "idle",
    blob_url: str | None = None,
    since: datetime | None = None,
) -> StateRecord:
    return StateRecord(
        state=state,
        blob_url=blob_url,
        awaiting_context_since=since,
        sid_ring=(),
    )


def _make_aoai_result() -> AoaiResult:
    return AoaiResult(
        transcript="hola mundo",
        summary="greeting",
        suggested_reply="hi back",
        model="gpt-audio-1.5",
        latency_ms=42,
        prompt_tokens=10,
        completion_tokens=5,
    )


def _make_twilio_message() -> TwilioMessage:
    return TwilioMessage(sid="SM" + "0" * 32, status="queued", to=_ALLOWED_FROM, from_="w:+1")


def _build_handler(
    *,
    settings: Any = None,
    state_record: StateRecord | None = None,
    duplicate_sid: bool = False,
    aoai_result: AoaiResult | Exception | None = None,
    download_bytes: bytes = b"WAVDATA",
    clock_value: datetime | None = None,
) -> tuple[WebhookHandler, dict[str, Any]]:
    """Build a WebhookHandler with AsyncMock dependencies. Returns (handler, mocks)."""
    settings_obj = settings or _make_settings()

    state_repo = AsyncMock()
    state_repo.check_and_record_sid = AsyncMock(return_value=duplicate_sid)
    state_repo.get_state = AsyncMock(return_value=state_record or _make_record())
    state_repo.set_state = AsyncMock(return_value=None)

    blob_repo = AsyncMock()
    blob_repo.upload_audio = AsyncMock(
        return_value=BlobRef(blob_url=_BLOB_URL, blob_name=_BLOB_NAME)
    )
    blob_repo.download_audio = AsyncMock(return_value=download_bytes)
    blob_repo.delete_audio = AsyncMock(return_value=None)

    aoai_client = AsyncMock()
    if isinstance(aoai_result, Exception):
        aoai_client.process = AsyncMock(side_effect=aoai_result)
    else:
        aoai_client.process = AsyncMock(return_value=aoai_result or _make_aoai_result())

    twilio_client = AsyncMock()
    twilio_client.send_text = AsyncMock(return_value=_make_twilio_message())

    fetch_mock = AsyncMock(return_value=b"OGGDATA")
    transcode_mock = AsyncMock(return_value=b"WAVDATA")

    media_fetcher: Callable[[str], Awaitable[bytes]] = fetch_mock
    transcoder: Callable[[bytes], Awaitable[bytes]] = transcode_mock

    handler = WebhookHandler(
        settings=settings_obj,
        state_repo=state_repo,
        blob_repo=blob_repo,
        aoai_client=aoai_client,
        twilio_client=twilio_client,
        media_fetcher=media_fetcher,
        transcoder=transcoder,
        clock=lambda: clock_value or _NOW,
    )
    return handler, {
        "settings": settings_obj,
        "state_repo": state_repo,
        "blob_repo": blob_repo,
        "aoai": aoai_client,
        "twilio": twilio_client,
        "fetch": fetch_mock,
        "transcode": transcode_mock,
    }


def _audio_msg(
    sid: str = "SM" + "1" * 32,
    sender: str = _ALLOWED_FROM,
    media_url: str = "https://api.twilio.com/Media/abc",
    mime: str = "audio/ogg",
    num_media: int = 1,
    body: str = "",
) -> InboundMessage:
    return InboundMessage(
        message_sid=sid,
        from_=sender,
        body=body,
        num_media=num_media,
        media_url_0=media_url,
        media_content_type_0=mime,
    )


def _text_msg(body: str, sender: str = _ALLOWED_FROM) -> InboundMessage:
    return InboundMessage(
        message_sid="SM" + "2" * 32,
        from_=sender,
        body=body,
        num_media=0,
        media_url_0=None,
        media_content_type_0=None,
    )


# -----------------------------------------------------------------------------
# idle + audio -> awaiting_context
# -----------------------------------------------------------------------------


async def test_idle_audio_inbound() -> None:
    handler, mocks = _build_handler(state_record=_make_record("idle"))
    inbound = _audio_msg()

    await handler.handle(inbound)

    mocks["fetch"].assert_awaited_once_with("https://api.twilio.com/Media/abc")
    mocks["transcode"].assert_awaited_once_with(b"OGGDATA")
    mocks["blob_repo"].upload_audio.assert_awaited_once_with(_ALLOWED_FROM, b"WAVDATA")
    mocks["state_repo"].set_state.assert_awaited_once_with(
        _ALLOWED_FROM,
        "awaiting_context",
        blob_url=_BLOB_URL,
        awaiting_context_since=_NOW,
    )
    mocks["twilio"].send_text.assert_awaited_once_with(
        _ALLOWED_FROM, mocks["settings"].msg_ack_received
    )
    mocks["aoai"].process.assert_not_awaited()


# -----------------------------------------------------------------------------
# idle + text -> idle hint
# -----------------------------------------------------------------------------


async def test_idle_text_only() -> None:
    handler, mocks = _build_handler(state_record=_make_record("idle"))
    inbound = _text_msg("hello")

    await handler.handle(inbound)

    mocks["twilio"].send_text.assert_awaited_once_with(
        _ALLOWED_FROM, mocks["settings"].msg_idle_text_hint
    )
    mocks["blob_repo"].upload_audio.assert_not_awaited()
    mocks["blob_repo"].download_audio.assert_not_awaited()
    mocks["aoai"].process.assert_not_awaited()
    mocks["state_repo"].set_state.assert_not_awaited()


# -----------------------------------------------------------------------------
# awaiting_context + text -> AOAI flow, three messages, reset to idle
# -----------------------------------------------------------------------------


async def test_awaiting_text_triggers_aoai_flow() -> None:
    record = _make_record("awaiting_context", blob_url=_BLOB_URL, since=_NOW - timedelta(seconds=5))
    handler, mocks = _build_handler(state_record=record)
    inbound = _text_msg("please process with this context")

    await handler.handle(inbound)

    mocks["blob_repo"].download_audio.assert_awaited_once_with(_BLOB_NAME)
    mocks["aoai"].process.assert_awaited_once_with(
        b"WAVDATA", context="please process with this context"
    )
    # Three outbound replies, in transcript -> summary -> suggested-reply order.
    assert mocks["twilio"].send_text.await_count == 3
    mocks["state_repo"].set_state.assert_awaited_once_with(
        _ALLOWED_FROM,
        "idle",
        blob_url=None,
        awaiting_context_since=None,
    )
    mocks["blob_repo"].delete_audio.assert_awaited_once_with(_BLOB_NAME)


async def test_awaiting_text_no_skips_context() -> None:
    record = _make_record("awaiting_context", blob_url=_BLOB_URL, since=_NOW - timedelta(seconds=5))
    handler, mocks = _build_handler(state_record=record)

    await handler.handle(_text_msg("no"))

    mocks["aoai"].process.assert_awaited_once_with(b"WAVDATA", context=None)


@pytest.mark.parametrize("body", ["no", "No", "NO", " no ", "  NO\t"])
async def test_awaiting_text_no_case_insensitive(body: str) -> None:
    record = _make_record("awaiting_context", blob_url=_BLOB_URL, since=_NOW - timedelta(seconds=5))
    handler, mocks = _build_handler(state_record=record)

    await handler.handle(_text_msg(body))

    mocks["aoai"].process.assert_awaited_once_with(b"WAVDATA", context=None)


async def test_three_messages_in_order() -> None:
    record = _make_record("awaiting_context", blob_url=_BLOB_URL, since=_NOW - timedelta(seconds=5))
    handler, mocks = _build_handler(state_record=record)

    await handler.handle(_text_msg("ctx"))

    calls = mocks["twilio"].send_text.await_args_list
    assert len(calls) == 3
    transcript_call, summary_call, reply_call = calls
    # Each call is ((to, body),). Inspect positional args.
    assert transcript_call.args[0] == _ALLOWED_FROM
    assert transcript_call.args[1].startswith("T(")  # label_transcript prefix
    assert "hola mundo" in transcript_call.args[1]
    assert summary_call.args[1].startswith("S:")
    assert "greeting" in summary_call.args[1]
    assert reply_call.args[1].startswith("R:")
    assert "hi back" in reply_call.args[1]


# -----------------------------------------------------------------------------
# awaiting_context + audio -> replace previous blob, stay in awaiting_context
# -----------------------------------------------------------------------------


async def test_awaiting_audio_replaces() -> None:
    record = _make_record(
        "awaiting_context",
        blob_url=_OLD_BLOB_URL,
        since=_NOW - timedelta(seconds=5),
    )
    handler, mocks = _build_handler(state_record=record)

    await handler.handle(_audio_msg())

    # Old blob deleted with the parsed-out name (the blob_name part of old URL).
    mocks["blob_repo"].delete_audio.assert_awaited_once_with(_OLD_BLOB_NAME)
    # New audio uploaded and state remains awaiting_context with a fresh ts.
    mocks["blob_repo"].upload_audio.assert_awaited_once()
    mocks["state_repo"].set_state.assert_awaited_once_with(
        _ALLOWED_FROM,
        "awaiting_context",
        blob_url=_BLOB_URL,
        awaiting_context_since=_NOW,
    )
    mocks["twilio"].send_text.assert_awaited_once_with(
        _ALLOWED_FROM, mocks["settings"].msg_replaced_audio
    )
    mocks["aoai"].process.assert_not_awaited()


# -----------------------------------------------------------------------------
# Allowlist gate
# -----------------------------------------------------------------------------


async def test_non_allowlisted_drops() -> None:
    handler, mocks = _build_handler()

    await handler.handle(_audio_msg(sender=_DENIED_FROM))

    mocks["state_repo"].check_and_record_sid.assert_not_awaited()
    mocks["state_repo"].get_state.assert_not_awaited()
    mocks["state_repo"].set_state.assert_not_awaited()
    mocks["blob_repo"].upload_audio.assert_not_awaited()
    mocks["aoai"].process.assert_not_awaited()
    mocks["twilio"].send_text.assert_not_awaited()


# -----------------------------------------------------------------------------
# Idempotency
# -----------------------------------------------------------------------------


async def test_idempotency_drops_duplicate_sid() -> None:
    handler, mocks = _build_handler(duplicate_sid=True)

    await handler.handle(_audio_msg())

    mocks["state_repo"].check_and_record_sid.assert_awaited_once()
    mocks["state_repo"].get_state.assert_not_awaited()
    mocks["state_repo"].set_state.assert_not_awaited()
    mocks["blob_repo"].upload_audio.assert_not_awaited()
    mocks["aoai"].process.assert_not_awaited()
    mocks["twilio"].send_text.assert_not_awaited()


# -----------------------------------------------------------------------------
# Passive timeout
# -----------------------------------------------------------------------------


async def test_passive_timeout_text_drops_old() -> None:
    record = _make_record(
        "awaiting_context",
        blob_url=_OLD_BLOB_URL,
        since=_NOW - timedelta(seconds=130),
    )
    handler, mocks = _build_handler(state_record=record)

    await handler.handle(_text_msg("anything"))

    # Stale record treated as idle; idle+text -> hint reply only.
    mocks["twilio"].send_text.assert_awaited_once_with(
        _ALLOWED_FROM, mocks["settings"].msg_idle_text_hint
    )
    mocks["aoai"].process.assert_not_awaited()
    mocks["blob_repo"].download_audio.assert_not_awaited()
    mocks["state_repo"].set_state.assert_not_awaited()


async def test_passive_timeout_audio_starts_fresh() -> None:
    record = _make_record(
        "awaiting_context",
        blob_url=_OLD_BLOB_URL,
        since=_NOW - timedelta(seconds=130),
    )
    handler, mocks = _build_handler(state_record=record)

    await handler.handle(_audio_msg())

    # Treated as fresh idle+audio: new upload, ack reply, state -> awaiting_context.
    mocks["blob_repo"].upload_audio.assert_awaited_once_with(_ALLOWED_FROM, b"WAVDATA")
    mocks["state_repo"].set_state.assert_awaited_once_with(
        _ALLOWED_FROM,
        "awaiting_context",
        blob_url=_BLOB_URL,
        awaiting_context_since=_NOW,
    )
    mocks["twilio"].send_text.assert_awaited_once_with(
        _ALLOWED_FROM, mocks["settings"].msg_ack_received
    )
    # Important: when treated as idle the old blob is NOT explicitly deleted
    # (the 24h lifecycle rule will sweep it). Confirm we did not call delete.
    mocks["blob_repo"].delete_audio.assert_not_awaited()


async def test_within_timeout_processes_normally() -> None:
    record = _make_record(
        "awaiting_context",
        blob_url=_BLOB_URL,
        since=_NOW - timedelta(seconds=60),
    )
    handler, mocks = _build_handler(state_record=record)

    await handler.handle(_text_msg("ctx"))

    mocks["aoai"].process.assert_awaited_once_with(b"WAVDATA", context="ctx")
    assert mocks["twilio"].send_text.await_count == 3


# -----------------------------------------------------------------------------
# AOAI error path
# -----------------------------------------------------------------------------


async def test_aoai_error_sends_error_msg_and_resets() -> None:
    record = _make_record("awaiting_context", blob_url=_BLOB_URL, since=_NOW - timedelta(seconds=5))
    err = AoaiError("boom")
    handler, mocks = _build_handler(state_record=record, aoai_result=err)

    with pytest.raises(AoaiError):
        await handler.handle(_text_msg("ctx"))

    # Error message sent, state reset, blob deleted, and the exception bubbles.
    mocks["twilio"].send_text.assert_awaited_once_with(
        _ALLOWED_FROM, mocks["settings"].msg_llm_error
    )
    mocks["state_repo"].set_state.assert_awaited_once_with(
        _ALLOWED_FROM,
        "idle",
        blob_url=None,
        awaiting_context_since=None,
    )
    mocks["blob_repo"].delete_audio.assert_awaited_once_with(_BLOB_NAME)


# -----------------------------------------------------------------------------
# Edge: media block present but mime-type is not audio/* -> treated as text
# -----------------------------------------------------------------------------


async def test_audio_with_non_audio_mimetype_treated_as_text() -> None:
    handler, mocks = _build_handler(state_record=_make_record("idle"))
    inbound = _audio_msg(mime="image/jpeg")

    await handler.handle(inbound)

    # Behaves like idle+text: hint reply only, no fetch/transcode/upload.
    mocks["twilio"].send_text.assert_awaited_once_with(
        _ALLOWED_FROM, mocks["settings"].msg_idle_text_hint
    )
    mocks["fetch"].assert_not_awaited()
    mocks["transcode"].assert_not_awaited()
    mocks["blob_repo"].upload_audio.assert_not_awaited()


# -----------------------------------------------------------------------------
# Tests for _chunk_message (c14 hotfix: Twilio 21617 character-limit error)
# -----------------------------------------------------------------------------


from wa_voicenote.handlers import _chunk_message  # noqa: E402


def test_chunk_short_message_is_single_chunk() -> None:
    assert _chunk_message("hi", 1500) == ["hi"]


def test_chunk_at_paragraph_boundary() -> None:
    para = "x" * 60
    body = f"{para}\n\n{para}"
    chunks = _chunk_message(body, max_chars=100)
    assert len(chunks) == 2
    assert chunks[0] == para
    assert chunks[1] == para


def test_chunk_at_sentence_boundary() -> None:
    sent = "x" * 40
    body = f"{sent}. {sent}. {sent}."
    chunks = _chunk_message(body, max_chars=80)
    assert all(len(c) <= 80 for c in chunks)
    assert "x" * (len(sent) * 3) in "".join(chunks).replace(" ", "").replace(".", "")


def test_chunk_hard_cut_when_no_whitespace() -> None:
    # No spaces or punctuation; chunker falls back to hard cut.
    body = "x" * 5000
    chunks = _chunk_message(body, max_chars=1500)
    # Each chunk respects the budget (max_chars - marker reserve).
    assert all(len(c) <= 1500 for c in chunks)
    assert "".join(chunks) == body


def test_chunk_respects_marker_reserve() -> None:
    # A 3000-char body with max 1500 should produce chunks small enough that
    # adding a "(i/N) " marker keeps each send under 1500.
    body = "word " * 600  # 3000 chars
    chunks = _chunk_message(body, max_chars=1500)
    assert all(len(f"(99/99) {c}") <= 1500 for c in chunks)


def test_chunk_preserves_total_content_when_split_on_space() -> None:
    body = " ".join([f"word{i}" for i in range(500)])
    chunks = _chunk_message(body, max_chars=200)
    joined = " ".join(chunks)
    # Some whitespace at chunk boundaries may collapse; verify by words.
    assert joined.split() == body.split()


def test_chunk_real_3min_transcript_length() -> None:
    # Roughly what a 3-minute voice note's transcript produced live.
    body = ("This is a sentence about the meeting. " * 200).strip()  # ~7600 chars
    chunks = _chunk_message(body, max_chars=1500)
    # All chunks fit within the per-message budget plus marker.
    assert all(len(f"({i}/99) {c}") <= 1600 for i, c in enumerate(chunks, start=1))
    # Content round-trip by word.
    assert " ".join(chunks).split() == body.split()
