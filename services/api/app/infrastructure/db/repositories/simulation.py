"""Loading trades for a counterfactual sweep and writing back what it computed.

Reuses the pattern repository's loader rather than defining a second one. Two loaders
would eventually disagree about which trades count, and the coach would then cite an
expectancy computed over one sample beside an expected improvement computed over
another — in the same sentence, with no way for a reader to tell.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.types import TradeRecord
from app.core.logging import get_logger
from app.infrastructure.db.models.ai import AiRecommendation
from app.infrastructure.db.repositories.patterns import SqlAlchemyPatternRepository

logger = get_logger(__name__)


class SqlAlchemySimulationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._trades = SqlAlchemyPatternRepository(session)

    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
    ) -> list[TradeRecord]:
        return await self._trades.load_trades(
            user_id,
            account_id=account_id,
            session_from=session_from,
            session_to=session_to,
        )

    async def attach_expected_improvement(
        self, user_id: UUID, recommendation_id: UUID, payload: dict[str, Any]
    ) -> bool:
        """Write a *computed* expected improvement onto a coaching recommendation.

        The column exists precisely so this figure has a home that the model cannot
        reach. A non-result is written as a non-result, with its reason — an empty
        object would be indistinguishable from "not yet simulated".
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

        row.expected_improvement = payload
        logger.info(
            "simulation.recommendation_quantified",
            user_id=str(user_id),
            recommendation_id=str(recommendation_id),
            established=payload.get("is_established"),
        )
        return True
