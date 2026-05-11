"""Tests for the From-number allowlist guard in handlers.py (PLAN §3, c6).

The full handler state machine lands in c12. For c6 the only public surface
is ``is_sender_allowed`` — exact-equality membership test against an iterable
of allowlist entries. These tests also verify the integration path from
``Settings`` (which strips whitespace at load time) into the guard.
"""

from __future__ import annotations

import pytest

from wa_voicenote.handlers import is_sender_allowed

# -----------------------------------------------------------------------------
# Direct unit tests against is_sender_allowed
# -----------------------------------------------------------------------------


def test_allowlisted_sender_passes() -> None:
    assert is_sender_allowed("whatsapp:+34611779374", ["whatsapp:+34611779374"]) is True


def test_non_allowlisted_sender_drops() -> None:
    assert is_sender_allowed("whatsapp:+1234", ["whatsapp:+34611779374"]) is False


def test_allowlist_exact_match_no_prefix() -> None:
    """Sender that is a strict prefix of an entry must NOT match."""
    assert is_sender_allowed("whatsapp:+34611", ["whatsapp:+346117793741"]) is False


def test_allowlist_exact_match_no_substring() -> None:
    """Entry that is a strict substring of the sender must NOT match."""
    assert is_sender_allowed("whatsapp:+34611779374", ["+34611779374"]) is False


def test_allowlist_empty_returns_false() -> None:
    assert is_sender_allowed("whatsapp:+34611779374", []) is False


def test_allowlist_multiple_entries() -> None:
    allowlist = ["whatsapp:+1111", "whatsapp:+2222", "whatsapp:+3333"]
    assert is_sender_allowed("whatsapp:+2222", allowlist) is True


# -----------------------------------------------------------------------------
# Integration with Settings (whitespace trimming happens at load time)
# -----------------------------------------------------------------------------


def test_allowlist_settings_integration(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    settings = Settings()
    sender = settings.twilio_allowlist[0]
    assert is_sender_allowed(sender, settings.twilio_allowlist) is True


def test_allowlist_whitespace_trimmed_by_settings(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("TWILIO_ALLOWLIST", " whatsapp:+1111 , whatsapp:+2222 ")
    settings = Settings()
    assert is_sender_allowed("whatsapp:+1111", settings.twilio_allowlist) is True
    assert is_sender_allowed("whatsapp:+2222", settings.twilio_allowlist) is True
    # The whitespace-padded form must NOT match (Settings strips entries).
    assert is_sender_allowed(" whatsapp:+1111 ", settings.twilio_allowlist) is False
