"""Position-state repository — persists resumable FIFO reconstruction state.

Without this, every sync would have to replay a trader's entire fill history to know
where their open lots sit. At 100k fills that is seconds of work every few minutes,
per account. Storing the open lots plus a pointer to the in-flight trade turns an
incremental sync into O(new fills).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.core.money import ZERO, weighted_average
from app.domain.common.enums import ExecutionRole, TradeStatus
from app.domain.trading.matching import PositionKey, PositionState
from app.domain.trading.trade import ReconstructedTrade, TradeLeg
from app.infrastructure.db.mappers import lots_from_json, lots_to_json
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Position, Trade, TradeExecution


class SqlAlchemyPositionStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, account_id: UUID) -> dict[PositionKey, PositionState]:
        """Rehydrate open positions, including the in-flight trade and its legs."""
        stmt = (
            select(Position, Instrument.symbol)
            .join(Instrument, Instrument.id == Position.instrument_id)
            .where(Position.account_id == account_id, Position.signed_quantity != 0)
        )
        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return {}

        trade_ids = [row.open_trade_id for row, _ in rows if row.open_trade_id is not None]
        open_trades = await self._load_open_trades(trade_ids)

        state: dict[PositionKey, PositionState] = {}
        for position, symbol in rows:
            trade = open_trades.get(position.open_trade_id) if position.open_trade_id else None
            if trade is None:
                # A position without a resumable trade would silently start a fresh
                # round turn mid-position. Skipping it forces a full rebuild for this
                # instrument instead, which is slower but correct.
                continue
            state[(str(account_id), symbol)] = PositionState(
                lots=lots_from_json(position.open_lots), open_trade=trade
            )
        return state

    async def save(self, account_id: UUID, state: dict[PositionKey, PositionState]) -> None:
        """Replace the account's position rows with the engine's closing state."""
        symbols = [symbol for _, symbol in state]
        instrument_ids = await self._instrument_ids(symbols)

        # Positions that closed during this run must be cleared, not left stale.
        keep = [instrument_ids[symbol] for symbol in symbols if symbol in instrument_ids]
        clear = delete(Position).where(Position.account_id == account_id)
        if keep:
            clear = clear.where(Position.instrument_id.not_in(keep))
        await self._session.execute(clear)

        for (_, symbol), position_state in state.items():
            instrument_id = instrument_ids.get(symbol)
            if instrument_id is None or position_state.open_trade is None:
                continue
            trade = position_state.open_trade
            open_lots = position_state.lots
            avg_price = weighted_average([(lot.price, lot.quantity) for lot in open_lots])
            signed_quantity = sum((lot.quantity for lot in open_lots), start=ZERO) * (
                trade.direction.signed_multiplier
            )

            trade_id = await self._resolve_trade_id(account_id, trade.origin_execution_id)
            values = {
                "id": uuid7(),
                "user_id": await self._user_id_for_account(account_id),
                "account_id": account_id,
                "instrument_id": instrument_id,
                "signed_quantity": signed_quantity,
                "avg_price": avg_price,
                "open_trade_id": trade_id,
                "open_lots": lots_to_json(open_lots),
                "last_execution_at": trade.last_activity_at,
            }
            stmt = insert(Position).values(values)
            await self._session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_positions_account_instrument",
                    set_={
                        key: getattr(stmt.excluded, key)
                        for key in (
                            "signed_quantity",
                            "avg_price",
                            "open_trade_id",
                            "open_lots",
                            "last_execution_at",
                        )
                    },
                )
            )

    # --- internals -------------------------------------------------------------

    async def _instrument_ids(self, symbols: list[str]) -> dict[str, UUID]:
        if not symbols:
            return {}
        stmt = select(Instrument.symbol, Instrument.id).where(Instrument.symbol.in_(symbols))
        return dict((await self._session.execute(stmt)).tuples().all())

    async def _user_id_for_account(self, account_id: UUID) -> UUID:
        from app.infrastructure.db.models.broker import Account

        stmt = select(Account.user_id).where(Account.id == account_id)
        return (await self._session.execute(stmt)).scalar_one()

    async def _resolve_trade_id(self, account_id: UUID, origin_execution_id: str) -> UUID | None:
        stmt = select(Trade.id).where(
            Trade.account_id == account_id, Trade.origin_execution_id == origin_execution_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _load_open_trades(self, trade_ids: list[UUID]) -> dict[UUID, ReconstructedTrade]:
        if not trade_ids:
            return {}

        stmt = (
            select(Trade, Instrument)
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .where(Trade.id.in_(trade_ids), Trade.status == TradeStatus.OPEN)
        )
        trade_rows = (await self._session.execute(stmt)).all()
        if not trade_rows:
            return {}

        legs_stmt = (
            select(TradeExecution)
            .where(TradeExecution.trade_id.in_([row.id for row, _ in trade_rows]))
            .order_by(TradeExecution.leg_index)
        )
        legs_by_trade: dict[UUID, list[TradeExecution]] = {}
        for leg in (await self._session.execute(legs_stmt)).scalars():
            legs_by_trade.setdefault(leg.trade_id, []).append(leg)

        # Broker fill ids are needed to rebuild domain legs, which key on them.
        from app.infrastructure.db.models.trading import Execution as ExecutionRow

        execution_ids = {leg.execution_id for legs in legs_by_trade.values() for leg in legs}
        broker_ids: dict[UUID, str] = {}
        if execution_ids:
            id_stmt = select(ExecutionRow.id, ExecutionRow.broker_execution_id).where(
                ExecutionRow.id.in_(execution_ids)
            )
            broker_ids = dict((await self._session.execute(id_stmt)).tuples().all())

        result: dict[UUID, ReconstructedTrade] = {}
        for trade, instrument in trade_rows:
            legs = tuple(
                TradeLeg(
                    execution_external_id=broker_ids.get(leg.execution_id, str(leg.execution_id)),
                    role=ExecutionRole(leg.role),
                    quantity=leg.quantity,
                    price=leg.price,
                    executed_at=leg.executed_at,
                    commission=leg.allocated_commission,
                    fees=leg.allocated_fees,
                    realized_points=leg.realized_points,
                )
                for leg in legs_by_trade.get(trade.id, [])
            )
            if not legs:
                continue
            result[trade.id] = ReconstructedTrade(
                account_key=str(trade.account_id),
                instrument_symbol=instrument.symbol,
                direction=trade.direction,
                status=TradeStatus.OPEN,
                opened_at=trade.opened_at,
                legs=legs,
                realized_points=trade.realized_points or Decimal(0),
                max_position_size=trade.max_position_size,
                point_value=instrument.tick_value / instrument.tick_size,
                tick_size=instrument.tick_size,
                currency=instrument.currency,
                origin_execution_id=trade.origin_execution_id,
            )
        return result
