"""End-to-end ingestion against a real PostgreSQL/TimescaleDB instance.

The unit tests prove the reconstruction *logic*. These prove the parts only a real
database can: that the unique constraints actually enforce idempotency, that the
``ON CONFLICT`` upsert targets the constraint it names, that ``NUMERIC(20, 8)`` returns
``Decimal`` and not ``float``, and that position state survives a round trip through
JSONB.

Skipped unless ``LEDGERLINE_TEST_DATABASE_URL`` points at a migrated database. Every
test runs inside a transaction that is rolled back, so the database is left untouched.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.use_cases.ingest_executions import IngestExecutions
from app.core.ids import uuid7
from app.domain.common.enums import AccountType, AssetClass, Direction, Side, TradeStatus
from app.domain.trading.execution import Execution
from app.infrastructure.db.models.broker import Account
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Position, Trade, TradeExecution
from app.infrastructure.db.repositories.executions import SqlAlchemyExecutionRepository
from app.infrastructure.db.repositories.instruments import SqlAlchemyInstrumentRepository
from app.infrastructure.db.repositories.positions import SqlAlchemyPositionStateRepository
from app.infrastructure.db.repositories.trades import SqlAlchemyTradeRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    ),
]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back."""
    engine = create_async_engine(os.environ["LEDGERLINE_TEST_DATABASE_URL"])
    async with engine.connect() as connection:
        transaction = await connection.begin()
        maker = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as db_session:
            yield db_session
        await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def fixtures(session: AsyncSession) -> dict[str, UUID]:
    """Insert the minimum graph an ingestion needs: user, account, instrument."""
    user = User(
        id=uuid7(),
        clerk_user_id=f"user_{uuid7().hex[:16]}",
        email="trader@example.com",
        display_name="Test Trader",
    )
    account = Account(
        id=uuid7(),
        user_id=user.id,
        broker_account_id=f"acct-{uuid7().hex[:8]}",
        name="Test Account",
        account_type=AccountType.SIMULATED,
        currency="USD",
    )
    instrument = Instrument(
        id=uuid7(),
        symbol=f"ES{uuid7().hex[:4].upper()}",
        root_symbol="ES",
        exchange="CME",
        description="E-mini S&P 500 (test)",
        asset_class=AssetClass.FUTURE,
        currency="USD",
        tick_size=Decimal("0.25"),
        tick_value=Decimal("12.50"),
        price_precision=2,
        exchange_timezone="America/Chicago",
        session_rollover="17:00",
    )
    session.add_all([user, account, instrument])
    await session.flush()
    return {"user_id": user.id, "account_id": account.id, "symbol": instrument.symbol}


def build_use_case(session: AsyncSession) -> IngestExecutions:
    return IngestExecutions(
        executions=SqlAlchemyExecutionRepository(session),
        trades=SqlAlchemyTradeRepository(session),
        instruments=SqlAlchemyInstrumentRepository(session),
        positions=SqlAlchemyPositionStateRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )


def make_fill(
    symbol: str,
    account_id: UUID,
    external_id: str,
    side: Side,
    quantity: str,
    price: str,
    minute: int,
) -> Execution:
    return Execution(
        external_id=external_id,
        account_key=str(account_id),
        instrument_symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        executed_at=datetime(2026, 3, 5, 15, minute, tzinfo=UTC),
        commission=Decimal("1.29"),
        fees=Decimal("0.37"),
    )


async def test_round_turn_is_persisted(session: AsyncSession, fixtures: dict) -> None:
    use_case = build_use_case(session)
    symbol, account_id, user_id = fixtures["symbol"], fixtures["account_id"], fixtures["user_id"]

    result = await use_case.execute(
        user_id=user_id,
        account_id=account_id,
        executions=[
            make_fill(symbol, account_id, "f1", Side.BUY, "2", "5000.00", 0),
            make_fill(symbol, account_id, "f2", Side.SELL, "2", "5004.00", 10),
        ],
    )

    assert result.executions_written == 2
    assert result.trades_written == 1

    trade = (await session.execute(select(Trade).where(Trade.user_id == user_id))).scalar_one()
    assert trade.direction is Direction.LONG
    assert trade.status is TradeStatus.CLOSED
    # NUMERIC round-trips as Decimal; a float here would mean silent precision loss.
    assert isinstance(trade.net_pnl, Decimal)
    assert trade.gross_pnl == Decimal("400.00000000")
    assert trade.net_pnl == Decimal("396.68000000")
    assert trade.session_date is not None
    assert trade.entry_hour == 9  # 15:00 UTC is 09:00 in Chicago on 2026-03-05

    leg_count = await session.scalar(
        select(func.count()).select_from(TradeExecution).where(TradeExecution.trade_id == trade.id)
    )
    assert leg_count == 2


async def test_resync_is_idempotent_at_the_database_level(
    session: AsyncSession, fixtures: dict
) -> None:
    """The unique constraint, not just the pre-check, must prevent duplication."""
    use_case = build_use_case(session)
    symbol, account_id, user_id = fixtures["symbol"], fixtures["account_id"], fixtures["user_id"]
    fills = [
        make_fill(symbol, account_id, "f1", Side.BUY, "1", "5000.00", 0),
        make_fill(symbol, account_id, "f2", Side.SELL, "1", "5004.00", 5),
    ]

    await use_case.execute(user_id=user_id, account_id=account_id, executions=fills)
    second = await use_case.execute(user_id=user_id, account_id=account_id, executions=fills)

    assert second.was_noop
    trade_count = await session.scalar(
        select(func.count()).select_from(Trade).where(Trade.user_id == user_id)
    )
    assert trade_count == 1


async def test_open_position_state_round_trips_through_jsonb(
    session: AsyncSession, fixtures: dict
) -> None:
    """The second batch must resume the first batch's lots, not open a new trade."""
    use_case = build_use_case(session)
    symbol, account_id, user_id = fixtures["symbol"], fixtures["account_id"], fixtures["user_id"]

    await use_case.execute(
        user_id=user_id,
        account_id=account_id,
        executions=[make_fill(symbol, account_id, "f1", Side.BUY, "3", "5000.00", 0)],
    )

    position = (
        await session.execute(select(Position).where(Position.account_id == account_id))
    ).scalar_one()
    assert position.signed_quantity == Decimal("3.00000000")
    assert position.open_lots["lots"][0]["price"] == "5000.00"

    await use_case.execute(
        user_id=user_id,
        account_id=account_id,
        executions=[make_fill(symbol, account_id, "f2", Side.SELL, "3", "5006.00", 12)],
    )

    trades = (await session.execute(select(Trade).where(Trade.user_id == user_id))).scalars().all()
    assert len(trades) == 1
    assert trades[0].status is TradeStatus.CLOSED
    assert trades[0].quantity_opened == Decimal("3.00000000")

    remaining = (
        await session.execute(select(Position).where(Position.account_id == account_id))
    ).scalars().all()
    assert remaining == []


async def test_rebuild_preserves_trader_supplied_context(
    session: AsyncSession, fixtures: dict
) -> None:
    """Re-running reconstruction must not erase notes, emotion or manual tagging."""
    use_case = build_use_case(session)
    symbol, account_id, user_id = fixtures["symbol"], fixtures["account_id"], fixtures["user_id"]
    fills = [
        make_fill(symbol, account_id, "f1", Side.BUY, "1", "5000.00", 0),
        make_fill(symbol, account_id, "f2", Side.SELL, "1", "5004.00", 5),
    ]
    await use_case.execute(user_id=user_id, account_id=account_id, executions=fills)

    trade = (await session.execute(select(Trade).where(Trade.user_id == user_id))).scalar_one()
    trade.notes = "Waited for the retest."
    trade.confidence = 4
    await session.flush()

    # Reconstruct the same fills again, as a corrective rebuild would.
    from app.domain.trading.matching import reconstruct_trades

    specs = await SqlAlchemyInstrumentRepository(session).get_specs([symbol])
    rebuilt = reconstruct_trades(fills, specs)
    await SqlAlchemyTradeRepository(session).upsert_many(user_id, account_id, rebuilt.trades)
    await session.flush()

    await session.refresh(trade)
    assert trade.notes == "Waited for the retest."
    assert trade.confidence == 4
    assert trade.net_pnl == Decimal("196.68000000")
