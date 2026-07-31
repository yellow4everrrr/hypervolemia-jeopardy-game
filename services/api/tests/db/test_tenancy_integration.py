"""Row-level security, proved against a real database.

**These tests must connect as a non-owner, non-superuser role or they prove nothing.**
That is not a detail — it is the entire finding this milestone produced. With the policies
created and ``FORCE ROW LEVEL SECURITY`` set, a connection as the migration role still
read every tenant's rows, because Postgres exempts superusers unconditionally and table
owners unless forced. A test written against the default connection would have passed
while the isolation was completely inert.

``SET LOCAL ROLE`` is what makes the check honest here: it drops the session to
``ledgerline_app`` for the transaction, which is neither owner nor superuser, exactly as
a deployed application connects.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.ids import uuid7
from app.domain.common.enums import AccountType, AssetClass, Direction, TradeStatus
from app.infrastructure.db.models.broker import Account
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade
from app.infrastructure.db.tenancy import (
    TENANT_TABLES,
    assert_rls_effective,
    clear_tenant,
    current_tenant,
    set_tenant,
)

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    ),
]

APP_ROLE = "ledgerline_app"


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


async def seed_two_tenants(db: AsyncSession) -> tuple[UUID, UUID]:
    """Two users, each with one trade. Seeded as the owning role, before dropping down."""
    instrument = Instrument(
        id=uuid7(),
        symbol=f"ES{uuid4().hex[:6].upper()}",
        root_symbol="ES",
        exchange="CME",
        asset_class=AssetClass.FUTURE,
        tick_size=Decimal("0.25"),
        tick_value=Decimal("12.50"),
        currency="USD",
        exchange_timezone="America/Chicago",
    )
    db.add(instrument)
    await db.flush()

    ids: list[UUID] = []
    for label in ("alice", "bob"):
        user = User(
            id=uuid7(), clerk_user_id=f"user_{label}_{uuid4().hex[:10]}", email=f"{label}@x.com"
        )
        db.add(user)
        await db.flush()
        account = Account(
            id=uuid7(),
            user_id=user.id,
            broker_account_id=f"acct-{uuid4().hex[:8]}",
            name=label,
            account_type=AccountType.SIMULATED,
        )
        db.add(account)
        await db.flush()

        opened = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
        db.add(
            Trade(
                id=uuid7(),
                user_id=user.id,
                account_id=account.id,
                instrument_id=instrument.id,
                origin_execution_id=f"fill-{label}-{uuid4().hex[:6]}",
                direction=Direction.LONG,
                status=TradeStatus.CLOSED,
                opened_at=opened,
                closed_at=opened + timedelta(minutes=5),
                duration_seconds=300,
                quantity_opened=Decimal(1),
                quantity_closed=Decimal(1),
                max_position_size=Decimal(1),
                avg_entry_price=Decimal("5000"),
                avg_exit_price=Decimal("5002"),
                net_pnl=Decimal("100"),
                gross_pnl=Decimal("100"),
                session_date=opened.date(),
            )
        )
        ids.append(user.id)
    await db.flush()
    return ids[0], ids[1]


async def become_app_role(db: AsyncSession) -> None:
    """Drop to the application role for the rest of the transaction.

    Without this every assertion below passes vacuously.
    """
    await db.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))


class TestIsolationIsReal:
    @pytest.mark.asyncio
    async def test_a_bound_session_sees_only_its_own_tenant(
        self, session: AsyncSession
    ) -> None:
        alice, bob = await seed_two_tenants(session)
        await become_app_role(session)
        await set_tenant(session, alice)

        owners = {
            str(trade.user_id) for trade in (await session.execute(select(Trade))).scalars()
        }

        assert owners == {str(alice)}
        assert str(bob) not in owners

    @pytest.mark.asyncio
    async def test_a_query_with_no_user_id_filter_still_cannot_cross_tenants(
        self, session: AsyncSession
    ) -> None:
        """The failure this whole mechanism exists for.

        A handler written without a ``user_id`` filter — the mistake a worker makes most
        easily, because there is no authenticated principal in scope to remind anyone —
        returns only the bound tenant's rows instead of everybody's.
        """
        _alice, bob = await seed_two_tenants(session)
        await become_app_role(session)
        await set_tenant(session, bob)

        rows = (await session.execute(text("SELECT user_id FROM trades"))).scalars().all()

        assert rows
        assert {str(value) for value in rows} == {str(bob)}

    @pytest.mark.asyncio
    async def test_an_unbound_session_sees_nothing(self, session: AsyncSession) -> None:
        """The correct failure direction.

        A path that forgets to bind a tenant returns empty rather than everything.
        Getting this backwards would make the whole mechanism worse than useless: it
        would look like it worked right up to the moment it did not.
        """
        await seed_two_tenants(session)
        await become_app_role(session)
        await clear_tenant(session)

        count = (await session.execute(text("SELECT count(*) FROM trades"))).scalar_one()

        assert count == 0

    @pytest.mark.asyncio
    async def test_a_write_cannot_be_attributed_to_another_tenant(
        self, session: AsyncSession
    ) -> None:
        """``WITH CHECK`` on the policy, not just ``USING``.

        Without it a bound session could *insert* rows owned by somebody else — read
        isolation with no write isolation, which is the more damaging half.
        """
        alice, bob = await seed_two_tenants(session)
        await become_app_role(session)
        await set_tenant(session, alice)

        with pytest.raises(ProgrammingError) as caught:
            await session.execute(
                text(
                    "INSERT INTO jobs (id, user_id, kind, state, payload, result, "
                    "run_after, attempts, max_attempts, created_at, updated_at) "
                    "VALUES (:id, :user_id, 'detect_patterns', 'pending', '{}'::jsonb, "
                    "'{}'::jsonb, now(), 0, 5, now(), now())"
                ),
                {"id": uuid7(), "user_id": bob},
            )

        assert "policy" in str(caught.value).lower()

    @pytest.mark.asyncio
    async def test_the_binding_is_scoped_to_the_transaction(
        self, session: AsyncSession
    ) -> None:
        """What makes this safe on a pooled connection.

        A session-level setting would survive back into the pool and bind the next
        request to the previous request's tenant — a cross-tenant leak caused by the
        isolation mechanism itself.
        """
        alice, _ = await seed_two_tenants(session)
        await set_tenant(session, alice)
        assert await current_tenant(session) == str(alice)

        await session.rollback()

        assert await current_tenant(session) is None


class TestTheGuardAgainstInertPolicies:
    @pytest.mark.asyncio
    async def test_it_reports_ineffective_for_a_superuser_connection(
        self, session: AsyncSession
    ) -> None:
        """The check must fail for the connection the test suite actually uses.

        This is the configuration that shipped policies which did nothing, so the guard
        has to detect exactly it. If this ever starts passing, the suite has stopped
        running as a superuser and the assertion below it needs revisiting.
        """
        is_super = (
            await session.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one()

        if not is_super:
            pytest.skip("suite is not running as a superuser; nothing to detect")

        assert await assert_rls_effective(session) is False

    @pytest.mark.asyncio
    async def test_it_reports_effective_for_the_application_role(
        self, session: AsyncSession
    ) -> None:
        await become_app_role(session)

        assert await assert_rls_effective(session) is True


class TestPolicyCoverage:
    @pytest.mark.asyncio
    async def test_every_tenant_table_has_the_policy_enabled_and_forced(
        self, session: AsyncSession
    ) -> None:
        """Enabled is not enough; unforced policies are inert for the owner.

        Checked per table rather than in aggregate so a failure names the table.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema() AND c.relname = ANY(:tables)
                    """
                ),
                {"tables": list(TENANT_TABLES)},
            )
        ).all()
        by_table = {row[0]: (row[1], row[2]) for row in rows}

        assert set(by_table) == set(TENANT_TABLES), (
            f"missing from the database: {set(TENANT_TABLES) - set(by_table)}"
        )
        for table, (enabled, forced) in by_table.items():
            assert enabled, f"{table} does not have row-level security enabled"
            assert forced, f"{table} does not force row-level security; the owner bypasses it"

    @pytest.mark.asyncio
    async def test_every_user_scoped_model_is_in_the_tenant_table_list(
        self, session: AsyncSession
    ) -> None:
        """Guards against a new tenant table shipping unprotected.

        Derived from the models rather than restated, so adding one and forgetting the
        policy fails here instead of in production.
        """
        from app.infrastructure.db.base import UserScopedMixin
        from app.infrastructure.db.models import Base

        scoped = {
            mapper.class_.__tablename__
            for mapper in Base.registry.mappers
            if issubclass(mapper.class_, UserScopedMixin)
        }

        assert scoped <= set(TENANT_TABLES), (
            f"user-scoped tables with no RLS policy: {scoped - set(TENANT_TABLES)}"
        )
