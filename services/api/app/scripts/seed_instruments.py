"""Seed contract specifications for commonly traded futures.

Tick values are the exchange's published contract specifications. They are seeded
rather than fetched because a wrong tick value silently scales every P&L figure by a
constant, and a broker API outage must never be able to cause that.

Idempotent: re-running updates the specs in place rather than duplicating them.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.core.ids import uuid7
from app.core.logging import configure_logging, get_logger
from app.domain.common.enums import AssetClass
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.session import dispose_engine, session_scope

logger = get_logger(__name__)

# (symbol, root, exchange, description, tick_size, tick_value, precision, timezone)
INSTRUMENTS: tuple[tuple[str, str, str, str, str, str, int, str], ...] = (
    ("ES", "ES", "CME", "E-mini S&P 500", "0.25", "12.50", 2, "America/Chicago"),
    ("MES", "MES", "CME", "Micro E-mini S&P 500", "0.25", "1.25", 2, "America/Chicago"),
    ("NQ", "NQ", "CME", "E-mini Nasdaq-100", "0.25", "5.00", 2, "America/Chicago"),
    ("MNQ", "MNQ", "CME", "Micro E-mini Nasdaq-100", "0.25", "0.50", 2, "America/Chicago"),
    ("YM", "YM", "CBOT", "E-mini Dow", "1.00", "5.00", 0, "America/Chicago"),
    ("MYM", "MYM", "CBOT", "Micro E-mini Dow", "1.00", "0.50", 0, "America/Chicago"),
    ("RTY", "RTY", "CME", "E-mini Russell 2000", "0.10", "5.00", 1, "America/Chicago"),
    ("M2K", "M2K", "CME", "Micro E-mini Russell 2000", "0.10", "0.50", 1, "America/Chicago"),
    ("CL", "CL", "NYMEX", "Crude Oil", "0.01", "10.00", 2, "America/New_York"),
    ("MCL", "MCL", "NYMEX", "Micro Crude Oil", "0.01", "1.00", 2, "America/New_York"),
    ("GC", "GC", "COMEX", "Gold", "0.10", "10.00", 1, "America/New_York"),
    ("MGC", "MGC", "COMEX", "Micro Gold", "0.10", "1.00", 1, "America/New_York"),
    ("SI", "SI", "COMEX", "Silver", "0.005", "25.00", 3, "America/New_York"),
    ("NG", "NG", "NYMEX", "Natural Gas", "0.001", "10.00", 3, "America/New_York"),
    ("ZB", "ZB", "CBOT", "30-Year U.S. Treasury Bond", "0.03125", "31.25", 5, "America/Chicago"),
    ("ZN", "ZN", "CBOT", "10-Year U.S. Treasury Note", "0.015625", "15.625", 6, "America/Chicago"),
    ("6E", "6E", "CME", "Euro FX", "0.00005", "6.25", 5, "America/Chicago"),
    ("6J", "6J", "CME", "Japanese Yen", "0.0000005", "6.25", 7, "America/Chicago"),
)


async def seed() -> int:
    """Insert or update every reference instrument. Returns the row count."""
    rows = [
        {
            "id": uuid7(),
            "symbol": symbol,
            "root_symbol": root,
            "exchange": exchange,
            "description": description,
            "asset_class": AssetClass.FUTURE,
            "currency": "USD",
            "tick_size": Decimal(tick_size),
            "tick_value": Decimal(tick_value),
            "price_precision": precision,
            "exchange_timezone": timezone,
            "session_rollover": "17:00",
            "is_continuous": True,
            "is_active": True,
        }
        for symbol, root, exchange, description, tick_size, tick_value, precision, timezone in (
            INSTRUMENTS
        )
    ]

    async with session_scope() as session:
        statement = insert(Instrument).values(rows)
        await session.execute(
            statement.on_conflict_do_update(
                constraint="uq_instruments_symbol_exchange",
                set_={
                    column: getattr(statement.excluded, column)
                    for column in (
                        "description",
                        "tick_size",
                        "tick_value",
                        "price_precision",
                        "exchange_timezone",
                        "is_active",
                    )
                },
            )
        )
    return len(rows)


async def main() -> None:
    configure_logging(get_settings())
    try:
        count = await seed()
        logger.info("seed.instruments_written", count=count)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
