"""Translation between domain objects and database rows.

Kept in one place so that the domain stays free of persistence concerns and the
repositories stay free of trading logic. Anything that needs to know both — how a
reconstructed trade becomes columns, how a row becomes a domain object — lives here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.domain.trading.instrument import InstrumentSpec
from app.domain.trading.matching import OpenLot
from app.domain.trading.session import SessionCalendar, resolve_session
from app.domain.trading.trade import ReconstructedTrade
from app.infrastructure.db.models.instruments import Instrument


def spec_from_row(row: Instrument) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=row.symbol,
        exchange=row.exchange,
        asset_class=row.asset_class,
        currency=row.currency,
        tick_size=row.tick_size,
        tick_value=row.tick_value,
        exchange_timezone=row.exchange_timezone,
        price_precision=row.price_precision,
    )


def calendar_from_row(row: Instrument) -> SessionCalendar:
    from datetime import time

    hour, _, minute = row.session_rollover.partition(":")
    return SessionCalendar(
        timezone=row.exchange_timezone,
        rollover=time(int(hour), int(minute or 0)),
    )


def trade_to_columns(
    trade: ReconstructedTrade,
    *,
    user_id: UUID,
    account_id: UUID,
    instrument_id: UUID,
    calendar: SessionCalendar,
) -> dict[str, Any]:
    """Flatten a reconstructed trade into a row payload.

    Session attribution is resolved here, in the instrument's exchange timezone, and
    denormalised onto the row. Hour-of-day and weekday breakdowns then read a
    ``smallint`` instead of re-deriving a timezone across a million rows on every
    dashboard load.
    """
    session = resolve_session(trade.opened_at, calendar)

    return {
        "user_id": user_id,
        "account_id": account_id,
        "instrument_id": instrument_id,
        "origin_execution_id": trade.origin_execution_id,
        "direction": trade.direction,
        "status": trade.status,
        "opened_at": trade.opened_at,
        "closed_at": trade.closed_at,
        "duration_seconds": trade.duration_seconds,
        "quantity_opened": trade.quantity_opened,
        "quantity_closed": trade.quantity_closed,
        "max_position_size": trade.max_position_size,
        "avg_entry_price": trade.avg_entry_price,
        "avg_exit_price": trade.avg_exit_price,
        "realized_points": trade.realized_points,
        "points_per_contract": trade.points_per_contract,
        "ticks_per_contract": trade.ticks_per_contract,
        "gross_pnl": trade.gross_pnl,
        "commission": trade.commission,
        "fees": trade.fees,
        "net_pnl": trade.net_pnl,
        "scaled_in": trade.is_scaled_in,
        "scaled_out": trade.is_scaled_out,
        "session_date": session.session_date,
        "session_segment": session.segment,
        "entry_hour": session.hour_of_day,
        "entry_weekday": session.weekday,
    }


def trade_legs_to_rows(
    trade: ReconstructedTrade, *, trade_id: UUID, execution_ids: dict[str, UUID]
) -> list[dict[str, Any]]:
    """Build ``trade_executions`` rows, allocating quantity and costs per leg."""
    rows: list[dict[str, Any]] = []
    for index, leg in enumerate(trade.legs):
        execution_id = execution_ids.get(leg.execution_external_id)
        if execution_id is None:
            # The fill must already be persisted; a missing id means ingestion wrote
            # trades before executions, which would corrupt the audit chain.
            raise KeyError(
                f"execution {leg.execution_external_id!r} is not persisted; "
                "cannot link it to a trade"
            )
        rows.append(
            {
                "trade_id": trade_id,
                "execution_id": execution_id,
                "role": leg.role,
                "quantity": leg.quantity,
                "price": leg.price,
                "executed_at": leg.executed_at,
                "allocated_commission": leg.commission,
                "allocated_fees": leg.fees,
                "realized_points": leg.realized_points,
                "leg_index": index,
            }
        )
    return rows


def lots_to_json(lots: tuple[OpenLot, ...]) -> dict[str, Any]:
    return {
        "lots": [
            {
                "quantity": str(lot.quantity),
                "price": str(lot.price),
                "execution_external_id": lot.execution_external_id,
            }
            for lot in lots
        ]
    }


def lots_from_json(payload: dict[str, Any]) -> tuple[OpenLot, ...]:
    from decimal import Decimal

    return tuple(
        OpenLot(
            quantity=Decimal(entry["quantity"]),
            price=Decimal(entry["price"]),
            execution_external_id=entry["execution_external_id"],
        )
        for entry in payload.get("lots", [])
    )
