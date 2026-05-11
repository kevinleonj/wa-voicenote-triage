"""Tests for :mod:`wa_voicenote.transcoder`.

These tests invoke the real ``ffmpeg`` binary as a subprocess. The module
is skipped entirely if ``ffmpeg`` is not on PATH (CI installs it via apt;
local dev typically via brew). See PLAN sec 3 for the test list.

WAV header offsets used here (per RIFF/WAVE spec):

* 0-3   RIFF magic
* 8-11  WAVE magic
* 20-21 audio format (1 = PCM)
* 22-23 num channels (LE u16)
* 24-27 sample rate (LE u32)
* 34-35 bits per sample (LE u16)
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wa_voicenote.transcoder import TranscodeError, transcode_to_wav

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample.ogg"


# WAV header field offsets.
OFFSET_NUM_CHANNELS = 22
OFFSET_SAMPLE_RATE = 24
OFFSET_BITS_PER_SAMPLE = 34

# Expected output format.
EXPECTED_SAMPLE_RATE = 16000
EXPECTED_NUM_CHANNELS = 1
EXPECTED_BITS_PER_SAMPLE = 16


@pytest.fixture
def ogg_bytes() -> bytes:
    """Load the committed OGG/Opus fixture as raw bytes."""
    assert FIXTURE_PATH.exists(), f"missing fixture: {FIXTURE_PATH}"
    return FIXTURE_PATH.read_bytes()


async def test_ogg_to_wav_produces_nonempty_bytes(ogg_bytes: bytes) -> None:
    """Happy path: real OGG input transcodes to non-empty bytes."""
    result = await transcode_to_wav(ogg_bytes)
    assert len(result) > 0


async def test_output_starts_with_riff_wave(ogg_bytes: bytes) -> None:
    """Output must carry a valid RIFF/WAVE header signature."""
    result = await transcode_to_wav(ogg_bytes)
    assert result[0:4] == b"RIFF"
    assert result[8:12] == b"WAVE"


async def test_output_sample_rate_16khz(ogg_bytes: bytes) -> None:
    """Parsed WAV header reports 16 kHz sample rate."""
    result = await transcode_to_wav(ogg_bytes)
    (sample_rate,) = struct.unpack_from("<I", result, OFFSET_SAMPLE_RATE)
    assert sample_rate == EXPECTED_SAMPLE_RATE


async def test_output_mono(ogg_bytes: bytes) -> None:
    """Parsed WAV header reports a single channel (mono)."""
    result = await transcode_to_wav(ogg_bytes)
    (num_channels,) = struct.unpack_from("<H", result, OFFSET_NUM_CHANNELS)
    assert num_channels == EXPECTED_NUM_CHANNELS


async def test_output_pcm16(ogg_bytes: bytes) -> None:
    """Parsed WAV header reports 16 bits per sample (PCM s16)."""
    result = await transcode_to_wav(ogg_bytes)
    (bits_per_sample,) = struct.unpack_from("<H", result, OFFSET_BITS_PER_SAMPLE)
    assert bits_per_sample == EXPECTED_BITS_PER_SAMPLE


async def test_corrupt_input_raises_transcode_error() -> None:
    """Non-audio bytes cause ffmpeg to exit non-zero with stderr captured."""
    with pytest.raises(TranscodeError) as exc_info:
        await transcode_to_wav(b"this is not audio data, no really")
    # The error message should include ffmpeg's non-zero exit info
    # and ideally some captured stderr content.
    assert "ffmpeg exited" in str(exc_info.value)


async def test_empty_input_raises_transcode_error() -> None:
    """Empty input cannot be a valid container; ffmpeg should fail."""
    with pytest.raises(TranscodeError):
        await transcode_to_wav(b"")


async def test_ffmpeg_not_found_raises(ogg_bytes: bytes) -> None:
    """An absent binary path raises FileNotFoundError from the OS layer.

    asyncio.create_subprocess_exec dispatches to the POSIX subprocess
    transport which raises FileNotFoundError when execvp cannot find the
    program. We deliberately do NOT wrap this in TranscodeError so callers
    can distinguish "environment broken" from "bad audio data".
    """
    with pytest.raises(FileNotFoundError):
        await transcode_to_wav(ogg_bytes, ffmpeg_path="/nonexistent/ffmpeg-xyz")


async def test_timeout_kills_process(ogg_bytes: bytes) -> None:
    """A sub-millisecond timeout should fire before ffmpeg can finish.

    asyncio.wait_for raises asyncio.TimeoutError which is an alias of the
    built-in TimeoutError in Python 3.11+. We assert on the built-in.
    """
    with pytest.raises(TimeoutError):
        await transcode_to_wav(ogg_bytes, timeout_seconds=0.001)


async def test_returns_bytes_not_str(ogg_bytes: bytes) -> None:
    """Result is raw bytes, not a decoded string."""
    result = await transcode_to_wav(ogg_bytes)
    assert isinstance(result, bytes)


async def test_zero_exit_but_empty_stdout_raises(ogg_bytes: bytes) -> None:
    """Defensive branch: ffmpeg exits 0 yet writes nothing.

    This should never happen in practice but is guarded so callers always
    get a typed error rather than empty bytes.
    """
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))

    with (
        patch("wa_voicenote.transcoder.asyncio.create_subprocess_exec", return_value=fake_proc),
        pytest.raises(TranscodeError, match="empty output"),
    ):
        await transcode_to_wav(ogg_bytes)


async def test_zero_exit_but_invalid_wav_header_raises(ogg_bytes: bytes) -> None:
    """Defensive branch: ffmpeg exits 0 but stdout is not a valid WAV."""
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"NOT_A_WAV_HEADER_PAYLOAD_AT_ALL", b""))

    with (
        patch("wa_voicenote.transcoder.asyncio.create_subprocess_exec", return_value=fake_proc),
        pytest.raises(TranscodeError, match="not a valid WAV"),
    ):
        await transcode_to_wav(ogg_bytes)
