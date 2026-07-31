"""Trade repository — derived, rebuildable round turns."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.domain.common.enums import TradeStatus
from app.domain.trading.trade import ReconstructedTrade
from app.infrastructure.db.mappers import calendar_from_row, trade_legs_to_rows, trade_to_columns
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade as TradeRow
from app.infrastructure.db.models.trading import TradeExecution
from app.infrastructure.db.repositories.executions import SqlAlchemyExecutionRepository


class SqlAlchemyTradeRepository:
    """Persists reconstructed trades.

    Trades are upserted on ``(account_id, origin_execution_id)`` — the id of the fill
    that opened them. That key is stable across rebuilds, so re-running reconstruction
    over a day updates the same rows rather than creating a second copy of every trade.
    Human-supplied context (notes, emotion, manual setup, tags) lives in columns the
    upsert deliberately does not touch, so a rebuild never erases the trader's work.
    """

    #: Columns owned by the reconstruction engine. Everything else on ``trades`` is
    #: owned by the trader or by a later pipeline stage and is preserved on rebuild.
    _RECONSTRUCTED_COLUMNS = (
        "direction",
        "status",
        "opened_at",
        "closed_at",
        "duration_seconds",
        "quantity_opened",
        "quantity_closed",
        "max_position_size",
        "avg_entry_price",
        "avg_exit_price",
        "realized_points",
        "points_per_contract",
        "ticks_per_contract",
        "gross_pnl",
        "commission",
        "fees",
        "net_pnl",
        "scaled_in",
        "scaled_out",
        "session_date",
        "session_segment",
        "entry_hour",
        "entry_weekday",
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._executions = SqlAlchemyExecutionRepository(session)

    async def upsert_many(
        self, user_id: UUID, account_id: UUID, trades: Sequence[ReconstructedTrade]
    ) -> int:
        if not trades:
            return 0

        symbols = sorted({trade.instrument_symbol for trade in trades})
        stmt = select(Instrument).where(Instrument.symbol.in_(symbols))
        instrument_rows = {row.symbol: row for row in (await self._session.execute(stmt)).scalars()}
        missing = set(symbols) - set(instrument_rows)
        if missing:
            raise KeyError(f"unknown instruments: {sorted(missing)}")

        payloads: list[dict[str, Any]] = []
        for trade in trades:
            instrument = instrument_rows[trade.instrument_symbol]
            columns = trade_to_columns(
                trade,
                user_id=user_id,
                account_id=account_id,
                instrument_id=instrument.id,
                calendar=calendar_from_row(instrument),
            )
            columns["id"] = uuid7()
            payloads.append(columns)

        insert_stmt = insert(TradeRow).values(payloads)
        upsert = insert_stmt.on_conflict_do_update(
            constraint="uq_trades_account_origin_execution",
            set_={
                column: getattr(insert_stmt.excluded, column)
                for column in self._RECONSTRUCTED_COLUMNS
            },
        ).returning(TradeRow.id, TradeRow.origin_execution_id)

        written = {
            origin: trade_id for trade_id, origin in (await self._session.execute(upsert)).all()
        }

        await self._replace_legs(account_id, trades, written)
        return len(written)

    async def _replace_legs(
        self,
        account_id: UUID,
        trades: Sequence[ReconstructedTrade],
        trade_ids: dict[str, UUID],
    ) -> None:
        """Rewrite trade↔execution links.

        Delete-then-insert rather than upsert: a rebuild can *move* a fill from one
        trade to another (a corrected timestamp changes the ordering), and an upsert
        would leave the stale link behind, double-counting the fill.
        """
        ids = [trade_ids[trade.origin_execution_id] for trade in trades]
        await self._session.execute(delete(TradeExecution).where(TradeExecution.trade_id.in_(ids)))

        external_ids = sorted(
            {leg.execution_external_id for trade in trades for leg in trade.legs}
        )
        execution_ids = await self._executions.ids_by_broker_id(account_id, external_ids)

        rows: list[dict[str, Any]] = []
        for trade in trades:
            rows.extend(
                trade_legs_to_rows(
                    trade,
                    trade_id=trade_ids[trade.origin_execution_id],
                    execution_ids=execution_ids,
                )
            )
        if rows:
            await self._session.execute(insert(TradeExecution).values(rows))

    # --- Reads -----------------------------------------------------------------

    async def count(self, user_id: UUID) -> int:
        stmt = select(func.count()).select_from(TradeRow).where(TradeRow.user_id == user_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        status: TradeStatus | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[TradeRow]:
        """Blotter query. Served by ``ix_trades_user_opened_at`` and its siblings."""
        stmt = select(TradeRow).where(TradeRow.user_id == user_id)
        if account_id is not None:
            stmt = stmt.where(TradeRow.account_id == account_id)
        if status is not None:
            stmt = stmt.where(TradeRow.status == status)
        if session_from is not None:
            stmt = stmt.where(TradeRow.session_date >= session_from)
        if session_to is not None:
            stmt = stmt.where(TradeRow.session_date <= session_to)
        stmt = stmt.order_by(TradeRow.opened_at.desc()).limit(limit).offset(offset)
        return (await self._session.execute(stmt)).scalars().all()

    async def get(self, user_id: UUID, trade_id: UUID) -> TradeRow | None:
        stmt = select(TradeRow).where(TradeRow.user_id == user_id, TradeRow.id == trade_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()
