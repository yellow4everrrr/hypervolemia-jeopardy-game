"""The predictive feature space, and the standardiser that must be fit on train only.

Every extractor here takes an :class:`~app.ml.snapshot.EntrySnapshot`, which has no
outcome fields, so leakage through a feature is impossible by construction rather than
by review.

**The feature set is small on purpose.** A trader with 400 trades and 30 features will
get a model that fits their history beautifully and predicts nothing. Nine features is
already generous at that sample size; the ridge penalty in :mod:`app.ml.logistic` is
what keeps it honest, and the walk-forward evaluation is what proves whether it worked.

**Categorical features are deliberately absent.** Strategy, setup, instrument and market
condition are all knowable at entry and all tempting. Two encodings are available and
both are traps at this sample size: one-hot over a dozen setups adds a dozen parameters
to a 400-row problem, and target encoding — replacing "breakout" with the mean outcome of
breakouts — is leakage unless recomputed inside every training fold, which is exactly the
kind of subtlety that survives review and corrupts results silently. Segmentation
(milestone 6) already answers "how do my setups compare" with a proper significance test.
The model does not need to answer it twice, worse.

**Standardisation is fit on training data only.** Computing the mean and standard
deviation over the full history and then splitting is one of the most common ways a
walk-forward evaluation quietly becomes an in-sample one: the scaler has already seen the
future. :class:`Standardiser` therefore has separate ``fit`` and ``apply`` steps, and
:mod:`app.ml.validation` never calls ``fit`` on anything but a training fold.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.analytics.statistics import mean, stdev
from app.ml.snapshot import EntrySnapshot

#: A feature must be present on at least this fraction of the training rows to be kept.
#: Same reasoning as the clustering matrix: below it, retaining the feature discards more
#: trades than the feature is worth.
MIN_FEATURE_COVERAGE = Decimal("0.7")


@dataclass(frozen=True, slots=True)
class PredictiveFeature:
    """One input column.

    Attributes:
        extract: Reads the value from a snapshot, or ``None`` when the snapshot does not
            have it. Never imputes — a missing value is missing, and the row is dropped
            with a count rather than filled with a fiction.
        description: Plain language, handed to the AI layer so a coefficient can be
            described without the model guessing what the column meant.
    """

    name: str
    description: str
    extract: Callable[[EntrySnapshot], Decimal | None]


def _minutes(snapshot: EntrySnapshot) -> Decimal | None:
    return snapshot.minutes_since_last_trade


def _hour(snapshot: EntrySnapshot) -> Decimal | None:
    return Decimal(snapshot.entry_hour) if snapshot.entry_hour is not None else None


def _weekday(snapshot: EntrySnapshot) -> Decimal | None:
    return Decimal(snapshot.entry_weekday) if snapshot.entry_weekday is not None else None


FEATURES: tuple[PredictiveFeature, ...] = (
    PredictiveFeature(
        name="entry_hour",
        description="hour of entry in the exchange's timezone",
        extract=_hour,
    ),
    PredictiveFeature(
        name="entry_weekday",
        description="ISO weekday of entry",
        extract=_weekday,
    ),
    PredictiveFeature(
        name="direction",
        description="long (1) or short (0)",
        extract=lambda s: Decimal(1) if s.direction.value == "long" else Decimal(0),
    ),
    PredictiveFeature(
        name="quantity",
        description="position size in contracts",
        extract=lambda s: s.quantity,
    ),
    PredictiveFeature(
        name="trades_before_today",
        description="trades already taken in this session before this one",
        extract=lambda s: Decimal(s.trades_before_today),
    ),
    PredictiveFeature(
        name="session_pnl_before",
        description="realized session P&L before this trade was opened",
        extract=lambda s: s.session_pnl_before,
    ),
    PredictiveFeature(
        name="is_down_on_session",
        description="whether the session was in the red when this trade was opened",
        extract=lambda s: Decimal(1) if s.is_down_on_session else Decimal(0),
    ),
    PredictiveFeature(
        name="consecutive_losses_before",
        description="losing streak carried into this trade",
        extract=lambda s: Decimal(s.consecutive_losses_before),
    ),
    PredictiveFeature(
        name="minutes_since_last_trade",
        description="minutes since the previous entry in this session",
        extract=_minutes,
    ),
)


@dataclass(frozen=True, slots=True)
class Standardiser:
    """Per-column centre and scale, learned from training rows only.

    Held separately from the model so that the "fit on train, apply to test" boundary is
    visible in the call site instead of buried inside a fit method.
    """

    features: tuple[PredictiveFeature, ...]
    centres: tuple[Decimal, ...]
    scales: tuple[Decimal, ...]

    @classmethod
    def fit(
        cls,
        snapshots: Sequence[EntrySnapshot],
        *,
        features: Sequence[PredictiveFeature] = FEATURES,
        min_coverage: Decimal = MIN_FEATURE_COVERAGE,
    ) -> Standardiser:
        """Choose which features to keep and learn their scaling.

        A feature is dropped when it is too often missing, or when it never varies in
        the training data. A constant column carries no information and would divide by
        zero; dropping it loses nothing.
        """
        retained: list[PredictiveFeature] = []
        centres: list[Decimal] = []
        scales: list[Decimal] = []

        if not snapshots:
            return cls((), (), ())

        for feature in features:
            values = [feature.extract(snapshot) for snapshot in snapshots]
            known = [value for value in values if value is not None]
            if Decimal(len(known)) / Decimal(len(snapshots)) < min_coverage:
                continue
            if len(set(known)) < 2:
                continue
            centre = mean(known) or Decimal(0)
            spread = stdev(known)
            retained.append(feature)
            centres.append(centre)
            scales.append(spread if spread and spread > 0 else Decimal(1))

        return cls(tuple(retained), tuple(centres), tuple(scales))

    @property
    def width(self) -> int:
        return len(self.features)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(feature.name for feature in self.features)

    def apply(self, snapshot: EntrySnapshot) -> tuple[Decimal, ...] | None:
        """Standardise one snapshot, or return ``None`` if it lacks a retained feature.

        Returning ``None`` rather than a partially filled vector is the point: a row
        missing a column is not a row with a zero in that column, and the caller counts
        it as skipped.
        """
        vector: list[Decimal] = []
        for feature, centre, scale in zip(self.features, self.centres, self.scales, strict=True):
            value = feature.extract(snapshot)
            if value is None:
                return None
            vector.append((value - centre) / scale)
        return tuple(vector)

    def to_payload(self) -> list[dict[str, str]]:
        return [
            {"name": feature.name, "description": feature.description}
            for feature in self.features
        ]


@dataclass(frozen=True, slots=True)
class DesignMatrix:
    """Standardised rows aligned with their labels, plus what was left out.

    ``skipped`` is carried rather than discarded: a model trained on 60% of a history is
    a different claim from one trained on all of it, and the API reports which.
    """

    rows: tuple[tuple[Decimal, ...], ...]
    labels: tuple[Decimal, ...]
    identifiers: tuple[str, ...]
    skipped: int

    @property
    def size(self) -> int:
        return len(self.rows)


def build_design(
    pairs: Sequence[tuple[EntrySnapshot, Decimal]], standardiser: Standardiser
) -> DesignMatrix:
    """Apply a fitted standardiser to (snapshot, label) pairs.

    The label is passed in as a ``Decimal`` by the caller — 1/0 for the classifier, an R
    multiple for the regressor — so this function never sees an outcome object and cannot
    be the place a leak is introduced.
    """
    rows: list[tuple[Decimal, ...]] = []
    labels: list[Decimal] = []
    identifiers: list[str] = []
    skipped = 0

    for snapshot, label in pairs:
        vector = standardiser.apply(snapshot)
        if vector is None:
            skipped += 1
            continue
        rows.append(vector)
        labels.append(label)
        identifiers.append(str(snapshot.trade_id))

    return DesignMatrix(tuple(rows), tuple(labels), tuple(identifiers), skipped)
