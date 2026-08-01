"""Loading trades for training, and storing the models that come out of it.

Reuses the pattern repository's loader for the same reason the simulation repository
does: two loaders would eventually disagree about which trades count, and a win
probability computed over one sample would then be shown beside an expectancy computed
over another, with nothing in the payload to reveal it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.types import TradeRecord
from app.core.logging import get_logger
from app.infrastructure.db.models.ai import PredictionModel
from app.infrastructure.db.repositories.patterns import SqlAlchemyPatternRepository
from app.ml.report import MODEL_VERSION, ModelReport

logger = get_logger(__name__)


class SqlAlchemyModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._trades = SqlAlchemyPatternRepository(session)

    async def load_trades(
        self, user_id: UUID, *, account_id: UUID | None = None
    ) -> list[TradeRecord]:
        return await self._trades.load_trades(user_id, account_id=account_id)

    async def save_model(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None,
        head: str,
        payload: dict[str, Any],
        report: ModelReport,
        trades: int,
    ) -> None:
        """Insert a new row rather than updating the previous one.

        Models are append-only. Overwriting would destroy the record of what was being
        served when a given piece of coaching was generated, and "the model said 68% on
        the 4th" has to remain answerable after the model is retrained. The serving path
        reads the newest row for a head.
        """
        skill = report.skill
        interval = skill.interval if skill is not None else None

        row = PredictionModel(
            user_id=user_id,
            account_id=account_id,
            head=head,
            is_deployable=report.is_deployable,
            # The database constraint requires exactly one of these to be set, which is
            # the invariant that makes an un-servable model impossible to serve.
            refusal=report.refusal,
            skill=skill.value if skill is not None else None,
            skill_low=interval.low if interval is not None else None,
            skill_high=interval.high if interval is not None else None,
            trades=trades,
            folds=report.validation.folds,
            out_of_sample_predictions=report.validation.size,
            detail=payload,
            model_version=MODEL_VERSION,
            trained_at=datetime.now(UTC),
        )
        self._session.add(row)

        logger.info(
            "ml.model_stored",
            user_id=str(user_id),
            head=head,
            deployable=report.is_deployable,
            out_of_sample=report.validation.size,
        )

    async def latest_model(
        self, user_id: UUID, *, head: str, account_id: UUID | None = None
    ) -> dict[str, Any] | None:
        statement = (
            select(PredictionModel)
            .where(PredictionModel.user_id == user_id, PredictionModel.head == head)
            .order_by(desc(PredictionModel.created_at))
            .limit(1)
        )
        if account_id is not None:
            statement = statement.where(PredictionModel.account_id == account_id)

        row = (await self._session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None

        return {
            "head": row.head,
            "is_deployable": row.is_deployable,
            "refusal": row.refusal,
            "skill": str(row.skill) if row.skill is not None else None,
            "skill_interval": (
                [str(row.skill_low), str(row.skill_high)]
                if row.skill_low is not None and row.skill_high is not None
                else None
            ),
            "trades": row.trades,
            "folds": row.folds,
            "out_of_sample_predictions": row.out_of_sample_predictions,
            "model_version": row.model_version,
            "trained_at": row.trained_at.isoformat() if row.trained_at else None,
            "detail": row.detail,
        }
