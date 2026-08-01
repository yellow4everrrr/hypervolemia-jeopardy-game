"""Persisting coaching analyses and their evidence.

``input_metrics`` stores the exact bundle the model was shown, verbatim. That is what
makes a claim auditable months later: the numbers can be re-derived, and a disputed
sentence can be checked against the evidence that produced it rather than against
whatever the analytics engine returns today.

Rejected analyses are stored too, with ``evidence_validated = false``. Discarding them
would hide the rejection rate — the metric that tells you whether the prompt is drifting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.generate_coaching import CoachingAnalysis
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.infrastructure.db.models.ai import AiAnalysis, AiRecommendation

logger = get_logger(__name__)

#: Model confidence as a number, for the ``confidence`` column. Deliberately coarse —
#: it is the model's own reading, not a statistical interval, and three buckets are as
#: much precision as that deserves.
CONFIDENCE_VALUES = {
    "high": Decimal("0.9"),
    "moderate": Decimal("0.6"),
    "low": Decimal("0.3"),
}


class SqlAlchemyCoachingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_today(self, user_id: UUID) -> int:
        """Analyses started since midnight UTC — the rate-limit counter.

        Counts rejected analyses as well as published ones, because a rejected analysis
        still cost a model call.
        """
        since = datetime.now(UTC) - timedelta(days=1)
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(AiAnalysis)
                    .where(AiAnalysis.user_id == user_id, AiAnalysis.created_at >= since)
                )
            ).scalar_one()
        )

    async def save_analysis(self, user_id: UUID, analysis: CoachingAnalysis) -> UUID:
        row = AiAnalysis(
            id=uuid7(),
            user_id=user_id,
            subject_type=analysis.subject_type,
            subject_id=analysis.subject_id,
            model=analysis.model or "unknown",
            prompt_version=analysis.prompt_version,
            input_metrics=analysis.bundle.to_payload(),
            output=analysis.to_payload(),
            summary=(
                analysis.to_payload()["headline"] if analysis.is_publishable else None
            ),
            confidence=_overall_confidence(analysis),
            evidence_validated=analysis.validation.is_valid,
            input_tokens=analysis.input_tokens,
            output_tokens=analysis.output_tokens,
            cost_usd=analysis.cost_usd,
            latency_ms=analysis.latency_ms,
            error=analysis.error,
        )
        self._session.add(row)
        await self._session.flush()

        # Recommendations are only broken out when the analysis passed validation. A
        # rejected analysis is kept whole in `output` for diagnosis, but its
        # recommendations must not become rows a UI can pick up and display.
        if analysis.is_publishable:
            rendered = analysis.to_payload()["recommendations"]
            self._session.add_all(
                AiRecommendation(
                    id=uuid7(),
                    user_id=user_id,
                    analysis_id=row.id,
                    priority=item["priority"],
                    category=item["category"],
                    statement=item["statement"],
                    evidence={
                        key: analysis.bundle.display_for(key)
                        for key in item["cites"]
                        if analysis.bundle.display_for(key) is not None
                    },
                    # Filled by the what-if simulator; never by the model.
                    expected_improvement={},
                    status="open",
                )
                for item in rendered
            )

        logger.info(
            "coach.stored",
            user_id=str(user_id),
            analysis_id=str(row.id),
            validated=analysis.validation.is_valid,
            recommendations=len(analysis.recommendations)
            if analysis.is_publishable
            else 0,
        )
        return row.id

    async def latest(
        self, user_id: UUID, *, limit: int = 20, published_only: bool = True
    ) -> list[AiAnalysis]:
        statement = (
            select(AiAnalysis)
            .where(AiAnalysis.user_id == user_id)
            .order_by(AiAnalysis.created_at.desc())
            .limit(limit)
        )
        if published_only:
            statement = statement.where(AiAnalysis.evidence_validated.is_(True))
        return list((await self._session.execute(statement)).scalars().all())

    async def open_recommendations(self, user_id: UUID) -> list[AiRecommendation]:
        return list(
            (
                await self._session.execute(
                    select(AiRecommendation)
                    .where(
                        AiRecommendation.user_id == user_id,
                        AiRecommendation.status == "open",
                    )
                    .order_by(AiRecommendation.priority)
                )
            )
            .scalars()
            .all()
        )

    async def set_recommendation_status(
        self, user_id: UUID, recommendation_id: UUID, status: str
    ) -> bool:
        """Track whether the trader acted on advice.

        The only question that matters about coaching is whether acting on it changed
        the trader's numbers, and that cannot be answered without knowing which
        recommendations were adopted.
        """
        row = (
            await self._session.execute(
                select(AiRecommendation).where(
                    AiRecommendation.id == recommendation_id,
                    AiRecommendation.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.status = status
        if status in {"resolved", "dismissed"}:
            row.resolved_at = datetime.now(UTC)
        return True


def _overall_confidence(analysis: CoachingAnalysis) -> Decimal | None:
    """Mean of the model's stated confidence across its claims.

    Recorded as the model's own reading and nothing more. It shares a column name with
    statistical confidence but never a meaning — the intervals live in
    ``input_metrics``, computed in Python.
    """
    scores = [
        CONFIDENCE_VALUES[claim.confidence]
        for claim in analysis.claims
        if claim.confidence in CONFIDENCE_VALUES
    ]
    if not scores:
        return None
    return (sum(scores, start=Decimal(0)) / Decimal(len(scores))).quantize(
        Decimal("0.01")
    )
