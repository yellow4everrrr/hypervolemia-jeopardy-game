"""Run a pattern scan over a user's trades and persist what it established.

The persistence rule is the interesting one: **everything tested is stored, not only
what survived.** A pattern that failed its significance test is written with
``is_significant = false`` and shown as an observation.

Storing only the survivors would quietly turn the database into a record of every scan's
luckiest result. It would also make the trend meaningless — "this leak has shown up in
four consecutive months" is only evidence if the months where it did not show up were
also recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from app.analytics.behaviours import BehaviourFinding
from app.analytics.discovery import (
    DISCOVERY_VERSION,
    ClusterFinding,
    DiscoveryConfig,
    PatternReport,
    discover_patterns,
    propose_setups,
)
from app.analytics.types import TradeRecord
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PatternRow:
    """One pattern, flattened for storage.

    Mirrors ``detected_patterns``. Built here rather than in the repository so the
    mapping from a statistical result to a stored row is testable without a database —
    it is the step where a p-value could most easily be attached to the wrong pattern.
    """

    pattern_kind: str
    label: str
    description: str
    polarity: str
    sample_size: int
    effect_size: Decimal | None
    p_value: Decimal | None
    confidence_low: Decimal | None
    confidence_high: Decimal | None
    is_significant: bool
    estimated_annual_impact: Decimal | None
    detail: dict[str, Any]
    first_observed_at: datetime | None
    last_observed_at: datetime | None


class PatternRepository(Protocol):
    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
    ) -> list[TradeRecord]: ...

    async def replace_patterns(self, user_id: UUID, rows: list[PatternRow]) -> int: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...


@dataclass
class DetectionOutcome:
    report: PatternReport
    stored: int = 0
    setup_proposals: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.report.to_payload(),
            "stored": self.stored,
            "setup_proposals": self.setup_proposals,
        }


class DetectPatterns:
    """Scan a user's trades for behavioural leaks and hidden groupings."""

    def __init__(self, *, repository: PatternRepository, uow: UnitOfWork) -> None:
        self._repository = repository
        self._uow = uow

    async def execute(
        self,
        *,
        user_id: UUID,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
        config: DiscoveryConfig | None = None,
        persist: bool = True,
    ) -> DetectionOutcome:
        trades = await self._repository.load_trades(
            user_id,
            account_id=account_id,
            session_from=session_from,
            session_to=session_to,
        )
        report = discover_patterns(trades, config)
        outcome = DetectionOutcome(
            report=report, setup_proposals=propose_setups(report)
        )

        if persist:
            rows = build_rows(report, trades)
            outcome.stored = await self._repository.replace_patterns(user_id, rows)
            await self._uow.commit()

        logger.info(
            "patterns.detected",
            user_id=str(user_id),
            trades=len(trades),
            tests=report.tests_performed,
            findings=len(report.findings),
            stored=outcome.stored,
        )
        return outcome


def build_rows(report: PatternReport, trades: list[TradeRecord]) -> list[PatternRow]:
    """Flatten every tested pattern into storable rows.

    ``estimated_annual_impact`` is only computed for currency-denominated findings, and
    only by scaling the observed cost by how much of a year the sample covers. It is an
    extrapolation of what already happened, not a forecast, and it is left ``None``
    rather than guessed when the sample spans too little time to scale from.
    """
    scale = _annualisation_factor(trades)
    rows: list[PatternRow] = []

    for behaviour in report.behaviours:
        if behaviour.comparison is None:
            continue
        interval = behaviour.expectancy_when_present.interval
        rows.append(
            PatternRow(
                pattern_kind=behaviour.kind,
                label=behaviour.label,
                description=behaviour.description,
                polarity=behaviour.polarity,
                sample_size=behaviour.affected,
                effect_size=behaviour.comparison.effect_size,
                p_value=behaviour.comparison.adjusted_p_value or behaviour.comparison.p_value,
                confidence_low=interval.low if interval else None,
                confidence_high=interval.high if interval else None,
                is_significant=behaviour.is_actionable,
                estimated_annual_impact=(
                    behaviour.estimated_cost * scale
                    if behaviour.estimated_cost is not None and scale is not None
                    else None
                ),
                detail=behaviour.to_payload(),
                first_observed_at=None,
                last_observed_at=None,
            )
        )

    for cluster in report.clusters:
        if cluster.comparison is None:
            continue
        interval = cluster.expectancy.interval
        difference = cluster.difference
        rows.append(
            PatternRow(
                pattern_kind="cluster",
                label=cluster.label,
                description=(
                    f"A group of {cluster.size} behaviourally similar trades found by "
                    "clustering, tested against the rest of the sample."
                ),
                polarity=cluster.polarity,
                sample_size=cluster.size,
                effect_size=cluster.comparison.effect_size,
                p_value=cluster.comparison.adjusted_p_value or cluster.comparison.p_value,
                confidence_low=interval.low if interval else None,
                confidence_high=interval.high if interval else None,
                is_significant=cluster.is_actionable,
                estimated_annual_impact=(
                    difference * Decimal(cluster.size) * scale
                    if difference is not None and scale is not None
                    else None
                ),
                detail=cluster.to_payload(),
                first_observed_at=None,
                last_observed_at=None,
            )
        )

    return rows


def _annualisation_factor(trades: list[TradeRecord]) -> Decimal | None:
    """How many times the sample's span fits into a year.

    ``None`` below a month of history. Scaling three days of trading up to a year
    multiplies both the effect and every error in it by 120, and the resulting figure
    would be quoted as though it meant something.
    """
    dates = [trade.session_date for trade in trades if trade.session_date is not None]
    if len(dates) < 2:
        return None
    span_days = (max(dates) - min(dates)).days + 1
    if span_days < 30:
        return None
    return Decimal(365) / Decimal(span_days)


#: Re-exported so callers can record which engine produced a stored row.
__all__ = [
    "DISCOVERY_VERSION",
    "BehaviourFinding",
    "ClusterFinding",
    "DetectPatterns",
    "DetectionOutcome",
    "PatternRepository",
    "PatternRow",
    "build_rows",
]
