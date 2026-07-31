"""Instrument reference data.

Global (not user-scoped): ES is ES for everyone, and duplicating contract specs per
tenant would let two users' P&L disagree about what a point is worth.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.common.enums import AssetClass
from app.infrastructure.db.base import (
    MONEY,
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class Instrument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("symbol", "exchange", name="uq_instruments_symbol_exchange"),
        Index("ix_instruments_root_symbol", "root_symbol"),
    )

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Contract root, e.g. ``ES`` for ``ESZ5``. Statistics group by root so that a
    #: quarterly roll does not fragment a trader's history into unusable samples.
    root_symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(160))
    asset_class: Mapped[AssetClass] = mapped_column(
        pg_enum(AssetClass, "asset_class"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    tick_size: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tick_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price_precision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    exchange_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="America/Chicago"
    )
    #: Local time at which the session date rolls over (17:00 for CME).
    session_rollover: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="17:00"
    )
    expiry_date: Mapped[date | None] = mapped_column(Date)
    is_continuous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
