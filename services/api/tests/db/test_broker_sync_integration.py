"""Broker sync against a real database, driven by a mocked Tradovate API.

The seam is deliberately placed at HTTP: everything from ``httpx`` inwards is the real
code path — the client, the four-way entity join, the mapper, the sync use case, the
ingestion pipeline, FIFO reconstruction, and every repository — with only Tradovate's
responses faked. That exercises the parts that unit tests with in-memory fakes cannot:
the cursor really persisting to JSONB, the instrument upsert really conflicting, and a
fill really becoming a trade row.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.ids import uuid7
from app.domain.common.enums import AccountType, Direction, SyncStatus, TradeStatus
from app.infrastructure.brokers.tradovate.auth import TradovateCredentials
from app.infrastructure.brokers.tradovate.factory import build_client, build_sync
from app.infrastructure.db.models.broker import Account, BrokerConnection, SyncRun
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    ),
]

CREDENTIALS = TradovateCredentials(
    username="trader", password="pw", app_id="Ledgerline", app_version="1.0",
    cid="1", secret="s",
)

# A round turn: buy 2 at 5000, sell 2 at 5004 → +8 points → $400 gross on ES.
FILL_TIME = datetime(2026, 3, 5, 15, 0, tzinfo=UTC)
EXIT_TIME = datetime(2026, 3, 5, 15, 20, tzinfo=UTC)


def tradovate_api(fills: list[dict[str, Any]]) -> httpx.MockTransport:
    """A Tradovate stand-in that answers the endpoints a sync actually calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path.endswith("/auth/accesstokenrequest"):
            return httpx.Response(
                200,
                json={
                    "accessToken": "tok",
                    "mdAccessToken": "md",
                    "expirationTime": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                    "userId": 4242,
                    "name": "trader",
                },
            )
        if path.endswith("/fill/list"):
            return httpx.Response(200, json=fills)
        if path.endswith("/order/items"):
            return httpx.Response(
                200, json=[{"id": 700, "accountId": 25, "contractId": 560901, "action": "Buy"}]
            )
        if path.endswith("/contract/items"):
            return httpx.Response(
                200, json=[{"id": 560901, "name": "ESM6", "contractMaturityId": 900}]
            )
        if path.endswith("/contractMaturity/items"):
            return httpx.Response(200, json=[{"id": 900, "productId": 10}])
        if path.endswith("/product/items"):
            # Sent as raw text so the JSON numbers stay Decimal end to end.
            return httpx.Response(
                200,
                text=(
                    '[{"id":10,"name":"ES","valuePerPoint":50,"tickSize":0.25,'
                    '"productType":"Futures","exchangeId":1}]'
                ),
                headers={"Content-Type": "application/json"},
            )
        if path.endswith("/fillFee/items"):
            return httpx.Response(
                200,
                text=(
                    '[{"id":1001,"commission":1.29,"exchangeFee":1.18,"nfaFee":0.02},'
                    '{"id":1002,"commission":1.29,"exchangeFee":1.18,"nfaFee":0.02}]'
                ),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404, json={"errorText": f"unexpected endpoint {path}"})

    return httpx.MockTransport(handler)


def fill_payload(fill_id: int, action: str, price: str, when: datetime) -> dict[str, Any]:
    return {
        "id": fill_id,
        "orderId": 700,
        "contractId": 560901,
        "timestamp": when.isoformat(),
        "action": action,
        "qty": 2,
        "price": float(price),
        "active": True,
    }


ROUND_TURN = [
    fill_payload(1001, "Buy", "5000.00", FILL_TIME),
    fill_payload(1002, "Sell", "5004.00", EXIT_TIME),
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
async def linked(session: AsyncSession) -> dict[str, UUID]:
    """A user with a Tradovate connection and the broker account already linked."""
    user = User(id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="t@example.com")
    connection = BrokerConnection(
        id=uuid7(),
        user_id=user.id,
        broker="tradovate",
        label="Demo",
        environment="demo",
        external_user_id="4242",
    )
    account = Account(
        id=uuid7(),
        user_id=user.id,
        broker_connection_id=connection.id,
        broker_account_id="25",
        name="Demo Account",
        account_type=AccountType.SIMULATED,
    )
    session.add_all([user, connection, account])
    await session.flush()
    return {"user_id": user.id, "connection_id": connection.id, "account_id": account.id}


def sync_for(session: AsyncSession, linked: dict[str, UUID], fills: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    client = build_client(CREDENTIALS, "demo", transport=tradovate_api(fills))
    return build_sync(
        session, client, user_id=linked["user_id"], connection_id=linked["connection_id"]
    )


# --- End to end -----------------------------------------------------------------


async def test_fills_become_a_trade(session: AsyncSession, linked: dict) -> None:
    """The whole point of the milestone: broker fills in, journalled trade out."""
    outcome = await sync_for(session, linked, ROUND_TURN).execute(
        user_id=linked["user_id"], connection_id=linked["connection_id"]
    )

    assert outcome.status is SyncStatus.SUCCEEDED
    assert outcome.executions_ingested == 2
    assert outcome.trades_written == 1

    trade = (
        await session.execute(select(Trade).where(Trade.user_id == linked["user_id"]))
    ).scalar_one()

    assert trade.direction is Direction.LONG
    assert trade.status is TradeStatus.CLOSED
    assert trade.quantity_opened == Decimal("2.00000000")
    assert trade.avg_entry_price == Decimal("5000.00000000")
    assert trade.avg_exit_price == Decimal("5004.00000000")
    # 4 points x $50 x 2 contracts.
    assert trade.gross_pnl == Decimal("400.00000000")
    # Two fills at $1.29 commission and $1.20 of fees each.
    assert trade.commission == Decimal("2.58000000")
    assert trade.fees == Decimal("2.40000000")
    assert trade.net_pnl == Decimal("395.02000000")


async def test_prices_survive_the_whole_pipeline_as_decimals(
    session: AsyncSession, linked: dict
) -> None:
    """From Tradovate's JSON doubles to a NUMERIC column, with no float in between."""
    fills = [
        fill_payload(1001, "Buy", "5000.25", FILL_TIME),
        fill_payload(1002, "Sell", "5004.75", EXIT_TIME),
    ]
    await sync_for(session, linked, fills).execute(
        user_id=linked["user_id"], connection_id=linked["connection_id"]
    )

    trade = (
        await session.execute(select(Trade).where(Trade.user_id == linked["user_id"]))
    ).scalar_one()
    assert trade.avg_entry_price == Decimal("5000.25")
    assert trade.avg_exit_price == Decimal("5004.75")
    assert trade.gross_pnl == Decimal("450.00000000")  # 4.5 points x $50 x 2


async def test_instrument_is_created_from_broker_reference_data(
    session: AsyncSession, linked: dict
) -> None:
    """A contract nobody seeded must not block the sync."""
    await sync_for(session, linked, ROUND_TURN).execute(
        user_id=linked["user_id"], connection_id=linked["connection_id"]
    )

    instrument = (
        await session.execute(select(Instrument).where(Instrument.symbol == "ESM6"))
    ).scalar_one()

    assert instrument.tick_size == Decimal("0.25000000")
    assert instrument.tick_value == Decimal("12.50000000")  # 50 x 0.25
    assert instrument.root_symbol == "ES"
    assert instrument.exchange_timezone == "America/Chicago"


async def test_cursor_persists_and_a_resync_is_a_noop(
    session: AsyncSession, linked: dict
) -> None:
    """Re-running a sync must converge, not duplicate."""
    first = await sync_for(session, linked, ROUND_TURN).execute(
        user_id=linked["user_id"], connection_id=linked["connection_id"]
    )
    assert first.cursor_after == 1002

    second = await sync_for(session, linked, ROUND_TURN).execute(
        user_id=linked["user_id"], connection_id=linked["connection_id"]
    )

    assert second.executions_fetched == 0
    assert second.cursor_before == 1002

    trade_count = await session.scalar(
        select(func.count()).select_from(Trade).where(Trade.user_id == linked["user_id"])
    )
    assert trade_count == 1


async def test_sync_run_is_recorded(session: AsyncSession, linked: dict) -> None:
    await sync_for(session, linked, ROUND_TURN).execute(
        user_id=linked["user_id"], connection_id=linked["connection_id"], trigger="manual"
    )

    run = (
        await session.execute(
            select(SyncRun).where(SyncRun.broker_connection_id == linked["connection_id"])
        )
    ).scalar_one()

    assert run.status is SyncStatus.SUCCEEDED
    assert run.trigger == "manual"
    assert run.executions_ingested == 2
    assert run.finished_at is not None
    assert run.detail["cursor_after"] == 1002


async def test_unlinked_broker_account_holds_the_cursor_back(
    session: AsyncSession, linked: dict
) -> None:
    """Fills for an account the user has not linked must be retried, never dropped."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/order/items"):
            # The order points at broker account 99, which is not linked here.
            return httpx.Response(
                200, json=[{"id": 700, "accountId": 99, "contractId": 560901, "action": "Buy"}]
            )
        return tradovate_api(ROUND_TURN).handler(request)  # type: ignore[attr-defined]

    client = build_client(CREDENTIALS, "demo", transport=httpx.MockTransport(handler))
    use_case = build_sync(
        session, client, user_id=linked["user_id"], connection_id=linked["connection_id"]
    )

    outcome = await use_case.execute(
        user_id=linked["user_id"], connection_id=linked["connection_id"]
    )

    assert outcome.status is SyncStatus.PARTIAL
    assert outcome.unknown_accounts == {"99"}
    assert outcome.cursor_after == 1000, "cursor advanced past fills that were never ingested"

    trade_count = await session.scalar(
        select(func.count()).select_from(Trade).where(Trade.user_id == linked["user_id"])
    )
    assert trade_count == 0
