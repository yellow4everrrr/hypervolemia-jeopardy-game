"""Market bar storage and retrieval.

``market_bars`` is the only genuinely large table in the system — one instrument-minute
for one year is ~350k rows, and a hundred instruments over a decade is hundreds of
millions. Every access pattern here is designed around that:

* Reads are **range scans on the primary key prefix** ``(instrument_id, timeframe, ts)``,
  which is exactly what a replay window asks for.
* Writes are **batched upserts** — a backfill inserts thousands of bars per statement,
  and re-fetching an overlapping range converges rather than conflicting.
* Nothing loads a full instrument's history. Every method takes a bounded window.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domain.common.enums import Timeframe
from app.domain.marketdata.bars import Bar, BarSeries, resample
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.marketdata import MarketBar

logger = get_logger(__name__)

#: Rows per INSERT. Large enough that a day's one-minute bars go in one round trip,
#: small enough to stay well under Postgres' parameter limit.
WRITE_BATCH = 2_000


class SqlAlchemyBarRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_series(
        self,
        instrument_id: UUID,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
    ) -> BarSeries:
        """Bars in ``[start, end]`` — an index-only range scan on the primary key."""
        statement = (
            select(MarketBar)
            .where(
                MarketBar.instrument_id == instrument_id,
                MarketBar.timeframe == timeframe,
                MarketBar.ts >= start,
                MarketBar.ts <= end,
            )
            .order_by(MarketBar.ts)
        )
        rows = (await self._session.execute(statement)).scalars().all()

        resolved = symbol
        if resolved is None:
            resolved = (
                await self._session.execute(
                    select(Instrument.symbol).where(Instrument.id == instrument_id)
                )
            ).scalar_one_or_none() or str(instrument_id)

        return BarSeries(
            instrument_symbol=resolved,
            timeframe=timeframe,
            bars=tuple(
                Bar(
                    ts=row.ts,
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                )
                for row in rows
            ),
        )

    async def stored_timeframes(self, instrument_id: UUID) -> list[Timeframe]:
        """Which resolutions this instrument actually has bars at, finest first.

        Needed because a caller asking for 2m bars cannot otherwise tell the difference
        between "this instrument has no data" and "this instrument has minute bars that
        nobody aggregated". Those look identical — an empty series — and the replay chart
        rendered the second as the first for every trade in the demo history.
        """
        rows = (
            await self._session.execute(
                select(MarketBar.timeframe)
                .where(MarketBar.instrument_id == instrument_id)
                .distinct()
            )
        ).scalars().all()
        return sorted((Timeframe(row) for row in rows), key=lambda tf: tf.seconds)

    async def load_at(
        self,
        instrument_id: UUID,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
    ) -> BarSeries:
        """Bars at ``timeframe``, aggregating from a finer stored series when needed.

        The replay endpoint has always documented this behaviour — "anything coarser than
        the stored resolution is aggregated on the fly rather than requiring a second
        stored series" — and only implemented it for an explicit caller override. The
        *automatic* timeframe, chosen from trade duration, went straight to storage: a
        38-minute trade selects 2m, the ingest wrote 1m, and the endpoint returned
        ``bar_count: 0`` with no error. The chart drew an empty pane and the page looked
        like it had loaded.

        Aggregating from the **coarsest** stored series that is still finer than the
        target minimises the rows read; only when nothing finer exists does this return
        empty, which is then a true statement about the data rather than about the query.
        """
        direct = await self.load_series(
            instrument_id, timeframe, start=start, end=end, symbol=symbol
        )
        if direct.bars:
            return direct

        finer = [
            candidate
            for candidate in await self.stored_timeframes(instrument_id)
            if candidate.seconds < timeframe.seconds
        ]
        if not finer:
            return direct

        base = await self.load_series(
            instrument_id, finer[-1], start=start, end=end, symbol=symbol
        )
        if not base.bars:
            return direct

        logger.debug(
            "bars.aggregated",
            instrument_id=str(instrument_id),
            stored=finer[-1].value,
            served=timeframe.value,
            bars=len(base),
        )
        return resample(base, timeframe)

    async def upsert_bars(
        self,
        instrument_id: UUID,
        timeframe: Timeframe,
        bars: Sequence[Bar],
        *,
        source: str = "broker",
    ) -> int:
        """Insert or update bars, batched.

        Updates on conflict rather than ignoring: a late-arriving correction from the
        feed should replace the provisional bar, and the last bar of any fetch is often
        still forming when it is first written.
        """
        if not bars:
            return 0

        written = 0
        for start in range(0, len(bars), WRITE_BATCH):
            chunk = bars[start : start + WRITE_BATCH]
            statement = insert(MarketBar).values(
                [
                    {
                        "instrument_id": instrument_id,
                        "timeframe": timeframe,
                        "ts": bar.ts,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "source": source,
                    }
                    for bar in chunk
                ]
            )
            await self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=["instrument_id", "timeframe", "ts"],
                    set_={
                        column: getattr(statement.excluded, column)
                        for column in ("open", "high", "low", "close", "volume", "source")
                    },
                )
            )
            written += len(chunk)

        logger.info(
            "marketdata.bars_written",
            instrument_id=str(instrument_id),
            timeframe=timeframe.value,
            count=written,
        )
        return written

    async def coverage(
        self, instrument_id: UUID, timeframe: Timeframe
    ) -> tuple[datetime | None, datetime | None, int]:
        """Earliest bar, latest bar, and count.

        What a backfill consults to decide what it still needs, so that re-running one
        fetches the gap rather than the whole history again.
        """
        statement = select(
            func.min(MarketBar.ts), func.max(MarketBar.ts), func.count()
        ).where(
            MarketBar.instrument_id == instrument_id,
            MarketBar.timeframe == timeframe,
        )
        earliest, latest, count = (await self._session.execute(statement)).one()
        return earliest, latest, int(count or 0)

    async def missing_ranges(
        self,
        instrument_id: UUID,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        """Sub-ranges of ``[start, end]`` with no bars at all.

        Deliberately coarse: it finds ranges entirely absent, not every intrabar gap.
        Session closes and holidays produce legitimate gaps everywhere, and treating
        those as missing data would make a backfill chase bars that do not exist.
        """
        series = await self.load_series(instrument_id, timeframe, start=start, end=end)
        if not series:
            return [(start, end)]

        missing: list[tuple[datetime, datetime]] = []
        first, last = series.bars[0].ts, series.bars[-1].ts
        if first > start:
            missing.append((start, first))
        if last < end:
            missing.append((last, end))
        return missing

    async def latest_close(
        self, instrument_id: UUID, timeframe: Timeframe, *, at: datetime
    ) -> Decimal | None:
        """Most recent close at or before ``at`` — for marking an open position."""
        statement = (
            select(MarketBar.close)
            .where(
                MarketBar.instrument_id == instrument_id,
                MarketBar.timeframe == timeframe,
                MarketBar.ts <= at,
            )
            .order_by(MarketBar.ts.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()
