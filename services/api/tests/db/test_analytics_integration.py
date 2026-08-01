"""Analytics against a real database.

The pure engine is tested exhaustively without I/O. These tests cover only what a real
database can: that ``NUMERIC`` round-trips as ``Decimal`` through the whole pipeline,
that the JSONB payload survives storage, that the metric upsert converges on
recomputation, and that the equity-curve hypertable accepts what the engine produces.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.analytics.engine import AnalyticsConfig
from app.application.use_cases.compute_metrics import ComputePerformanceMetrics
from app.core.ids import uuid7
from app.domain.common.enums import (
    AccountType,
    AssetClass,
    Direction,
    MetricScope,
    TradeStatus,
)
from app.infrastructure.db.models.analytics import (
    EquityCurvePoint,
    PerformanceMetric,
    RiskMetric,
)
from app.infrastructure.db.models.broker import Account
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade
from app.infrastructure.db.repositories.analytics import SqlAlchemyAnalyticsRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    ),
]

FAST = AnalyticsConfig(
    bootstrap_iterations=300,
    monte_carlo_iterations=300,
    segmentation_dimensions=("weekday", "direction"),
    test_significance=False,
)

BASE = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


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
    """A user with 40 closed trades on one account."""
    user = User(id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="a@example.com")
    account = Account(
        id=uuid7(),
        user_id=user.id,
        broker_account_id="acct-1",
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
    )
    session.add_all([user, account, instrument])
    await session.flush()

    pattern = ["150.25", "-100.50", "200.75", "-100.50", "-50.25", "300.00", "-100.50", "100.25"]
    for index in range(40):
        value = Decimal(pattern[index % len(pattern)])
        opened = BASE + timedelta(hours=index)
        session.add(
            Trade(
                id=uuid7(),
                user_id=user.id,
                account_id=account.id,
                instrument_id=instrument.id,
                origin_execution_id=f"fill-{index}",
                direction=Direction.LONG if index % 2 else Direction.SHORT,
                status=TradeStatus.CLOSED,
                opened_at=opened,
                closed_at=opened + timedelta(minutes=10),
                duration_seconds=600,
                quantity_opened=Decimal(1),
                quantity_closed=Decimal(1),
                max_position_size=Decimal(1),
                net_pnl=value,
                gross_pnl=value + Decimal("2.49"),
                commission=Decimal("1.29"),
                fees=Decimal("1.20"),
                realized_r=(value / Decimal(100)).quantize(Decimal("0.01")),
                session_date=opened.date(),
                entry_hour=opened.hour,
                entry_weekday=opened.isoweekday(),
            )
        )
    await session.flush()
    return {"user_id": user.id, "account_id": account.id}


def use_case_for(session: AsyncSession) -> ComputePerformanceMetrics:
    return ComputePerformanceMetrics(
        repository=SqlAlchemyAnalyticsRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )


# --- Reading --------------------------------------------------------------------


async def test_trades_load_as_decimals(session: AsyncSession, seeded: dict) -> None:
    """A float anywhere in this path would silently corrupt every statistic."""
    trades = await SqlAlchemyAnalyticsRepository(session).load_trades(seeded["user_id"])

    assert len(trades) == 40
    assert all(isinstance(trade.net_pnl, Decimal) for trade in trades)
    assert trades[0].net_pnl == Decimal("150.25")
    assert trades[0].instrument_root == "ES"


async def test_open_trades_are_excluded_from_analysis(
    session: AsyncSession, seeded: dict
) -> None:
    """Unrealized P&L must not move expectancy or drawdown."""
    session.add(
        Trade(
            id=uuid7(),
            user_id=seeded["user_id"],
            account_id=seeded["account_id"],
            instrument_id=(
                await session.execute(select(Instrument.id).limit(1))
            ).scalar_one(),
            origin_execution_id="open-1",
            direction=Direction.LONG,
            status=TradeStatus.OPEN,
            opened_at=BASE,
            quantity_opened=Decimal(1),
            max_position_size=Decimal(1),
            net_pnl=Decimal("99999"),
        )
    )
    await session.flush()

    trades = await SqlAlchemyAnalyticsRepository(session).load_trades(seeded["user_id"])
    assert len(trades) == 40


# --- Computing and persisting ---------------------------------------------------


async def test_metrics_are_computed_and_stored(
    session: AsyncSession, seeded: dict
) -> None:
    result = await use_case_for(session).execute(
        user_id=seeded["user_id"], account_id=seeded["account_id"], config=FAST
    )

    assert result.trades_analysed == 40
    assert result.metric_rows_written > 1  # overall plus one per segment

    overall = (
        await session.execute(
            select(PerformanceMetric).where(
                PerformanceMetric.user_id == seeded["user_id"],
                PerformanceMetric.scope == MetricScope.OVERALL,
            )
        )
    ).scalar_one()

    assert overall.sample_size == 40
    assert isinstance(overall.net_pnl, Decimal)
    assert overall.expectancy is not None
    # 40 trades of the 8-value pattern: five full cycles of +399.50 net.
    assert overall.net_pnl == Decimal("1997.50000000")


async def test_the_payload_survives_jsonb(session: AsyncSession, seeded: dict) -> None:
    """The stored payload is what the AI layer will read as its evidence base."""
    await use_case_for(session).execute(
        user_id=seeded["user_id"], account_id=seeded["account_id"], config=FAST
    )

    overall = (
        await session.execute(
            select(PerformanceMetric).where(
                PerformanceMetric.scope == MetricScope.OVERALL,
                PerformanceMetric.user_id == seeded["user_id"],
            )
        )
    ).scalar_one()

    metrics = overall.metrics
    assert metrics["sample"]["trades"] == 40
    assert isinstance(metrics["core"]["expectancy"]["value"], str)
    assert metrics["core"]["expectancy"]["interval"]["method"] == "bootstrap_percentile"
    # Decimals are stored as strings, so nothing became a float in transit.
    assert isinstance(metrics["pnl"]["net"], str)


async def test_recomputation_converges_rather_than_duplicating(
    session: AsyncSession, seeded: dict
) -> None:
    """Metrics are derived; recomputing must update in place."""
    await use_case_for(session).execute(
        user_id=seeded["user_id"], account_id=seeded["account_id"], config=FAST
    )
    first_count = await session.scalar(
        select(func.count()).select_from(PerformanceMetric).where(
            PerformanceMetric.user_id == seeded["user_id"]
        )
    )

    await use_case_for(session).execute(
        user_id=seeded["user_id"], account_id=seeded["account_id"], config=FAST
    )
    second_count = await session.scalar(
        select(func.count()).select_from(PerformanceMetric).where(
            PerformanceMetric.user_id == seeded["user_id"]
        )
    )

    assert first_count == second_count


async def test_equity_curve_is_persisted(session: AsyncSession, seeded: dict) -> None:
    result = await use_case_for(session).execute(
        user_id=seeded["user_id"], account_id=seeded["account_id"], config=FAST
    )
    assert result.equity_points_written == 40

    points = (
        await session.execute(
            select(EquityCurvePoint)
            .where(EquityCurvePoint.user_id == seeded["user_id"])
            .order_by(EquityCurvePoint.recorded_at)
        )
    ).scalars().all()

    assert len(points) == 40
    assert points[0].cumulative_net_pnl == Decimal("150.25000000")
    assert points[-1].cumulative_net_pnl == Decimal("1997.50000000")
    assert all(point.peak_equity is not None for point in points)


async def test_risk_metrics_store_their_assumptions(
    session: AsyncSession, seeded: dict
) -> None:
    """A forward-looking estimate without its assumptions cannot be interpreted."""
    await use_case_for(session).execute(
        user_id=seeded["user_id"], account_id=seeded["account_id"], config=FAST
    )

    risk = (
        await session.execute(
            select(RiskMetric).where(RiskMetric.user_id == seeded["user_id"])
        )
    ).scalar_one()

    assert risk.sample_size == 40
    assert risk.max_drawdown is not None
    assert "iid bootstrap" in risk.assumptions["resampling"]
    assert risk.simulation["iterations"] == 300


async def test_persist_can_be_skipped(session: AsyncSession, seeded: dict) -> None:
    """The interactive endpoint computes without writing."""
    result = await use_case_for(session).execute(
        user_id=seeded["user_id"], account_id=seeded["account_id"], config=FAST, persist=False
    )

    assert result.trades_analysed == 40
    assert result.metric_rows_written == 0
    count = await session.scalar(
        select(func.count()).select_from(PerformanceMetric).where(
            PerformanceMetric.user_id == seeded["user_id"]
        )
    )
    assert count == 0


async def test_a_user_with_no_trades_produces_an_empty_report(
    session: AsyncSession,
) -> None:
    """The first thing a new user sees; it must not raise or write."""
    user = User(id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="new@example.com")
    session.add(user)
    await session.flush()

    result = await use_case_for(session).execute(user_id=user.id, config=FAST)

    assert result.was_noop
    assert result.report.sample_size == 0
    assert result.metric_rows_written == 0
