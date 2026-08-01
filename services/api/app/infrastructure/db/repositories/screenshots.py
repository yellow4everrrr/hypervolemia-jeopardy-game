"""Loading what a screenshot capture needs, and recording what it produced."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.capture_screenshots import Frame
from app.core.ids import uuid7
from app.domain.common.enums import ExecutionRole, Timeframe
from app.domain.marketdata.bars import BarSeries
from app.domain.marketdata.replay import (
    LegInput,
    ReplayWindow,
    build_markers,
    build_window,
    max_window_padding,
)
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.journal import Screenshot
from app.infrastructure.db.models.marketdata import MarketBar
from app.infrastructure.db.models.trading import Trade, TradeExecution
from app.infrastructure.db.repositories.bars import SqlAlchemyBarRepository


class SqlAlchemyScreenshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._bars = SqlAlchemyBarRepository(session)

    async def load_window(self, user_id: UUID, trade_id: UUID) -> ReplayWindow | None:
        """The same window the replay endpoint builds, from the same function.

        Reusing `build_window` rather than choosing a range here is what keeps a
        screenshot and its replay showing the same chart. Two implementations would
        eventually disagree about how much context a trade gets, and the screenshot — the
        one that is stored and looked at months later — would be the wrong one.
        """
        row = (
            await self._session.execute(
                select(Trade, Instrument)
                .join(Instrument, Instrument.id == Trade.instrument_id)
                .where(Trade.id == trade_id, Trade.user_id == user_id)
            )
        ).first()
        if row is None:
            return None
        trade, instrument = row

        window = build_window(
            instrument_symbol=instrument.symbol,
            opened_at=trade.opened_at,
            closed_at=trade.closed_at,
            duration_seconds=trade.duration_seconds,
        )

        legs = (
            (
                await self._session.execute(
                    select(TradeExecution)
                    .where(TradeExecution.trade_id == trade_id)
                    .order_by(TradeExecution.leg_index)
                )
            )
            .scalars()
            .all()
        )
        markers = build_markers(
            direction=trade.direction,
            legs=[
                LegInput(
                    at=leg.executed_at,
                    price=leg.price,
                    quantity=leg.quantity,
                    is_entry=leg.role is ExecutionRole.ENTRY,
                )
                for leg in legs
            ],
            stop_price=trade.initial_stop_price,
            target_price=trade.initial_target_price,
            mae_price=trade.mae_price,
            mfe_price=trade.mfe_price,
        )
        return ReplayWindow(
            instrument_symbol=window.instrument_symbol,
            primary_timeframe=window.primary_timeframe,
            higher_timeframe=window.higher_timeframe,
            window_start=window.window_start,
            window_end=window.window_end,
            trade_start=window.trade_start,
            trade_end=window.trade_end,
            bars_before=window.bars_before,
            bars_after=window.bars_after,
            markers=tuple(markers),
        )

    async def instrument_for(self, user_id: UUID, trade_id: UUID) -> UUID | None:
        return (
            await self._session.execute(
                select(Trade.instrument_id).where(
                    Trade.id == trade_id, Trade.user_id == user_id
                )
            )
        ).scalar_one_or_none()

    async def load_bars(
        self,
        instrument_id: UUID,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
    ) -> BarSeries:
        # `load_at`, so a window asking for a resolution nobody stored aggregates from a
        # finer one instead of returning an empty series — the defect that left the replay
        # chart blank for a year of history (ADR 0016).
        return await self._bars.load_at(instrument_id, timeframe, start=start, end=end)

    async def record(
        self, user_id: UUID, trade_id: UUID, frame: Frame, *, storage_key: str
    ) -> None:
        """Upsert on ``(trade, kind, timeframe)``, matching the table's unique constraint.

        Upsert rather than insert because capture runs from an at-least-once queue and
        because a trade can be re-captured after its bars are backfilled. A re-run should
        replace the image with the better one, not fail on the constraint or leave the
        stale key in place.
        """
        statement = insert(Screenshot).values(
            id=uuid7(),
            user_id=user_id,
            trade_id=trade_id,
            kind=frame.kind,
            timeframe=frame.timeframe,
            storage_key=storage_key,
            content_type="image/png",
            width=frame.width,
            height=frame.height,
            byte_size=len(frame.png),
            captured_at=frame.captured_at,
            source="auto",
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_screenshots_trade_kind_tf",
                set_={
                    column: getattr(statement.excluded, column)
                    for column in (
                        "storage_key",
                        "width",
                        "height",
                        "byte_size",
                        "captured_at",
                    )
                },
            )
        )

    async def trades_needing_capture(self, user_id: UUID, *, limit: int) -> list[UUID]:
        """Closed trades that have no screenshots yet **and have bars to draw**, newest first.

        A sweep rather than a fan-out of one job per trade. Three reasons, and the third
        is the one that matters:

        1. A backfill importing six months produces thousands of trades; a job each would
           bury every other kind in the queue behind them.
        2. It is idempotent by construction — a re-run simply finds fewer trades.
        3. **It self-heals.** A trade whose bars had not been backfilled when it was first
           seen captures nothing and stays in this result, so the next sweep picks it up.
           A per-trade job fired once at import time would have missed it permanently.

        The bar-coverage clause is what makes (3) true rather than merely intended.
        Without it the candidate set is every uncaptured closed trade, most of which
        cannot be captured because market data was never backfilled that far — and since
        the ordering is newest-first and the sweep is capped, it would return the same
        uncapturable page on every run. Measured on a year of demo trades: 1,447 closed
        trades against a single day of bars, so two consecutive sweeps each loaded 200
        windows, captured nothing, and left any trade past the cap permanently
        unreachable even after its bars arrived.

        **The clause pads the trade's span rather than matching it**, by the widest lead
        and trail `build_window` can produce. The window a capture actually renders is
        wider than the trade — 120 bars of setup before entry, 60 after exit — so a trade
        with no bar during its own two minutes can still render six perfectly good
        frames. Filtering on the bare span cut this demo's captures from four trades to
        two. `max_window_padding` is a bound, not a reproduction of the timeframe choice:
        it keeps a superset of what can capture, so this can only ever discard trades
        that would have skipped anyway, and the in-sweep skip stays the real answer.

        Deliberately not filtered by timeframe: `load_bars` aggregates a coarse window
        from a finer stored series, so any resolution covering the span is enough.
        """
        lead, trail = max_window_padding()
        captured = select(Screenshot.trade_id).where(Screenshot.user_id == user_id)
        has_bars = (
            select(MarketBar.ts)
            .where(
                MarketBar.instrument_id == Trade.instrument_id,
                MarketBar.ts >= Trade.opened_at - lead,
                MarketBar.ts <= Trade.closed_at + trail,
            )
            .exists()
        )
        statement = (
            select(Trade.id)
            .where(
                Trade.user_id == user_id,
                Trade.closed_at.is_not(None),
                Trade.id.not_in(captured),
                has_bars,
            )
            .order_by(Trade.opened_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def list_for_trade(self, user_id: UUID, trade_id: UUID) -> list[Any]:
        return list(
            (
                await self._session.execute(
                    select(Screenshot)
                    .where(Screenshot.user_id == user_id, Screenshot.trade_id == trade_id)
                    .order_by(Screenshot.kind)
                )
            )
            .scalars()
            .all()
        )

    async def get(self, user_id: UUID, screenshot_id: UUID) -> Screenshot | None:
        return (
            await self._session.execute(
                select(Screenshot).where(
                    Screenshot.id == screenshot_id, Screenshot.user_id == user_id
                )
            )
        ).scalar_one_or_none()
