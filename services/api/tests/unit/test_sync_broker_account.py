"""Tests for broker sync orchestration.

The centre of gravity here is the **cursor**. Everything else in a sync is recoverable:
a failed request retries, a duplicate fill is idempotent, a crashed run repeats. But a
cursor that advances past a fill we did not ingest loses that trade permanently — the
next run starts after it, and nothing ever looks back. These tests exist mainly to pin
that down.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.application.use_cases.sync_broker_account import (
    DeferredItem,
    FetchResult,
    SyncBrokerAccount,
)
from app.domain.common.enums import Side, SyncStatus
from app.domain.trading.execution import Execution
from app.domain.trading.instrument import InstrumentSpec
from tests.conftest import ES

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
CONNECTION_ID = UUID("00000000-0000-7000-8000-000000000002")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000003")


def execution(fill_id: int, account: str = "25", price: str = "5000.00") -> Execution:
    return Execution(
        external_id=str(fill_id),
        account_key=account,
        instrument_symbol="ESZ5",
        side=Side.BUY,
        quantity=Decimal(1),
        price=Decimal(price),
        executed_at=datetime(2026, 3, 5, 15, 0, tzinfo=UTC),
        sequence=fill_id,
    )


class FakeSource:
    def __init__(self, result: FetchResult) -> None:
        self.result = result
        self.calls: list[int] = []

    async def fetch_executions(
        self, *, since_cursor: int, account_filter: set[str] | None = None
    ) -> FetchResult:
        self.calls.append(since_cursor)
        return self.result


@dataclass
class FakeIngestResult:
    executions_written: int
    executions_skipped: int = 0
    trades_written: int = 0


class FakeIngestor:
    def __init__(self) -> None:
        self.batches: list[tuple[UUID, list[Execution]]] = []

    async def execute(
        self, *, user_id: UUID, account_id: UUID, executions: Sequence[Execution]
    ) -> FakeIngestResult:
        self.batches.append((account_id, list(executions)))
        return FakeIngestResult(
            executions_written=len(executions), trades_written=max(1, len(executions) // 2)
        )


class FakeInstruments:
    def __init__(self) -> None:
        self.upserted: dict[str, InstrumentSpec] = {}

    async def upsert_specs(self, specs: dict[str, InstrumentSpec]) -> int:
        self.upserted.update(specs)
        return len(specs)


class FakeAccounts:
    def __init__(self, mapping: dict[str, UUID] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {"25": ACCOUNT_ID}

    async def resolve(self, broker_account_id: str) -> UUID | None:
        return self.mapping.get(broker_account_id)


class FakeCursors:
    def __init__(self, initial: int = 0) -> None:
        self.value = initial
        self.saves: list[int] = []

    async def load(self, connection_id: UUID) -> int:
        return self.value

    async def save(self, connection_id: UUID, cursor: int) -> None:
        self.value = cursor
        self.saves.append(cursor)


class FakeRuns:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[dict[str, Any]] = []

    async def start(self, connection_id: UUID, trigger: str) -> UUID:
        self.started.append(trigger)
        return UUID("00000000-0000-7000-8000-00000000000f")

    async def finish(self, run_id: UUID, **kwargs: Any) -> None:
        self.finished.append(kwargs)


def build(result: FetchResult, *, cursor: int = 0, accounts: dict[str, UUID] | None = None):  # type: ignore[no-untyped-def]
    parts = {
        "source": FakeSource(result),
        "ingestor": FakeIngestor(),
        "instruments": FakeInstruments(),
        "accounts": FakeAccounts(accounts),
        "cursors": FakeCursors(cursor),
        "runs": FakeRuns(),
    }
    return SyncBrokerAccount(**parts), parts  # type: ignore[arg-type]


# --- Happy path -----------------------------------------------------------------


async def test_ingests_fills_and_advances_the_cursor() -> None:
    result = FetchResult(
        executions=(execution(101), execution(102)), specs={"ESZ5": ES}, highest_id=102
    )
    use_case, parts = build(result)

    outcome = await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    assert outcome.status is SyncStatus.SUCCEEDED
    assert outcome.executions_ingested == 2
    assert outcome.cursor_after == 102
    assert parts["cursors"].saves == [102]  # type: ignore[union-attr]


async def test_instrument_specs_are_upserted_from_what_traded() -> None:
    """A newly listed contract must never block a sync waiting to be seeded."""
    result = FetchResult(executions=(execution(1),), specs={"ESZ5": ES}, highest_id=1)
    use_case, parts = build(result)

    await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    assert "ESZ5" in parts["instruments"].upserted  # type: ignore[union-attr]


async def test_fills_are_grouped_by_account() -> None:
    second_account = UUID("00000000-0000-7000-8000-000000000004")
    result = FetchResult(
        executions=(execution(1, account="25"), execution(2, account="99")),
        specs={},
        highest_id=2,
    )
    use_case, parts = build(result, accounts={"25": ACCOUNT_ID, "99": second_account})

    await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    ingested = {account for account, _ in parts["ingestor"].batches}  # type: ignore[union-attr]
    assert ingested == {ACCOUNT_ID, second_account}


async def test_cursor_is_passed_to_the_source() -> None:
    result = FetchResult(executions=(), specs={}, highest_id=500)
    use_case, parts = build(result, cursor=500)

    await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    assert parts["source"].calls == [500]  # type: ignore[union-attr]


async def test_empty_sync_does_not_move_the_cursor_backwards() -> None:
    result = FetchResult(executions=(), specs={}, highest_id=0)
    use_case, parts = build(result, cursor=900)

    outcome = await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    assert outcome.cursor_after == 900
    assert parts["cursors"].saves == []  # type: ignore[union-attr]


# --- Cursor safety: the property that matters -----------------------------------


async def test_cursor_stops_below_a_deferred_fill() -> None:
    """The single most important behaviour in this use case.

    Fill 102 could not be mapped. If the cursor advanced to 103 it would never be
    fetched again and the trade would be gone — silently, permanently, with the
    journal showing no sign that anything is missing.
    """
    result = FetchResult(
        executions=(execution(101), execution(103)),
        specs={},
        deferred=(DeferredItem(external_id=102, reason="order not available"),),
        highest_id=103,
    )
    use_case, parts = build(result)

    outcome = await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    assert outcome.cursor_after == 101, "cursor advanced past a fill that was not ingested"
    assert outcome.status is SyncStatus.PARTIAL
    assert parts["cursors"].saves == [101]  # type: ignore[union-attr]


async def test_next_run_refetches_the_deferred_fill() -> None:
    """Holding the cursor back is only useful if the retry actually happens."""
    first = FetchResult(
        executions=(execution(101),),
        specs={},
        deferred=(DeferredItem(external_id=102, reason="fees pending"),),
        highest_id=103,
    )
    use_case, parts = build(first)
    await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    source = parts["source"]
    source.result = FetchResult(  # type: ignore[union-attr]
        executions=(execution(102), execution(103)), specs={}, highest_id=103
    )
    outcome = await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    assert source.calls == [0, 101]  # type: ignore[union-attr]
    assert outcome.cursor_after == 103


async def test_earliest_deferral_wins_when_several_are_deferred() -> None:
    result = FetchResult(
        executions=(execution(100),),
        specs={},
        deferred=(
            DeferredItem(external_id=150, reason="a"),
            DeferredItem(external_id=110, reason="b"),
        ),
        highest_id=200,
    )
    use_case, _ = build(result)

    outcome = await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)
    assert outcome.cursor_after == 109


async def test_unknown_account_also_holds_the_cursor() -> None:
    """A fill for an unlinked account is not lost — it is retried once linked."""
    result = FetchResult(
        executions=(execution(50, account="25"), execution(60, account="unlinked")),
        specs={},
        highest_id=60,
    )
    use_case, _ = build(result, accounts={"25": ACCOUNT_ID})

    outcome = await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    assert outcome.unknown_accounts == {"unlinked"}
    assert outcome.cursor_after == 59
    assert outcome.status is SyncStatus.PARTIAL


async def test_cursor_never_moves_backwards() -> None:
    """Deferrals below the stored cursor must not rewind it into already-synced history."""
    result = FetchResult(
        executions=(),
        specs={},
        deferred=(DeferredItem(external_id=5, reason="stale"),),
        highest_id=1000,
    )
    use_case, _ = build(result, cursor=900)

    outcome = await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)
    assert outcome.cursor_after == 900


# --- Bookkeeping ----------------------------------------------------------------


async def test_a_run_is_recorded_for_every_sync() -> None:
    """"Why is yesterday missing?" has to be answerable."""
    result = FetchResult(executions=(execution(1),), specs={}, highest_id=1)
    use_case, parts = build(result)

    await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID, trigger="manual")

    runs = parts["runs"]
    assert runs.started == ["manual"]  # type: ignore[union-attr]
    assert runs.finished[0]["status"] is SyncStatus.SUCCEEDED  # type: ignore[union-attr]
    assert runs.finished[0]["executions_ingested"] == 1  # type: ignore[union-attr]


async def test_deferrals_are_recorded_in_the_run_detail() -> None:
    result = FetchResult(
        executions=(),
        specs={},
        deferred=(DeferredItem(external_id=7, reason="order not available"),),
        highest_id=7,
    )
    use_case, parts = build(result)

    await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    detail = parts["runs"].finished[0]["detail"]  # type: ignore[union-attr]
    assert detail["deferred"] == 1
    assert detail["cursor_before"] == 0


async def test_failure_is_recorded_and_re_raised() -> None:
    """A failed sync must leave evidence, then fail loudly rather than silently."""

    class ExplodingSource:
        async def fetch_executions(self, **kwargs: Any) -> FetchResult:
            raise RuntimeError("broker unreachable")

    use_case, parts = build(FetchResult(executions=(), specs={}))
    use_case._source = ExplodingSource()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="broker unreachable"):
        await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    finished = parts["runs"].finished[0]  # type: ignore[union-attr]
    assert finished["status"] is SyncStatus.FAILED
    assert "broker unreachable" in finished["error"]


async def test_failed_sync_leaves_the_cursor_alone() -> None:
    """A cursor advanced on a failed run would skip whatever the run did not process."""

    class ExplodingSource:
        async def fetch_executions(self, **kwargs: Any) -> FetchResult:
            raise RuntimeError("boom")

    use_case, parts = build(FetchResult(executions=(), specs={}), cursor=500)
    use_case._source = ExplodingSource()  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        await use_case.execute(user_id=USER_ID, connection_id=CONNECTION_ID)

    assert parts["cursors"].saves == []  # type: ignore[union-attr]
    assert parts["cursors"].value == 500  # type: ignore[union-attr]
