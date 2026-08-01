"""Reading trades that need excursions and writing the results back."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.compute_excursions import (
    ExcursionUpdate,
    TradeForExcursion,
)
from app.core.logging import get_logger
from app.domain.common.enums import TradeStatus
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade

logger = get_logger(__name__)


class SqlAlchemyExcursionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def trades_needing_excursions(
        self, user_id: UUID, *, account_id: UUID | None = None, limit: int = 500
    ) -> list[TradeForExcursion]:
        """Closed trades with an entry price but no measured excursion yet.

        Ordered newest first: recent trades are the ones a trader is reviewing, and
        recent bars are the ones most likely to be in the store.
        """
        statement = (
            select(Trade, Instrument)
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.closed_at.is_not(None),
                Trade.avg_entry_price.is_not(None),
                Trade.mae_price.is_(None),
            )
            .order_by(Trade.opened_at.desc())
            .limit(limit)
        )
        if account_id is not None:
            statement = statement.where(Trade.account_id == account_id)

        return [
            TradeForExcursion(
                trade_id=trade.id,
                instrument_id=instrument.id,
                instrument_symbol=instrument.symbol,
                direction=trade.direction,
                entry_price=trade.avg_entry_price,
                quantity=trade.max_position_size,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                duration_seconds=trade.duration_seconds,
                point_value=instrument.tick_value / instrument.tick_size,
                planned_risk_amount=trade.planned_risk_amount,
            )
            for trade, instrument in (await self._session.execute(statement)).all()
        ]

    async def apply_excursions(self, updates: list[ExcursionUpdate]) -> int:
        """Write measured excursions back, in one statement per column group.

        A CASE expression rather than a statement per trade: measuring a year of
        history produces thousands of updates, and one round trip each would dominate
        the runtime of the whole job.
        """
        if not updates:
            return 0

        ids = [item.trade_id for item in updates]
        await self._session.execute(
            update(Trade)
            .where(Trade.id.in_(ids))
            .values(
                mae_price=case(
                    {item.trade_id: item.mae_price for item in updates},
                    value=Trade.id,
                ),
                mfe_price=case(
                    {item.trade_id: item.mfe_price for item in updates},
                    value=Trade.id,
                ),
                mae_amount=case(
                    {item.trade_id: item.mae_amount for item in updates},
                    value=Trade.id,
                ),
                mfe_amount=case(
                    {item.trade_id: item.mfe_amount for item in updates},
                    value=Trade.id,
                ),
                mae_r=case(
                    {item.trade_id: item.mae_r for item in updates},
                    value=Trade.id,
                ),
                mfe_r=case(
                    {item.trade_id: item.mfe_r for item in updates},
                    value=Trade.id,
                ),
                edge_ratio=case(
                    {item.trade_id: item.edge_ratio for item in updates},
                    value=Trade.id,
                ),
            )
        )
        return len(updates)
