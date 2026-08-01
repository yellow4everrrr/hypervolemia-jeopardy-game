"""Walk-forward validation, split on session boundaries.

Every number the ML layer reports comes from here, so the two rules this module enforces
are worth stating before the code.

**Rule one: never a random split.** Cross-validation shuffles rows, which for time series
means training on next March and testing on last January. The resulting score is not
optimistic, it is meaningless — it measures how well the model interpolates a history it
has already seen both sides of. Trading data makes this worse than usual because it is
non-stationary in every direction at once: the market regime changes, and so does the
trader, who is learning. A model validated by random k-fold will report an accuracy it
can never reproduce on the next trade, which is the only trade anyone cares about.

The split here is strictly forward: train on everything up to a point, test on what comes
after, roll the point forward, repeat. Every prediction scored is a prediction made from
data that existed before the trade happened.

**Rule two: never split inside a session.** Trades in the same session are not
independent draws. They share a market regime, a news cycle, and — most importantly — a
trader, in one mood, running one plan, on one particular morning. Splitting mid-session
puts the same day's trades on both sides of the boundary and lets the model learn the
day, which is leakage that no amount of forward ordering prevents. Folds therefore break
on session dates and never inside one.

**What is deliberately not here: a validation set for hyperparameter selection.** The
ridge penalty is chosen inside each training fold, by a nested forward split of the
training window alone. Choosing it on the test fold — even indirectly, even just by
picking the penalty with the best test score afterwards — makes the reported performance
an in-sample number wearing an out-of-sample label. That is the most common way a
walk-forward evaluation lies, and it is invisible in the output.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.ml.features import FEATURES, PredictiveFeature, Standardiser, build_design
from app.ml.regression import PENALTY_GRID, FittedModel, fit_linear, fit_logistic
from app.ml.snapshot import LabelledSnapshot

#: Extracts the training target from a labelled snapshot, or ``None`` when the trade has
#: no target — a trade with no recorded stop has no R multiple, and such rows are dropped
#: with a count rather than imputed.
LabelFn = Callable[[LabelledSnapshot], Decimal | None]

#: Sessions in the first training window. A model trained on three days of trades and
#: tested on the fourth is measuring the fourth day's weather.
MIN_TRAIN_SESSIONS = 20

#: Sessions advanced per fold. One session per fold maximises the number of scored
#: predictions but refits the model once per trading day of history, which on three years
#: of data is 750 fits. Five is the compromise; it is a constructor argument because the
#: right answer depends on how much history a trader has.
DEFAULT_STEP_SESSIONS = 5

#: Fewer scored predictions than this and no calibration claim is made at all.
MIN_SCORED_PREDICTIONS = 50


@dataclass(frozen=True, slots=True)
class Prediction:
    """One out-of-sample prediction and what actually happened.

    Attributes:
        fold: Which walk-forward fold produced it. Kept so that a reviewer can check
            whether apparent skill is concentrated in one lucky window — a model that is
            excellent in fold 3 and useless in the other six has not found an edge, it
            has found a month.
    """

    trade_id: str
    fold: int
    predicted: Decimal
    actual: Decimal
    won: bool
    #: The session this prediction belongs to. Carried so that the skill interval can
    #: resample whole sessions: trades within a day are correlated, and resampling them
    #: individually would produce an interval far narrower than the evidence supports.
    session: date | None = None


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Out-of-sample predictions plus the bookkeeping that qualifies them."""

    predictions: tuple[Prediction, ...]
    folds: int
    train_rows: int
    skipped_rows: int
    chosen_penalties: tuple[Decimal, ...]
    #: Set when validation could not run, in plain language for the API to pass through.
    refusal: str | None = None

    @property
    def size(self) -> int:
        return len(self.predictions)

    @property
    def is_scorable(self) -> bool:
        return self.refusal is None and self.size >= MIN_SCORED_PREDICTIONS


def _sessions_of(samples: Sequence[LabelledSnapshot]) -> list[date]:
    """Ordered distinct session dates.

    A snapshot without a session date cannot be placed in the forward ordering at all, so
    it is excluded upstream rather than sorted next to a real one.
    """
    seen: dict[date, None] = {}
    for sample in samples:
        session = sample.snapshot.session_date
        if session is not None:
            seen.setdefault(session, None)
    return sorted(seen)


def _select_penalty(
    train: Sequence[LabelledSnapshot],
    *,
    features: Sequence[PredictiveFeature],
    label_of: LabelFn,
    classify: bool,
) -> Decimal:
    """Choose a ridge strength using only the training window.

    A nested forward split: fit on the first 70% of the training sessions, score the
    rest, keep the penalty with the best score. When the training window is too short to
    split, the middle of the grid is used rather than an arbitrary end — a deliberate
    default, not a fitted one, and the deployability gate does not care which since a
    window that short will not clear the sample requirements anyway.
    """
    sessions = _sessions_of(train)
    if len(sessions) < 4:
        return PENALTY_GRID[len(PENALTY_GRID) // 2]

    cut = sessions[max(1, (len(sessions) * 7) // 10) - 1]
    inner_train = [s for s in train if s.snapshot.session_date and s.snapshot.session_date <= cut]
    inner_test = [s for s in train if s.snapshot.session_date and s.snapshot.session_date > cut]
    if not inner_train or not inner_test:
        return PENALTY_GRID[len(PENALTY_GRID) // 2]

    best_penalty = PENALTY_GRID[len(PENALTY_GRID) // 2]
    best_score: Decimal | None = None

    for penalty in PENALTY_GRID:
        fitted = _fit_fold(
            inner_train, features=features, label_of=label_of, classify=classify,
            penalty=penalty,
        )
        if fitted is None:
            continue
        model, standardiser = fitted
        score = _score_fold(inner_test, model, standardiser, label_of, classify)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best_score = score
            best_penalty = penalty

    return best_penalty


def _fit_fold(
    train: Sequence[LabelledSnapshot],
    *,
    features: Sequence[PredictiveFeature],
    label_of: LabelFn,
    classify: bool,
    penalty: Decimal,
) -> tuple[FittedModel, Standardiser] | None:
    """Fit one model on a training window, standardiser included.

    The standardiser is fit here — inside the training window — and returned with the
    model so that the caller physically cannot apply a scaler learned from test data.
    """
    labelled = [(sample, label_of(sample)) for sample in train]
    usable = [(snapshot.snapshot, value) for snapshot, value in labelled if value is not None]
    if not usable:
        return None

    standardiser = Standardiser.fit([snapshot for snapshot, _ in usable], features=features)
    if standardiser.width == 0:
        return None

    design = build_design(usable, standardiser)
    if design.size == 0:
        return None

    fit = fit_logistic if classify else fit_linear
    model = fit(design.rows, design.labels, standardiser.names, penalty=penalty)
    return model, standardiser


def _score_fold(
    test: Sequence[LabelledSnapshot],
    model: FittedModel,
    standardiser: Standardiser,
    label_of: LabelFn,
    classify: bool,
) -> Decimal | None:
    """Mean squared error on a held-out window. Lower is better, for both heads.

    For the classifier this is the Brier score, which is a proper scoring rule — it is
    minimised by honest probabilities, so penalty selection cannot be gamed by a model
    that becomes more confident. Selecting on accuracy instead would reward exactly that.
    """
    total = Decimal(0)
    count = 0
    for sample in test:
        value = label_of(sample)
        if value is None:
            continue
        vector = standardiser.apply(sample.snapshot)
        if vector is None:
            continue
        predicted = model.probability(vector) if classify else model.score(vector)
        total += (predicted - value) ** 2
        count += 1
    if count == 0:
        return None
    return total / Decimal(count)


def walk_forward(
    samples: Sequence[LabelledSnapshot],
    *,
    classify: bool,
    label_of: LabelFn,
    features: Sequence[PredictiveFeature] = FEATURES,
    min_train_sessions: int = MIN_TRAIN_SESSIONS,
    step_sessions: int = DEFAULT_STEP_SESSIONS,
) -> WalkForwardResult:
    """Expanding-window walk-forward evaluation over session boundaries.

    Each fold trains on every session up to a cutoff and predicts the next
    ``step_sessions`` worth of trades, then the cutoff advances. The window expands
    rather than sliding, because a trader's older history is still their history — and
    because a sliding window silently changes the training size between folds, which
    makes the folds incomparable.
    """
    ordered = sorted(
        (sample for sample in samples if sample.snapshot.session_date is not None),
        key=lambda sample: (sample.snapshot.opened_at, sample.snapshot.trade_id.hex),
    )
    sessions = _sessions_of(ordered)

    if len(sessions) < min_train_sessions + 1:
        return WalkForwardResult(
            (), 0, 0, 0, (),
            refusal=(
                f"walk-forward validation needs at least {min_train_sessions + 1} "
                f"trading sessions and this history has {len(sessions)} — a model "
                "cannot be tested on data it has not been given a chance to miss"
            ),
        )

    by_session: dict[date, list[LabelledSnapshot]] = {}
    for sample in ordered:
        session = sample.snapshot.session_date
        assert session is not None
        by_session.setdefault(session, []).append(sample)

    predictions: list[Prediction] = []
    penalties: list[Decimal] = []
    train_rows = 0
    skipped = 0
    fold = 0
    cursor = min_train_sessions

    while cursor < len(sessions):
        train_sessions = sessions[:cursor]
        test_sessions = sessions[cursor : cursor + step_sessions]
        cursor += step_sessions
        fold += 1

        train = [sample for session in train_sessions for sample in by_session[session]]
        test = [sample for session in test_sessions for sample in by_session[session]]
        if not train or not test:
            continue

        penalty = _select_penalty(
            train, features=features, label_of=label_of, classify=classify
        )
        fitted = _fit_fold(
            train, features=features, label_of=label_of, classify=classify, penalty=penalty
        )
        if fitted is None:
            continue
        model, standardiser = fitted
        penalties.append(penalty)
        train_rows += model.training_rows

        for sample in test:
            value = label_of(sample)
            if value is None:
                skipped += 1
                continue
            vector = standardiser.apply(sample.snapshot)
            if vector is None:
                skipped += 1
                continue
            predicted = model.probability(vector) if classify else model.score(vector)
            predictions.append(
                Prediction(
                    trade_id=str(sample.snapshot.trade_id),
                    fold=fold,
                    predicted=predicted,
                    actual=value,
                    won=sample.won,
                    session=sample.snapshot.session_date,
                )
            )

    return WalkForwardResult(
        predictions=tuple(predictions),
        folds=fold,
        train_rows=train_rows,
        skipped_rows=skipped,
        chosen_penalties=tuple(penalties),
    )


def final_model(
    samples: Sequence[LabelledSnapshot],
    *,
    classify: bool,
    label_of: LabelFn,
    features: Sequence[PredictiveFeature] = FEATURES,
) -> tuple[FittedModel, Standardiser] | None:
    """Refit on the full history, for serving.

    Deliberately separate from :func:`walk_forward`. The walk-forward run establishes
    *whether* the approach works; this fits the model that is actually served, using
    every trade available. The performance numbers reported alongside it always come from
    the walk-forward run — never from this fit, which has seen everything and would
    report the past perfectly.
    """
    penalty = _select_penalty(samples, features=features, label_of=label_of, classify=classify)
    return _fit_fold(
        samples, features=features, label_of=label_of, classify=classify, penalty=penalty
    )


def win_label(sample: LabelledSnapshot) -> Decimal:
    return Decimal(1) if sample.won else Decimal(0)


def r_label(sample: LabelledSnapshot) -> Decimal | None:
    """R multiple, or ``None`` when the trade had no recorded stop.

    Never imputed. A trade with no stop has no R multiple, and filling it with zero would
    teach the model that stopless trades break even.
    """
    return sample.r_multiple
