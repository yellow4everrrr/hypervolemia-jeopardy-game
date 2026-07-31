"""Daily journals, notes and screenshots.

Trade *data* arrives automatically; trade *context* — how the trader slept, what they
expected of the day, what they were feeling — cannot. These tables hold the small
amount of input the trader still provides, and the automatically captured images that
support replay and review.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.common.enums import ScreenshotKind, Timeframe
from app.infrastructure.db.base import (
    RATIO,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UserScopedMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class DailyJournal(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "daily_journals"
    __table_args__ = (
        UniqueConstraint("user_id", "journal_date", name="uq_daily_journals_user_date"),
        CheckConstraint(
            "focus_score IS NULL OR (focus_score BETWEEN 1 AND 10)", name="focus_score_range"
        ),
    )

    journal_date: Mapped[date] = mapped_column(nullable=False)
    pre_market_plan: Mapped[str | None] = mapped_column(Text)
    post_market_review: Mapped[str | None] = mapped_column(Text)
    market_notes: Mapped[str | None] = mapped_column(Text)
    #: Self-reported state. Correlating these against realized expectancy is one of the
    #: few genuinely novel findings a journal can surface — and one of the easiest to
    #: over-interpret, so the analytics layer reports effect sizes with sample counts.
    mood: Mapped[str | None] = mapped_column(String(32))
    sleep_hours: Mapped[Decimal | None] = mapped_column(RATIO)
    focus_score: Mapped[int | None] = mapped_column(SmallInteger)
    stress_score: Mapped[int | None] = mapped_column(SmallInteger)
    followed_plan: Mapped[bool | None]
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )


class Note(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Free-text note attached to any entity.

    Polymorphic by ``(entity_type, entity_id)`` rather than one FK column per target:
    notes will eventually hang off trades, sessions, strategies, setups and reports,
    and a nullable-FK-per-type table degrades badly past two or three.
    """

    __tablename__ = "notes"
    __table_args__ = (Index("ix_notes_entity", "entity_type", "entity_id"),)

    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    pinned: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))


class Screenshot(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """A chart image captured around a trade.

    Images live in S3-compatible object storage; the database holds the key and enough
    metadata to render a gallery without fetching anything. Capture is automatic at six
    moments per trade (before entry, entry, exit, after exit, higher timeframe,
    execution timeframe), which is why ``kind`` is an enum rather than free text.
    """

    __tablename__ = "screenshots"
    __table_args__ = (
        UniqueConstraint("trade_id", "kind", "timeframe", name="uq_screenshots_trade_kind_tf"),
        Index("ix_screenshots_user_captured", "user_id", "captured_at"),
    )

    trade_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ScreenshotKind] = mapped_column(
        pg_enum(ScreenshotKind, "screenshot_kind"), nullable=False
    )
    timeframe: Mapped[Timeframe | None] = mapped_column(pg_enum(Timeframe, "timeframe"))
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="image/webp"
    )
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(nullable=False)
    #: ``auto`` for pipeline captures, ``manual`` for trader uploads.
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="auto")
