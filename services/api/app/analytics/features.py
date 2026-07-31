"""Turning a trade into numbers a clustering algorithm can work with.

Clustering needs a fixed-width numeric vector per trade. Getting from a
:class:`TradeRecord` to that vector is where most of the honesty risk lives, so the
choices are made explicitly here rather than inside the algorithm.

**Features are named and documented.** A cluster is only useful if it can be described
back to the trader — "short trades, held under two minutes, taken late in the session"
— and that requires knowing what each dimension meant. An anonymous feature matrix
produces clusters nobody can act on.

**Missing values are never imputed.** A trade with no recorded stop has no R multiple.
Filling that with a zero, or with the sample mean, invents a trade that did not happen
and lets it pull a centroid. Instead a feature is dropped from the whole run when too
many trades lack it, and trades still missing a retained feature are excluded from
clustering and reported as excluded.

**Everything is standardised before use.** Duration is in seconds and R multiple is in
single digits; without standardisation, Euclidean distance is a measurement of duration
with rounding noise attached. Z-scores put every feature on the same footing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.analytics.statistics import mean, stdev
from app.analytics.types import TradeRecord

#: A feature must be present on at least this fraction of trades to be used at all.
#: Below it, keeping the feature would discard more trades than the feature is worth.
MIN_FEATURE_COVERAGE = Decimal("0.7")

#: Fewer trades than this and clustering is not attempted. Partitioning 12 trades into
#: 4 groups produces 4 groups; it does not produce 4 kinds of trade.
MIN_TRADES_FOR_CLUSTERING = 40


@dataclass(frozen=True, slots=True)
class Feature:
    """One dimension of the trade feature space.

    Attributes:
        name: Stable key, used in payloads and cluster descriptions.
        description: Plain-language meaning, rendered in the UI and handed to the AI
            layer so a cluster can be described without the model guessing.
        extract: Pulls the value from a trade, or ``None`` when the trade does not
            have it.
        higher_is: What a high value means in words — used to phrase a cluster
            description as "held longer than usual" rather than "duration z = 1.4".
    """

    name: str
    description: str
    extract: Callable[[TradeRecord], Decimal | None]
    higher_is: str
    lower_is: str


def _duration(trade: TradeRecord) -> Decimal | None:
    return Decimal(trade.duration_seconds) if trade.duration_seconds is not None else None


def _hour(trade: TradeRecord) -> Decimal | None:
    return Decimal(trade.entry_hour) if trade.entry_hour is not None else None


def _weekday(trade: TradeRecord) -> Decimal | None:
    return Decimal(trade.entry_weekday) if trade.entry_weekday is not None else None


def _direction(trade: TradeRecord) -> Decimal:
    """Long as 1, short as 0.

    A binary feature standardises to two symmetric values, which is exactly what is
    wanted: it separates clusters by direction without dominating the distance.
    """
    return Decimal(1) if trade.direction.value == "long" else Decimal(0)


def _efficiency(trade: TradeRecord) -> Decimal | None:
    """How much of the favourable excursion was actually captured.

    Near 1 means exits near the best available price; near 0 means the trade went the
    right way and gave it all back. This is the single most diagnostic feature in the
    set, and it exists only because milestone 4 supplies excursions.
    """
    if trade.mfe_r is None or trade.r_multiple is None or trade.mfe_r <= 0:
        return None
    return trade.r_multiple / trade.mfe_r


#: The feature space. Deliberately small: every added dimension dilutes Euclidean
#: distance (the curse of dimensionality is not a slogan at n = 500), and every feature
#: that is frequently missing costs trades from the run.
FEATURES: tuple[Feature, ...] = (
    Feature(
        name="duration_seconds",
        description="holding period in seconds",
        extract=_duration,
        higher_is="held longer than usual",
        lower_is="held briefly",
    ),
    Feature(
        name="entry_hour",
        description="hour of entry in the exchange's timezone",
        extract=_hour,
        higher_is="entered later in the day",
        lower_is="entered early in the session",
    ),
    Feature(
        name="entry_weekday",
        description="ISO weekday of entry",
        extract=_weekday,
        higher_is="later in the week",
        lower_is="earlier in the week",
    ),
    Feature(
        name="quantity",
        description="position size in contracts",
        extract=lambda trade: trade.quantity,
        higher_is="sized larger than usual",
        lower_is="sized smaller than usual",
    ),
    Feature(
        name="direction",
        description="long (1) or short (0)",
        extract=_direction,
        higher_is="long",
        lower_is="short",
    ),
    Feature(
        name="mae_r",
        description="maximum adverse excursion in R",
        extract=lambda trade: trade.mae_r,
        higher_is="went less far against you",
        lower_is="went deep underwater before resolving",
    ),
    Feature(
        name="mfe_r",
        description="maximum favourable excursion in R",
        extract=lambda trade: trade.mfe_r,
        higher_is="offered a large move in your favour",
        lower_is="never moved far in your favour",
    ),
    Feature(
        name="capture_efficiency",
        description="realized R as a fraction of the best R available",
        extract=_efficiency,
        higher_is="captured most of the available move",
        lower_is="gave back most of the available move",
    ),
)

#: Outcome is deliberately **not** a feature. Clustering on P&L and then testing whether
#: the clusters differ in P&L is circular — it always finds a "losing pattern", because
#: it built one. Clusters are formed from what the trade *was*; the test asks whether
#: those groups happened to perform differently.
EXCLUDED_FROM_CLUSTERING = ("net_pnl", "r_multiple", "gross_pnl")


@dataclass(frozen=True, slots=True)
class FeatureMatrix:
    """Standardised feature vectors, plus the bookkeeping needed to explain them.

    Attributes:
        features: The features actually retained, in column order.
        rows: One standardised vector per included trade, aligned with ``trades``.
        trades: The trades that had every retained feature.
        excluded: Trades dropped for missing a retained feature. Reported, never
            silently discarded — a run that clustered 60% of the sample is a different
            claim from one that clustered all of it.
        centres / scales: Per-feature mean and standard deviation used to standardise,
            kept so a centroid can be translated back into real units.
    """

    features: tuple[Feature, ...]
    rows: tuple[tuple[Decimal, ...], ...]
    trades: tuple[TradeRecord, ...]
    excluded: tuple[TradeRecord, ...]
    centres: tuple[Decimal, ...]
    scales: tuple[Decimal, ...]
    dropped_features: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.rows)

    @property
    def coverage(self) -> Decimal | None:
        total = len(self.trades) + len(self.excluded)
        if total == 0:
            return None
        return Decimal(len(self.trades)) / Decimal(total)

    def to_real_units(self, vector: Sequence[Decimal]) -> dict[str, Decimal]:
        """Convert a standardised vector back into the units a trader recognises."""
        return {
            feature.name: centre + value * scale
            for feature, value, centre, scale in zip(
                self.features, vector, self.centres, self.scales, strict=True
            )
        }


def build_matrix(
    trades: Sequence[TradeRecord],
    *,
    features: Sequence[Feature] = FEATURES,
    min_coverage: Decimal = MIN_FEATURE_COVERAGE,
) -> FeatureMatrix:
    """Extract, filter and standardise features across a set of trades.

    Two passes. The first decides which features are present often enough to be worth
    keeping; the second keeps only the trades that have all of them. Doing it in that
    order matters: choosing features per trade would give different trades different
    dimensionality, and distance between vectors of different meaning is not distance.
    """
    if not trades:
        return FeatureMatrix((), (), (), (), (), (), ())

    extracted: dict[str, list[Decimal | None]] = {
        feature.name: [feature.extract(trade) for trade in trades] for feature in features
    }

    retained: list[Feature] = []
    dropped: list[str] = []
    for feature in features:
        values = extracted[feature.name]
        present = sum(1 for value in values if value is not None)
        coverage = Decimal(present) / Decimal(len(trades))
        if coverage < min_coverage:
            dropped.append(feature.name)
            continue
        # A feature with no variation carries no information and would divide by zero
        # when standardised. Dropping it is not a loss: every trade has the same value.
        known = [value for value in values if value is not None]
        if len(set(known)) < 2:
            dropped.append(feature.name)
            continue
        retained.append(feature)

    if not retained:
        return FeatureMatrix((), (), (), tuple(trades), (), (), tuple(dropped))

    kept: list[TradeRecord] = []
    excluded: list[TradeRecord] = []
    raw: list[tuple[Decimal, ...]] = []
    for index, trade in enumerate(trades):
        values = [extracted[feature.name][index] for feature in retained]
        if any(value is None for value in values):
            excluded.append(trade)
            continue
        kept.append(trade)
        raw.append(tuple(value for value in values if value is not None))

    if not raw:
        return FeatureMatrix(tuple(retained), (), (), tuple(excluded), (), (), tuple(dropped))

    centres: list[Decimal] = []
    scales: list[Decimal] = []
    for column in range(len(retained)):
        column_values = [row[column] for row in raw]
        centre = mean(column_values) or Decimal(0)
        spread = stdev(column_values)
        centres.append(centre)
        # A retained feature always varies, so spread is positive; the guard is for the
        # degenerate case where every value differs only below the precision floor.
        scales.append(spread if spread and spread > 0 else Decimal(1))

    standardised = tuple(
        tuple(
            (value - centre) / scale
            for value, centre, scale in zip(row, centres, scales, strict=True)
        )
        for row in raw
    )

    return FeatureMatrix(
        features=tuple(retained),
        rows=standardised,
        trades=tuple(kept),
        excluded=tuple(excluded),
        centres=tuple(centres),
        scales=tuple(scales),
        dropped_features=tuple(dropped),
    )
