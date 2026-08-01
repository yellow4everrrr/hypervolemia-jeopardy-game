"""Instrument repository."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.trading.instrument import InstrumentSpec
from app.domain.trading.session import SessionCalendar
from app.infrastructure.db.mappers import calendar_from_row, spec_from_row
from app.infrastructure.db.models.instruments import Instrument


class SqlAlchemyInstrumentRepository:
    """Reference data lookups.

    Instruments change rarely and are read on every ingestion batch, so this is the
    first repository milestone 13 will put behind a Redis cache. The interface is
    already shaped for it: batch reads, keyed by symbol.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_row(self, symbol: str, exchange: str | None = None) -> Instrument | None:
        stmt = select(Instrument).where(Instrument.symbol == symbol)
        if exchange is not None:
            stmt = stmt.where(Instrument.exchange == exchange)
        return (await self._session.execute(stmt.limit(1))).scalar_one_or_none()

    async def get_spec(self, symbol: str, exchange: str | None = None) -> InstrumentSpec | None:
        row = await self.get_row(symbol, exchange)
        return None if row is None else spec_from_row(row)

    async def get_specs(self, symbols: Sequence[str]) -> dict[str, InstrumentSpec]:
        if not symbols:
            return {}
        stmt = select(Instrument).where(Instrument.symbol.in_(list(symbols)))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.symbol: spec_from_row(row) for row in rows}

    async def get_calendars(self, symbols: Sequence[str]) -> dict[str, SessionCalendar]:
        if not symbols:
            return {}
        stmt = select(Instrument).where(Instrument.symbol.in_(list(symbols)))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.symbol: calendar_from_row(row) for row in rows}

    async def resolve_id(self, symbol: str, exchange: str | None = None) -> UUID | None:
        row = await self.get_row(symbol, exchange)
        return None if row is None else row.id

    async def resolve_ids(self, symbols: Sequence[str]) -> dict[str, UUID]:
        if not symbols:
            return {}
        stmt = select(Instrument.symbol, Instrument.id).where(
            Instrument.symbol.in_(list(symbols))
        )
        return dict((await self._session.execute(stmt)).tuples().all())
