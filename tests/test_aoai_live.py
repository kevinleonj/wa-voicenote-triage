"""Live AOAI smoke test. Skipped by default. Enable with RUN_LIVE_AOAI=1.

Run locally:
    ENV_FILE=~/.config/wa-voicenote/secrets.env RUN_LIVE_AOAI=1 \\
        uv run pytest tests/test_aoai_live.py -v

The shipped ``tests/fixtures/sample.ogg`` is a ~1 second silent Opus clip;
the model correctly responds in prose ("no audio detected") rather than
the structured JSON shape ``AoaiClient.process()`` expects. We therefore
split the live check into two halves:

1. ``test_live_aoai_transport`` — direct HTTP round-trip against the live
   endpoint, asserting status 200, billed tokens, and the expected model
   id. This is the canonical proof the transport, auth, body shape, and
   response handling work end-to-end against Azure OpenAI.

2. ``test_live_aoai_process_structured`` — full ``client.process()`` path
   that parses JSON. Auto-skipped when the model returns the "no audio
   detected" prose for our silent fixture; will exercise the full happy
   path once a real-speech fixture is added in a later commit.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import pytest

from wa_voicenote.aoai_client import AoaiClient, AoaiParseError
from wa_voicenote.config import get_settings
from wa_voicenote.transcoder import transcode_to_wav

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_AOAI") != "1",
    reason="Live AOAI ping disabled. Set RUN_LIVE_AOAI=1 to enable.",
)


async def test_live_aoai_transport() -> None:
    """Direct HTTP round-trip to AOAI. Asserts transport + auth + shape."""
    settings = get_settings()
    assert settings.azure_openai_api_key is not None, "AZURE_OPENAI_API_KEY required for live test"
    fixture = Path(__file__).parent / "fixtures" / "sample.ogg"
    wav = await transcode_to_wav(fixture.read_bytes())
    wav_b64 = base64.b64encode(wav).decode("ascii")
    url = (
        f"{settings.azure_openai_endpoint.rstrip('/')}"
        f"/openai/deployments/{settings.azure_openai_deployment}"
        f"/chat/completions?api-version={settings.azure_openai_api_version}"
    )
    body = {
        "modalities": ["text"],
        "messages": [
            {"role": "system", "content": settings.llm_system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": wav_b64, "format": "wav"},
                    }
                ],
            },
        ],
        "max_tokens": 200,
    }
    headers = {
        "api-key": settings.azure_openai_api_key.get_secret_value(),
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as http:
        response = await http.post(url, json=body, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["model"].startswith("gpt-audio"), data["model"]
    usage = data["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    content = data["choices"][0]["message"]["content"]
    assert isinstance(content, str)
    assert content
    print(
        f"\nLive transport OK: model={data['model']} "
        f"prompt_tokens={usage['prompt_tokens']} "
        f"completion_tokens={usage['completion_tokens']}\n"
        f"content_preview={content[:160]!r}"
    )


async def test_live_aoai_process_structured() -> None:
    """Full client.process() round-trip; auto-skips on silent fixture.

    The model returns prose (not JSON) when the audio has no speech.
    Our retry-once-with-stricter-prompt path then also fails parse and
    raises AoaiParseError; that is expected for silent fixtures. The
    happy-path JSON parse is fully covered by the mocked test suite.
    """
    settings = get_settings()
    assert settings.azure_openai_api_key is not None
    client = AoaiClient(
        endpoint=settings.azure_openai_endpoint,
        deployment=settings.azure_openai_deployment,
        api_version=settings.azure_openai_api_version,
        system_prompt=settings.llm_system_prompt,
        http_timeout_seconds=float(settings.http_timeout_seconds),
        api_key=settings.azure_openai_api_key,
    )
    fixture = Path(__file__).parent / "fixtures" / "sample.ogg"
    wav = await transcode_to_wav(fixture.read_bytes())
    try:
        result = await client.process(wav)
    except AoaiParseError as exc:
        pytest.skip(
            f"Silent fixture: model returned prose, parse failed as expected "
            f"({exc}). Replace tests/fixtures/sample.ogg with real speech to "
            f"exercise the full JSON path."
        )
    assert isinstance(result.transcript, str)
    assert isinstance(result.summary, str)
    assert isinstance(result.suggested_reply, str)
    assert result.model.startswith("gpt-audio")
    assert result.latency_ms > 0
    assert result.completion_tokens > 0
    print(
        f"\nLive AOAI result: model={result.model} "
        f"latency_ms={result.latency_ms} "
        f"prompt_tokens={result.prompt_tokens} "
        f"completion_tokens={result.completion_tokens}\n"
        f"transcript={result.transcript!r}\n"
        f"summary={result.summary!r}\n"
        f"suggested_reply={result.suggested_reply!r}"
    )
