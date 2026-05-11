"""Tests for ``blob_repo.BlobRepo``.

The ``azure.storage.blob.aio.ContainerClient`` is mocked end-to-end so these
tests run offline and deterministically. Covers PLAN §3 ``test_blob_repo.py``
plus the extras in the c8 spec:

- upload returns a ``BlobRef`` whose ``blob_name`` follows ``{phone_hash}/{iso}.wav``
- upload uses ``overwrite=True`` (Twilio retries are tolerated)
- blob name uses a hashed phone, never plaintext — same input -> same hash
- blob name middle segment is an ISO-8601 timestamp parseable by ``fromisoformat``
- different phones produce different hash prefixes
- download returns the bytes from ``download_blob().readall()``
- download propagates a missing blob as ``BlobNotFoundError``
- delete invokes ``delete_blob`` once and is idempotent on missing blobs
- non-NotFound Azure errors during upload surface as ``BlobUploadError``
- ``blob_url`` is anchored at the container URL; ``blob_name`` is relative
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import AzureError, ResourceNotFoundError

from wa_voicenote.blob_repo import (
    BlobNotFoundError,
    BlobRef,
    BlobRepo,
    BlobUploadError,
)

PHONE = "whatsapp:+34611779374"
OTHER_PHONE = "whatsapp:+34699111222"
CONTAINER_URL = "https://stwavoicenote.blob.core.windows.net/audio-staging"
WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt fake-payload"

# Pattern: 16 lowercase-hex chars, slash, ISO-8601 timestamp, ``.wav``.
_BLOB_NAME_RE = re.compile(r"^[0-9a-f]{16}/[0-9T:.+\-]+\.wav$")


def _make_container_client(
    blob_client: MagicMock | None = None,
    container_url: str = CONTAINER_URL,
) -> MagicMock:
    """Build a MagicMock ContainerClient with .url + get_blob_client wired."""
    container = MagicMock()
    container.url = container_url
    if blob_client is None:
        blob_client = _make_blob_client()
    # ``get_blob_client`` is synchronous in the SDK; it returns a BlobClient.
    container.get_blob_client = MagicMock(return_value=blob_client)
    return container


def _make_blob_client(
    *,
    upload_side_effect: BaseException | None = None,
    download_bytes: bytes | None = b"",
    download_side_effect: BaseException | None = None,
    delete_side_effect: BaseException | None = None,
    blob_url: str = f"{CONTAINER_URL}/placeholder",
) -> MagicMock:
    """Build a MagicMock BlobClient with the three async APIs we use."""
    blob = MagicMock()
    blob.url = blob_url

    if upload_side_effect is not None:
        blob.upload_blob = AsyncMock(side_effect=upload_side_effect)
    else:
        blob.upload_blob = AsyncMock(return_value={})

    # ``download_blob`` returns a ``StorageStreamDownloader`` which has an
    # async ``readall`` method. Model both as awaitables.
    downloader = MagicMock()
    if download_side_effect is not None:
        downloader.readall = AsyncMock(side_effect=download_side_effect)
    else:
        downloader.readall = AsyncMock(return_value=download_bytes)

    if download_side_effect is not None and not isinstance(download_side_effect, type(None)):
        # If the download itself (not readall) should raise, set on download_blob.
        # We distinguish by attaching a sentinel on the exception: ResourceNotFoundError
        # in our tests is raised by download_blob (matches SDK behavior on 404).
        blob.download_blob = AsyncMock(side_effect=download_side_effect)
    else:
        blob.download_blob = AsyncMock(return_value=downloader)

    if delete_side_effect is not None:
        blob.delete_blob = AsyncMock(side_effect=delete_side_effect)
    else:
        blob.delete_blob = AsyncMock(return_value=None)

    return blob


# ----------------------------------------------------------------------------
# upload_audio
# ----------------------------------------------------------------------------


async def test_upload_audio_returns_blob_ref() -> None:
    blob = _make_blob_client(blob_url=f"{CONTAINER_URL}/will-be-overwritten")
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    ref = await repo.upload_audio(PHONE, WAV_BYTES)

    assert isinstance(ref, BlobRef)
    assert _BLOB_NAME_RE.match(ref.blob_name), f"blob_name {ref.blob_name!r} mismatch"
    assert ref.blob_url.endswith(ref.blob_name)


async def test_upload_audio_uses_overwrite_true() -> None:
    blob = _make_blob_client()
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    await repo.upload_audio(PHONE, WAV_BYTES)

    blob.upload_blob.assert_awaited_once()
    # Verify ``overwrite=True`` was passed (kwarg, not positional).
    _args, kwargs = blob.upload_blob.await_args
    assert kwargs.get("overwrite") is True


async def test_upload_audio_passes_bytes_to_upload_blob() -> None:
    """The exact bytes handed in must be uploaded verbatim — no transformation."""
    blob = _make_blob_client()
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    await repo.upload_audio(PHONE, WAV_BYTES)

    args, _kwargs = blob.upload_blob.await_args
    # Either positional[0] or kwargs["data"] depending on call style — accept both.
    sent = args[0] if args else blob.upload_blob.await_args.kwargs.get("data")
    assert sent == WAV_BYTES


async def test_upload_audio_calls_get_blob_client_with_blob_name() -> None:
    blob = _make_blob_client()
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    ref = await repo.upload_audio(PHONE, WAV_BYTES)

    container.get_blob_client.assert_called_once()
    args, _kwargs = container.get_blob_client.call_args
    passed_name = args[0] if args else container.get_blob_client.call_args.kwargs.get("blob")
    assert passed_name == ref.blob_name


async def test_blob_name_phone_hashed_not_plaintext() -> None:
    """No part of the raw phone (digits or 'whatsapp') must leak into the blob name."""
    blob = _make_blob_client()
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    ref = await repo.upload_audio(PHONE, WAV_BYTES)

    # No part of the raw "whatsapp:+34611779374" must leak into the name.
    # Note: ISO-8601 timestamps include "+00:00" for UTC, so a bare "+" check
    # would false-fire on the timestamp segment; we check for digit clusters
    # and the scheme prefix instead.
    assert "whatsapp" not in ref.blob_name
    assert "+34" not in ref.blob_name
    assert "34611779374" not in ref.blob_name
    assert "611779374" not in ref.blob_name

    # Hash prefix is the first 16 lowercase-hex chars.
    prefix = ref.blob_name.split("/", 1)[0]
    assert len(prefix) == 16
    assert all(c in "0123456789abcdef" for c in prefix)


async def test_same_phone_yields_same_hash_prefix() -> None:
    blob_a = _make_blob_client()
    blob_b = _make_blob_client()
    repo_a = BlobRepo(container_client=_make_container_client(blob_client=blob_a))
    repo_b = BlobRepo(container_client=_make_container_client(blob_client=blob_b))

    ref_a = await repo_a.upload_audio(PHONE, WAV_BYTES)
    ref_b = await repo_b.upload_audio(PHONE, WAV_BYTES)

    assert ref_a.blob_name.split("/", 1)[0] == ref_b.blob_name.split("/", 1)[0]


async def test_blob_name_includes_iso_timestamp() -> None:
    blob = _make_blob_client()
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    ref = await repo.upload_audio(PHONE, WAV_BYTES)

    _prefix, rest = ref.blob_name.split("/", 1)
    ts_str = rest.removesuffix(".wav")
    parsed = datetime.fromisoformat(ts_str)
    assert parsed.tzinfo is not None, "timestamp must be timezone-aware (UTC)"


async def test_different_phones_get_different_hashes() -> None:
    blob_a = _make_blob_client()
    blob_b = _make_blob_client()
    repo_a = BlobRepo(container_client=_make_container_client(blob_client=blob_a))
    repo_b = BlobRepo(container_client=_make_container_client(blob_client=blob_b))

    ref_a = await repo_a.upload_audio(PHONE, WAV_BYTES)
    ref_b = await repo_b.upload_audio(OTHER_PHONE, WAV_BYTES)

    assert ref_a.blob_name.split("/", 1)[0] != ref_b.blob_name.split("/", 1)[0]


async def test_blob_url_starts_with_container_url() -> None:
    blob = _make_blob_client()
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    ref = await repo.upload_audio(PHONE, WAV_BYTES)

    assert ref.blob_url.startswith(CONTAINER_URL + "/")


async def test_blob_name_is_relative_not_absolute() -> None:
    blob = _make_blob_client()
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    ref = await repo.upload_audio(PHONE, WAV_BYTES)

    assert not ref.blob_name.startswith("https://")
    assert not ref.blob_name.startswith("/")


async def test_upload_handles_other_azure_errors() -> None:
    blob = _make_blob_client(upload_side_effect=AzureError("503 boom"))
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    with pytest.raises(BlobUploadError, match="boom"):
        await repo.upload_audio(PHONE, WAV_BYTES)


async def test_upload_handles_missing_container_as_upload_error() -> None:
    """A 404 on upload (container itself absent) is surfaced as BlobUploadError."""
    blob = _make_blob_client(upload_side_effect=ResourceNotFoundError("container missing"))
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    with pytest.raises(BlobUploadError, match="container does not exist"):
        await repo.upload_audio(PHONE, WAV_BYTES)


def test_blob_name_helper_rejects_naive_datetime() -> None:
    """The blob-name composer enforces tz-aware datetimes at the boundary."""
    from wa_voicenote.blob_repo import _blob_name  # type: ignore[attr-defined]

    naive = datetime(2026, 5, 10, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        _blob_name(PHONE, naive)


# ----------------------------------------------------------------------------
# download_audio
# ----------------------------------------------------------------------------


async def test_download_audio_returns_bytes() -> None:
    expected = b"some-wav-payload"
    blob = _make_blob_client(download_bytes=expected)
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    out = await repo.download_audio("abcdef0123456789/2026-05-10T12:00:00+00:00.wav")

    assert out == expected


async def test_download_audio_uses_get_blob_client_with_name() -> None:
    blob = _make_blob_client(download_bytes=b"x")
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)
    target = "abcdef0123456789/2026-05-10T12:00:00+00:00.wav"

    await repo.download_audio(target)

    args, _kwargs = container.get_blob_client.call_args
    passed = args[0] if args else container.get_blob_client.call_args.kwargs.get("blob")
    assert passed == target


async def test_download_audio_missing_raises() -> None:
    blob = _make_blob_client(download_side_effect=ResourceNotFoundError("404"))
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    with pytest.raises(BlobNotFoundError):
        await repo.download_audio("missing/0.wav")


# ----------------------------------------------------------------------------
# delete_audio
# ----------------------------------------------------------------------------


async def test_delete_audio_calls_delete() -> None:
    blob = _make_blob_client()
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    await repo.delete_audio("abcdef0123456789/2026-05-10T12:00:00+00:00.wav")

    blob.delete_blob.assert_awaited_once()


async def test_delete_audio_idempotent_on_missing() -> None:
    blob = _make_blob_client(delete_side_effect=ResourceNotFoundError("404"))
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    # Must NOT raise — idempotent semantics.
    await repo.delete_audio("never-existed/0.wav")

    blob.delete_blob.assert_awaited_once()


async def test_delete_audio_propagates_other_azure_errors() -> None:
    """A genuine service error (not 404) must bubble up — we don't silence those."""
    blob = _make_blob_client(delete_side_effect=AzureError("503"))
    container = _make_container_client(blob_client=blob)
    repo = BlobRepo(container_client=container)

    with pytest.raises(AzureError):
        await repo.delete_audio("any/0.wav")


# ----------------------------------------------------------------------------
# BlobRef value type
# ----------------------------------------------------------------------------


def test_blob_ref_is_frozen_dataclass() -> None:
    ref = BlobRef(
        blob_url=f"{CONTAINER_URL}/abc/2026-05-10T00:00:00+00:00.wav",
        blob_name="abc/2026-05-10T00:00:00+00:00.wav",
    )
    with pytest.raises((AttributeError, Exception)):
        ref.blob_name = "mutated"  # type: ignore[misc]


def test_blob_ref_value_equality() -> None:
    a = BlobRef(blob_url="u", blob_name="n")
    b = BlobRef(blob_url="u", blob_name="n")
    assert a == b


# ----------------------------------------------------------------------------
# Sanity: phone-hash invariant across module reloads
# ----------------------------------------------------------------------------


async def test_phone_hash_invariant_across_repo_instances() -> None:
    """Two independent BlobRepo instances must compute identical hash prefixes
    for the same phone — the hash is deterministic and process-stable.
    """
    blob1 = _make_blob_client()
    blob2 = _make_blob_client()
    r1 = BlobRepo(container_client=_make_container_client(blob_client=blob1))
    r2 = BlobRepo(container_client=_make_container_client(blob_client=blob2))

    ref1 = await r1.upload_audio(PHONE, WAV_BYTES)
    ref2 = await r2.upload_audio(PHONE, WAV_BYTES)

    assert ref1.blob_name.split("/", 1)[0] == ref2.blob_name.split("/", 1)[0]


# ----------------------------------------------------------------------------
# Type-hint touch points — these exist so mypy --strict catches API drift.
# ----------------------------------------------------------------------------


def test_blob_ref_field_types() -> None:
    ref: BlobRef = BlobRef(blob_url="u", blob_name="n")
    blob_url: str = ref.blob_url
    blob_name: str = ref.blob_name
    assert isinstance(blob_url, str)
    assert isinstance(blob_name, str)


def test_repo_constructor_accepts_container_client() -> None:
    container: Any = _make_container_client()
    repo = BlobRepo(container_client=container)
    assert isinstance(repo, BlobRepo)
