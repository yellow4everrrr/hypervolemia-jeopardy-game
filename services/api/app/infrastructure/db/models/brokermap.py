"""Broker-specific identifier mapping.

Brokers name instruments in their own terms. Tradovate identifies a contract by a
numeric ``contractId`` that means nothing outside Tradovate and changes between demo
and live. Storing that id on ``instruments`` would tie a global reference table to one
broker; storing it here keeps the mapping where it belongs and lets a second broker
map the same instrument independently.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.common.enums import BrokerKind
from app.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum


class BrokerInstrumentMap(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Maps a broker's instrument identifier to ours.

    Keyed by ``(broker, environment, external_id)`` because Tradovate's demo and live
    environments assign different contract ids to the same contract — a mapping learned
    in demo must not be trusted in live.
    """

    __tablename__ = "broker_instrument_map"
    __table_args__ = (
        UniqueConstraint(
            "broker", "environment", "external_id", name="uq_broker_instrument_map_identity"
        ),
        Index("ix_broker_instrument_map_instrument", "instrument_id"),
    )

    broker: Mapped[BrokerKind] = mapped_column(pg_enum(BrokerKind, "broker_kind"), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False, server_default="demo")
    #: The broker's identifier, as a string so numeric and symbolic brokers share a table.
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The broker's own symbol at the time of mapping, kept for diagnostics.
    external_symbol: Mapped[str | None] = mapped_column(String(64))
    instrument_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    #: Raw broker reference payload (product, maturity), kept so a mis-mapped
    #: instrument can be diagnosed without re-querying the broker.
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
