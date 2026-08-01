"""Report persistence and scheduling against a real database.

The builder is tested exhaustively without I/O. These cover what only Postgres can show:
that the scheduler is idempotent against the table's own unique constraint, that session
bounds are read without loading the history, and that a stored report keeps the evidence
it was built from rather than a pointer to data that will move.
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
from app.application.use_cases.generate_report import GenerateReport
from app.core.ids import uuid7
from app.domain.common.enums import (
    AccountType,
    AssetClass,
    Direction,
    ReportType,
    TradeStatus,
)
from app.infrastructure.db.models.analytics import Report
from app.infrastructure.db.models.broker import Account
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade
from app.infrastructure.db.repositories.reports import SqlAlchemyReportRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.reports.builder import BuildConfig
from app.reports.periods import period_containing

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    ),
]

FAST = BuildConfig(
    discovery=DiscoveryConfig(permutations=150, include_clustering=False),
    comparison_permutations=150,
)


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


async def seed(
    db: AsyncSession, *, months: tuple[int, ...] = (3, 4), per_month: int = 30
) -> tuple[UUID, UUID]:
    user = User(id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="rep@example.com")
    instrument = Instrument(
        id=uuid7(),
        symbol=f"ES{uuid7().hex[:6]}",
        root_symbol="ES",
        exchange="CME",
        asset_class=AssetClass.FUTURE,
        tick_size=Decimal("0.25"),
        tick_value=Decimal("12.50"),
        currency="USD",
        exchange_timezone="America/Chicago",
    )
    account = Account(
        id=uuid7(),
        user_id=user.id,
        broker_account_id=f"acct-{uuid7().hex[:8]}",
        name="Primary",
        account_type=AccountType.SIMULATED,
    )
    db.add_all([user, instrument, account])
    await db.flush()

    rng = random.Random(808)
    for month in months:
        for index in range(per_month):
            session_date = date(2026, month, (index % 20) + 1)
            opened = datetime(
                2026, month, (index % 20) + 1, 14, 30, tzinfo=UTC
            ) + timedelta(minutes=11 * index)
            won = rng.random() < 0.45
            db.add(
                Trade(
                    id=uuid7(),
                    user_id=user.id,
                    account_id=account.id,
                    instrument_id=instrument.id,
                    origin_execution_id=f"fill-{month}-{index}",
                    direction=Direction.LONG if rng.random() < 0.5 else Direction.SHORT,
                    status=TradeStatus.CLOSED,
                    opened_at=opened,
                    closed_at=opened + timedelta(minutes=7),
                    duration_seconds=420,
                    quantity_opened=Decimal(2),
                    quantity_closed=Decimal(2),
                    max_position_size=Decimal(rng.randint(1, 3)),
                    avg_entry_price=Decimal("5010.00"),
                    avg_exit_price=Decimal("5012.00"),
                    net_pnl=Decimal("140") if won else Decimal("-110"),
                    gross_pnl=Decimal("144") if won else Decimal("-106"),
                    commission=Decimal("2"),
                    fees=Decimal("2"),
                    realized_r=Decimal("1.25") if won else Decimal("-1.0"),
                    session_date=session_date,
                    entry_hour=opened.hour,
                    entry_weekday=session_date.isoweekday(),
                )
            )
    await db.flush()
    return user.id, account.id


def use_case(session: AsyncSession) -> GenerateReport:
    return GenerateReport(
        repository=SqlAlchemyReportRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
        config=FAST,
    )


class TestSessionBounds:
    @pytest.mark.asyncio
    async def test_bounds_come_from_the_database_not_from_loading_trades(
        self, session: AsyncSession
    ) -> None:
        """On a multi-year history this runs on every scheduler pass; loading the whole
        table to read two dates would not survive contact with real data."""
        user_id, _ = await seed(session)

        earliest, latest = await SqlAlchemyReportRepository(session).session_bounds(user_id)

        assert earliest == date(2026, 3, 1)
        assert latest == date(2026, 4, 20)

    @pytest.mark.asyncio
    async def test_a_trader_with_no_trades_has_no_bounds(self, session: AsyncSession) -> None:
        user = User(id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="e@example.com")
        session.add(user)
        await session.flush()

        assert await SqlAlchemyReportRepository(session).session_bounds(user.id) == (
            None,
            None,
        )


class TestScheduling:
    @pytest.mark.asyncio
    async def test_a_run_generates_the_completed_months(self, session: AsyncSession) -> None:
        """Only the months the history spans.

        The seeded trades run March to 20 April, so May gets no report even though it
        finished before ``today`` — a trader who has stopped trading should not accrue
        an empty report every month forever.
        """
        user_id, _ = await seed(session)

        outcome = await use_case(session).run_schedule(
            user_id=user_id, today=date(2026, 6, 1), types=(ReportType.MONTHLY,)
        )

        starts = {item["period"]["start"] for item in outcome.generated}
        assert starts == {"2026-03-01", "2026-04-01"}

    @pytest.mark.asyncio
    async def test_a_second_run_generates_nothing(self, session: AsyncSession) -> None:
        """Idempotency, keyed against the table's own unique constraint. The scheduler
        is re-run after every failure, and a retry that duplicated a month would leave
        two of them disagreeing."""
        user_id, _ = await seed(session)
        runner = use_case(session)
        kwargs = {
            "user_id": user_id,
            "today": date(2026, 6, 1),
            "types": (ReportType.MONTHLY,),
        }

        first = await runner.run_schedule(**kwargs)  # type: ignore[arg-type]
        second = await runner.run_schedule(**kwargs)  # type: ignore[arg-type]

        assert first.generated
        assert second.generated == []
        assert second.skipped_existing >= len(first.generated)

    @pytest.mark.asyncio
    async def test_the_incomplete_current_month_is_never_generated(
        self, session: AsyncSession
    ) -> None:
        user_id, _ = await seed(session)

        outcome = await use_case(session).run_schedule(
            user_id=user_id, today=date(2026, 4, 15), types=(ReportType.MONTHLY,)
        )

        starts = {item["period"]["start"] for item in outcome.generated}
        assert "2026-04-01" not in starts

    @pytest.mark.asyncio
    async def test_a_batch_limit_defers_the_rest_and_says_so(
        self, session: AsyncSession
    ) -> None:
        user_id, _ = await seed(session)

        outcome = await use_case(session).run_schedule(
            user_id=user_id, today=date(2026, 6, 1), types=(ReportType.MONTHLY,), limit=1
        )

        assert len(outcome.generated) == 1
        assert outcome.deferred > 0
        assert "call again" in outcome.to_payload()["note"]

    @pytest.mark.asyncio
    async def test_reports_are_generated_oldest_first(self, session: AsyncSession) -> None:
        """A report records what was believed at a point in time; producing December
        before November inverts the one artefact that exists to preserve that."""
        user_id, _ = await seed(session)

        outcome = await use_case(session).run_schedule(
            user_id=user_id, today=date(2026, 6, 1), types=(ReportType.MONTHLY,)
        )
        starts = [item["period"]["start"] for item in outcome.generated]

        assert starts == sorted(starts)


class TestPersistence:
    @pytest.mark.asyncio
    async def test_a_report_stores_the_evidence_it_was_built_from(
        self, session: AsyncSession
    ) -> None:
        """Not a reference to live data. Re-running analytics next month would otherwise
        change last month's report, and a record whose contents move is not a record."""
        user_id, _ = await seed(session)
        period = period_containing(ReportType.MONTHLY, date(2026, 3, 15))

        await use_case(session).execute(user_id=user_id, period=period, persist=True)

        row = (
            await session.execute(select(Report).where(Report.user_id == user_id))
        ).scalars().one()

        assert row.metrics_snapshot
        assert row.metrics_snapshot["trades"] > 0
        assert row.findings["sections"]
        assert row.summary

    @pytest.mark.asyncio
    async def test_regenerating_appends_rather_than_overwriting(
        self, session: AsyncSession
    ) -> None:
        """"The March report said the overtrading cost $3,000" must stay answerable."""
        user_id, _ = await seed(session)
        period = period_containing(ReportType.MONTHLY, date(2026, 3, 15))
        runner = use_case(session)

        await runner.execute(user_id=user_id, period=period, persist=True)
        await runner.execute(user_id=user_id, period=period, persist=True)

        count = (
            await session.execute(
                select(func.count()).select_from(Report).where(Report.user_id == user_id)
            )
        ).scalar_one()

        assert count == 2

    @pytest.mark.asyncio
    async def test_the_period_boundary_is_stored_as_the_report_covers_it(
        self, session: AsyncSession
    ) -> None:
        user_id, _ = await seed(session)
        period = period_containing(ReportType.MONTHLY, date(2026, 3, 15))

        await use_case(session).execute(user_id=user_id, period=period, persist=True)
        row = (
            await session.execute(select(Report).where(Report.user_id == user_id))
        ).scalars().one()

        assert row.period_start == date(2026, 3, 1)
        assert row.period_end == date(2026, 3, 31)

    @pytest.mark.asyncio
    async def test_the_latest_report_is_the_most_recent_period(
        self, session: AsyncSession
    ) -> None:
        user_id, _ = await seed(session)
        runner = use_case(session)
        for month in (3, 4):
            await runner.execute(
                user_id=user_id,
                period=period_containing(ReportType.MONTHLY, date(2026, month, 15)),
                persist=True,
            )

        stored = await SqlAlchemyReportRepository(session).latest(
            user_id, report_type=ReportType.MONTHLY
        )

        assert stored is not None
        assert stored["period_start"] == "2026-04-01"

    @pytest.mark.asyncio
    async def test_the_listing_omits_the_full_evidence_base(
        self, session: AsyncSession
    ) -> None:
        """Enough to choose a report, without shipping every metric set in the list."""
        user_id, _ = await seed(session)
        await use_case(session).run_schedule(
            user_id=user_id, today=date(2026, 6, 1), types=(ReportType.MONTHLY,)
        )

        items = await SqlAlchemyReportRepository(session).listing(user_id)

        assert items
        assert all("report" not in item for item in items)
        assert all(item["summary"] for item in items)


class TestTheComparisonUsesRealNeighbours:
    @pytest.mark.asyncio
    async def test_aprils_report_compares_against_march(
        self, session: AsyncSession
    ) -> None:
        """Both periods come from the same loader, so they cannot be assembled by
        subtly different rules."""
        user_id, _ = await seed(session)
        period = period_containing(ReportType.MONTHLY, date(2026, 4, 15))

        report = await use_case(session).execute(
            user_id=user_id, period=period, persist=False
        )
        comparison = report.section("comparison")

        assert comparison is not None
        assert comparison.is_present
        assert comparison.payload["previous_period"]["start"] == "2026-03-01"

    @pytest.mark.asyncio
    async def test_the_first_month_has_nothing_to_compare_against(
        self, session: AsyncSession
    ) -> None:
        user_id, _ = await seed(session)
        period = period_containing(ReportType.MONTHLY, date(2026, 3, 15))

        report = await use_case(session).execute(
            user_id=user_id, period=period, persist=False
        )
        comparison = report.section("comparison")

        assert comparison is not None
        assert not comparison.is_present
        assert "first period with activity" in (comparison.withheld_reason or "")
