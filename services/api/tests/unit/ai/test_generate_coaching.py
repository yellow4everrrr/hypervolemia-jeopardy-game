"""The coaching use case, driven by a fake model.

The model is replaced with a stub that returns exactly what a real one might — including
the things a real one gets wrong. What is being tested is not the model's quality but
the pipeline's refusal to publish a claim it cannot trace.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.ai.evidence import COUNT, CURRENCY, PERCENT, EvidenceBundle
from app.application.use_cases.generate_coaching import CoachingAnalysis, GenerateCoaching
from app.core.errors import ExternalServiceError
from app.core.ids import uuid7
from app.domain.common.enums import AnalysisSubject


def make_bundle() -> EvidenceBundle:
    bundle = EvidenceBundle()
    bundle.put("sample.trades", 312, unit=COUNT, label="closed trades")
    bundle.put(
        "core.expectancy",
        Decimal("18.40"),
        unit=CURRENCY,
        label="expectancy per trade",
        sample_size=312,
    )
    bundle.put(
        "core.win_rate", Decimal("0.51"), unit=PERCENT, label="win rate", sample_size=312
    )
    return bundle


GOOD_OUTPUT: dict[str, Any] = {
    "headline": "You have a small positive edge over {{sample.trades}} trades.",
    "claims": [
        {
            "statement": "Expectancy is {{core.expectancy}} per trade across "
            "{{sample.trades}} trades.",
            "cites": ["core.expectancy", "sample.trades"],
            "kind": "observation",
            "confidence": "high",
        }
    ],
    "recommendations": [
        {
            "statement": "Keep sizing flat until expectancy holds above "
            "{{core.expectancy}} for another quarter.",
            "category": "sizing",
            "priority": 1,
            "cites": ["core.expectancy"],
            "confidence": "moderate",
        }
    ],
    "what_the_data_cannot_say": [
        "Whether the edge persists in a different volatility regime."
    ],
}

FABRICATED_OUTPUT: dict[str, Any] = {
    "headline": "Your Friday afternoon win rate is 34%.",
    "claims": [
        {
            "statement": "Your Friday afternoon win rate is 34%, well below your "
            "average.",
            "cites": ["core.win_rate"],
            "kind": "finding",
            "confidence": "high",
        }
    ],
    "recommendations": [],
    "what_the_data_cannot_say": [],
}


class FakeResponse:
    def __init__(
        self,
        output: dict[str, Any] | None = None,
        *,
        refused: bool = False,
        error: str | None = None,
    ) -> None:
        self.output = output or {}
        self.model = "claude-opus-5"
        self.prompt_version = "coach-v1"
        self.input_tokens = 4_200
        self.output_tokens = 800
        self.cost_usd = Decimal("0.041")
        self.latency_ms = 3_100
        self.refused = refused
        self.refusal_category = "cyber" if refused else None
        self.error = error


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls = 0

    async def analyse(
        self, bundle_payload: dict[str, Any], *, question: str | None = None
    ) -> FakeResponse:
        self.calls += 1
        self.last_payload = bundle_payload
        return self._response


class FakeRepository:
    def __init__(self, used_today: int = 0) -> None:
        self.used_today = used_today
        self.saved: list[CoachingAnalysis] = []

    async def count_today(self, user_id: UUID) -> int:
        return self.used_today

    async def save_analysis(self, user_id: UUID, analysis: CoachingAnalysis) -> UUID:
        self.saved.append(analysis)
        return uuid7()


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def build(
    response: FakeResponse, *, used_today: int = 0, limit: int = 50
) -> tuple[GenerateCoaching, FakeClient, FakeRepository, FakeUnitOfWork]:
    client = FakeClient(response)
    repository = FakeRepository(used_today)
    uow = FakeUnitOfWork()
    return (
        GenerateCoaching(
            client=client, repository=repository, uow=uow, daily_limit=limit
        ),
        client,
        repository,
        uow,
    )


async def run(use_case: GenerateCoaching, bundle: EvidenceBundle) -> CoachingAnalysis:
    return await use_case.execute(
        user_id=uuid7(),
        subject_type=AnalysisSubject.MONTH,
        subject_id=uuid7(),
        bundle=bundle,
    )


@pytest.mark.asyncio
async def test_a_well_grounded_analysis_is_published() -> None:
    use_case, _, repository, uow = build(FakeResponse(GOOD_OUTPUT))

    analysis = await run(use_case, make_bundle())

    assert analysis.is_publishable
    assert analysis.validation.is_valid
    assert repository.saved and uow.commits == 1

    payload = analysis.to_payload()
    assert payload["headline"] == "You have a small positive edge over 312 trades."
    assert "$18.40" in payload["claims"][0]["statement"]


@pytest.mark.asyncio
async def test_a_fabricated_statistic_is_never_published() -> None:
    """The end-to-end version of the central guarantee."""
    use_case, _, repository, _ = build(FakeResponse(FABRICATED_OUTPUT))

    analysis = await run(use_case, make_bundle())

    assert not analysis.is_publishable
    assert "34%" in analysis.validation.uncited_numbers
    # Stored anyway — the rejection rate is the signal that the prompt has drifted, and
    # discarding rejected analyses would hide it.
    assert repository.saved


@pytest.mark.asyncio
async def test_a_rejected_analysis_returns_its_reasons() -> None:
    use_case, _, _, _ = build(FakeResponse(FABRICATED_OUTPUT))

    payload = (await run(use_case, make_bundle())).to_payload()

    assert payload["is_publishable"] is False
    assert payload["validation"]["reasons"]


@pytest.mark.asyncio
async def test_recommendations_are_held_to_the_claim_standard() -> None:
    """A recommendation is a stronger act than an observation, not a weaker one."""
    output = {
        **GOOD_OUTPUT,
        "recommendations": [
            {
                "statement": "Cut size by 40% — your drawdown is unsustainable.",
                "category": "sizing",
                "priority": 1,
                "cites": [],
                "confidence": "high",
            }
        ],
    }
    use_case, _, _, _ = build(FakeResponse(output))

    analysis = await run(use_case, make_bundle())

    assert not analysis.is_publishable
    assert "40%" in analysis.validation.uncited_numbers


@pytest.mark.asyncio
async def test_an_empty_bundle_never_reaches_the_model() -> None:
    """Asking a model to interpret nothing reliably produces something."""
    use_case, client, _, _ = build(FakeResponse(GOOD_OUTPUT))

    analysis = await use_case.execute(
        user_id=uuid7(),
        subject_type=AnalysisSubject.MONTH,
        subject_id=uuid7(),
        bundle=EvidenceBundle(),
    )

    assert client.calls == 0
    assert not analysis.is_publishable
    assert analysis.error is not None


@pytest.mark.asyncio
async def test_a_refusal_is_reported_not_raised() -> None:
    """Safety classifiers returning 200 must not read as an outage."""
    use_case, _, _, _ = build(FakeResponse(refused=True))

    analysis = await run(use_case, make_bundle())

    assert not analysis.is_publishable
    assert analysis.error is not None and "declined" in analysis.error


@pytest.mark.asyncio
async def test_a_truncated_response_is_reported_not_published() -> None:
    use_case, _, _, _ = build(FakeResponse(error="could not parse structured output"))

    analysis = await run(use_case, make_bundle())

    assert not analysis.is_publishable
    assert analysis.error is not None


@pytest.mark.asyncio
async def test_the_daily_limit_is_enforced_before_the_model_is_called() -> None:
    """The cap has to bite before the spend, not after it."""
    use_case, client, _, _ = build(FakeResponse(GOOD_OUTPUT), used_today=50, limit=50)

    with pytest.raises(ExternalServiceError, match="daily coaching limit"):
        await run(use_case, make_bundle())

    assert client.calls == 0


@pytest.mark.asyncio
async def test_the_model_is_shown_the_gaps_not_just_the_numbers() -> None:
    """A model shown an absence guesses; one shown a stated absence reports it."""
    bundle = make_bundle()
    bundle.gaps.append("No segment survived significance testing.")
    use_case, client, _, _ = build(FakeResponse(GOOD_OUTPUT))

    await run(use_case, bundle)

    assert "No segment survived significance testing." in client.last_payload["gaps"]


@pytest.mark.asyncio
async def test_the_stored_evidence_is_the_exact_bundle_shown_to_the_model() -> None:
    """ADR 0002: every claim must be checkable against the evidence that produced it."""
    bundle = make_bundle()
    use_case, client, _, _ = build(FakeResponse(GOOD_OUTPUT))

    analysis = await run(use_case, bundle)

    assert analysis.bundle.to_payload() == client.last_payload


@pytest.mark.asyncio
async def test_cost_and_tokens_are_recorded() -> None:
    """The coach is the most expensive path in the product; it has to be auditable."""
    use_case, _, _, _ = build(FakeResponse(GOOD_OUTPUT))

    analysis = await run(use_case, make_bundle())

    assert analysis.input_tokens == 4_200
    assert analysis.cost_usd == Decimal("0.041")
    assert analysis.model == "claude-opus-5"
