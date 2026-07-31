"""Pattern persistence against a real database.

The engine is tested exhaustively without I/O. These cover what only Postgres can show:
that a p-value survives ``NUMERIC`` round-tripping, that a rescan replaces the previous
verdicts rather than accumulating them, and that patterns which *failed* their test are
stored too.
"""

from __future__ import annotations

import os
import random
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.analytics.discovery import DiscoveryConfig
from app.application.use_cases.detect_patterns import DetectPatterns
from app.core.ids import uuid7
from app.domain.common.enums import AccountType, AssetClass, Direction, TradeStatus
from app.infrastructure.db.models.ai import DetectedPattern
from app.infrastructure.db.models.broker import Account
from app.infrastructure.db.models.catalog import Setup
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade
from app.infrastructure.db.repositories.patterns import SqlAlchemyPatternRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    ),
]

#: Clustering is skipped: it costs seconds and is covered thoroughly in the unit suite.
#: What is exercised here is storage, which the behavioural half already drives.
FAST = DiscoveryConfig(permutations=300, include_clustering=False)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(os.environ["LEDGERLINE_TEST_DATABASE_URL"])
    async with engine.connect() as connection:
        transaction = await connection.begin()
        maker = async_sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        async with maker() as db_session:
            yield db_session
        await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> dict[str, UUID]:
    """A user with 240 closed trades over 30 sessions, carrying a real late-session leak."""
    user = User(id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="p@example.com")
    account = Account(
        id=uuid7(),
        user_id=user.id,
        broker_account_id=f"acct-{uuid7().hex[:8]}",
        name="Main",
        account_type=AccountType.SIMULATED,
    )
    instrument = Instrument(
        id=uuid7(),
        symbol=f"ES{uuid7().hex[:4].upper()}",
        root_symbol="ES",
        exchange="CME",
        asset_class=AssetClass.FUTURE,
        currency="USD",
        tick_size=Decimal("0.25"),
        tick_value=Decimal("12.50"),
        exchange_timezone="America/Chicago",
    )
    session.add_all([user, account, instrument])
    await session.flush()

    shape = random.Random(3)
    pnl_draw = random.Random(90_210)
    session_date = date(2026, 1, 5)
    opened_base = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)

    for index in range(240):
        position = index % 8
        if position == 0:
            session_date += timedelta(days=1)
            opened_base += timedelta(days=1)

        pnl = Decimal(str(round(pnl_draw.gauss(20, 200), 2)))
        if position >= 5:
            pnl -= Decimal(280)

        opened = opened_base + timedelta(minutes=position * 20)
        duration = shape.randint(60, 3000)
        session.add(
            Trade(
                id=uuid7(),
                user_id=user.id,
                account_id=account.id,
                instrument_id=instrument.id,
                origin_execution_id=f"fill-{index}",
                direction=Direction.LONG,
                status=TradeStatus.CLOSED,
                opened_at=opened,
                closed_at=opened + timedelta(seconds=duration),
                duration_seconds=duration,
                quantity_opened=Decimal(1),
                quantity_closed=Decimal(1),
                max_position_size=Decimal(shape.randint(1, 4)),
                avg_entry_price=Decimal("5010.00"),
                avg_exit_price=Decimal("5012.00"),
                net_pnl=pnl,
                gross_pnl=pnl,
                session_date=session_date,
                entry_hour=9 + position // 2,
                entry_weekday=session_date.isoweekday(),
            )
        )
    await session.flush()
    return {"user_id": user.id, "account_id": account.id}


def use_case(session: AsyncSession) -> DetectPatterns:
    return DetectPatterns(
        repository=SqlAlchemyPatternRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )


async def test_a_scan_stores_every_tested_pattern(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    """Not only the survivors.

    Storing just the findings would make the table a record of each scan's luckiest
    result, and would make "this leak recurred for four months" unfalsifiable — the
    months it did not recur would be missing.
    """
    outcome = await use_case(session).execute(user_id=seeded["user_id"], config=FAST)

    stored = (
        await session.execute(
            select(func.count())
            .select_from(DetectedPattern)
            .where(DetectedPattern.user_id == seeded["user_id"])
        )
    ).scalar_one()

    assert stored == outcome.report.tests_performed
    assert stored > len(outcome.report.findings)

    insignificant = (
        await session.execute(
            select(func.count())
            .select_from(DetectedPattern)
            .where(
                DetectedPattern.user_id == seeded["user_id"],
                DetectedPattern.is_significant.is_(False),
            )
        )
    ).scalar_one()
    assert insignificant > 0


async def test_the_injected_leak_is_stored_as_significant(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    await use_case(session).execute(user_id=seeded["user_id"], config=FAST)

    row = (
        await session.execute(
            select(DetectedPattern).where(
                DetectedPattern.user_id == seeded["user_id"],
                DetectedPattern.pattern_kind == "overtrading",
            )
        )
    ).scalar_one()

    assert row.is_significant
    assert row.polarity == "leak"
    assert row.p_value is not None and row.p_value < Decimal("0.05")
    assert row.effect_size is not None
    assert row.sample_size > 0
    # Thirty sessions is enough span to annualise from.
    assert row.estimated_annual_impact is not None
    assert row.estimated_annual_impact < 0


async def test_rescanning_replaces_rather_than_accumulates(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    await use_case(session).execute(user_id=seeded["user_id"], config=FAST)
    first = (
        await session.execute(
            select(func.count())
            .select_from(DetectedPattern)
            .where(DetectedPattern.user_id == seeded["user_id"])
        )
    ).scalar_one()

    await use_case(session).execute(user_id=seeded["user_id"], config=FAST)
    second = (
        await session.execute(
            select(func.count())
            .select_from(DetectedPattern)
            .where(DetectedPattern.user_id == seeded["user_id"])
        )
    ).scalar_one()

    assert first == second


async def test_decimal_statistics_survive_the_numeric_columns(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    outcome = await use_case(session).execute(user_id=seeded["user_id"], config=FAST)

    rows = {
        row.pattern_kind: row
        for row in await SqlAlchemyPatternRepository(session).list_patterns(seeded["user_id"])
    }
    for behaviour in outcome.report.behaviours:
        if behaviour.comparison is None:
            continue
        stored = rows[behaviour.kind]
        expected = behaviour.comparison.adjusted_p_value or behaviour.comparison.p_value
        assert expected is not None
        # NUMERIC(18, 8): equal to the column's scale, not merely close.
        assert stored.p_value == expected.quantize(Decimal("0.00000001"))


async def test_patterns_are_listed_worst_first(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    await use_case(session).execute(user_id=seeded["user_id"], config=FAST)
    rows = await SqlAlchemyPatternRepository(session).list_patterns(seeded["user_id"])

    assert rows
    assert rows[0].is_significant
    impacts = [
        row.estimated_annual_impact
        for row in rows
        if row.is_significant and row.estimated_annual_impact is not None
    ]
    assert impacts == sorted(impacts)


async def test_accepting_a_cluster_labels_the_trades(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    """The scan proposes; only an explicit accept applies a label."""
    repository = SqlAlchemyPatternRepository(session)
    trades = (
        (
            await session.execute(
                select(Trade).where(Trade.user_id == seeded["user_id"]).limit(3)
            )
        )
        .scalars()
        .all()
    )

    updated = await repository.apply_setup(
        seeded["user_id"],
        setup_name="Late session fade",
        assignments=[
            {"trade_id": str(trade.id), "confidence": Decimal("0.82")} for trade in trades
        ],
    )
    await session.flush()

    assert updated == 3
    setup = (
        await session.execute(
            select(Setup).where(
                Setup.user_id == seeded["user_id"], Setup.name == "Late session fade"
            )
        )
    ).scalar_one()

    for trade in trades:
        await session.refresh(trade)
        assert trade.setup_id == setup.id
        assert trade.setup_source == "auto"
        assert trade.setup_confidence == Decimal("0.82000000")


async def test_accepting_the_same_setup_twice_reuses_it(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    repository = SqlAlchemyPatternRepository(session)
    trade = (
        await session.execute(select(Trade).where(Trade.user_id == seeded["user_id"]).limit(1))
    ).scalar_one()
    assignment = [{"trade_id": str(trade.id), "confidence": Decimal("0.9")}]

    await repository.apply_setup(seeded["user_id"], setup_name="Fade", assignments=assignment)
    await repository.apply_setup(seeded["user_id"], setup_name="Fade", assignments=assignment)
    await session.flush()

    count = (
        await session.execute(
            select(func.count())
            .select_from(Setup)
            .where(Setup.user_id == seeded["user_id"], Setup.name == "Fade")
        )
    ).scalar_one()
    assert count == 1
