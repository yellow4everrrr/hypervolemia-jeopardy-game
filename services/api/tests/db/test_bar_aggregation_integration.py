"""Serving a timeframe nobody stored.

The replay window picks its resolution from the trade's duration — a 38-minute trade gets
2m candles. The ingest writes whatever the feed provides, typically 1m. Those two choices
are made independently and there is no reason they should agree, so the storage layer has
to bridge them.

It did not. ``load_series`` matched ``timeframe`` exactly, found no 2m rows, and returned
an empty series. The endpoint reported ``bar_count: 0`` and the chart drew an empty pane —
no error, no warning, nothing in a log. The replay screen was blank for every trade in a
year of history while the bars it needed sat in the same table one resolution down.

The failure is worth naming precisely: **an empty result was used to mean two different
things.** "No data for this instrument" and "data exists, at a resolution this query did
not ask for" are not the same statement, and only the first is a fact about the market.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.ids import uuid7
from app.domain.common.enums import AssetClass, Timeframe
from app.domain.marketdata.bars import Bar
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.repositories.bars import SqlAlchemyBarRepository

pytestmark = pytest.mark.asyncio

START = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


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
async def instrument_with_minute_bars(session: AsyncSession) -> Instrument:
    """One instrument with sixty 1m bars and nothing coarser."""
    instrument = Instrument(
        id=uuid7(),
        symbol=f"ES{uuid7().hex[:4].upper()}",
        root_symbol="ES",
        exchange="CME",
        asset_class=AssetClass.FUTURE,
        tick_size=Decimal("0.25"),
        tick_value=Decimal("12.50"),
        currency="USD",
        exchange_timezone="America/Chicago",
    )
    session.add(instrument)
    await session.flush()

    # A deterministic ramp: bar n opens at 5000+n and closes at 5000+n+0.5, so the
    # aggregate's open, high, low and close are all predictable by hand below.
    bars = [
        Bar(
            ts=START + timedelta(minutes=index),
            open=Decimal(5000 + index),
            high=Decimal(5000 + index) + Decimal("0.75"),
            low=Decimal(5000 + index) - Decimal("0.25"),
            close=Decimal(5000 + index) + Decimal("0.50"),
            volume=Decimal(100),
        )
        for index in range(60)
    ]
    await SqlAlchemyBarRepository(session).upsert_bars(
        instrument.id, Timeframe.M1, bars, source="test"
    )
    await session.flush()
    return instrument


async def test_a_stored_resolution_is_returned_directly(
    session: AsyncSession, instrument_with_minute_bars: Instrument
) -> None:
    series = await SqlAlchemyBarRepository(session).load_at(
        instrument_with_minute_bars.id,
        Timeframe.M1,
        start=START,
        end=START + timedelta(hours=1),
    )

    assert len(series) == 60
    assert series.timeframe is Timeframe.M1


async def test_a_coarser_timeframe_is_aggregated_rather_than_returned_empty(
    session: AsyncSession, instrument_with_minute_bars: Instrument
) -> None:
    """The bug. Nothing is stored at 5m; sixty 1m bars must become twelve 5m bars."""
    series = await SqlAlchemyBarRepository(session).load_at(
        instrument_with_minute_bars.id,
        Timeframe.M5,
        start=START,
        end=START + timedelta(hours=1),
    )

    assert len(series) == 12, "1m bars were not aggregated up to 5m"
    assert series.timeframe is Timeframe.M5


async def test_the_aggregate_is_a_correct_candle(
    session: AsyncSession, instrument_with_minute_bars: Instrument
) -> None:
    """Aggregating to the right *count* is not the same as aggregating correctly.

    A resample that took, say, the last bar's open would produce twelve candles and pass
    the test above while drawing a chart that never happened.
    """
    series = await SqlAlchemyBarRepository(session).load_at(
        instrument_with_minute_bars.id,
        Timeframe.M5,
        start=START,
        end=START + timedelta(hours=1),
    )

    first = series.bars[0]
    # Bars 0-4: opens 5000..5004, closes 5000.5..5004.5, highs +0.75, lows -0.25.
    assert first.open == Decimal(5000)
    assert first.high == Decimal("5004.75")
    assert first.low == Decimal("4999.75")
    assert first.close == Decimal("5004.50")
    assert first.volume == Decimal(500)


async def test_an_instrument_with_no_bars_stays_empty(session: AsyncSession) -> None:
    """The other half of the distinction.

    Aggregation must not invent a series where there is genuinely no data — an empty
    result has to remain possible, or the fix has simply moved the lie.
    """
    empty = Instrument(
        id=uuid7(),
        symbol=f"NQ{uuid7().hex[:4].upper()}",
        root_symbol="NQ",
        exchange="CME",
        asset_class=AssetClass.FUTURE,
        tick_size=Decimal("0.25"),
        tick_value=Decimal("5.00"),
        currency="USD",
        exchange_timezone="America/Chicago",
    )
    session.add(empty)
    await session.flush()

    series = await SqlAlchemyBarRepository(session).load_at(
        empty.id, Timeframe.M5, start=START, end=START + timedelta(hours=1)
    )

    assert len(series) == 0


async def test_a_finer_timeframe_than_anything_stored_stays_empty(
    session: AsyncSession, instrument_with_minute_bars: Instrument
) -> None:
    """Aggregation only goes coarser.

    Producing 15-second candles from minute bars would require inventing the path price
    took inside each minute, which is the one thing this whole subsystem refuses to do.
    """
    series = await SqlAlchemyBarRepository(session).load_at(
        instrument_with_minute_bars.id,
        Timeframe.S15,
        start=START,
        end=START + timedelta(hours=1),
    )

    assert len(series) == 0


async def test_stored_timeframes_reports_what_exists(
    session: AsyncSession, instrument_with_minute_bars: Instrument
) -> None:
    assert await SqlAlchemyBarRepository(session).stored_timeframes(
        instrument_with_minute_bars.id
    ) == [Timeframe.M1]
