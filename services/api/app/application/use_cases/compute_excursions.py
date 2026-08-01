"""Compute MAE/MFE for trades and store them.

This is the use case that turns on `edge_ratio` — the one metric milestone 3 had to
report as undefined. It answers the question no amount of fill data can: how far did
price run in favour before the exit, and how close did the trade come to being stopped?

Two rules govern it:

**A trade whose bars are missing is skipped, not zeroed.** An excursion of zero is a
claim that price never moved, which is never true. Trades without coverage are counted
and reported so a thin market-data backfill is visible rather than silently producing
flattering edge ratios.

**Partial coverage is recorded as such.** When the bar window does not span the whole
holding period, the excursion is a lower bound. It is stored with a flag, and the
analytics engine can then decline to aggregate it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from app.core.logging import get_logger
from app.domain.common.enums import Direction, Timeframe
from app.domain.marketdata.bars import BarSeries, compute_excursion
from app.domain.marketdata.replay import choose_timeframe
from app.domain.trading.risk import r_multiple

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TradeForExcursion:
    """The minimum a trade must expose to have its excursions measured."""

    trade_id: UUID
    instrument_id: UUID
    instrument_symbol: str
    direction: Direction
    entry_price: Decimal
    quantity: Decimal
    opened_at: datetime
    closed_at: datetime
    duration_seconds: int | None
    point_value: Decimal
    planned_risk_amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ExcursionUpdate:
    """What to write back to the trade row."""

    trade_id: UUID
    mae_price: Decimal
    mfe_price: Decimal
    mae_amount: Decimal
    mfe_amount: Decimal
    mae_r: Decimal | None
    mfe_r: Decimal | None
    edge_ratio: Decimal | None
    bars_observed: int
    is_partial: bool


class BarReader(Protocol):
    async def load_at(
        self,
        instrument_id: UUID,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
    ) -> BarSeries: ...


class ExcursionWriter(Protocol):
    async def apply_excursions(self, updates: list[ExcursionUpdate]) -> int: ...


class TradeReader(Protocol):
    async def trades_needing_excursions(
        self, user_id: UUID, *, account_id: UUID | None = None, limit: int = 500
    ) -> list[TradeForExcursion]: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...


@dataclass
class ExcursionResult:
    trades_considered: int = 0
    trades_measured: int = 0
    trades_skipped_no_bars: int = 0
    trades_partial: int = 0
    skipped_instruments: set[str] = field(default_factory=set)

    @property
    def coverage_ratio(self) -> float:
        """Fraction of considered trades that got a measurement.

        Surfaced because a low ratio means the edge ratio and every excursion statistic
        describe a biased subset — typically the trades in liquid instruments during
        hours the backfill happened to cover.
        """
        if self.trades_considered == 0:
            return 0.0
        return self.trades_measured / self.trades_considered


class ComputeExcursions:
    """Measure MAE/MFE for trades that do not yet have them."""

    def __init__(
        self,
        *,
        trades: TradeReader,
        bars: BarReader,
        writer: ExcursionWriter,
        uow: UnitOfWork,
    ) -> None:
        self._trades = trades
        self._bars = bars
        self._writer = writer
        self._uow = uow

    async def execute(
        self, *, user_id: UUID, account_id: UUID | None = None, limit: int = 500
    ) -> ExcursionResult:
        candidates = await self._trades.trades_needing_excursions(
            user_id, account_id=account_id, limit=limit
        )
        result = ExcursionResult(trades_considered=len(candidates))
        if not candidates:
            return result

        updates: list[ExcursionUpdate] = []
        for trade in candidates:
            timeframe, _ = choose_timeframe(
                trade.duration_seconds
                or max(int((trade.closed_at - trade.opened_at).total_seconds()), 1)
            )
            # `load_at`, not `load_series` — the same distinction ADR 0016 records for the
            # replay endpoint, which was never applied here.
            #
            # `choose_timeframe` picks a resolution from the holding period, and anything
            # over fifteen minutes selects 2m or coarser. A feed that writes 1m therefore
            # has no row at the requested resolution, `load_series` returns an empty
            # series, and the trade is counted as `skipped_no_bars`. Measured on a
            # 1,403-trade history with minute bars for every session: 478 measured, 925
            # reported as having no bars, all 925 of them longer than fifteen minutes.
            #
            # The damage is in what that number then means. `coverage_ratio` is presented
            # as the honest caveat on the excursion statistics — a low ratio is documented
            # as "these describe a biased subset". It did, but not the subset the caption
            # names: the sample was silently filtered to trades shorter than a quarter of
            # an hour, and holding period correlates with almost everything the excursion
            # statistics are used to argue about.
            series = await self._bars.load_at(
                trade.instrument_id,
                timeframe,
                start=trade.opened_at,
                end=trade.closed_at,
                symbol=trade.instrument_symbol,
            )

            excursion = compute_excursion(
                series,
                direction=trade.direction,
                entry_price=trade.entry_price,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
            )
            if excursion is None:
                # No bars at all. Recording zero would assert price never moved.
                result.trades_skipped_no_bars += 1
                result.skipped_instruments.add(trade.instrument_symbol)
                continue

            mae_amount = excursion.mae_points * trade.point_value * trade.quantity
            mfe_amount = excursion.mfe_points * trade.point_value * trade.quantity

            updates.append(
                ExcursionUpdate(
                    trade_id=trade.trade_id,
                    mae_price=excursion.mae_price,
                    mfe_price=excursion.mfe_price,
                    mae_amount=mae_amount,
                    mfe_amount=mfe_amount,
                    mae_r=r_multiple(mae_amount, trade.planned_risk_amount),
                    mfe_r=r_multiple(mfe_amount, trade.planned_risk_amount),
                    edge_ratio=excursion.edge_ratio,
                    bars_observed=excursion.bars_observed,
                    is_partial=excursion.is_partial,
                )
            )
            if excursion.is_partial:
                result.trades_partial += 1

        if updates:
            result.trades_measured = await self._writer.apply_excursions(updates)
            await self._uow.commit()

        logger.info(
            "excursions.computed",
            user_id=str(user_id),
            considered=result.trades_considered,
            measured=result.trades_measured,
            skipped=result.trades_skipped_no_bars,
            partial=result.trades_partial,
            coverage=round(result.coverage_ratio, 3),
        )
        return result


def summarise(result: ExcursionResult) -> dict[str, Any]:
    return {
        "considered": result.trades_considered,
        "measured": result.trades_measured,
        "skipped_no_bars": result.trades_skipped_no_bars,
        "partial": result.trades_partial,
        "coverage_ratio": round(result.coverage_ratio, 4),
        "instruments_without_bars": sorted(result.skipped_instruments),
    }
