"""Live Twilio smoke test. Skipped by default. Enable with RUN_LIVE_TWILIO=1.

Run locally:
    ENV_FILE=~/.config/wa-voicenote/secrets.env RUN_LIVE_TWILIO=1 \\
        uv run pytest tests/test_twilio_live.py -v -s --no-cov

Sends ONE real WhatsApp message to the first entry in TWILIO_ALLOWLIST
(Kevin's number per the project plan). Cost ~$0.005. Gated so that
ordinary CI runs never accidentally send real messages.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TWILIO") != "1",
    reason="Live Twilio send disabled. Set RUN_LIVE_TWILIO=1 to enable.",
)


async def test_live_twilio_send() -> None:
    """Send one real WhatsApp message to the first allowlisted recipient."""
    # Import inside the test so module collection does not trigger settings
    # construction in environments without secrets configured.
    from wa_voicenote.config import get_settings
    from wa_voicenote.twilio_client import TwilioClient

    settings = get_settings()
    client = TwilioClient(
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        from_number=settings.twilio_from,
        http_timeout_seconds=float(settings.http_timeout_seconds),
    )
    # First allowlisted number is Kevin's
    to = settings.twilio_allowlist[0]
    msg = await client.send_text(to, body="c11 live smoke from wa-voicenote-triage")

    # Twilio Message SIDs are 34 chars: 2-letter prefix + 32 hex.
    # SMS/WhatsApp text messages return an SM prefix; media-bearing messages MM.
    assert msg.sid.startswith("SM") or msg.sid.startswith("MM"), msg.sid
    assert msg.status in {"queued", "sending", "sent", "accepted"}, msg.status
    print(f"\nSent SID={msg.sid} status={msg.status} to={msg.to}")
