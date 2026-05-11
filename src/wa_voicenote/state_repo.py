"""Per-phone conversation state in Azure Table Storage.

Stores a single two-state record per phone:

- ``PartitionKey`` = phone (e.g. ``whatsapp:+34611779374``)
- ``RowKey`` = ``"current"`` (single row per phone)
- ``state`` = ``"idle"`` | ``"awaiting_context"``
- ``blob_url`` = pointer to the staged voice note in ``audio-staging`` (or
  empty string when absent — empty string is the on-the-wire sentinel because
  the Tables service rejects ``None`` for string properties; the repo
  translates to/from Python ``None`` at the boundary).
- ``awaiting_context_since`` = timezone-aware ``datetime`` set when entering
  ``awaiting_context``; consumed by handlers (c12) to enforce the passive
  ``CONTEXT_TIMEOUT_SECONDS`` drop.
- ``sid_ring`` = JSON-encoded list of the last ``ring_size`` Twilio MessageSids
  used for replay-protection.

Authentication is the caller's responsibility: pass in a ready-to-use
``azure.data.tables.aio.TableClient`` (built with ``DefaultAzureCredential`` in
production or ``from_connection_string`` in local dev). The repo owns no
client lifecycle.

Concurrency: ``set_state`` and ``check_and_record_sid`` perform read-modify-
write without optimistic concurrency. This is acceptable for a single-user
personal bot; concurrent writes for the same phone are vanishingly rare.
Document the limitation here so it is not rediscovered by surprise.

References (verify-docs):
- https://learn.microsoft.com/en-us/python/api/azure-data-tables/azure.data.tables.aio.tableclient
- https://pypi.org/project/azure-data-tables/  (v12.7.0, requires Python >=3.9)
- ``ResourceNotFoundError`` lives in ``azure.core.exceptions``.
- ``upsert_entity(entity, mode=UpdateMode.REPLACE)`` overwrites the row wholesale
  with the dict we hand it; merge-mode is unnecessary because we always read-
  modify-write.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode
from azure.data.tables.aio import TableClient

_ROW_KEY: Final[str] = "current"
_STATE_IDLE: Final[str] = "idle"
_STATE_AWAITING: Final[str] = "awaiting_context"
_VALID_STATES: Final[frozenset[str]] = frozenset({_STATE_IDLE, _STATE_AWAITING})

_KEY_PARTITION: Final[str] = "PartitionKey"
_KEY_ROW: Final[str] = "RowKey"
_KEY_STATE: Final[str] = "state"
_KEY_BLOB_URL: Final[str] = "blob_url"
_KEY_SINCE: Final[str] = "awaiting_context_since"
_KEY_SID_RING: Final[str] = "sid_ring"

# Azure Tables rejects ``None`` for string properties, so the on-the-wire
# representation of "absent" is the empty string. The repo converts ``""`` -> ``None``
# on read and ``None`` -> ``""`` on write so callers never see the sentinel.
_ABSENT: Final[str] = ""


@dataclass(frozen=True)
class StateRecord:
    """Immutable snapshot of one phone's conversation state."""

    state: str
    blob_url: str | None
    awaiting_context_since: datetime | None
    sid_ring: tuple[str, ...]


class StateRepo:
    """Async repository over the ``convstate`` table.

    Caller supplies the ``TableClient`` and the ring size; the repo holds no
    configuration of its own (PLAN §10.2: zero hardcoded knobs in business
    modules).
    """

    def __init__(self, table_client: TableClient, ring_size: int) -> None:
        if ring_size <= 0:
            raise ValueError("ring_size must be a positive integer")
        self._table: TableClient = table_client
        self._ring_size: int = ring_size

    # ----- Public API --------------------------------------------------------

    async def get_state(self, phone: str) -> StateRecord:
        """Return the current ``StateRecord`` for ``phone``.

        When the entity does not exist, returns the canonical idle default
        ``StateRecord(state="idle", blob_url=None, awaiting_context_since=None,
        sid_ring=())``.
        """
        entity = await self._read_entity(phone)
        if entity is None:
            return StateRecord(
                state=_STATE_IDLE,
                blob_url=None,
                awaiting_context_since=None,
                sid_ring=(),
            )
        return self._entity_to_record(entity)

    async def set_state(
        self,
        phone: str,
        state: str,
        blob_url: str | None = None,
        awaiting_context_since: datetime | None = None,
    ) -> None:
        """Write ``state``, ``blob_url``, and ``awaiting_context_since``.

        Preserves ``sid_ring`` from any existing record (read-modify-write).
        Raises ``ValueError`` on an unknown state name or a naive datetime.
        """
        if state not in _VALID_STATES:
            raise ValueError(f"state must be one of {sorted(_VALID_STATES)}; got {state!r}")
        if awaiting_context_since is not None and awaiting_context_since.tzinfo is None:
            raise ValueError(
                "awaiting_context_since must be timezone-aware (use datetime.now(tz=UTC))"
            )

        existing = await self._read_entity(phone)
        existing_ring = self._parse_ring(existing) if existing is not None else ()

        entity = self._build_entity(
            phone=phone,
            state=state,
            blob_url=blob_url,
            awaiting_context_since=awaiting_context_since,
            sid_ring=existing_ring,
        )
        await self._table.upsert_entity(entity, mode=UpdateMode.REPLACE)

    async def check_and_record_sid(self, phone: str, sid: str) -> bool:
        """Return True iff ``sid`` is already present in the per-phone ring.

        On a novel SID, append it to the ring (evicting the oldest entry when
        the ring is full) and persist. On a duplicate, do nothing and return
        True. Read-modify-write; concurrent calls for the same phone race
        — acceptable for the single-user use case.
        """
        existing = await self._read_entity(phone)

        if existing is None:
            new_ring: tuple[str, ...] = (sid,)
            entity = self._build_entity(
                phone=phone,
                state=_STATE_IDLE,
                blob_url=None,
                awaiting_context_since=None,
                sid_ring=new_ring,
            )
            await self._table.upsert_entity(entity, mode=UpdateMode.REPLACE)
            return False

        ring = self._parse_ring(existing)
        if sid in ring:
            return True

        appended = (*ring, sid)
        if len(appended) > self._ring_size:
            # Drop the oldest (head) entries until we are within ring_size.
            appended = appended[-self._ring_size :]

        entity = self._build_entity(
            phone=phone,
            state=self._parse_state(existing),
            blob_url=self._parse_blob_url(existing),
            awaiting_context_since=self._parse_since(existing),
            sid_ring=appended,
        )
        await self._table.upsert_entity(entity, mode=UpdateMode.REPLACE)
        return False

    # ----- Internal helpers --------------------------------------------------

    async def _read_entity(self, phone: str) -> Mapping[str, Any] | None:
        try:
            entity = await self._table.get_entity(partition_key=phone, row_key=_ROW_KEY)
        except ResourceNotFoundError:
            return None
        return entity

    @staticmethod
    def _build_entity(
        phone: str,
        state: str,
        blob_url: str | None,
        awaiting_context_since: datetime | None,
        sid_ring: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            _KEY_PARTITION: phone,
            _KEY_ROW: _ROW_KEY,
            _KEY_STATE: state,
            _KEY_BLOB_URL: blob_url if blob_url is not None else _ABSENT,
            _KEY_SINCE: (awaiting_context_since if awaiting_context_since is not None else _ABSENT),
            _KEY_SID_RING: json.dumps(list(sid_ring)),
        }

    @classmethod
    def _entity_to_record(cls, entity: Mapping[str, Any]) -> StateRecord:
        return StateRecord(
            state=cls._parse_state(entity),
            blob_url=cls._parse_blob_url(entity),
            awaiting_context_since=cls._parse_since(entity),
            sid_ring=cls._parse_ring(entity),
        )

    @staticmethod
    def _parse_state(entity: Mapping[str, Any]) -> str:
        raw = entity.get(_KEY_STATE, _STATE_IDLE)
        return str(raw) if raw else _STATE_IDLE

    @staticmethod
    def _parse_blob_url(entity: Mapping[str, Any]) -> str | None:
        raw = entity.get(_KEY_BLOB_URL, _ABSENT)
        if raw is None or raw == _ABSENT:
            return None
        return str(raw)

    @staticmethod
    def _parse_since(entity: Mapping[str, Any]) -> datetime | None:
        raw = entity.get(_KEY_SINCE, _ABSENT)
        if raw is None or raw == _ABSENT:
            return None
        if isinstance(raw, datetime):
            return raw
        # Defensive: should never happen because Tables deserializes Edm.DateTime
        # to a datetime, but we don't want to crash if a legacy row stored a string.
        return None

    @staticmethod
    def _parse_ring(entity: Mapping[str, Any]) -> tuple[str, ...]:
        raw = entity.get(_KEY_SID_RING, "")
        if not raw:
            return ()
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return ()
        if not isinstance(decoded, list):
            return ()
        return tuple(str(item) for item in decoded)
