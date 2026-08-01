"""Produce a coaching analysis, and refuse to publish one that cannot be traced.

The order of operations is the whole design:

1. Python computes the statistics.
2. They become an evidence bundle — a closed set of keyed values.
3. The model is given the bundle and returns claims written in placeholders.
4. **Every claim is validated against the bundle.**
5. Only then are placeholders rendered into the numbers the trader reads.

Step 4 can reject the analysis. That is a working system, not a failure: the row is
stored with ``evidence_validated = false`` and nothing is shown. A rising rejection rate
is a signal about the prompt, and the temptation it creates — to relax the check —
is precisely what the check exists to resist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from app.ai.contract import Claim, ValidationResult, render, validate
from app.ai.evidence import EvidenceBundle
from app.ai.prompts import PROMPT_VERSION
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.common.enums import AnalysisSubject

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Recommendation:
    statement: str
    category: str
    priority: int
    cites: tuple[str, ...]
    confidence: str


@dataclass
class CoachingAnalysis:
    """A validated analysis, ready to store and show — or a rejection, with reasons."""

    subject_type: AnalysisSubject
    subject_id: UUID
    headline: str = ""
    claims: list[Claim] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    validation: ValidationResult = field(default_factory=ValidationResult)
    bundle: EvidenceBundle = field(default_factory=EvidenceBundle)
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal | None = None
    latency_ms: int = 0
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_publishable(self) -> bool:
        return self.validation.is_valid and self.error is None and bool(self.claims)

    def to_payload(self) -> dict[str, Any]:
        """What the API returns.

        Rendered text only — the raw placeholder form stays internal. A client that
        received both would eventually display the wrong one.
        """
        return {
            "headline": render(self.headline, self.bundle),
            "is_publishable": self.is_publishable,
            "claims": [
                {
                    "statement": claim.rendered(self.bundle),
                    "kind": claim.kind,
                    "confidence": claim.confidence,
                    "cites": list(claim.cites),
                }
                for claim in self.claims
            ],
            "recommendations": [
                {
                    "statement": render(item.statement, self.bundle),
                    "category": item.category,
                    "priority": item.priority,
                    "confidence": item.confidence,
                    "cites": list(item.cites),
                }
                for item in sorted(self.recommendations, key=lambda r: r.priority)
            ],
            "what_the_data_cannot_say": list(self.limitations),
            "evidence_gaps": list(self.bundle.gaps),
            "validation": self.validation.to_payload(),
            "model": self.model,
            "prompt_version": self.prompt_version,
            "error": self.error,
        }


class CoachClient(Protocol):
    async def analyse(
        self, bundle_payload: dict[str, Any], *, question: str | None = None
    ) -> Any: ...


class CoachingRepository(Protocol):
    async def save_analysis(self, user_id: UUID, analysis: CoachingAnalysis) -> UUID: ...

    async def count_today(self, user_id: UUID) -> int: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...


class GenerateCoaching:
    """Assemble evidence, ask the model, validate the answer, then store it."""

    def __init__(
        self,
        *,
        client: CoachClient,
        repository: CoachingRepository,
        uow: UnitOfWork,
        daily_limit: int = 50,
    ) -> None:
        self._client = client
        self._repository = repository
        self._uow = uow
        self._daily_limit = daily_limit

    async def execute(
        self,
        *,
        user_id: UUID,
        subject_type: AnalysisSubject,
        subject_id: UUID,
        bundle: EvidenceBundle,
        question: str | None = None,
        persist: bool = True,
    ) -> CoachingAnalysis:
        analysis = CoachingAnalysis(
            subject_type=subject_type, subject_id=subject_id, bundle=bundle
        )

        if bundle.is_empty():
            # Asking a model to interpret nothing reliably produces something.
            analysis.error = "there is nothing to analyse — no statistics were computed"
            analysis.validation.is_valid = False
            return analysis

        used = await self._repository.count_today(user_id)
        if used >= self._daily_limit:
            raise ExternalServiceError(
                f"daily coaching limit of {self._daily_limit} analyses reached",
                details={"used": used, "limit": self._daily_limit},
            )

        response = await self._client.analyse(bundle.to_payload(), question=question)

        analysis.model = response.model
        analysis.prompt_version = response.prompt_version
        analysis.input_tokens = response.input_tokens
        analysis.output_tokens = response.output_tokens
        analysis.cost_usd = response.cost_usd
        analysis.latency_ms = response.latency_ms

        if response.refused:
            analysis.error = (
                "the model declined to analyse this request"
                + (f" ({response.refusal_category})" if response.refusal_category else "")
            )
            analysis.validation.is_valid = False
        elif response.error:
            analysis.error = response.error
            analysis.validation.is_valid = False
        else:
            _populate(analysis, response.output)
            analysis.validation = validate(analysis.claims, bundle)

        if not analysis.validation.is_valid:
            logger.warning(
                "coach.rejected",
                user_id=str(user_id),
                reasons=analysis.validation.reasons,
                error=analysis.error,
            )

        if persist:
            await self._repository.save_analysis(user_id, analysis)
            await self._uow.commit()

        return analysis


def _populate(analysis: CoachingAnalysis, output: dict[str, Any]) -> None:
    """Read the model's structured output into typed claims.

    Defensive despite the enforced schema: a truncated response can satisfy the parser
    and still be missing fields, and a missing ``kind`` silently defaulting to
    ``finding`` would upgrade an untested observation into an established one.
    """
    analysis.headline = str(output.get("headline") or "")
    analysis.limitations = [
        str(item) for item in output.get("what_the_data_cannot_say") or []
    ]

    for raw in output.get("claims") or []:
        analysis.claims.append(
            Claim(
                statement=str(raw.get("statement") or ""),
                cites=tuple(str(key) for key in raw.get("cites") or ()),
                confidence=str(raw.get("confidence") or "low"),
                kind=str(raw.get("kind") or "observation"),
            )
        )

    for raw in output.get("recommendations") or []:
        analysis.recommendations.append(
            Recommendation(
                statement=str(raw.get("statement") or ""),
                category=str(raw.get("category") or "process"),
                priority=int(raw.get("priority") or 3),
                cites=tuple(str(key) for key in raw.get("cites") or ()),
                confidence=str(raw.get("confidence") or "low"),
            )
        )

    # A recommendation is a stronger act than a claim, so it is held to the claim
    # standard: same validator, same rejection.
    analysis.claims.extend(
        Claim(
            statement=item.statement,
            cites=item.cites,
            confidence=item.confidence,
            kind="finding",
        )
        for item in analysis.recommendations
    )
