"""Model persistence against a real database.

The engine is tested exhaustively without I/O. These cover what only Postgres can show:
that the skill interval survives ``NUMERIC`` round-tripping, that retraining appends
rather than overwrites, and — most importantly — that the database itself refuses a row
claiming to be both servable and refused.
"""

from __future__ import annotations

import os
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.use_cases.train_models import TrainModels
from app.core.ids import uuid7
from app.domain.common.enums import AccountType, AssetClass, Direction, TradeStatus
from app.infrastructure.db.models.ai import PredictionModel
from app.infrastructure.db.models.broker import Account
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade
from app.infrastructure.db.repositories.ml import SqlAlchemyModelRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    ),
]

BASE = datetime(2025, 2, 3, 14, 30, tzinfo=UTC)


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


async def seed(db: AsyncSession, *, sessions: int = 40, per_session: int = 4) -> tuple[UUID, UUID]:
    """A history long enough for walk-forward validation to actually run."""
    user = User(id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="ml@example.com")
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

    rng = random.Random(4242)
    day = 0
    made = 0
    while made < sessions:
        candidate = BASE + timedelta(days=day)
        day += 1
        if candidate.weekday() >= 5:
            continue
        made += 1
        for index in range(per_session):
            opened = candidate + timedelta(minutes=19 * index)
            won = rng.random() < 0.45
            db.add(
                Trade(
                    id=uuid7(),
                    user_id=user.id,
                    account_id=account.id,
                    instrument_id=instrument.id,
                    origin_execution_id=f"fill-{made}-{index}",
                    direction=Direction.LONG if rng.random() < 0.5 else Direction.SHORT,
                    status=TradeStatus.CLOSED,
                    opened_at=opened,
                    closed_at=opened + timedelta(minutes=6),
                    duration_seconds=360,
                    quantity_opened=Decimal(2),
                    quantity_closed=Decimal(2),
                    max_position_size=Decimal(rng.randint(1, 3)),
                    avg_entry_price=Decimal("5010.00"),
                    avg_exit_price=Decimal("5012.00"),
                    net_pnl=Decimal("130") if won else Decimal("-100"),
                    gross_pnl=Decimal("134") if won else Decimal("-96"),
                    commission=Decimal("2"),
                    fees=Decimal("2"),
                    realized_r=Decimal("1.3") if won else Decimal("-1.0"),
                    session_date=candidate.date(),
                    entry_hour=opened.hour,
                    entry_weekday=candidate.isoweekday(),
                )
            )
    await db.flush()
    return user.id, account.id


class TestPersistence:
    @pytest.mark.asyncio
    async def test_training_stores_a_row_for_every_head(self, session: AsyncSession) -> None:
        """Including the refused ones.

        A refused model is a result: the trader is owed the reason, and the next run
        needs a prior to be compared against.
        """
        user_id, _ = await seed(session)
        await TrainModels(
            repository=SqlAlchemyModelRepository(session),
            uow=SqlAlchemyUnitOfWork(session),
        ).execute(user_id=user_id)

        heads = (
            await session.execute(
                select(PredictionModel.head).where(PredictionModel.user_id == user_id)
            )
        ).scalars().all()

        assert sorted(heads) == ["expected_r", "win_probability"]

    @pytest.mark.asyncio
    async def test_a_refused_model_stores_its_reason(self, session: AsyncSession) -> None:
        """The seeded history is random, so neither head should establish skill."""
        user_id, _ = await seed(session)
        await TrainModels(
            repository=SqlAlchemyModelRepository(session),
            uow=SqlAlchemyUnitOfWork(session),
        ).execute(user_id=user_id)

        rows = (
            await session.execute(
                select(PredictionModel).where(PredictionModel.user_id == user_id)
            )
        ).scalars().all()

        assert rows
        for row in rows:
            assert row.is_deployable is False
            assert row.refusal

    @pytest.mark.asyncio
    async def test_retraining_appends_rather_than_overwriting(
        self, session: AsyncSession
    ) -> None:
        """"The model said 68% on the 4th" must stay answerable after a retrain."""
        user_id, _ = await seed(session)
        use_case = TrainModels(
            repository=SqlAlchemyModelRepository(session),
            uow=SqlAlchemyUnitOfWork(session),
        )
        await use_case.execute(user_id=user_id)
        await use_case.execute(user_id=user_id)

        count = (
            await session.execute(
                select(func.count())
                .select_from(PredictionModel)
                .where(
                    PredictionModel.user_id == user_id,
                    PredictionModel.head == "win_probability",
                )
            )
        ).scalar_one()

        assert count == 2

    @pytest.mark.asyncio
    async def test_the_skill_interval_survives_the_numeric_column(
        self, session: AsyncSession
    ) -> None:
        """A ``NUMERIC(18, 8)`` round trip must not move the bounds.

        The interval is what the deployability gate reads. A value that shifted in
        storage could make a stored decision disagree with the one that was made.
        """
        user_id, _ = await seed(session)
        repository = SqlAlchemyModelRepository(session)
        await TrainModels(repository=repository, uow=SqlAlchemyUnitOfWork(session)).execute(
            user_id=user_id
        )

        stored = await repository.latest_model(user_id, head="win_probability")
        assert stored is not None
        assert stored["skill"] is not None

        detail_skill = stored["detail"]["skill"]
        assert stored["skill_interval"] is not None
        scale = Decimal("0.00000001")
        assert Decimal(stored["skill_interval"][0]) == Decimal(
            detail_skill["interval"][0]
        ).quantize(scale)

    @pytest.mark.asyncio
    async def test_the_latest_model_is_the_newest_one(self, session: AsyncSession) -> None:
        user_id, _ = await seed(session)
        repository = SqlAlchemyModelRepository(session)
        use_case = TrainModels(repository=repository, uow=SqlAlchemyUnitOfWork(session))

        await use_case.execute(user_id=user_id)
        first = await repository.latest_model(user_id, head="win_probability")
        await use_case.execute(user_id=user_id)
        second = await repository.latest_model(user_id, head="win_probability")

        assert first is not None and second is not None
        assert second["trained_at"] >= first["trained_at"]


class TestTheDatabaseConstraint:
    """The invariant the whole milestone rests on, enforced below the application."""

    @pytest.mark.asyncio
    async def test_a_deployable_model_may_not_carry_a_refusal(
        self, session: AsyncSession
    ) -> None:
        user_id, _ = await seed(session, sessions=2, per_session=1)
        session.add(
            PredictionModel(
                id=uuid7(),
                user_id=user_id,
                head="win_probability",
                is_deployable=True,
                refusal="this should be impossible",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

    @pytest.mark.asyncio
    async def test_a_refused_model_must_say_why(self, session: AsyncSession) -> None:
        """A refusal with no reason is indistinguishable from a bug.

        Application code always sets one; the constraint means a backfill or a manual
        fix cannot produce a row that silently serves nothing and explains nothing.
        """
        user_id, _ = await seed(session, sessions=2, per_session=1)
        session.add(
            PredictionModel(
                id=uuid7(),
                user_id=user_id,
                head="expected_r",
                is_deployable=False,
                refusal=None,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
