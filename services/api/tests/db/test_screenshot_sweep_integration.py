"""Which trades the screenshot sweep offers to capture.

The sweep is capped — it takes the newest N uncaptured trades — and that cap is only
safe if the candidates it returns are trades that can actually produce an image.

The first version selected every uncaptured closed trade. On a year of demo data that is
1,447 trades against however much market data has been backfilled, which was one day. Two
consecutive sweeps each loaded 200 windows, rendered nothing, and finished with the same
1,443 trades outstanding. Nothing errored: every one of those trades legitimately had no
bars, the use case reported `skipped` for each, and the job succeeded. But because the
ordering is by recency and the cap is fixed, the sweep returned *the same uncapturable
page every time* — so a trade below the cap whose bars arrived later would never be looked
at again. The docstring claiming the sweep self-heals was describing an intention.

The correction has a trap in it, and this file exists mostly for the trap. Filtering to
trades with a bar inside the trade's own span is the obvious fix and it is wrong: the
window a capture renders is much wider than the trade — 120 bars of setup before entry,
60 after the exit — so a two-minute trade in a quiet stretch can hold no bar at all and
still render six good frames. That filter cut the demo from four captured trades to two,
silently, with the sweep still reporting success.

So the clause pads by the widest lead and trail `build_window` can produce. It is a bound
rather than a prediction: it keeps a superset of what can capture, which is the property
that lets it discard thousands of trades without ever discarding a capturable one.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.ids import uuid7
from app.domain.common.enums import (
    AccountType,
    AssetClass,
    Direction,
    Timeframe,
    TradeStatus,
)
from app.domain.marketdata.bars import Bar
from app.domain.marketdata.replay import max_window_padding
from app.infrastructure.db.models.broker import Account
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade
from app.infrastructure.db.repositories.bars import SqlAlchemyBarRepository
from app.infrastructure.db.repositories.screenshots import SqlAlchemyScreenshotRepository

pytestmark = pytest.mark.asyncio

#: Where the bars are. Everything else is positioned relative to this.
COVERED = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
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


class World:
    """A user, an instrument with one hour of 1m bars, and a way to add trades."""

    def __init__(self, session: AsyncSession, user_id: UUID, instrument_id: UUID) -> None:
        self._session = session
        self._account_id: UUID
        self.user_id = user_id
        self.instrument_id = instrument_id
        self.named: dict[str, UUID] = {}

    async def trade(self, name: str, *, opened_at: datetime, minutes: int = 10) -> UUID:
        trade_id = uuid7()
        self._session.add(
            Trade(
                id=trade_id,
                user_id=self.user_id,
                account_id=self._account_id,
                instrument_id=self.instrument_id,
                origin_execution_id=f"fill-{name}",
                direction=Direction.LONG,
                status=TradeStatus.CLOSED,
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=minutes),
                duration_seconds=minutes * 60,
                quantity_opened=Decimal(1),
                quantity_closed=Decimal(1),
                max_position_size=Decimal(1),
                net_pnl=Decimal("125.00"),
                gross_pnl=Decimal("127.49"),
                session_date=opened_at.date(),
                entry_hour=opened_at.hour,
                entry_weekday=opened_at.isoweekday(),
            )
        )
        await self._session.flush()
        self.named[name] = trade_id
        return trade_id


@pytest_asyncio.fixture
async def world(session: AsyncSession) -> World:
    user = User(id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="s@example.com")
    account = Account(
        id=uuid7(),
        user_id=user.id,
        broker_account_id="acct-shots",
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

    await SqlAlchemyBarRepository(session).upsert_bars(
        instrument.id,
        Timeframe.M1,
        [
            Bar(
                ts=COVERED + timedelta(minutes=index),
                open=Decimal(5000 + index),
                high=Decimal(5000 + index) + Decimal("0.75"),
                low=Decimal(5000 + index) - Decimal("0.25"),
                close=Decimal(5000 + index) + Decimal("0.50"),
                volume=Decimal(100),
            )
            for index in range(60)
        ],
        source="test",
    )
    await session.flush()

    built = World(session, user.id, instrument.id)
    built._account_id = account.id
    return built


async def test_a_trade_inside_the_bars_is_a_candidate(world: World) -> None:
    await world.trade("covered", opened_at=COVERED + timedelta(minutes=10))

    candidates = await SqlAlchemyScreenshotRepository(world._session).trades_needing_capture(
        world.user_id, limit=50
    )

    assert candidates == [world.named["covered"]]


async def test_a_trade_light_years_from_any_bar_is_not_a_candidate(world: World) -> None:
    """The whole point: uncapturable trades must not consume the cap."""
    await world.trade("covered", opened_at=COVERED + timedelta(minutes=10))
    await world.trade("ancient", opened_at=COVERED - timedelta(days=300))
    await world.trade("distant_future", opened_at=COVERED + timedelta(days=300))

    candidates = await SqlAlchemyScreenshotRepository(world._session).trades_needing_capture(
        world.user_id, limit=50
    )

    assert candidates == [world.named["covered"]]


async def test_a_trade_near_the_bars_but_not_over_them_is_still_a_candidate(
    world: World,
) -> None:
    """**The regression that a span-matched filter causes.**

    This trade contains no bar during its own ten minutes — the bars ended hours before
    it opened. Its *window*, though, reaches back 120 bars for the setup, so a capture
    would render real candles. A filter matching the trade's span excludes it, four
    demo trades become two, and nothing reports a problem.
    """
    lead, _ = max_window_padding()
    assert lead > timedelta(hours=2), "the padding must exceed the offset used below"

    await world.trade("just_after_the_bars", opened_at=COVERED + timedelta(hours=2))

    candidates = await SqlAlchemyScreenshotRepository(world._session).trades_needing_capture(
        world.user_id, limit=50
    )

    assert world.named["just_after_the_bars"] in candidates


async def test_an_already_captured_trade_drops_out(world: World) -> None:
    """Idempotence: the sweep runs after every sync and must converge, not repeat."""
    repository = SqlAlchemyScreenshotRepository(world._session)
    trade_id = await world.trade("covered", opened_at=COVERED + timedelta(minutes=10))
    assert await repository.trades_needing_capture(world.user_id, limit=50) == [trade_id]

    from app.application.use_cases.capture_screenshots import Frame
    from app.domain.common.enums import ScreenshotKind

    await repository.record(
        world.user_id,
        trade_id,
        Frame(
            kind=ScreenshotKind.ENTRY,
            timeframe=Timeframe.M1,
            png=b"\x89PNG\r\n\x1a\n",
            width=960,
            height=540,
            bars=12,
            captured_at=COVERED,
        ),
        storage_key="k/entry.png",
    )
    await world._session.flush()

    assert await repository.trades_needing_capture(world.user_id, limit=50) == []


async def test_the_cap_is_spent_on_the_newest_capturable_trades(world: World) -> None:
    """Recency ordering survives the filter — a fresh trade is never queued behind old ones."""
    await world.trade("older", opened_at=COVERED + timedelta(minutes=5))
    await world.trade("newer", opened_at=COVERED + timedelta(minutes=40))
    await world.trade("uncapturable", opened_at=COVERED + timedelta(days=300))

    candidates = await SqlAlchemyScreenshotRepository(world._session).trades_needing_capture(
        world.user_id, limit=1
    )

    assert candidates == [world.named["newer"]]


async def test_another_users_trade_is_never_a_candidate(world: World) -> None:
    await world.trade("mine", opened_at=COVERED + timedelta(minutes=10))

    stranger = uuid7()
    candidates = await SqlAlchemyScreenshotRepository(world._session).trades_needing_capture(
        stranger, limit=50
    )

    assert candidates == []
