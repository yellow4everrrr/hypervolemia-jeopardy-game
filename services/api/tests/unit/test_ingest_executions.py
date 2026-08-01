"""Tests for the ingestion use case, against in-memory fakes.

The fakes exist because the properties worth testing here — idempotency, transaction
boundaries, refusal to guess an instrument — are properties of the *orchestration*, not
of SQL. Testing them without a database keeps them fast enough to run on every save.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.ingest_executions import IngestExecutions
from app.core.errors import DomainError
from app.domain.common.enums import Side
from app.domain.trading.execution import Execution
from app.domain.trading.instrument import InstrumentSpec
from app.domain.trading.matching import PositionKey, PositionState
from app.domain.trading.trade import ReconstructedTrade
from tests.conftest import SPECS, ExecutionFactory

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000002")


class FakeExecutionRepository:
    def __init__(self) -> None:
        self.stored: dict[str, Execution] = {}

    async def existing_broker_ids(self, account_id: UUID, ids: Sequence[str]) -> set[str]:
        return {external_id for external_id in ids if external_id in self.stored}

    async def add_many(
        self, user_id: UUID, account_id: UUID, executions: Sequence[Execution]
    ) -> int:
        for execution in executions:
            self.stored[execution.external_id] = execution
        return len(executions)

    async def list_for_rebuild(self, account_id: UUID, since: object = None) -> Sequence[Execution]:
        return list(self.stored.values())


class FakeTradeRepository:
    def __init__(self) -> None:
        self.trades: list[ReconstructedTrade] = []

    async def upsert_many(
        self, user_id: UUID, account_id: UUID, trades: Sequence[ReconstructedTrade]
    ) -> int:
        self.trades.extend(trades)
        return len(trades)

    async def count(self, user_id: UUID) -> int:
        return len(self.trades)


class FakeInstrumentRepository:
    def __init__(self, specs: dict[str, InstrumentSpec] | None = None) -> None:
        self.specs = specs if specs is not None else dict(SPECS)

    async def get_spec(self, symbol: str, exchange: str | None = None) -> InstrumentSpec | None:
        return self.specs.get(symbol)

    async def get_specs(self, symbols: Sequence[str]) -> dict[str, InstrumentSpec]:
        return {symbol: self.specs[symbol] for symbol in symbols if symbol in self.specs}

    async def resolve_id(self, symbol: str, exchange: str | None = None) -> UUID | None:
        return uuid4() if symbol in self.specs else None


class FakePositionRepository:
    def __init__(self) -> None:
        self.state: dict[PositionKey, PositionState] = {}

    async def load(self, account_id: UUID) -> dict[PositionKey, PositionState]:
        return dict(self.state)

    async def save(self, account_id: UUID, state: dict[PositionKey, PositionState]) -> None:
        self.state = dict(state)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def build(
    instruments: FakeInstrumentRepository | None = None,
) -> tuple[IngestExecutions, dict[str, object]]:
    parts = {
        "executions": FakeExecutionRepository(),
        "trades": FakeTradeRepository(),
        "instruments": instruments or FakeInstrumentRepository(),
        "positions": FakePositionRepository(),
        "uow": FakeUnitOfWork(),
    }
    return IngestExecutions(**parts), parts  # type: ignore[arg-type]


async def test_ingests_and_builds_trades() -> None:
    use_case, parts = build()
    fill = ExecutionFactory()

    result = await use_case.execute(
        user_id=USER_ID,
        account_id=ACCOUNT_ID,
        executions=[fill(Side.BUY, 1, "5000.00"), fill(Side.SELL, 1, "5004.00", minute=5)],
    )

    assert result.executions_written == 2
    assert result.trades_written == 1
    assert parts["uow"].commits == 1  # type: ignore[union-attr]


async def test_resyncing_the_same_fills_is_a_noop() -> None:
    """At-least-once delivery over an idempotent write: the core sync guarantee."""
    use_case, parts = build()
    fill = ExecutionFactory()
    executions = [fill(Side.BUY, 1, "5000.00"), fill(Side.SELL, 1, "5004.00", minute=5)]

    first = await use_case.execute(user_id=USER_ID, account_id=ACCOUNT_ID, executions=executions)
    second = await use_case.execute(user_id=USER_ID, account_id=ACCOUNT_ID, executions=executions)

    assert first.executions_written == 2
    assert second.was_noop
    assert second.executions_skipped == 2
    assert second.trades_written == 0
    assert parts["uow"].commits == 1  # type: ignore[union-attr] — no commit for a no-op


async def test_partial_overlap_ingests_only_new_fills() -> None:
    use_case, _ = build()
    fill = ExecutionFactory()
    first = fill(Side.BUY, 1, "5000.00")
    second = fill(Side.SELL, 1, "5004.00", minute=5)

    await use_case.execute(user_id=USER_ID, account_id=ACCOUNT_ID, executions=[first])
    result = await use_case.execute(
        user_id=USER_ID, account_id=ACCOUNT_ID, executions=[first, second]
    )

    assert result.executions_written == 1
    assert result.executions_skipped == 1


async def test_incremental_batches_close_the_open_trade() -> None:
    """State persisted by the first batch must let the second one complete the trade."""
    use_case, parts = build()
    fill = ExecutionFactory()

    await use_case.execute(
        user_id=USER_ID, account_id=ACCOUNT_ID, executions=[fill(Side.BUY, 2, "5000.00")]
    )
    assert len(parts["positions"].state) == 1  # type: ignore[union-attr]

    await use_case.execute(
        user_id=USER_ID,
        account_id=ACCOUNT_ID,
        executions=[fill(Side.SELL, 2, "5006.00", minute=10)],
    )

    closed = [trade for trade in parts["trades"].trades if trade.closed_at is not None]  # type: ignore[union-attr]
    assert len(closed) == 1
    assert closed[0].quantity_opened == 2
    assert parts["positions"].state == {}  # type: ignore[union-attr]


async def test_unknown_instrument_aborts_the_batch() -> None:
    """Better a failed sync than P&L that is silently wrong by a constant factor."""
    use_case, parts = build(instruments=FakeInstrumentRepository(specs={}))
    fill = ExecutionFactory()

    with pytest.raises(DomainError, match="unknown instruments"):
        await use_case.execute(
            user_id=USER_ID, account_id=ACCOUNT_ID, executions=[fill(Side.BUY, 1, "5000.00")]
        )

    assert parts["executions"].stored == {}  # type: ignore[union-attr]
    assert parts["uow"].commits == 0  # type: ignore[union-attr]


async def test_empty_batch_does_nothing() -> None:
    use_case, parts = build()
    result = await use_case.execute(user_id=USER_ID, account_id=ACCOUNT_ID, executions=[])
    assert result.executions_received == 0
    assert parts["uow"].commits == 0  # type: ignore[union-attr]
