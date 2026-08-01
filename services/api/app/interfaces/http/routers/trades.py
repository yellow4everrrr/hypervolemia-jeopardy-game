"""Trade read endpoints.

Milestone 1 exposes reads only. There is no endpoint to create or edit a trade, and
there will not be one: trades are derived from fills, and a hand-edited trade is a
statistic that no longer describes anything that happened. Traders correct *context*
(setup, emotion, notes) — which milestone 5 exposes — never P&L.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.core.errors import NotFoundError
from app.infrastructure.db.repositories.trades import SqlAlchemyTradeRepository
from app.interfaces.http.deps import CurrentUserDep, SessionDep
from app.interfaces.http.schemas.trades import TradeFilters, TradeListResponse, TradeSummary

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("", response_model=TradeListResponse, summary="List trades")
async def list_trades(
    user: CurrentUserDep,
    session: SessionDep,
    filters: Annotated[TradeFilters, Query()],
) -> TradeListResponse:
    repository = SqlAlchemyTradeRepository(session)
    # One extra row tells us whether another page exists without a COUNT(*) over the
    # whole filtered set — which at 100k trades is the difference between 2ms and 200ms.
    rows = await repository.list_trades(
        user.id,
        account_id=filters.account_id,
        status=filters.status,
        session_from=filters.session_from,
        session_to=filters.session_to,
        limit=filters.limit + 1,
        offset=filters.offset,
    )
    has_more = len(rows) > filters.limit
    return TradeListResponse(
        items=[TradeSummary.model_validate(row) for row in rows[: filters.limit]],
        limit=filters.limit,
        offset=filters.offset,
        has_more=has_more,
    )


@router.get("/{trade_id}", response_model=TradeSummary, summary="Get a trade")
async def get_trade(trade_id: UUID, user: CurrentUserDep, session: SessionDep) -> TradeSummary:
    row = await SqlAlchemyTradeRepository(session).get(user.id, trade_id)
    if row is None:
        raise NotFoundError("trade not found")
    return TradeSummary.model_validate(row)
