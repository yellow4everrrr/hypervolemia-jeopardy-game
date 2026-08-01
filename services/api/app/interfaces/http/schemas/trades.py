"""API schemas for trades.

Decimals are serialised as **strings**, not JSON numbers. A JSON number is a float in
every browser: ``12345.67`` survives, but an eight-decimal crypto price or a large
cumulative P&L does not. The frontend parses these with a decimal library, and the
round trip is exact.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.domain.common.enums import Direction, SessionSegment, TradeStatus

DecimalStr = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]


class TradeSummary(BaseModel):
    """Row shape for the trade blotter."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    instrument_id: UUID
    direction: Direction
    status: TradeStatus
    opened_at: datetime
    closed_at: datetime | None
    duration_seconds: int | None
    quantity_opened: DecimalStr
    quantity_closed: DecimalStr
    avg_entry_price: DecimalStr | None
    avg_exit_price: DecimalStr | None
    gross_pnl: DecimalStr
    commission: DecimalStr
    fees: DecimalStr
    net_pnl: DecimalStr
    points_per_contract: DecimalStr | None
    ticks_per_contract: DecimalStr | None
    realized_r: DecimalStr | None
    session_date: date | None
    session_segment: SessionSegment | None
    entry_hour: int | None
    entry_weekday: int | None
    scaled_in: bool
    scaled_out: bool


class TradeListResponse(BaseModel):
    items: list[TradeSummary]
    limit: int
    offset: int
    #: Total is deliberately absent: a `COUNT(*)` over a filtered blotter is the most
    #: expensive query on the page and the UI does not need it to paginate. `has_more`
    #: is derived by requesting one extra row.
    has_more: bool


class TradeFilters(BaseModel):
    account_id: UUID | None = None
    status: TradeStatus | None = None
    session_from: date | None = None
    session_to: date | None = None
    limit: Annotated[int, Field(ge=1, le=500)] = 50
    offset: Annotated[int, Field(ge=0)] = 0
