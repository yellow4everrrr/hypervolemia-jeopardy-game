"""What-if simulator endpoints.

A sweep runs a bootstrap per scenario, so it is an explicit POST rather than something
a dashboard triggers. Custom scenarios are accepted, but note the warning the response
carries: every extra scenario raises the bar for all of them through the family-wide
correction, which is the correct accounting and the reason a sweep cannot be used to
shop for a result.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.analytics.whatif import Scenario, default_scenarios
from app.application.use_cases.run_simulation import RunSimulation
from app.core.errors import NotFoundError, ValidationError
from app.infrastructure.db.repositories.simulation import SqlAlchemySimulationRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.interfaces.http.deps import CurrentUserDep, SessionDep

router = APIRouter(prefix="/simulator", tags=["simulator"])

#: Hard cap on a custom sweep. Not a performance limit — a statistical one. Testing
#: fifty variants against one history makes it nearly impossible for any of them to
#: establish anything once the family is corrected, and a caller who does it anyway has
#: built a machine for finding nothing.
MAX_SCENARIOS = 12


class ScenarioInput(BaseModel):
    label: Annotated[str, Field(min_length=1, max_length=120)]
    stop_r: Annotated[Decimal | None, Field(gt=0, le=20)] = None
    target_r: Annotated[Decimal | None, Field(gt=0, le=50)] = None
    max_trades_per_session: Annotated[int | None, Field(ge=1, le=100)] = None
    skip_after_consecutive_losses: Annotated[int | None, Field(ge=1, le=20)] = None
    only_hours: Annotated[list[int] | None, Field(max_length=24)] = None

    def to_scenario(self) -> Scenario:
        return Scenario(
            label=self.label,
            stop_r=self.stop_r,
            target_r=self.target_r,
            max_trades_per_session=self.max_trades_per_session,
            skip_after_consecutive_losses=self.skip_after_consecutive_losses,
            only_hours=tuple(self.only_hours) if self.only_hours else None,
        )

    def is_empty(self) -> bool:
        return not any(
            (
                self.stop_r,
                self.target_r,
                self.max_trades_per_session,
                self.skip_after_consecutive_losses,
                self.only_hours,
            )
        )


class SweepRequest(BaseModel):
    account_id: UUID | None = None
    session_from: date | None = None
    session_to: date | None = None
    #: Omit for the standard sweep.
    scenarios: Annotated[list[ScenarioInput] | None, Field(max_length=MAX_SCENARIOS)] = None


class QuantifyRequest(BaseModel):
    """Compute the expected improvement for one coaching recommendation."""

    recommendation_id: UUID
    scenario: ScenarioInput
    account_id: UUID | None = None


@router.post("/sweep", summary="Simulate counterfactual rules against real history")
async def run_sweep(
    payload: SweepRequest, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Re-price the trader's history under each scenario, corrected as one family.

    ``best`` is the largest *established* improvement, not the largest improvement.
    The best-performing variant in any sweep is the one that got luckiest, and
    returning it unqualified is how a simulator becomes a curve-fitting tool.
    """
    if payload.session_from and payload.session_to and payload.session_from > payload.session_to:
        raise ValidationError("session_from must not be after session_to")

    scenarios = None
    if payload.scenarios is not None:
        empty = [item.label for item in payload.scenarios if item.is_empty()]
        if empty:
            raise ValidationError(
                "a scenario must change at least one rule",
                details={"empty_scenarios": empty},
            )
        scenarios = [item.to_scenario() for item in payload.scenarios]

    outcome = await RunSimulation(
        repository=SqlAlchemySimulationRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).execute(
        user_id=user.id,
        account_id=payload.account_id,
        session_from=payload.session_from,
        session_to=payload.session_to,
        scenarios=scenarios,
    )
    return outcome.to_payload()


@router.post("/quantify", summary="Compute a recommendation's expected improvement")
async def quantify(
    payload: QuantifyRequest, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Attach a computed expected improvement to a coaching recommendation.

    The only path by which `ai_recommendations.expected_improvement` is ever filled.
    The model never writes that figure — it is simulated against the trader's real
    history, and recorded as an explicit non-result when it cannot be established.
    """
    if payload.scenario.is_empty():
        raise ValidationError("a scenario must change at least one rule")

    result = await RunSimulation(
        repository=SqlAlchemySimulationRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).quantify_recommendation(
        user_id=user.id,
        recommendation_id=payload.recommendation_id,
        scenario=payload.scenario.to_scenario(),
        account_id=payload.account_id,
    )
    if not result:
        raise NotFoundError(f"recommendation {payload.recommendation_id} not found")
    return result


@router.get("/scenarios", summary="The standard sweep")
async def scenarios(
    include_note: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """What the default sweep tests, and why it is deliberately short."""
    payload: dict[str, Any] = {
        "scenarios": [scenario.to_payload() for scenario in default_scenarios()],
        "max_custom_scenarios": MAX_SCENARIOS,
    }
    if include_note:
        payload["note"] = (
            "Every scenario added to a sweep raises the significance bar for all of "
            "them, because the family is corrected together. A short sweep of "
            "well-motivated rules will establish more than a long sweep of guesses."
        )
    return payload
