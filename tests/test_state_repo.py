"""Tests for ``state_repo.StateRepo``.

The ``TableClient`` is mocked end-to-end so these tests run offline and
deterministically. Cover PLAN §3 + §10.5 additions:

- idle default when entity missing
- read/write of state, blob_url, awaiting_context_since, sid_ring
- preservation of sid_ring across set_state calls
- upsert (not create) semantics
- check_and_record_sid: novel vs duplicate, eviction at ring_size
- JSON encoding of sid_ring on the wire
- timezone-aware datetime persistence and round-trip
- PartitionKey == phone, RowKey == "current"
- naive datetime rejected at the boundary
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import ResourceNotFoundError

from wa_voicenote.state_repo import StateRecord, StateRepo

PHONE = "whatsapp:+34611779374"
ROW_KEY = "current"
RING_SIZE = 100


def _make_table_client(get_returns: Any | ResourceNotFoundError) -> MagicMock:
    """Build a MagicMock TableClient with async get_entity / upsert_entity."""
    client = MagicMock()
    if isinstance(get_returns, BaseException):
        client.get_entity = AsyncMock(side_effect=get_returns)
    else:
        client.get_entity = AsyncMock(return_value=get_returns)
    client.upsert_entity = AsyncMock(return_value={})
    return client


def _make_repo(table_client: MagicMock, ring_size: int = RING_SIZE) -> StateRepo:
    return StateRepo(table_client=table_client, ring_size=ring_size)


async def test_get_state_idle_default_when_missing() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table)

    record = await repo.get_state(PHONE)

    assert record == StateRecord(
        state="idle",
        blob_url=None,
        awaiting_context_since=None,
        sid_ring=(),
    )
    table.get_entity.assert_awaited_once_with(partition_key=PHONE, row_key=ROW_KEY)


async def test_get_state_reads_existing_idle() -> None:
    table = _make_table_client(
        {
            "PartitionKey": PHONE,
            "RowKey": ROW_KEY,
            "state": "idle",
            "blob_url": "",
            "awaiting_context_since": "",
            "sid_ring": json.dumps([]),
        }
    )
    repo = _make_repo(table)

    record = await repo.get_state(PHONE)

    assert record.state == "idle"
    assert record.blob_url is None
    assert record.awaiting_context_since is None
    assert record.sid_ring == ()


async def test_get_state_reads_existing_awaiting_context() -> None:
    since = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    table = _make_table_client(
        {
            "PartitionKey": PHONE,
            "RowKey": ROW_KEY,
            "state": "awaiting_context",
            "blob_url": "https://acct.blob.core.windows.net/audio-staging/x.wav",
            "awaiting_context_since": since,
            "sid_ring": json.dumps(["SM1", "SM2", "SM3"]),
        }
    )
    repo = _make_repo(table)

    record = await repo.get_state(PHONE)

    assert record.state == "awaiting_context"
    assert record.blob_url == "https://acct.blob.core.windows.net/audio-staging/x.wav"
    assert record.awaiting_context_since == since
    assert record.sid_ring == ("SM1", "SM2", "SM3")


async def test_set_state_idle_clears_blob_and_since() -> None:
    existing_since = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    table = _make_table_client(
        {
            "PartitionKey": PHONE,
            "RowKey": ROW_KEY,
            "state": "awaiting_context",
            "blob_url": "https://x/y.wav",
            "awaiting_context_since": existing_since,
            "sid_ring": json.dumps(["SM1", "SM2"]),
        }
    )
    repo = _make_repo(table)

    await repo.set_state(PHONE, "idle")

    table.upsert_entity.assert_awaited_once()
    entity = table.upsert_entity.await_args.args[0]
    assert entity["state"] == "idle"
    assert entity["blob_url"] == ""
    assert entity["awaiting_context_since"] == ""
    # sid_ring preserved verbatim
    assert json.loads(entity["sid_ring"]) == ["SM1", "SM2"]


async def test_set_state_awaiting_context_writes_blob_and_since() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table)
    since = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)

    await repo.set_state(
        PHONE,
        "awaiting_context",
        blob_url="https://acct.blob.core.windows.net/audio-staging/x.wav",
        awaiting_context_since=since,
    )

    entity = table.upsert_entity.await_args.args[0]
    assert entity["state"] == "awaiting_context"
    assert entity["blob_url"] == "https://acct.blob.core.windows.net/audio-staging/x.wav"
    assert entity["awaiting_context_since"] == since


async def test_set_state_preserves_sid_ring() -> None:
    sids = [f"SM{i}" for i in range(5)]
    table = _make_table_client(
        {
            "PartitionKey": PHONE,
            "RowKey": ROW_KEY,
            "state": "idle",
            "blob_url": "",
            "awaiting_context_since": "",
            "sid_ring": json.dumps(sids),
        }
    )
    repo = _make_repo(table)

    await repo.set_state(PHONE, "idle")

    entity = table.upsert_entity.await_args.args[0]
    assert json.loads(entity["sid_ring"]) == sids


async def test_set_state_uses_upsert() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    # Spec create_entity as an AsyncMock so we can prove it was never awaited.
    table.create_entity = AsyncMock(return_value={})
    repo = _make_repo(table)

    await repo.set_state(PHONE, "idle")

    assert table.upsert_entity.await_count == 1
    # Repo must not depend on create_entity (which would fail on an existing record).
    table.create_entity.assert_not_awaited()


async def test_check_and_record_sid_novel_returns_false() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table)

    duplicate = await repo.check_and_record_sid(PHONE, "SMnew")

    assert duplicate is False
    entity = table.upsert_entity.await_args.args[0]
    assert json.loads(entity["sid_ring"]) == ["SMnew"]


async def test_check_and_record_sid_duplicate_returns_true() -> None:
    table = _make_table_client(
        {
            "PartitionKey": PHONE,
            "RowKey": ROW_KEY,
            "state": "idle",
            "blob_url": "",
            "awaiting_context_since": "",
            "sid_ring": json.dumps(["SMexisting"]),
        }
    )
    repo = _make_repo(table)

    duplicate = await repo.check_and_record_sid(PHONE, "SMexisting")

    assert duplicate is True
    # No write on duplicate — ring is unchanged.
    table.upsert_entity.assert_not_awaited()


async def test_check_and_record_sid_evicts_oldest_when_full() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table, ring_size=3)

    # Insert SM1, SM2, SM3 — ring becomes [SM1, SM2, SM3] (oldest first).
    for sid in ("SM1", "SM2", "SM3"):
        assert await repo.check_and_record_sid(PHONE, sid) is False
        last_entity = table.upsert_entity.await_args.args[0]
        # Subsequent reads must observe what we last wrote so the read-modify-write
        # builds on the latest state. Wire it back to the mock.
        table.get_entity = AsyncMock(return_value=dict(last_entity))

    # SM4 evicts SM1.
    assert await repo.check_and_record_sid(PHONE, "SM4") is False
    after_evict = table.upsert_entity.await_args.args[0]
    assert json.loads(after_evict["sid_ring"]) == ["SM2", "SM3", "SM4"]

    # SM1 is no longer treated as duplicate after eviction.
    table.get_entity = AsyncMock(return_value=dict(after_evict))
    assert await repo.check_and_record_sid(PHONE, "SM1") is False


async def test_sid_ring_persisted_as_json() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table)

    await repo.check_and_record_sid(PHONE, "SMabc")

    entity = table.upsert_entity.await_args.args[0]
    raw = entity["sid_ring"]
    assert isinstance(raw, str)
    assert json.loads(raw) == ["SMabc"]


async def test_awaiting_context_since_persisted() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table)
    now = datetime.now(tz=UTC)

    await repo.set_state(
        PHONE,
        "awaiting_context",
        blob_url="https://x/y.wav",
        awaiting_context_since=now,
    )

    entity = table.upsert_entity.await_args.args[0]
    assert isinstance(entity["awaiting_context_since"], datetime)
    assert entity["awaiting_context_since"].tzinfo is not None
    assert entity["awaiting_context_since"] == now


async def test_get_state_returns_since_as_datetime() -> None:
    since = datetime.now(tz=UTC) - timedelta(seconds=30)
    table = _make_table_client(
        {
            "PartitionKey": PHONE,
            "RowKey": ROW_KEY,
            "state": "awaiting_context",
            "blob_url": "https://x/y.wav",
            "awaiting_context_since": since,
            "sid_ring": json.dumps([]),
        }
    )
    repo = _make_repo(table)

    record = await repo.get_state(PHONE)

    assert isinstance(record.awaiting_context_since, datetime)
    assert record.awaiting_context_since == since


async def test_partition_key_is_phone() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table)

    await repo.set_state(PHONE, "idle")

    entity = table.upsert_entity.await_args.args[0]
    assert entity["PartitionKey"] == PHONE


async def test_row_key_is_current() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table)

    await repo.set_state(PHONE, "idle")

    entity = table.upsert_entity.await_args.args[0]
    assert entity["RowKey"] == ROW_KEY


async def test_naive_datetime_rejected() -> None:
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table)
    naive = datetime(2026, 5, 10, 12, 0, 0)  # intentional naive for boundary test

    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.set_state(
            PHONE,
            "awaiting_context",
            blob_url="https://x/y.wav",
            awaiting_context_since=naive,
        )

    table.upsert_entity.assert_not_awaited()


async def test_invalid_state_rejected() -> None:
    """Defensive: only 'idle' and 'awaiting_context' are valid state values."""
    table = _make_table_client(ResourceNotFoundError("missing"))
    repo = _make_repo(table)

    with pytest.raises(ValueError, match="state"):
        await repo.set_state(PHONE, "garbage")

    table.upsert_entity.assert_not_awaited()
