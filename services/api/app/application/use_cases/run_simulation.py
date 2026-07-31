"""Run a what-if sweep, and fill in the expected improvement the model may not write.

ADR 0002's fourth mechanism: ``ai_recommendations.expected_improvement`` is computed,
never generated. This use case is what computes it. A recommendation the coach made —
"cap yourself at three trades a session" — is turned into a scenario, simulated against
the trader's actual history, and the resulting figure is attached to the recommendation
with its confidence interval and its coverage.

If the simulation cannot establish the improvement, the field records that rather than
a number. A recommendation whose expected improvement is "not established at this sample
size" is more useful than one carrying a confident figure nobody could support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from app.analytics.types import TradeRecord
from app.analytics.whatif import Scenario, SweepReport, default_scenarios, simulate, sweep
from app.core.logging import get_logger

logger = get_logger(__name__)


class SimulationRepository(Protocol):
    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
    ) -> list[TradeRecord]: ...

    async def attach_expected_improvement(
        self, user_id: UUID, recommendation_id: UUID, payload: dict[str, Any]
    ) -> bool: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...


@dataclass
class SimulationOutcome:
    report: SweepReport = field(default_factory=SweepReport)
    recommendations_updated: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.report.to_payload(),
            "recommendations_updated": self.recommendations_updated,
        }


class RunSimulation:
    """Sweep a trader's history against counterfactual rules."""

    def __init__(
        self, *, repository: SimulationRepository, uow: UnitOfWork
    ) -> None:
        self._repository = repository
        self._uow = uow

    async def execute(
        self,
        *,
        user_id: UUID,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
        scenarios: list[Scenario] | None = None,
        permutations: int | None = None,
    ) -> SimulationOutcome:
        trades = await self._repository.load_trades(
            user_id,
            account_id=account_id,
            session_from=session_from,
            session_to=session_to,
        )
        report = sweep(
            trades, scenarios or default_scenarios(), permutations=permutations
        )
        logger.info(
            "simulation.swept",
            user_id=str(user_id),
            trades=len(trades),
            scenarios=len(report.results),
            actionable=len(report.actionable),
        )
        return SimulationOutcome(report=report)

    async def quantify_recommendation(
        self,
        *,
        user_id: UUID,
        recommendation_id: UUID,
        scenario: Scenario,
        account_id: UUID | None = None,
        permutations: int | None = None,
    ) -> dict[str, Any]:
        """Attach a computed expected improvement to one coaching recommendation.

        The only path by which ``expected_improvement`` is ever populated. The figure
        comes from re-simulating the trader's real history, carries the interval and
        the coverage that qualify it, and is written as an explicit non-result when the
        simulation cannot support it.
        """
        trades = await self._repository.load_trades(user_id, account_id=account_id)
        result = simulate(trades, scenario, permutations=permutations)

        payload: dict[str, Any] = {
            "scenario": scenario.to_payload(),
            "computed_by": "what-if simulator",
            "is_established": result.is_actionable,
            "coverage": str(result.coverage) if result.coverage is not None else None,
            "trades_evaluated": result.evaluated,
        }

        if result.is_actionable:
            payload["total_difference"] = str(result.difference)
            payload["per_trade_difference"] = (
                str(result.comparison.difference)
                if result.comparison and result.comparison.difference is not None
                else None
            )
            payload["interval"] = (
                [str(result.delta_interval.low), str(result.delta_interval.high)]
                if result.delta_interval
                else None
            )
        else:
            payload["reason"] = _why_not(result)

        updated = await self._repository.attach_expected_improvement(
            user_id, recommendation_id, payload
        )
        if updated:
            await self._uow.commit()
        return payload


def _why_not(result: Any) -> str:
    """Plain language for why an improvement could not be established.

    Written out because "expected improvement: unavailable" tells a trader nothing,
    while "only 12 of your trades have the recorded stop this needs" tells them what to
    fix.
    """
    if result.evaluated < 30:
        return (
            f"only {result.evaluated} trades could be simulated — too few to "
            "distinguish an improvement from chance"
        )
    if result.coverage is not None and result.coverage < Decimal("0.5"):
        return (
            f"only {result.coverage:.0%} of trades have the recorded stop and "
            "excursion data this scenario needs, so the result would describe a "
            "subset rather than your history"
        )
    if result.delta_interval is not None and not result.delta_interval.excludes_zero:
        return (
            "resampling this history produces both improvements and losses under this "
            "rule — the effect is not distinguishable from chance at this sample size"
        )
    return "the difference did not survive correction across the scenarios tested"
