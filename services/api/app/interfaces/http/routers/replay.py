"""Replay and market-data endpoints.

The replay endpoint returns everything the chart needs in one response: the window, the
bars, and the markers. Three round trips to render one replay would make scrubbing
through a day's trades feel broken.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from app.application.use_cases.capture_screenshots import CaptureScreenshots
from app.application.use_cases.compute_excursions import ComputeExcursions, summarise
from app.core.errors import NotFoundError
from app.domain.common.enums import ExecutionRole, Timeframe
from app.domain.marketdata.replay import LegInput, build_markers, build_risk_box, build_window
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade, TradeExecution
from app.infrastructure.db.repositories.bars import SqlAlchemyBarRepository
from app.infrastructure.db.repositories.excursions import SqlAlchemyExcursionRepository
from app.infrastructure.db.repositories.screenshots import SqlAlchemyScreenshotRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.storage.objects import screenshot_key
from app.interfaces.http.deps import CurrentUserDep, ObjectStoreDep, SessionDep

router = APIRouter(prefix="/replay", tags=["replay"])

#: Cap on bars returned in one response. A misconfigured window on a 15-second chart
#: could otherwise ask for a hundred thousand candles.
MAX_BARS = 5_000


@router.get("/trades/{trade_id}", summary="Replay data for one trade")
async def trade_replay(
    trade_id: UUID,
    user: CurrentUserDep,
    session: SessionDep,
    timeframe: Annotated[Timeframe | None, Query()] = None,
) -> dict[str, Any]:
    """Window, bars and markers for one trade, in a single response.

    ``timeframe`` overrides the automatic choice. Anything coarser than the stored
    resolution is aggregated on the fly rather than requiring a second stored series.
    """
    row = (
        await session.execute(
            select(Trade, Instrument)
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .where(Trade.id == trade_id, Trade.user_id == user.id)
        )
    ).first()
    if row is None:
        raise NotFoundError("trade not found")
    trade, instrument = row

    window = build_window(
        instrument_symbol=instrument.symbol,
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
        duration_seconds=trade.duration_seconds,
    )

    legs = (
        await session.execute(
            select(TradeExecution)
            .where(TradeExecution.trade_id == trade_id)
            .order_by(TradeExecution.leg_index)
        )
    ).scalars().all()

    markers = build_markers(
        direction=trade.direction,
        legs=[
            LegInput(
                at=leg.executed_at,
                price=leg.price,
                quantity=leg.quantity,
                is_entry=leg.role is ExecutionRole.ENTRY,
            )
            for leg in legs
        ],
        stop_price=trade.initial_stop_price,
        target_price=trade.initial_target_price,
        mae_price=trade.mae_price,
        mfe_price=trade.mfe_price,
    )

    # `load_at`, not `load_series`: aggregation has to apply to the *automatic* timeframe
    # too, not only to a caller's override. `build_window` picks a resolution from the
    # trade's duration, and that resolution is frequently one nobody stored — a 38-minute
    # trade selects 2m against a feed that writes 1m. Reading storage directly returned an
    # empty series, which the endpoint reported as `bar_count: 0` and the chart drew as a
    # blank pane with no indication that a resample would have filled it.
    served = timeframe or window.primary_timeframe
    if served.seconds < window.primary_timeframe.seconds:
        served = window.primary_timeframe

    stored = await SqlAlchemyBarRepository(session).load_at(
        instrument.id,
        served,
        start=window.window_start,
        end=window.window_end,
        symbol=instrument.symbol,
    )

    payload = window.to_payload()
    # The resolution the bars are actually at, which is not always the one the window
    # chose: a caller may coarsen it. The UI labels the chart from this rather than from
    # `primary_timeframe`, so the axis never claims an interval the candles are not.
    payload["served_timeframe"] = served.value
    payload["markers"] = [marker.to_payload() for marker in markers]
    payload["risk_box"] = build_risk_box(
        direction=trade.direction,
        entry_price=trade.avg_entry_price or Decimal(0),
        stop_price=trade.initial_stop_price,
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
    )
    payload["bars"] = [
        {
            "ts": bar.ts.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume) if bar.volume is not None else None,
        }
        for bar in stored.bars[:MAX_BARS]
    ]
    payload["bar_count"] = len(stored)
    # Gaps are surfaced so the chart can break the line rather than drawing a
    # straight segment across a session close as though price moved through it.
    payload["gaps"] = [
        {"from": start.isoformat(), "to": end.isoformat()} for start, end in stored.gaps()
    ]
    payload["trade"] = {
        "id": str(trade.id),
        "direction": trade.direction.value,
        "net_pnl": str(trade.net_pnl),
        "avg_entry_price": str(trade.avg_entry_price) if trade.avg_entry_price else None,
        "avg_exit_price": str(trade.avg_exit_price) if trade.avg_exit_price else None,
    }
    return payload


@router.post("/excursions", summary="Measure MAE/MFE for trades that lack it")
async def compute_excursions(
    user: CurrentUserDep,
    session: SessionDep,
    account_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> dict[str, Any]:
    """Backfill excursions, which is what turns on the edge ratio.

    Reports coverage explicitly: a low ratio means the excursion statistics describe a
    biased subset of trades — typically those in liquid instruments during the hours
    the bar backfill happened to reach.
    """
    repository = SqlAlchemyExcursionRepository(session)
    use_case = ComputeExcursions(
        trades=repository,
        bars=SqlAlchemyBarRepository(session),
        writer=repository,
        uow=SqlAlchemyUnitOfWork(session),
    )
    result = await use_case.execute(user_id=user.id, account_id=account_id, limit=limit)
    return summarise(result)


@router.post("/trades/{trade_id}/screenshots", summary="Capture chart images for a trade")
async def capture_screenshots(
    trade_id: UUID,
    user: CurrentUserDep,
    session: SessionDep,
    store: ObjectStoreDep,
) -> dict[str, Any]:
    """Render and store the six frames now, rather than waiting for the queue.

    The automatic path is a job enqueued per trade after a sync; this is the same use
    case invoked directly, for a trade whose bars arrived late or whose first capture
    predates a backfill. Idempotent either way — content-addressed keys and an upsert on
    (trade, kind, timeframe).
    """
    outcome = await CaptureScreenshots(
        repository=SqlAlchemyScreenshotRepository(session),
        storage=store,
        uow=SqlAlchemyUnitOfWork(session),
        key_builder=screenshot_key,
    ).execute(user_id=user.id, trade_id=trade_id, now=datetime.now(UTC))
    return outcome.to_payload()


@router.get("/trades/{trade_id}/screenshots", summary="Chart images for a trade")
async def list_screenshots(
    trade_id: UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    rows = await SqlAlchemyScreenshotRepository(session).list_for_trade(user.id, trade_id)
    return {
        "items": [
            {
                "id": str(row.id),
                "kind": row.kind.value,
                "timeframe": row.timeframe.value if row.timeframe else None,
                "content_type": row.content_type,
                "width": row.width,
                "height": row.height,
                "byte_size": row.byte_size,
                "captured_at": row.captured_at.isoformat(),
                "source": row.source,
            }
            for row in rows
        ]
    }


@router.get("/screenshots/{screenshot_id}/image", summary="One chart image")
async def screenshot_image(
    screenshot_id: UUID,
    user: CurrentUserDep,
    session: SessionDep,
    store: ObjectStoreDep,
) -> Response:
    """Stream the bytes through the API rather than handing out a bucket URL.

    A presigned URL would be cheaper and would also be a capability that outlives the
    session, travels in a referrer header and cannot be revoked. These are images of a
    trader's positions; the extra hop is worth it.
    """
    row = await SqlAlchemyScreenshotRepository(session).get(user.id, screenshot_id)
    if row is None:
        raise NotFoundError(f"screenshot {screenshot_id} not found")

    data = await store.get(row.storage_key)
    return Response(
        content=data,
        media_type=row.content_type,
        # Immutable: the key is a hash of the bytes, so a given id always returns the
        # same image and a cache never needs to revalidate.
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/bars/{instrument_id}", summary="Raw bars for an instrument")
async def bars(
    instrument_id: UUID,
    user: CurrentUserDep,
    session: SessionDep,
    timeframe: Annotated[Timeframe, Query()] = Timeframe.M1,
    start: Annotated[str | None, Query()] = None,
    end: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Bars in a window, plus what coverage exists for this instrument."""
    from datetime import UTC, datetime, timedelta

    finish = datetime.fromisoformat(end) if end else datetime.now(UTC)
    begin = datetime.fromisoformat(start) if start else finish - timedelta(days=1)

    repository = SqlAlchemyBarRepository(session)
    series = await repository.load_series(instrument_id, timeframe, start=begin, end=finish)
    earliest, latest, count = await repository.coverage(instrument_id, timeframe)

    return {
        "instrument_id": str(instrument_id),
        "timeframe": timeframe.value,
        "coverage": {
            "earliest": earliest.isoformat() if earliest else None,
            "latest": latest.isoformat() if latest else None,
            "total_bars": count,
        },
        "bars": [
            {
                "ts": bar.ts.isoformat(),
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume) if bar.volume is not None else None,
            }
            for bar in series.bars[:MAX_BARS]
        ],
    }
