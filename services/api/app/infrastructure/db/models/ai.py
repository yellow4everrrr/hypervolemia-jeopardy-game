"""AI analysis and recommendations.

The central constraint of this product: **the model interprets, it never computes.**
Two schema decisions enforce that rather than merely requesting it.

``input_metrics`` stores the exact deterministic payload the model was shown. Any
number appearing in ``output`` must be traceable to it, and the AI layer validates this
before the row is written — a claim citing a statistic absent from the payload is
rejected, not published.

``prompt_version`` and ``model`` are recorded on every row so that when a prompt or a
model changes, previously generated advice remains attributable and comparable rather
than silently blended with output from a different system.
"""

from __future__ import annotations

from datetime import datetime
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.common.enums import AnalysisSubject
from app.infrastructure.db.base import (
    MONEY,
    RATIO,
    Base,
    TimestampMixin,
    UserScopedMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class AiAnalysis(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    __tablename__ = "ai_analyses"
    __table_args__ = (
        Index("ix_ai_analyses_subject", "subject_type", "subject_id"),
        Index("ix_ai_analyses_user_created", "user_id", "created_at"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
    )

    subject_type: Mapped[AnalysisSubject] = mapped_column(
        pg_enum(AnalysisSubject, "analysis_subject"), nullable=False
    )
    #: The trade, session, strategy or report the analysis is about. Not a foreign key
    #: because the target table varies with ``subject_type``.
    subject_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The deterministic statistics the model was given. The evidence base.
    input_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    #: Structured model output: findings, cited metric keys, hypotheses.
    output: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    summary: Mapped[str | None] = mapped_column(Text)
    #: Model's stated confidence in its own reading, 0–1. Distinct from any statistical
    #: confidence interval, which is computed in Python and lives in ``input_metrics``.
    confidence: Mapped[Decimal | None] = mapped_column(RATIO)
    #: False when validation found a claim not supported by ``input_metrics``.
    evidence_validated: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    recommendations: Mapped[list[AiRecommendation]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )


class AiRecommendation(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """A single actionable recommendation, with its evidence and expected value.

    Recommendations are tracked over time (``status``) so the platform can answer the
    only question that matters about coaching: did acting on it actually change the
    trader's numbers?
    """

    __tablename__ = "ai_recommendations"
    __table_args__ = (
        Index("ix_ai_recommendations_user_status", "user_id", "status"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="priority_range"),
    )

    analysis_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("ai_analyses.id", ondelete="CASCADE"), nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    #: Metric keys and values from the analysis payload that support this claim.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    #: Python-computed estimate of the improvement from adopting this change, with its
    #: own interval. Never a number the model invented.
    expected_improvement: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    #: ``open``, ``accepted``, ``dismissed``, ``resolved``.
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    resolved_at: Mapped[datetime | None]

    analysis: Mapped[AiAnalysis] = relationship(back_populates="recommendations")


class DetectedPattern(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """A behavioural or statistical pattern found by the detection engine.

    Every pattern carries its own hypothesis test — effect size, p-value, sample size,
    and the comparison it was measured against. A pattern that has not cleared its
    significance threshold is stored with ``is_significant = false`` and shown as an
    observation, never as a finding. This is the difference between a journal that
    finds edges and one that manufactures them from noise.
    """

    __tablename__ = "detected_patterns"
    __table_args__ = (
        Index("ix_detected_patterns_user_kind", "user_id", "pattern_kind"),
        Index("ix_detected_patterns_user_significant", "user_id", "is_significant"),
    )

    pattern_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: ``edge`` or ``leak`` — patterns that make money and patterns that lose it.
    polarity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="leak")
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    effect_size: Mapped[Decimal | None] = mapped_column(RATIO)
    p_value: Mapped[Decimal | None] = mapped_column(RATIO)
    confidence_low: Mapped[Decimal | None] = mapped_column(RATIO)
    confidence_high: Mapped[Decimal | None] = mapped_column(RATIO)
    is_significant: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    #: Estimated annual P&L impact of the pattern, computed by the analytics engine.
    estimated_annual_impact: Mapped[Decimal | None] = mapped_column(MONEY)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    first_observed_at: Mapped[datetime | None]
    last_observed_at: Mapped[datetime | None]
    engine_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class PredictionModel(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """One trained predictive model, with the evidence that decided whether to serve it.

    Every column that qualifies the model is stored beside it, because a coefficient
    vector without its provenance is unusable: a probability served today has to be
    traceable to the walk-forward run that justified deploying it, and to the exact
    arithmetic that produced it.

    ``is_deployable`` is stored rather than recomputed on read. The gate depends on a
    bootstrap and a calibration simulation costing seconds; recomputing it per request
    would push a statistical decision into the serving path, where it would eventually
    be cached, then skipped. A model row that is not deployable carries ``refusal`` and
    is never served — the row exists so the trader can be told *why*, and so a later
    training run can be compared against it.
    """

    __tablename__ = "prediction_models"
    __table_args__ = (
        Index("ix_prediction_models_user_head", "user_id", "head"),
        Index(
            "ix_prediction_models_user_head_created",
            "user_id",
            "head",
            "created_at",
        ),
    )

    #: ``win_probability`` or ``expected_r``. The two heads are trained, gated and
    #: refused independently: a trader who records no stops still has a win-rate
    #: history, and refusing both because one lacks its target discards real evidence.
    head: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )

    is_deployable: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    #: Plain language for why this model is not served. Null exactly when deployable.
    refusal: Mapped[str | None] = mapped_column(Text)

    #: Out-of-sample skill against the baseline that needed no model, with the session
    #: block bootstrap interval that decides whether it is real.
    skill: Mapped[Decimal | None] = mapped_column(RATIO)
    skill_low: Mapped[Decimal | None] = mapped_column(RATIO)
    skill_high: Mapped[Decimal | None] = mapped_column(RATIO)

    trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    folds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: Predictions scored on data the model had not seen. The denominator behind every
    #: performance figure on this row.
    out_of_sample_predictions: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    #: Coefficients, feature list, calibration curve and skill evidence. JSONB because
    #: the feature set changes with ``model_version`` and a schema requiring a migration
    #: per feature is a schema that stops being extended.
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    model_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    trained_at: Mapped[datetime | None]
