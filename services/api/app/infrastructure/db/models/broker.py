"""Broker connections, accounts, balance history and sync bookkeeping.

Credentials are **never** stored here. ``credential_ref`` is a pointer into a secret
manager (AWS Secrets Manager / Railway secrets); the database holds only the handle,
so a database dump does not hand over anyone's brokerage account.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.common.enums import AccountType, BrokerKind, ConnectionStatus, SyncStatus
from app.infrastructure.db.base import (
    MONEY,
    Base,
    TimestampMixin,
    UserScopedMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)
from app.infrastructure.db.models.identity import User


class BrokerConnection(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    __tablename__ = "broker_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "broker", "label", name="uq_broker_connections_user_label"),
    )

    broker: Mapped[BrokerKind] = mapped_column(pg_enum(BrokerKind, "broker_kind"), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    #: ``demo`` or ``live`` — Tradovate exposes separate hosts and separate data.
    environment: Mapped[str] = mapped_column(String(16), nullable=False, server_default="demo")
    status: Mapped[ConnectionStatus] = mapped_column(
        pg_enum(ConnectionStatus, "connection_status"),
        nullable=False,
        server_default=ConnectionStatus.PENDING.value,
    )
    #: Opaque handle into the secret manager. Never a token, never a password.
    credential_ref: Mapped[str | None] = mapped_column(String(256))
    #: The broker's own user id for this login. Required to subscribe to the real-time
    #: user feed (`user/syncrequest` takes a user id, not an account id) and to tell
    #: two logins apart when one trader holds several.
    external_user_id: Mapped[str | None] = mapped_column(String(64))
    last_sync_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    #: Per-endpoint incremental sync cursors, e.g. ``{"fills": {"since_id": 91823}}``.
    sync_cursor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    user: Mapped[User] = relationship(back_populates="broker_connections")
    accounts: Mapped[list[Account]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )


class Account(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint(
            "broker_connection_id",
            "broker_account_id",
            name="uq_accounts_connection_broker_account",
        ),
        Index("ix_accounts_user_active", "user_id", "is_active"),
    )

    broker_connection_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("broker_connections.id", ondelete="SET NULL")
    )
    #: The broker's own account identifier — the join key for every incoming fill.
    broker_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_type: Mapped[AccountType] = mapped_column(
        pg_enum(AccountType, "account_type"),
        nullable=False,
        server_default=AccountType.SIMULATED.value,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    #: Prop-firm accounts have externally imposed rules (daily loss limit, trailing
    #: drawdown) that the compliance engine scores against in milestone 7.
    prop_firm: Mapped[str | None] = mapped_column(String(80))
    starting_balance: Mapped[Decimal | None] = mapped_column(MONEY)
    max_daily_loss: Mapped[Decimal | None] = mapped_column(MONEY)
    max_drawdown: Mapped[Decimal | None] = mapped_column(MONEY)
    profit_target: Mapped[Decimal | None] = mapped_column(MONEY)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    user: Mapped[User] = relationship(back_populates="accounts")
    connection: Mapped[BrokerConnection | None] = relationship(back_populates="accounts")


class AccountBalanceSnapshot(UUIDPrimaryKeyMixin, UserScopedMixin, Base):
    """Point-in-time equity, sampled by the sync worker.

    Kept separate from trade-derived P&L on purpose: reconciling our reconstructed
    numbers against the broker's own balance is how we detect a reconstruction bug
    before the trader does.
    """

    __tablename__ = "account_balance_snapshots"
    __table_args__ = (
        Index("ix_account_balance_snapshots_account_ts", "account_id", "as_of"),
        UniqueConstraint("account_id", "as_of", name="uq_account_balance_snapshots_account_as_of"),
    )

    account_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    as_of: Mapped[datetime] = mapped_column(nullable=False)
    cash_balance: Mapped[Decimal | None] = mapped_column(MONEY)
    equity: Mapped[Decimal | None] = mapped_column(MONEY)
    open_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    realized_pnl_day: Mapped[Decimal | None] = mapped_column(MONEY)
    margin_used: Mapped[Decimal | None] = mapped_column(MONEY)


class SyncRun(UUIDPrimaryKeyMixin, UserScopedMixin, Base):
    """Audit trail for every broker sync attempt.

    Without this, "why is yesterday missing?" is unanswerable. With it, every fill can
    be traced to the run that ingested it.
    """

    __tablename__ = "sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_connection_started", "broker_connection_id", "started_at"),
    )

    broker_connection_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("broker_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[SyncStatus] = mapped_column(
        pg_enum(SyncStatus, "sync_status"), nullable=False, server_default=SyncStatus.RUNNING.value
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, server_default="scheduled")
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None]
    executions_ingested: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    executions_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    trades_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
