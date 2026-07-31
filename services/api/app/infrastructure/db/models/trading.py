"""Orders, executions, trades, positions and daily rollups.

The write path is strictly one-directional:

``orders → executions → trade_executions → trades → trading_sessions``

Executions are append-only and carry the broker's identifier as a unique key, which
makes re-syncing a day a no-op instead of a duplication. Trades are *derived* and may
be rebuilt at any time from their executions — the ``trade_executions`` association
carries per-trade quantity and cost allocation so that a fill spanning a position flip
is split correctly between the two trades it touches.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.common.enums import (
    Direction,
    Emotion,
    ExecutionRole,
    OrderStatus,
    OrderType,
    SessionSegment,
    Side,
    TimeInForce,
    TradeStatus,
)
from app.infrastructure.db.base import (
    MONEY,
    QUANTITY,
    RATIO,
    Base,
    TimestampMixin,
    UserScopedMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class Order(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """Broker order. Retained because *intent* is evidence.

    Stop and target orders are how the journal learns a trade's planned risk without
    asking the trader to type it in — and comparing the working stop over time is how
    "you move your stops" stops being an accusation and becomes a measurement.
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("account_id", "broker_order_id", name="uq_orders_account_broker_order"),
        Index("ix_orders_user_placed_at", "user_id", "placed_at"),
        Index("ix_orders_account_instrument_placed", "account_id", "instrument_id", "placed_at"),
    )

    account_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    broker_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_order_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    side: Mapped[Side] = mapped_column(pg_enum(Side, "order_side"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(pg_enum(OrderType, "order_type"), nullable=False)
    time_in_force: Mapped[TimeInForce | None] = mapped_column(pg_enum(TimeInForce, "time_in_force"))
    status: Mapped[OrderStatus] = mapped_column(
        pg_enum(OrderStatus, "order_status"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False, server_default="0")
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    stop_price: Mapped[Decimal | None] = mapped_column(MONEY)
    placed_at: Mapped[datetime] = mapped_column(nullable=False)
    filled_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )


class Execution(UUIDPrimaryKeyMixin, UserScopedMixin, Base):
    """A fill. Immutable, append-only, the system's unit of truth."""

    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "broker_execution_id", name="uq_executions_account_broker_execution"
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("price >= 0", name="price_non_negative"),
        Index("ix_executions_user_executed_at", "user_id", "executed_at"),
        Index(
            "ix_executions_account_instrument_executed",
            "account_id",
            "instrument_id",
            "executed_at",
        ),
    )

    account_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    broker_execution_id: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[Side] = mapped_column(pg_enum(Side, "order_side"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(nullable=False)
    #: Broker-provided tie-breaker for fills sharing a timestamp. Reconstruction is
    #: order-sensitive, so this is persisted rather than re-derived.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    fees: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    liquidity_flag: Mapped[str | None] = mapped_column(String(16))
    sync_run_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    ingested_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )


class Trade(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """A round turn: the unit a trader makes decisions in, and the unit we score.

    Columns fall into four groups:

    1. *Reconstructed* — direction, prices, quantities, P&L. Derived from fills; never
       edited by hand.
    2. *Planned* — stop, target, risk. Inferred from working orders where possible.
    3. *Contextual* — strategy, setup, market condition, emotion. Classified
       automatically (milestone 8) and correctable by the trader.
    4. *Scored* — execution quality, discipline, compliance. Computed by the rule
       engine (milestone 7), never by the language model.
    """

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "origin_execution_id", name="uq_trades_account_origin_execution"
        ),
        CheckConstraint(
            "closed_at IS NULL OR closed_at >= opened_at", name="closed_after_opened"
        ),
        CheckConstraint(
            "(status = 'closed' AND closed_at IS NOT NULL)"
            " OR (status = 'open' AND closed_at IS NULL)",
            name="status_matches_closed_at",
        ),
        # The blotter's default query — a user's trades newest first — is served
        # entirely by this index.
        Index("ix_trades_user_opened_at", "user_id", "opened_at"),
        Index("ix_trades_user_session_date", "user_id", "session_date"),
        Index("ix_trades_account_instrument_opened", "account_id", "instrument_id", "opened_at"),
        Index("ix_trades_user_status", "user_id", "status"),
        Index("ix_trades_user_strategy", "user_id", "strategy_id"),
        Index("ix_trades_user_setup", "user_id", "setup_id"),
    )

    account_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    #: Identifier of the fill that opened this trade — the idempotency key that lets a
    #: rebuild recognise an existing trade instead of creating a duplicate.
    origin_execution_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # --- 1. Reconstructed --------------------------------------------------------
    direction: Mapped[Direction] = mapped_column(
        pg_enum(Direction, "trade_direction"), nullable=False
    )
    status: Mapped[TradeStatus] = mapped_column(
        pg_enum(TradeStatus, "trade_status"), nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(nullable=False)
    closed_at: Mapped[datetime | None]
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    quantity_opened: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    quantity_closed: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False, server_default="0")
    max_position_size: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    avg_entry_price: Mapped[Decimal | None] = mapped_column(MONEY)
    avg_exit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    realized_points: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    points_per_contract: Mapped[Decimal | None] = mapped_column(MONEY)
    ticks_per_contract: Mapped[Decimal | None] = mapped_column(MONEY)
    gross_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    fees: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    net_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    scaled_in: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    scaled_out: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    # --- Session attribution -----------------------------------------------------
    session_date: Mapped[date | None]
    session_segment: Mapped[SessionSegment | None] = mapped_column(
        pg_enum(SessionSegment, "session_segment")
    )
    #: Exchange-local hour and ISO weekday, denormalised so hourly/weekday breakdowns
    #: never re-derive timezones across a million rows.
    entry_hour: Mapped[int | None] = mapped_column(SmallInteger)
    entry_weekday: Mapped[int | None] = mapped_column(SmallInteger)

    # --- 2. Planned --------------------------------------------------------------
    initial_stop_price: Mapped[Decimal | None] = mapped_column(MONEY)
    initial_target_price: Mapped[Decimal | None] = mapped_column(MONEY)
    final_stop_price: Mapped[Decimal | None] = mapped_column(MONEY)
    planned_risk_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    planned_reward_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    planned_r_multiple: Mapped[Decimal | None] = mapped_column(RATIO)
    realized_r: Mapped[Decimal | None] = mapped_column(RATIO)

    # --- Excursions (filled by the replay/market-data engine) --------------------
    mae_price: Mapped[Decimal | None] = mapped_column(MONEY)
    mfe_price: Mapped[Decimal | None] = mapped_column(MONEY)
    mae_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    mfe_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    mae_r: Mapped[Decimal | None] = mapped_column(RATIO)
    mfe_r: Mapped[Decimal | None] = mapped_column(RATIO)
    edge_ratio: Mapped[Decimal | None] = mapped_column(RATIO)

    # --- 3. Context --------------------------------------------------------------
    strategy_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("strategies.id", ondelete="SET NULL")
    )
    setup_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("setups.id", ondelete="SET NULL")
    )
    market_condition_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("market_conditions.id", ondelete="SET NULL")
    )
    #: How the setup was assigned: ``auto`` (classifier) or ``manual`` (trader). Kept
    #: so model-assigned labels can be evaluated against corrected ones.
    setup_source: Mapped[str | None] = mapped_column(String(16))
    setup_confidence: Mapped[Decimal | None] = mapped_column(RATIO)
    confidence: Mapped[int | None] = mapped_column(SmallInteger)
    emotion: Mapped[Emotion | None] = mapped_column(pg_enum(Emotion, "emotion"))
    notes: Mapped[str | None] = mapped_column(Text)

    # --- 4. Scores ---------------------------------------------------------------
    execution_quality_score: Mapped[Decimal | None] = mapped_column(RATIO)
    discipline_score: Mapped[Decimal | None] = mapped_column(RATIO)
    compliance_score: Mapped[Decimal | None] = mapped_column(RATIO)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_analysis_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))

    #: Version of the reconstruction engine that produced this row. Lets a corrected
    #: engine identify exactly which trades need rebuilding.
    reconstruction_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    executions: Mapped[list[TradeExecution]] = relationship(
        back_populates="trade", cascade="all, delete-orphan"
    )


class TradeExecution(Base):
    """Association between a trade and the fills that compose it.

    Not a plain foreign key on ``executions`` because one fill can belong to two
    trades: selling 5 while long 2 closes one trade and opens another. Quantity and
    costs are allocated pro rata, so summing ``allocated_commission`` across trades
    reproduces the broker's charge exactly.
    """

    __tablename__ = "trade_executions"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_trade_executions_execution", "execution_id"),
    )

    trade_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True
    )
    execution_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("executions.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[ExecutionRole] = mapped_column(
        pg_enum(ExecutionRole, "execution_role"), primary_key=True
    )
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(nullable=False)
    allocated_commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    allocated_fees: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    #: Realized points contributed by this leg (exit legs only), signed by direction.
    realized_points: Mapped[Decimal | None] = mapped_column(MONEY)
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    trade: Mapped[Trade] = relationship(back_populates="executions")


class Position(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """Live position state per (account, instrument), including resumable FIFO lots.

    ``open_lots`` persists the reconstruction engine's state so an incremental sync
    processes only new fills instead of replaying a trader's entire history — the
    difference between a constant-time and a linear-time sync at 100k trades.
    """

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("account_id", "instrument_id", name="uq_positions_account_instrument"),
    )

    account_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    #: Signed: positive long, negative short, zero flat.
    signed_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False, server_default="0")
    avg_price: Mapped[Decimal | None] = mapped_column(MONEY)
    open_trade_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL")
    )
    open_lots: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    last_execution_at: Mapped[datetime | None]


class TradingSession(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """One row per (account, session date): the daily rollup the calendar view reads.

    Materialised rather than aggregated on read. A year of trading is ~250 rows here
    versus tens of thousands of trades, and the calendar heatmap must render instantly.
    """

    __tablename__ = "trading_sessions"
    __table_args__ = (
        UniqueConstraint("account_id", "session_date", name="uq_trading_sessions_account_date"),
        Index("ix_trading_sessions_user_date", "user_id", "session_date"),
    )

    account_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    session_date: Mapped[date] = mapped_column(nullable=False)
    first_trade_at: Mapped[datetime | None]
    last_trade_at: Mapped[datetime | None]
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    win_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    scratch_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    gross_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    fees: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    net_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    total_r: Mapped[Decimal | None] = mapped_column(RATIO)
    max_intraday_drawdown: Mapped[Decimal | None] = mapped_column(MONEY)
    volume_contracts: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False, server_default="0")
