"""Strategies, rules, setups, tags and market conditions.

This is the vocabulary a trader uses to describe their own edge. It is user-scoped and
unbounded by design: the classifier ships with common setups, but any trader can add
their own and the analytics cube treats custom labels identically to built-in ones.

Strategies are **versioned**. When rules change, a new version is created rather than
the old one edited, so historical compliance scores remain reproducible against the
rules that were actually in force at the time.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.common.enums import RuleSeverity, RuleType
from app.infrastructure.db.base import (
    RATIO,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UserScopedMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class Strategy(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "strategies"
    __table_args__ = (
        UniqueConstraint("user_id", "name", "version", name="uq_strategies_user_name_version"),
        Index("ix_strategies_user_active", "user_id", "is_active"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    #: Set when a newer version supersedes this one, forming a lineage.
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("strategies.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    effective_from: Mapped[datetime | None]
    effective_to: Mapped[datetime | None]
    #: Free-form checklist items the trader ticks pre-trade; scored alongside rules.
    checklist: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    rules: Mapped[list[StrategyRule]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan"
    )


class StrategyRule(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """A single machine-evaluable rule.

    ``expression`` holds a small declarative predicate AST rather than code — the rule
    engine (milestone 7) interprets it. Storing an AST instead of a string keeps rules
    inspectable, diffable between strategy versions, and safe: nothing user-supplied is
    ever executed.
    """

    __tablename__ = "strategy_rules"
    __table_args__ = (
        Index("ix_strategy_rules_strategy", "strategy_id"),
        CheckConstraint("weight >= 0", name="weight_non_negative"),
    )

    strategy_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False
    )
    rule_type: Mapped[RuleType] = mapped_column(pg_enum(RuleType, "rule_type"), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[RuleSeverity] = mapped_column(
        pg_enum(RuleSeverity, "rule_severity"),
        nullable=False,
        server_default=RuleSeverity.MINOR.value,
    )
    #: Relative contribution to the compliance score.
    weight: Mapped[Decimal] = mapped_column(RATIO, nullable=False, server_default="1")
    expression: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    strategy: Mapped[Strategy] = relationship(back_populates="rules")


class RuleEvaluation(UUIDPrimaryKeyMixin, UserScopedMixin, Base):
    """Result of one rule against one trade.

    Persisted per trade rather than recomputed so that "which rule did I break most
    often, and what did it cost me?" is a single indexed aggregate.
    """

    __tablename__ = "rule_evaluations"
    __table_args__ = (
        UniqueConstraint("trade_id", "rule_id", name="uq_rule_evaluations_trade_rule"),
        Index("ix_rule_evaluations_user_rule_passed", "user_id", "rule_id", "passed"),
    )

    trade_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("strategy_rules.id", ondelete="CASCADE"), nullable=False
    )
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: ``None`` when the rule could not be evaluated (missing stop, no bar data).
    # Unevaluable is not the same as violated and must not be scored as one.
    evaluable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    evaluated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class Setup(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A recognisable pattern: trend pullback, ORB, liquidity sweep, or anything custom."""

    __tablename__ = "setups"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_setups_user_name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: True for setups Ledgerline ships with, so they can be updated centrally.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: Feature thresholds the automatic classifier uses to propose this setup.
    detection_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    color: Mapped[str | None] = mapped_column(String(16))


class Tag(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tags_user_name"),)

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str | None] = mapped_column(String(32))
    color: Mapped[str | None] = mapped_column(String(16))


class TradeTag(Base):
    __tablename__ = "trade_tags"
    __table_args__ = (Index("ix_trade_tags_tag", "tag_id"),)

    trade_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="manual")


class MarketCondition(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """The regime a trade happened in — trending, balanced, high-volatility, news.

    Regime is the most common confounder in trading statistics: a setup that "stopped
    working" has usually just met a different regime. Storing it explicitly lets the
    analytics engine control for it instead of averaging across it.
    """

    __tablename__ = "market_conditions"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_market_conditions_user_name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    criteria: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
