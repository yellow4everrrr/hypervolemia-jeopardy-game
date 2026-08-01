"""Predictive model endpoints.

Training is an explicit ``POST`` and takes seconds — dozens of model fits plus a
bootstrap. Milestone 13 moves it to a worker; until then it is never triggered by a page
render.

Every response that carries a probability also carries what qualifies it: the
out-of-sample skill interval, the calibration report, and the sample the figures came
from. A response shaped so a client *can* render the number without the qualification is
a response that eventually will be.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from app.application.use_cases.train_models import TrainModels
from app.core.errors import NotFoundError
from app.infrastructure.db.repositories.ml import SqlAlchemyModelRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.interfaces.http.deps import CurrentUserDep, SessionDep
from app.ml.features import FEATURES
from app.ml.report import MODEL_VERSION
from app.ml.snapshot import OUTCOME_FIELDS

router = APIRouter(prefix="/predictions", tags=["predictions"])

HEADS = ("win_probability", "expected_r")


class TrainRequest(BaseModel):
    account_id: UUID | None = None
    #: Set false to evaluate without writing a row — used to preview whether a model
    #: would be deployable before committing one.
    persist: bool = True


@router.post("/train", summary="Train and validate the predictive models")
async def train_models(
    payload: TrainRequest, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Fit both heads, validate them walk-forward, and gate them.

    A model is served only if it beats the baseline that needed no model, its advantage
    survives resampling whole sessions, and its stated probabilities match observed
    frequencies. Models that fail are stored with their reason and never served.
    """
    outcome = await TrainModels(
        repository=SqlAlchemyModelRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).execute(user_id=user.id, account_id=payload.account_id, persist=payload.persist)
    return outcome.to_payload()


@router.get("/next", summary="Predict the next trade in the current session context")
async def predict_next(
    user: CurrentUserDep,
    session: SessionDep,
    account_id: UUID | None = None,
) -> dict[str, Any]:
    """Win probability and expected R for a trade taken now.

    The sequence context — trades so far today, session P&L, losing streak — comes from
    the trader's actual history through the same builder that produced the training
    rows, so a served feature cannot drift from a trained one.

    When a head is not deployable its value is ``null`` and its reason is returned in
    full. There is no degraded number and no default.
    """
    return await TrainModels(
        repository=SqlAlchemyModelRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).predict_next(user_id=user.id, account_id=account_id)


@router.get("/models/{head}", summary="The most recently trained model for a head")
async def latest_model(
    head: str,
    user: CurrentUserDep,
    session: SessionDep,
    account_id: UUID | None = None,
) -> dict[str, Any]:
    """Coefficients and evidence for the newest model of one head.

    Refused models are returned too, with their reason. They are part of the record: a
    trader asking why no probability is shown deserves the answer, and a training run
    that produced nothing is still a result.
    """
    if head not in HEADS:
        raise NotFoundError(f"unknown model head {head!r}")

    stored = await SqlAlchemyModelRepository(session).latest_model(
        user.id, head=head, account_id=account_id
    )
    if stored is None:
        raise NotFoundError(f"no {head} model has been trained yet")
    return stored


@router.get("/features", summary="What the models are allowed to look at")
async def features() -> dict[str, Any]:
    """The predictive feature space, and the fields structurally excluded from it.

    Published because "what does the model know about my trade" is a question a trader
    is entitled to a complete answer to, and because listing the excluded fields is the
    clearest way to state that the model is not reading the outcome.
    """
    return {
        "version": MODEL_VERSION,
        "features": [
            {"name": feature.name, "description": feature.description}
            for feature in FEATURES
        ],
        "excluded_outcome_fields": list(OUTCOME_FIELDS),
        "note": (
            "Every feature is read from a snapshot of what was knowable when the trade "
            "was opened. The outcome fields listed here do not exist on that snapshot, "
            "so a feature cannot reach one even by mistake. Strategy, setup and "
            "instrument are deliberately absent: encoding them at this sample size "
            "either overfits or leaks, and the segmentation analytics already compare "
            "them with a proper significance test."
        ),
    }


@router.get("/exit-rule", summary="Where to take profit, if anywhere")
async def exit_rule(
    user: CurrentUserDep,
    session: SessionDep,
    account_id: UUID | None = None,
) -> dict[str, Any]:
    """Walk-forward selection of a take-profit level.

    Only targets are searched. Re-pricing a *stop* from excursion data is systematically
    optimistic — an excursion says price reached a level, not that a fill was available
    there — by an amount comparable to any improvement it would report. A target is a
    limit order and the same artefact runs the other way, so an improvement that survives
    here survives despite a bias working against it. See ADR 0014.

    The figure reported is the out-of-sample performance of the *selection procedure*,
    never the in-sample performance of the winning rule.
    """
    from app.ml.exits import recommend

    trades = await SqlAlchemyModelRepository(session).load_trades(
        user.id, account_id=account_id
    )
    return recommend(trades).to_payload()
