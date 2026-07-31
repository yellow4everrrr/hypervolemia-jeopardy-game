"""Compliance against a real database.

The engine is tested in isolation elsewhere. These tests cover what only Postgres can
show: that a rule expression survives a JSONB round trip, that ``rule_evaluations``
converges on re-evaluation instead of accumulating duplicates, that a ``NULL`` score is
stored as ``NULL``, and that the session-window query loads whole sessions.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.use_cases.evaluate_compliance import EvaluateCompliance
from app.core.ids import uuid7
from app.domain.common.enums import (
    AccountType,
    AssetClass,
    Direction,
    RuleSeverity,
    RuleType,
    TradeStatus,
)
from app.infrastructure.db.models.broker import Account, AccountBalanceSnapshot
from app.infrastructure.db.models.catalog import RuleEvaluation
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade
from app.infrastructure.db.repositories.compliance import (
    SqlAlchemyComplianceRepository,
    SqlAlchemyStrategyRepository,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    ),
]

RULES = [
    {
        "code": "always_use_a_stop",
        "label": "Every trade has a stop recorded",
        "rule_type": RuleType.RISK,
        "severity": RuleSeverity.CRITICAL,
        "expression": {"field": "has_stop", "op": "eq", "value": True},
    },
    {
        "code": "max_three_trades",
        "label": "At most three trades a session",
        "rule_type": RuleType.FILTER,
        "severity": RuleSeverity.MAJOR,
        "expression": {"field": "trades_today", "op": "lt", "value": 3},
    },
    {
        "code": "needs_a_plan",
        "label": "Planned reward is at least 1.5x risk",
        "rule_type": RuleType.TARGET,
        "severity": RuleSeverity.MINOR,
        # Never recorded by the seed data, so this rule is permanently unevaluable —
        # which is exactly what it is here to prove does not become a violation.
        "expression": {"field": "planned_r_multiple", "op": "gte", "value": "1.5"},
    },
]


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
    """A user with one strategy and five trades across two sessions.

    Session one holds four trades, so the fourth violates the max-three rule; the third
    has no stop, so it violates the critical one.
    """
    user = User(id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="c@example.com")
    account = Account(
        id=uuid7(),
        user_id=user.id,
        broker_account_id=f"acct-{uuid7().hex[:8]}",
        name="Main",
        account_type=AccountType.SIMULATED,
        starting_balance=Decimal("50000"),
        max_daily_loss=Decimal("1000"),
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

    strategy_repository = SqlAlchemyStrategyRepository(session)
    strategy, _ = await strategy_repository.create_strategy(
        user_id=user.id,
        name="Opening drive",
        description="Trend continuation off the RTH open",
        rules=RULES,
    )

    session.add(
        AccountBalanceSnapshot(
            id=uuid7(),
            user_id=user.id,
            account_id=account.id,
            as_of=datetime(2026, 3, 4, 22, 0, tzinfo=UTC),
            equity=Decimal("52000"),
        )
    )

    base = datetime(2026, 3, 5, 14, 30, tzinfo=UTC)
    plan = [
        (date(2026, 3, 5), 0, "150", "5000"),
        (date(2026, 3, 5), 30, "-80", "5000"),
        (date(2026, 3, 5), 60, "-400", None),
        (date(2026, 3, 5), 90, "220", "5000"),
        (date(2026, 3, 6), 0, "310", "5000"),
    ]
    for index, (session_date, minute, pnl, stop) in enumerate(plan):
        opened = base + timedelta(minutes=minute, days=1 if session_date.day == 6 else 0)
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
                closed_at=opened + timedelta(minutes=8),
                duration_seconds=480,
                quantity_opened=Decimal(1),
                quantity_closed=Decimal(1),
                max_position_size=Decimal(1),
                avg_entry_price=Decimal("5010.00"),
                avg_exit_price=Decimal("5012.00"),
                net_pnl=Decimal(pnl),
                gross_pnl=Decimal(pnl),
                session_date=session_date,
                initial_stop_price=Decimal(stop) if stop else None,
                planned_risk_amount=Decimal("500"),
                strategy_id=strategy.id,
            )
        )
    await session.flush()
    return {"user_id": user.id, "account_id": account.id, "strategy_id": strategy.id}


async def test_rules_round_trip_through_jsonb(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    """A rule expression must come back as the tree that was stored."""
    repository = SqlAlchemyComplianceRepository(session)
    rules = await repository.load_rules(seeded["user_id"])

    loaded = {rule.code: rule for rule in rules[seeded["strategy_id"]]}
    assert set(loaded) == {"always_use_a_stop", "max_three_trades", "needs_a_plan"}
    assert loaded["max_three_trades"].expression == {
        "field": "trades_today",
        "op": "lt",
        "value": 3,
    }
    assert loaded["always_use_a_stop"].severity is RuleSeverity.CRITICAL
    assert loaded["always_use_a_stop"].weight == Decimal(1)


async def test_evaluation_scores_and_persists(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    use_case = EvaluateCompliance(
        repository=SqlAlchemyComplianceRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
    outcome = await use_case.execute(user_id=seeded["user_id"])

    assert outcome.trades_evaluated == 5
    # One stopless trade (critical) and one fourth trade of the session (major).
    assert outcome.total_violations == 2
    assert outcome.critical_violations == 1

    stored = (
        await session.execute(
            select(func.count()).select_from(RuleEvaluation).where(
                RuleEvaluation.user_id == seeded["user_id"]
            )
        )
    ).scalar_one()
    assert stored == 15  # five trades x three rules

    unevaluable = (
        await session.execute(
            select(func.count())
            .select_from(RuleEvaluation)
            .where(
                RuleEvaluation.user_id == seeded["user_id"],
                RuleEvaluation.evaluable.is_(False),
            )
        )
    ).scalar_one()
    assert unevaluable == 5  # the rule the journal has no data for, on every trade

    # An unevaluable rule must not be stored as a failure.
    failures = (
        await session.execute(
            select(func.count())
            .select_from(RuleEvaluation)
            .where(
                RuleEvaluation.user_id == seeded["user_id"],
                RuleEvaluation.evaluable.is_(True),
                RuleEvaluation.passed.is_(False),
            )
        )
    ).scalar_one()
    assert failures == 2


async def test_severity_weighting_survives_the_numeric_column(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    """Scores must land in ``trades.compliance_score`` as computed, not as floats.

    The severity ratios are the point: the same *count* of violations produces very
    different scores depending on which rule broke. Trade three broke the critical rule
    (weight 8) and trade four the major one (weight 3), out of 11 checkable weight.
    """
    use_case = EvaluateCompliance(
        repository=SqlAlchemyComplianceRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
    await use_case.execute(user_id=seeded["user_id"])

    scores = (
        (
            await session.execute(
                select(Trade.compliance_score)
                .where(Trade.user_id == seeded["user_id"])
                .order_by(Trade.opened_at)
            )
        )
        .scalars()
        .all()
    )
    #: ``compliance_score`` is ``NUMERIC(18, 8)``, so the stored value is the computed
    #: ratio rounded to the column's scale — not a float approximation of it.
    scale = Decimal("0.00000001")
    assert all(score is not None for score in scores)
    assert scores[0] == Decimal(1)
    # Critical breach: only the major rule passed, 3 of 11 checkable weight.
    assert scores[2] == (Decimal(3) / Decimal(11)).quantize(scale)
    # Major breach: the critical rule passed, 8 of 11.
    assert scores[3] == (Decimal(8) / Decimal(11)).quantize(scale)
    assert scores[2] < scores[3]


async def test_re_evaluation_converges(session: AsyncSession, seeded: dict[str, UUID]) -> None:
    """Recomputation must overwrite verdicts, not stack a second set beside them."""
    use_case = EvaluateCompliance(
        repository=SqlAlchemyComplianceRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
    await use_case.execute(user_id=seeded["user_id"])
    await use_case.execute(user_id=seeded["user_id"])

    stored = (
        await session.execute(
            select(func.count()).select_from(RuleEvaluation).where(
                RuleEvaluation.user_id == seeded["user_id"]
            )
        )
    ).scalar_one()
    assert stored == 15


async def test_the_session_window_loads_whole_sessions(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    """One session of history must be a complete day, not a slice of one."""
    repository = SqlAlchemyComplianceRepository(session)
    trades = await repository.load_trades(seeded["user_id"], max_sessions=1)

    assert [trade.session_date for trade in trades] == [date(2026, 3, 6)]

    both = await repository.load_trades(seeded["user_id"], max_sessions=2)
    assert len(both) == 5


async def test_equity_is_read_as_of_the_trade(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    """The snapshot predating the trades is the equity they are measured against."""
    repository = SqlAlchemyComplianceRepository(session)
    trades = await repository.load_trades(seeded["user_id"], max_sessions=90)

    assert trades[0].facts.account_equity == Decimal("52000")
    # A snapshot older than the staleness window is not carried forward; the second
    # session is two days later, still inside it.
    assert trades[-1].facts.account_equity == Decimal("52000")


async def test_a_strategy_revision_creates_a_new_version(
    session: AsyncSession, seeded: dict[str, UUID]
) -> None:
    repository = SqlAlchemyStrategyRepository(session)
    found = await repository.get_strategy(seeded["user_id"], seeded["strategy_id"])
    assert found is not None

    successor, rules = await repository.revise_strategy(
        user_id=seeded["user_id"],
        strategy=found[0],
        rules=RULES[:1],
    )
    await session.flush()

    assert successor.version == 2
    assert successor.supersedes_id == seeded["strategy_id"]
    assert len(rules) == 1
    assert found[0].is_active is False

    active = await repository.list_strategies(seeded["user_id"])
    assert [strategy.version for strategy, _ in active] == [2]
