"""Audio transcoder: any inbound voice format -> 16kHz mono PCM16 WAV.

Uses ffmpeg via asyncio.create_subprocess_exec. Input bytes are piped to
ffmpeg stdin; output WAV bytes are read from stdout. Stderr is captured
for error reporting only. ffmpeg must be on PATH (installed via apt in
Dockerfile c2; on dev machines, install via brew/apt manually).

The output format (PCM s16le, 16 kHz, mono) matches what Azure OpenAI
gpt-audio-1.5 accepts as ``input_audio`` content blocks. See PLAN sec
10.4 for the AOAI request shape.
"""

from __future__ import annotations

import asyncio


class TranscodeError(Exception):
    """Raised when ffmpeg exits non-zero or produces invalid output."""


# WAV header magic bytes (RIFF container, WAVE format identifier).
_RIFF_MAGIC = b"RIFF"
_WAVE_MAGIC = b"WAVE"
_WAVE_OFFSET_START = 8
_WAVE_OFFSET_END = 12


async def transcode_to_wav(
    input_bytes: bytes,
    *,
    ffmpeg_path: str = "ffmpeg",
    timeout_seconds: float = 30.0,
) -> bytes:
    """Transcode arbitrary audio bytes to PCM16 16kHz mono WAV bytes.

    Args:
        input_bytes: source audio (OGG/Opus, MP3, WAV, M4A, etc.).
        ffmpeg_path: command name or absolute path. Default reads from PATH.
        timeout_seconds: hard kill on hang.

    Returns:
        WAV bytes starting with RIFF/WAVE header.

    Raises:
        TranscodeError: ffmpeg exits non-zero, produces empty output, or
            output does not start with RIFF/WAVE.
        FileNotFoundError: ffmpeg binary is not present on PATH or at the
            provided absolute path. Raised by asyncio when the executable
            cannot be located (documented behavior of subprocess transport
            creation under POSIX).
        TimeoutError: ffmpeg ran longer than ``timeout_seconds``. asyncio
            ``wait_for`` raises ``asyncio.TimeoutError`` which is an alias
            of the built-in ``TimeoutError`` in Python 3.11+.
    """
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",  # read input from stdin
        "-vn",  # no video stream
        "-ar",
        "16000",  # sample rate 16 kHz
        "-ac",
        "1",  # mono
        "-acodec",
        "pcm_s16le",  # PCM 16-bit little-endian
        "-f",
        "wav",  # WAV container on stdout
        "pipe:1",  # write to stdout
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_bytes),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise TranscodeError(f"ffmpeg exited {proc.returncode}: {err}")

    if not stdout:
        raise TranscodeError("ffmpeg produced empty output")

    has_riff = stdout.startswith(_RIFF_MAGIC)
    has_wave = stdout[_WAVE_OFFSET_START:_WAVE_OFFSET_END] == _WAVE_MAGIC
    if not (has_riff and has_wave):
        raise TranscodeError("ffmpeg output is not a valid WAV (missing RIFF/WAVE header)")

    return stdout
