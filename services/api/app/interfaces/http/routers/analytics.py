"""Analytics endpoints.

Computed on request rather than read from a cache, because milestone 3 has no scheduler
yet. The engine runs a full bootstrap and Monte Carlo, which takes a second or two on a
few thousand trades — acceptable for an explicit request, not for a dashboard poll. The
``light`` query parameter drops segmentation and significance testing for interactive
use, and milestone 13 moves the heavy path to a background job reading
``performance_metrics``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from app.analytics.engine import AnalyticsConfig
from app.analytics.segmentation import SEGMENT_DIMENSIONS
from app.application.use_cases.compute_metrics import ComputePerformanceMetrics
from app.core.errors import NotFoundError, ValidationError
from app.infrastructure.db.repositories.analytics import SqlAlchemyAnalyticsRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.interfaces.http.deps import CurrentUserDep, SessionDep

router = APIRouter(prefix="/analytics", tags=["analytics"])

#: Interactive renders skip the parts that dominate runtime. Nothing that is skipped can
#: be presented as a finding: without significance testing every segment reports
#: ``is_actionable = false``, so a fast render is less informative, never misleading.
LIGHT_CONFIG = AnalyticsConfig(
    bootstrap_iterations=2_000,
    monte_carlo_iterations=1_000,
    include_segmentation=False,
    test_significance=False,
)


@router.get("/performance", summary="Full performance report")
async def performance(
    user: CurrentUserDep,
    session: SessionDep,
    account_id: Annotated[UUID | None, Query()] = None,
    session_from: Annotated[date | None, Query()] = None,
    session_to: Annotated[date | None, Query()] = None,
    starting_equity: Annotated[Decimal | None, Query()] = None,
    light: Annotated[bool, Query(description="Skip segmentation for a faster response")] = False,
    persist: Annotated[bool, Query(description="Store the computed metrics")] = False,
) -> dict[str, Any]:
    """Compute the complete metric set for the caller's trades.

    ``starting_equity`` unlocks the statistics that need a capital base: a comparable
    Sharpe, MAR, and risk of ruin. Without it those are returned undefined with an
    explanation rather than computed on a dollar basis and silently mislabelled.
    """
    if session_from and session_to and session_from > session_to:
        raise ValidationError("session_from must not be after session_to")

    use_case = ComputePerformanceMetrics(
        repository=SqlAlchemyAnalyticsRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
    config = (
        LIGHT_CONFIG
        if light
        else AnalyticsConfig(starting_equity=starting_equity)
    )
    if light and starting_equity is not None:
        config = AnalyticsConfig(
            starting_equity=starting_equity,
            bootstrap_iterations=LIGHT_CONFIG.bootstrap_iterations,
            monte_carlo_iterations=LIGHT_CONFIG.monte_carlo_iterations,
            include_segmentation=False,
            test_significance=False,
        )

    result = await use_case.execute(
        user_id=user.id,
        account_id=account_id,
        session_from=session_from,
        session_to=session_to,
        starting_equity=starting_equity,
        config=config,
        persist=persist,
    )
    return result.report.to_payload()


@router.get("/segments/{dimension}", summary="Performance along one dimension")
async def segment(
    dimension: str,
    user: CurrentUserDep,
    session: SessionDep,
    account_id: Annotated[UUID | None, Query()] = None,
    session_from: Annotated[date | None, Query()] = None,
    session_to: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    """Break performance down by one dimension, with significance control.

    Every segment is tested against the rest of the sample and the whole family is
    FDR-adjusted, so ``is_actionable`` means "survived the scan", not "looked best".
    """
    if dimension not in SEGMENT_DIMENSIONS:
        raise NotFoundError(
            f"unknown dimension {dimension!r}",
            details={"available": sorted(SEGMENT_DIMENSIONS)},
        )

    use_case = ComputePerformanceMetrics(
        repository=SqlAlchemyAnalyticsRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
    result = await use_case.execute(
        user_id=user.id,
        account_id=account_id,
        session_from=session_from,
        session_to=session_to,
        config=AnalyticsConfig(segmentation_dimensions=(dimension,)),
        persist=False,
    )

    payload = result.report.to_payload()
    return {
        "dimension": dimension,
        "sample": payload["sample"],
        "analysis": payload["segments"].get(dimension, {}),
    }


@router.get("/dimensions", summary="Available segmentation dimensions")
async def dimensions() -> dict[str, list[str]]:
    return {"dimensions": sorted(SEGMENT_DIMENSIONS)}


@router.get("/equity-curve", summary="Cumulative equity curve")
async def equity_curve(
    user: CurrentUserDep,
    session: SessionDep,
    account_id: Annotated[UUID | None, Query()] = None,
    session_from: Annotated[date | None, Query()] = None,
    session_to: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    """Points for the equity chart, with drawdown at each step.

    Decimals are serialised as strings; a large cumulative P&L would lose precision as
    a JSON number in every browser that parses it.
    """
    from app.analytics.drawdown import build_equity_curve, summarise_drawdown

    repository = SqlAlchemyAnalyticsRepository(session)
    trades = await repository.load_trades(
        user.id, account_id=account_id, session_from=session_from, session_to=session_to
    )
    points = build_equity_curve(trades)
    drawdown = summarise_drawdown(points)

    return {
        "points": [
            {
                "index": point.index,
                "at": point.at.isoformat(),
                "trade_pnl": str(point.trade_pnl),
                "cumulative_pnl": str(point.cumulative_pnl),
                "peak": str(point.peak),
                "drawdown": str(point.drawdown),
                "cumulative_r": str(point.cumulative_r) if point.cumulative_r else None,
            }
            for point in points
        ],
        "summary": {
            "max_drawdown": str(drawdown.max_drawdown),
            "current_drawdown": str(drawdown.current_drawdown),
            "final_equity": str(drawdown.final_equity),
            "peak_equity": str(drawdown.peak_equity),
            "recovery_factor": (
                str(drawdown.recovery_factor) if drawdown.recovery_factor else None
            ),
            "is_underwater": drawdown.is_underwater,
        },
    }
