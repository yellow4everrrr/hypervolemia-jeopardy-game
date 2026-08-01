"""Market bars and trade replay metadata.

``market_bars`` is the only genuinely large table in the system: one instrument at one
minute for one year is ~350k rows, and replay needs seconds. It is a TimescaleDB
hypertable partitioned on ``ts``, with compression on older chunks. Bars are global
reference data, not user-scoped — every trader replaying ES at 09:31 sees the same
candle, and duplicating that per tenant would multiply the largest table by the user
count.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.common.enums import Timeframe
from app.infrastructure.db.base import (
    MONEY,
    QUANTITY,
    Base,
    TimestampMixin,
    UserScopedMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class MarketBar(Base):
    """OHLCV candle.

    Primary key ``(instrument_id, timeframe, ts)`` — the exact prefix every replay and
    MAE/MFE query uses, so reads are index-only range scans.
    """

    __tablename__ = "market_bars"
    __table_args__ = (
        CheckConstraint("high >= low", name="high_at_least_low"),
        CheckConstraint("high >= open AND high >= close", name="high_is_max"),
        CheckConstraint("low <= open AND low <= close", name="low_is_min"),
    )

    instrument_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    timeframe: Mapped[Timeframe] = mapped_column(pg_enum(Timeframe, "timeframe"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    open: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    high: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    low: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    close: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    volume: Mapped[Decimal | None] = mapped_column(QUANTITY)
    trade_count: Mapped[int | None] = mapped_column(BigInteger)
    #: Provenance matters: a bar from the broker's feed and a bar from a vendor can
    #: differ, and a trader replaying a trade must see the feed they traded on.
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="broker")


class ReplayMetadata(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """Everything needed to reconstruct a trade's replay without recomputing it.

    The window is stored rather than derived at render time so replays are stable: a
    trade reviewed today and re-reviewed in a year shows the same chart, the same
    context bars and the same markers.
    """

    __tablename__ = "replay_metadata"
    __table_args__ = (
        UniqueConstraint("trade_id", "primary_timeframe", name="uq_replay_metadata_trade_tf"),
        Index("ix_replay_metadata_user", "user_id"),
    )

    trade_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    primary_timeframe: Mapped[Timeframe] = mapped_column(
        pg_enum(Timeframe, "timeframe"), nullable=False
    )
    higher_timeframe: Mapped[Timeframe | None] = mapped_column(pg_enum(Timeframe, "timeframe"))
    #: Context window: bars before entry and after exit, so the replay starts with the
    #: setup visible rather than at the moment of entry.
    window_start: Mapped[datetime] = mapped_column(nullable=False)
    window_end: Mapped[datetime] = mapped_column(nullable=False)
    context_bars_before: Mapped[int] = mapped_column(Integer, nullable=False, server_default="120")
    context_bars_after: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    #: Entry/exit/stop/target markers and the risk box, precomputed for the chart.
    markers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    #: Indicator configuration active for this replay (EMAs, VWAP, volume profile).
    indicators: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    #: Trader-added drawings, persisted so annotations survive a reload.
    drawings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
