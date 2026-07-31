"""Train the predictive models, store them, and serve predictions from them.

Training and serving are separate operations for a reason worth stating: training is
seconds of bootstrapping and dozens of model fits, serving is a dot product. Collapsing
them into one endpoint would put a statistical decision on the request path, where it
would be cached to make it fast, and a cached deployability gate is a gate that
eventually stops being checked.

**Refused models are stored.** A row saying "this model does not beat your base rate" is
worth keeping: the trader is owed the reason, and the next training run needs a prior
result to be compared against. The database constraint enforces that such a row can
never be served.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.analytics.types import TradeRecord
from app.core.logging import get_logger
from app.ml.report import MODEL_VERSION, ModelReport, TrainingReport, predict, train
from app.ml.snapshot import build_snapshots

logger = get_logger(__name__)


class ModelRepository(Protocol):
    async def load_trades(
        self, user_id: UUID, *, account_id: UUID | None = None
    ) -> list[TradeRecord]: ...

    async def save_model(
        self, user_id: UUID, *, account_id: UUID | None, head: str, payload: dict[str, Any],
        report: ModelReport, trades: int,
    ) -> None: ...

    async def latest_model(
        self, user_id: UUID, *, head: str, account_id: UUID | None = None
    ) -> dict[str, Any] | None: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...


@dataclass
class TrainingOutcome:
    report: TrainingReport
    stored: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {**self.report.to_payload(), "models_stored": self.stored}


class TrainModels:
    """Fit, validate, gate and persist a trader's predictive models."""

    def __init__(self, *, repository: ModelRepository, uow: UnitOfWork) -> None:
        self._repository = repository
        self._uow = uow

    async def execute(
        self,
        *,
        user_id: UUID,
        account_id: UUID | None = None,
        persist: bool = True,
    ) -> TrainingOutcome:
        trades = await self._repository.load_trades(user_id, account_id=account_id)
        report = train(build_snapshots(trades))

        stored = 0
        if persist:
            for head in (report.win_probability, report.expected_r):
                await self._repository.save_model(
                    user_id,
                    account_id=account_id,
                    head=head.head,
                    payload=head.to_payload(),
                    report=head,
                    trades=report.trades,
                )
                stored += 1
            await self._uow.commit()

        logger.info(
            "ml.trained",
            user_id=str(user_id),
            trades=report.trades,
            version=MODEL_VERSION,
            win_probability_deployable=report.win_probability.is_deployable,
            expected_r_deployable=report.expected_r.is_deployable,
        )
        return TrainingOutcome(report=report, stored=stored)

    async def predict_next(
        self,
        *,
        user_id: UUID,
        account_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Predict the outcome of the trader's *next* trade in the current context.

        Retrains rather than loading stored coefficients. That is deliberate for now and
        deliberately temporary: the sequence features — trades so far today, session P&L,
        losing streak — are derived from the history, and reconstructing that context
        from a stored model without re-deriving it is a second code path that could
        disagree with the first. Milestone 13 moves training to a worker and lets this
        read the stored row; until then, correctness beats latency on an endpoint nobody
        calls in a loop.
        """
        trades = await self._repository.load_trades(user_id, account_id=account_id)
        samples = build_snapshots(trades)
        report = train(samples)

        if not samples:
            return {
                "available": False,
                "reason": "no trades have been recorded yet",
            }

        # The context the next trade would be taken in: the state left behind by the
        # most recent one. Built through the same function that builds training rows, so
        # the served features cannot drift from the trained ones.
        latest = samples[-1].snapshot

        win = predict(report.win_probability, latest)
        expected = predict(report.expected_r, latest)

        return {
            "available": win is not None or expected is not None,
            "as_of": datetime.now(UTC).isoformat(),
            "context": {
                "session_date": latest.session_date.isoformat() if latest.session_date else None,
                "trades_before_today": latest.trades_before_today,
                "session_pnl_before": str(latest.session_pnl_before),
                "consecutive_losses_before": latest.consecutive_losses_before,
            },
            "win_probability": (
                {"value": str(win)}
                if win is not None
                else {"value": None, "reason": report.win_probability.refusal}
            ),
            "expected_r": (
                {"value": str(expected)}
                if expected is not None
                else {"value": None, "reason": report.expected_r.refusal}
            ),
            "caveat": (
                "These figures describe trades resembling your past ones in the "
                "features the model can see. They are not a forecast of the market, and "
                "they cannot account for anything about this particular setup that your "
                "history does not record."
            ),
        }
