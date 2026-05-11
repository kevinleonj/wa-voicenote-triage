"""Audio blob staging for WhatsApp voice notes.

Stores transcoded WAV bytes in Azure Blob Storage (container=audio-staging).
Blob names follow the pattern ``{phone_hash}/{iso_timestamp_utc}.wav`` so no
plaintext phone numbers appear in storage. The container has a 24h lifecycle
delete rule configured at the resource level (see PLAN §6).

Auth: Managed Identity in prod (``DefaultAzureCredential`` from
``azure.identity.aio``); the caller injects the ``ContainerClient`` so tests
can mock and prod can wire MI from ``Settings``. The repo owns no client
lifecycle.

References (verify-docs, current 2026-05-10):
- ``azure-storage-blob`` 12.28.x on PyPI; async API lives in
  ``azure.storage.blob.aio`` (``BlobServiceClient``, ``ContainerClient``,
  ``BlobClient``). https://pypi.org/project/azure-storage-blob/
- ``upload_blob(data, overwrite=True)`` is the supported idempotent upload
  for the same blob name. https://learn.microsoft.com/en-us/python/api/overview/azure/storage-blob-readme
- ``download_blob()`` returns a ``StorageStreamDownloader`` whose async
  ``readall()`` resolves to ``bytes``.
- ``ResourceNotFoundError`` lives in ``azure.core.exceptions`` and is raised
  on 404; ``AzureError`` is the common base for service errors.
- Blob names accept ``/`` (used as a virtual folder separator) and may be up
  to 1024 chars; ours are well under that.

PII handling: only Kevin's WhatsApp number is allowlisted upstream, so
collisions on the 16-hex hash prefix are not a security concern; the hash
exists purely to keep plaintext phone numbers out of blob URLs and Azure
diagnostic logs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob.aio import ContainerClient

# Hash truncation length (hex chars). SHA-256 truncated to 16 hex chars (64
# bits) is sufficient: the upstream allowlist gates inputs to a single phone
# number, so collision probability across the lifetime of the deployment is
# effectively zero. Kept private and constant because it is a cryptographic
# parameter, not a user-facing configuration knob.
_PHONE_HASH_LEN: Final[int] = 16

_BLOB_SUFFIX: Final[str] = ".wav"


class BlobNotFoundError(Exception):
    """Raised by ``download_audio`` when the blob does not exist (404)."""


class BlobUploadError(Exception):
    """Raised by ``upload_audio`` on any non-404 Azure error during upload."""


@dataclass(frozen=True)
class BlobRef:
    """Immutable pointer to a staged audio blob.

    ``blob_url`` is the absolute URL (e.g.
    ``https://stwavoicenote.blob.core.windows.net/audio-staging/<name>``).
    For a private container, this URL is not directly fetchable without a
    credential — clients of this repo treat it as an opaque pointer.

    ``blob_name`` is the name relative to the container (e.g.
    ``abcdef0123456789/2026-05-10T12:00:00+00:00.wav``) and is what should be
    persisted into ``state_repo.StateRecord.blob_url`` when the upstream
    handler enters ``awaiting_context``.
    """

    blob_url: str
    blob_name: str


class BlobRepo:
    """Async repository over the ``audio-staging`` blob container.

    The caller supplies a ready-to-use ``ContainerClient`` bound to the
    target container. The repo never instantiates clients or credentials,
    keeping authentication and pool lifecycle a concern of the composition
    root (``main.py``).
    """

    def __init__(self, container_client: ContainerClient) -> None:
        self._container: ContainerClient = container_client

    # ----- Public API --------------------------------------------------------

    async def upload_audio(self, phone: str, wav_bytes: bytes) -> BlobRef:
        """Upload ``wav_bytes`` for ``phone`` and return a ``BlobRef``.

        The blob name is computed as ``{sha256(phone)[:16]}/{utc_now.isoformat()}.wav``.
        Uploads use ``overwrite=True`` so a Twilio retry that lands on the
        same blob name is a no-op rather than a 409.
        """
        blob_name = _blob_name(phone, datetime.now(tz=UTC))
        blob_client = self._container.get_blob_client(blob_name)
        try:
            await blob_client.upload_blob(wav_bytes, overwrite=True)
        except ResourceNotFoundError:
            # Container itself is missing — surface as an upload error so the
            # caller can decide whether to alert vs. retry.
            raise BlobUploadError(f"container does not exist for blob {blob_name!r}") from None
        except AzureError as exc:
            raise BlobUploadError(str(exc)) from exc
        # Compose the URL from the container URL + relative blob name. This is
        # what the SDK does internally for ``BlobClient.url``; building it here
        # keeps the URL deterministic from inputs we control and makes the
        # value testable without depending on mock attribute plumbing.
        blob_url = f"{self._container.url}/{blob_name}"
        return BlobRef(blob_url=blob_url, blob_name=blob_name)

    async def download_audio(self, blob_name: str) -> bytes:
        """Download and return the bytes of the blob named ``blob_name``.

        Raises ``BlobNotFoundError`` if the blob does not exist.
        """
        blob_client = self._container.get_blob_client(blob_name)
        try:
            downloader = await blob_client.download_blob()
            return await downloader.readall()
        except ResourceNotFoundError as exc:
            raise BlobNotFoundError(blob_name) from exc

    async def delete_audio(self, blob_name: str) -> None:
        """Delete the blob named ``blob_name`` (idempotent on 404)."""
        blob_client = self._container.get_blob_client(blob_name)
        try:
            await blob_client.delete_blob()
        except ResourceNotFoundError:
            # Idempotent: deleting an already-gone blob is success.
            return


# ----- Module helpers --------------------------------------------------------


def _phone_hash(phone: str) -> str:
    """Return the first ``_PHONE_HASH_LEN`` hex chars of SHA-256(phone).

    Deterministic across processes and restarts. Lowercase hex.
    """
    digest = hashlib.sha256(phone.encode("utf-8")).hexdigest()
    return digest[:_PHONE_HASH_LEN]


def _blob_name(phone: str, now: datetime) -> str:
    """Compose ``{phone_hash}/{iso8601_utc}.wav``.

    ``now`` must be timezone-aware; this is enforced because the resulting
    blob name encodes the timestamp and we want it unambiguous.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (use datetime.now(tz=UTC))")
    return f"{_phone_hash(phone)}/{now.isoformat()}{_BLOB_SUFFIX}"
