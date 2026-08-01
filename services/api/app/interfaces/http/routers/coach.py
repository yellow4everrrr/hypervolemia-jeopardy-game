"""Coaching endpoints.

Generating an analysis runs the analytics engine, the compliance engine and the pattern
scan, then calls a model — seconds of work and real money per request. It is an
explicit POST with a daily cap, and previous analyses are read back rather than
regenerated.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.ai.builder import build_bundle
from app.ai.evidence import EvidenceBundle
from app.analytics.discovery import DiscoveryConfig
from app.analytics.engine import AnalyticsConfig
from app.application.use_cases.compute_metrics import ComputePerformanceMetrics
from app.application.use_cases.detect_patterns import DetectPatterns
from app.application.use_cases.evaluate_compliance import EvaluateCompliance
from app.application.use_cases.generate_coaching import GenerateCoaching
from app.core.config import get_settings
from app.core.errors import ExternalServiceError, NotFoundError, ValidationError
from app.domain.common.enums import AnalysisSubject
from app.infrastructure.ai.anthropic_client import AnthropicCoach
from app.infrastructure.db.repositories.analytics import SqlAlchemyAnalyticsRepository
from app.infrastructure.db.repositories.coaching import SqlAlchemyCoachingRepository
from app.infrastructure.db.repositories.compliance import SqlAlchemyComplianceRepository
from app.infrastructure.db.repositories.patterns import SqlAlchemyPatternRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.interfaces.http.deps import CurrentUserDep, SessionDep

router = APIRouter(prefix="/coach", tags=["coach"])

#: The evidence assembly runs three engines. These configs keep a coaching request in
#: the seconds rather than the tens of seconds; the statistics are unchanged, only the
#: resampling resolution is lower.
COACH_ANALYTICS = AnalyticsConfig(bootstrap_iterations=4_000, monte_carlo_iterations=2_000)
COACH_PATTERNS = DiscoveryConfig(permutations=3_000)


class AnalysisRequest(BaseModel):
    account_id: UUID | None = None
    session_from: date | None = None
    session_to: date | None = None
    starting_equity: Decimal | None = None
    #: A specific question. Omit for a general review.
    question: Annotated[str | None, Field(max_length=500)] = None


class RecommendationStatus(BaseModel):
    status: Annotated[str, Field(pattern="^(open|accepted|dismissed|resolved)$")]


@router.post("/analyse", summary="Generate a coaching analysis")
async def analyse(
    payload: AnalysisRequest, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Compute the evidence, ask the coach, validate every claim, then store it.

    A response with ``is_publishable = false`` is a *successful* request whose analysis
    did not survive validation. The claims are withheld and ``validation.reasons``
    explains why — the alternative, showing an unverified claim with a caveat, is how a
    fabricated statistic reaches a trading decision.
    """
    settings = get_settings()
    if settings.anthropic_api_key is None:
        raise ExternalServiceError(
            "the coaching model is not configured for this deployment",
            details={"missing": "anthropic_api_key"},
        )
    if payload.session_from and payload.session_to and payload.session_from > payload.session_to:
        raise ValidationError("session_from must not be after session_to")

    bundle = await _assemble_evidence(
        user_id=user.id,
        session=session,
        account_id=payload.account_id,
        session_from=payload.session_from,
        session_to=payload.session_to,
        starting_equity=payload.starting_equity,
    )

    use_case = GenerateCoaching(
        client=AnthropicCoach(settings),
        repository=SqlAlchemyCoachingRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
        daily_limit=settings.ai_daily_analysis_limit,
    )
    analysis = await use_case.execute(
        user_id=user.id,
        subject_type=AnalysisSubject.STRATEGY if payload.question else AnalysisSubject.MONTH,
        subject_id=payload.account_id or user.id,
        bundle=bundle,
        question=payload.question,
    )
    return analysis.to_payload()


@router.get("/evidence", summary="The evidence a coaching analysis would be given")
async def evidence(
    user: CurrentUserDep,
    session: SessionDep,
    account_id: Annotated[UUID | None, Query()] = None,
    session_from: Annotated[date | None, Query()] = None,
    session_to: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    """Return the bundle without calling a model.

    Exposed deliberately. The claim that the coach only interprets computed statistics
    is checkable by a trader, not just by us: this is the complete set of things it is
    able to say, and anything absent here is something it cannot claim.
    """
    bundle = await _assemble_evidence(
        user_id=user.id,
        session=session,
        account_id=account_id,
        session_from=session_from,
        session_to=session_to,
        starting_equity=None,
    )
    return {
        **bundle.to_payload(),
        "citable_keys": sorted(bundle.citable_keys),
        "note": (
            "This is the coach's entire factual world. It writes placeholders naming "
            "these keys and the server substitutes the computed values, so a statistic "
            "absent from this list is one it cannot state."
        ),
    }


@router.get("/analyses", summary="Previous analyses")
async def analyses(
    user: CurrentUserDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    include_rejected: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    rows = await SqlAlchemyCoachingRepository(session).latest(
        user.id, limit=limit, published_only=not include_rejected
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "created_at": row.created_at.isoformat(),
                "subject_type": row.subject_type.value,
                "summary": row.summary,
                "model": row.model,
                "prompt_version": row.prompt_version,
                "evidence_validated": row.evidence_validated,
                "confidence": str(row.confidence) if row.confidence else None,
                "cost_usd": str(row.cost_usd) if row.cost_usd else None,
                "output": row.output,
            }
            for row in rows
        ]
    }


@router.get("/recommendations", summary="Open recommendations")
async def recommendations(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    rows = await SqlAlchemyCoachingRepository(session).open_recommendations(user.id)
    return {
        "items": [
            {
                "id": str(row.id),
                "priority": row.priority,
                "category": row.category,
                "statement": row.statement,
                "evidence": row.evidence,
                "expected_improvement": row.expected_improvement,
                "status": row.status,
            }
            for row in rows
        ],
        "note": (
            "expected_improvement is filled by the what-if simulator, never by the "
            "model. An empty object means the counterfactual has not been run."
        ),
    }


@router.patch("/recommendations/{recommendation_id}", summary="Accept or dismiss")
async def update_recommendation(
    recommendation_id: UUID,
    payload: RecommendationStatus,
    user: CurrentUserDep,
    session: SessionDep,
) -> dict[str, Any]:
    """Record whether the trader acted.

    Without this the platform cannot answer the only question that matters about
    coaching: did following it change their numbers?
    """
    repository = SqlAlchemyCoachingRepository(session)
    if not await repository.set_recommendation_status(
        user.id, recommendation_id, payload.status
    ):
        raise NotFoundError(f"recommendation {recommendation_id} not found")
    await SqlAlchemyUnitOfWork(session).commit()
    return {"id": str(recommendation_id), "status": payload.status}


async def _assemble_evidence(
    *,
    user_id: UUID,
    session: Any,
    account_id: UUID | None,
    session_from: date | None,
    session_to: date | None,
    starting_equity: Decimal | None,
) -> EvidenceBundle:
    """Run every engine and fold the results into one bundle.

    Nothing here is persisted. A coaching request must not silently rewrite the stored
    metrics — the trader asked a question, not for a recomputation.
    """
    metrics = await ComputePerformanceMetrics(
        repository=SqlAlchemyAnalyticsRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).execute(
        user_id=user_id,
        account_id=account_id,
        session_from=session_from,
        session_to=session_to,
        starting_equity=starting_equity,
        config=COACH_ANALYTICS,
        persist=False,
    )

    patterns = await DetectPatterns(
        repository=SqlAlchemyPatternRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).execute(
        user_id=user_id,
        account_id=account_id,
        session_from=session_from,
        session_to=session_to,
        config=COACH_PATTERNS,
        persist=False,
    )

    compliance = await EvaluateCompliance(
        repository=SqlAlchemyComplianceRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).execute(
        user_id=user_id,
        account_id=account_id,
        session_from=session_from,
        session_to=session_to,
        persist=False,
    )

    return build_bundle(
        analytics=metrics.report,
        patterns=patterns.report,
        compliance=compliance.to_payload(),
        rule_impacts=compliance.impacts,
    )
