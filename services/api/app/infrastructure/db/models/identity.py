"""Identity and per-user preferences.

Clerk owns authentication; this table owns the *application's* notion of a user. The
two are joined by ``clerk_user_id``. Keeping a local user row (rather than scattering
Clerk subject strings through every foreign key) means the identity provider can be
swapped, and that analytics joins stay on indexed UUIDs.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import (
    MONEY,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.broker import Account, BrokerConnection


class User(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"

    clerk_user_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    #: IANA timezone the trader lives in — used for *reporting* boundaries. Session
    #: dates still come from the exchange timezone, never from this field.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    onboarded_at: Mapped[datetime | None]

    settings: Mapped[UserSetting | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    # Onboarding writes a user and their first connection and account in one flush.
    # Without an ORM relationship the unit of work has no dependency edge between the
    # mappers and can order the child INSERT first, violating the foreign key. Only the
    # onboarding graph needs this: trades, executions and the rest are always written
    # long after the user exists, in bulk statements rather than as ORM objects.
    broker_connections: Mapped[list[BrokerConnection]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    accounts: Mapped[list[Account]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSetting(TimestampMixin, Base):
    """One row per user. Split from ``users`` so that hot identity reads stay narrow."""

    __tablename__ = "user_settings"

    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    theme: Mapped[str] = mapped_column(String(16), nullable=False, server_default="dark")
    #: Default fraction of account equity risked per trade, used to fill in planned
    #: risk when the broker gives us no stop order.
    default_risk_fraction: Mapped[Decimal | None] = mapped_column(MONEY)
    default_account_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    #: Saved dashboard layouts, keyed by layout name.
    dashboard_layouts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    notification_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    user: Mapped[User] = relationship(back_populates="settings")
