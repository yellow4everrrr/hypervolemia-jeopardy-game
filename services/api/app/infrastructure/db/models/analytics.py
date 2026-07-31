"""Computed analytics: metric sets, equity curves, risk metrics and reports.

Two rules govern this module.

**Nothing here is written by a language model.** Every row is produced by the Python
analytics engine. The AI layer reads these tables and writes prose into
``ai_analyses``; it never writes a number back.

**Metric sets are stored as JSONB keyed by scope, not as wide columns.** The list of
interesting breakdowns (by hour, by setup, by drawdown state, by consecutive-loss
streak…) grows every month, and a schema where a new breakdown means a migration is a
schema that will stop being extended. The trade-off — no per-metric type checking in
the database — is bought back by a versioned, validated payload contract in the
analytics engine.
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
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.common.enums import MetricScope, ReportStatus, ReportType
from app.infrastructure.db.base import (
    MONEY,
    RATIO,
    Base,
    TimestampMixin,
    UserScopedMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class PerformanceMetric(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """One computed metric set for one slice of one period."""

    __tablename__ = "performance_metrics"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "account_id",
            "scope",
            "scope_key",
            "period_start",
            "period_end",
            "engine_version",
            name="uq_performance_metrics_slice",
        ),
        CheckConstraint("sample_size >= 0", name="sample_size_non_negative"),
        Index("ix_performance_metrics_user_scope", "user_id", "scope", "scope_key"),
    )

    #: NULL means "across all accounts" — a legitimate slice, not missing data.
    account_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    scope: Mapped[MetricScope] = mapped_column(pg_enum(MetricScope, "metric_scope"), nullable=False)
    #: Discriminator within the scope: an instrument id, an hour, a weekday, a setup
    #: name. Empty string for ``OVERALL``.
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False, server_default="")
    period_start: Mapped[date] = mapped_column(nullable=False)
    period_end: Mapped[date] = mapped_column(nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: Full metric payload, validated against a versioned schema before insert.
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    #: Promoted out of the payload because they are sorted and filtered on directly.
    net_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    expectancy: Mapped[Decimal | None] = mapped_column(MONEY)
    expectancy_r: Mapped[Decimal | None] = mapped_column(RATIO)
    win_rate: Mapped[Decimal | None] = mapped_column(RATIO)
    profit_factor: Mapped[Decimal | None] = mapped_column(RATIO)
    #: Bumped whenever a formula changes, so stale rows are identifiable and
    #: recomputable rather than silently mixed with new ones.
    engine_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    computed_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class EquityCurvePoint(Base):
    """Cumulative performance after each trade — a TimescaleDB hypertable.

    Composite primary key ``(user_id, account_id, recorded_at)`` rather than a UUID:
    this table is append-heavy and read as ranges, so the key doubles as the index the
    equity chart scans. Converted to a hypertable in the migration and partitioned by
    time, which keeps a decade of history queryable in constant time per window.

    ``user_id`` is declared here rather than inherited from ``UserScopedMixin`` because
    a hypertable's partitioning column must be part of its primary key.
    """

    __tablename__ = "equity_curve_points"
    __table_args__ = (Index("ix_equity_curve_points_user_time", "user_id", "recorded_at"),)

    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    account_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    recorded_at: Mapped[datetime] = mapped_column(primary_key=True)
    trade_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    cumulative_net_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cumulative_r: Mapped[Decimal | None] = mapped_column(RATIO)
    peak_equity: Mapped[Decimal | None] = mapped_column(MONEY)
    drawdown: Mapped[Decimal | None] = mapped_column(MONEY)
    drawdown_pct: Mapped[Decimal | None] = mapped_column(RATIO)
    trade_number: Mapped[int | None] = mapped_column(Integer)


class RiskMetric(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """Risk-of-ruin, Kelly and drawdown statistics as of a point in time.

    Separate from ``performance_metrics`` because these are *forward-looking* estimates
    with their own assumptions (sample window, bootstrap iterations, ruin threshold)
    that must be recorded alongside the result for it to mean anything.
    """

    __tablename__ = "risk_metrics"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "account_id", "as_of", name="uq_risk_metrics_user_account_as_of"
        ),
    )

    account_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    as_of: Mapped[datetime] = mapped_column(nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    kelly_fraction: Mapped[Decimal | None] = mapped_column(RATIO)
    half_kelly_fraction: Mapped[Decimal | None] = mapped_column(RATIO)
    risk_of_ruin: Mapped[Decimal | None] = mapped_column(RATIO)
    ruin_threshold_pct: Mapped[Decimal | None] = mapped_column(RATIO)
    max_drawdown: Mapped[Decimal | None] = mapped_column(MONEY)
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(RATIO)
    average_drawdown: Mapped[Decimal | None] = mapped_column(MONEY)
    recovery_factor: Mapped[Decimal | None] = mapped_column(RATIO)
    mar_ratio: Mapped[Decimal | None] = mapped_column(RATIO)
    value_at_risk_95: Mapped[Decimal | None] = mapped_column(MONEY)
    conditional_var_95: Mapped[Decimal | None] = mapped_column(MONEY)
    #: Monte Carlo / bootstrap configuration and percentile outputs.
    simulation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    assumptions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    engine_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Report(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """Daily → annual and compliance reports.

    One table with a ``report_type`` discriminator rather than ``weekly_reports`` and
    ``compliance_reports`` as separate tables: they share every column, every index and
    every lifecycle transition, and splitting them would triple the query surface for
    the report list view without buying any constraint the enum does not already give.
    """

    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "account_id",
            "report_type",
            "period_start",
            name="uq_reports_user_type_period",
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        Index("ix_reports_user_type_period", "user_id", "report_type", "period_start"),
    )

    account_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    report_type: Mapped[ReportType] = mapped_column(
        pg_enum(ReportType, "report_type"), nullable=False
    )
    status: Mapped[ReportStatus] = mapped_column(
        pg_enum(ReportStatus, "report_status"),
        nullable=False,
        server_default=ReportStatus.QUEUED.value,
    )
    period_start: Mapped[date] = mapped_column(nullable=False)
    period_end: Mapped[date] = mapped_column(nullable=False)
    #: Deterministic statistics the report is built from — the evidence base the AI
    #: narrative is required to cite and forbidden to exceed.
    metrics_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    #: Structured findings: strengths, weaknesses, leaks, recommendations.
    findings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    summary: Mapped[str | None] = mapped_column(Text)
    ai_analysis_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    generated_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
