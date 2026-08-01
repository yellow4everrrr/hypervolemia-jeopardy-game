"""k-means over trade features, in exact decimal arithmetic and fully deterministic.

Three properties matter more here than raw clustering quality.

**Deterministic.** The same trades must produce the same clusters on every run. A
trader who reloads the page and sees a different set of "discovered patterns" has
learned, correctly, that the feature does not mean anything. Initialisation is seeded,
Lloyd's algorithm is run to convergence rather than to a wall-clock budget, and every
tie — nearest centroid, best k — breaks on a stable key rather than on dict ordering.

**Exact.** No floats, consistent with the rest of the engine. Distances are compared,
not reported, so squared distances are used throughout and the square root is only
taken where a value is surfaced.

**Honest about whether there are clusters at all.** k is chosen by silhouette over a
bounded range, and then the winning clustering has to beat a null reference before it
is reported at all — see :func:`structure_p_value`. This second step is not optional
polish. k-means partitions anything: 300 uniformly random points come back as six tidy
groups with a silhouette near 0.29, comfortably above any fixed threshold low enough to
be useful on real trades, and the descriptions generated from those centroids read
exactly as confidently as the real ones. A trader cannot tell the difference by looking,
so the module has to tell them, and ``None`` — *these trades do not fall into kinds* —
is the answer more often than not.

The structure test costs roughly as much as the clustering itself times the reference
count, which puts a full scan in the seconds rather than milliseconds. That is why
discovery is an explicit request rather than something a dashboard triggers on render.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from app.analytics.features import Feature
from app.analytics.montecarlo import DEFAULT_SEED
from app.analytics.statistics import mean, sqrt
from app.analytics.types import TradeRecord

Vector = tuple[Decimal, ...]

#: Range of k to consider. Below two there is nothing to compare; above six, clusters
#: stop being describable in a sentence and a trader cannot act on them.
MIN_K = 2
MAX_K = 6

#: Lloyd's algorithm converges quickly on data this size; the cap is a guard against a
#: pathological oscillation, not an expected exit.
MAX_ITERATIONS = 100

#: Absolute floor on silhouette. A cheap first screen only — the real test is
#: :func:`structure_p_value`, because a bare silhouette threshold cannot distinguish
#: structure from the partition k-means imposes on any data at all.
MIN_SILHOUETTE = Decimal("0.15")

#: References drawn for the structure test. Twenty gives a smallest attainable p-value
#: of 1/21 ≈ 0.048, which is exactly enough resolution for a 0.05 screen and no more
#: than that costs.
STRUCTURE_REFERENCES = 20

#: A clustering must beat this fraction of null references to be reported at all.
STRUCTURE_ALPHA = Decimal("0.05")

#: Silhouette is O(n²). Above this many trades it is computed on a seeded subsample,
#: which estimates the same quantity without making k selection quadratic in a year of
#: trading.
SILHOUETTE_SAMPLE_CAP = 400


@dataclass(frozen=True, slots=True)
class Clustering:
    """An assignment of trades to clusters.

    Attributes:
        labels: Cluster index per row, aligned with the feature matrix.
        centroids: Cluster centres in standardised space.
        silhouette: Mean silhouette score, a measure of how separated the clusters are.
            High on its own means little — see :func:`structure_p_value`.
        k: Number of clusters.
        inertia: Total squared distance to assigned centroids — reported for
            diagnostics, never for deciding anything, since it always falls with k.
    """

    labels: tuple[int, ...]
    centroids: tuple[Vector, ...]
    silhouette: Decimal | None
    k: int
    inertia: Decimal
    #: Fraction of null references that clustered at least as tightly. ``None`` until
    #: :func:`choose_clustering` runs the test, so an untested clustering cannot be
    #: mistaken for one that passed.
    structure_p_value: Decimal | None = None

    def members(self, cluster: int) -> tuple[int, ...]:
        return tuple(index for index, label in enumerate(self.labels) if label == cluster)

    def sizes(self) -> tuple[int, ...]:
        return tuple(len(self.members(cluster)) for cluster in range(self.k))


def squared_distance(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    return sum(
        ((a - b) * (a - b) for a, b in zip(left, right, strict=True)), start=Decimal(0)
    )


def _nearest(vector: Sequence[Decimal], centroids: Sequence[Vector]) -> tuple[int, Decimal]:
    """Index of the closest centroid. Ties break to the lowest index, deterministically."""
    best_index = 0
    best_distance = squared_distance(vector, centroids[0])
    for index in range(1, len(centroids)):
        distance = squared_distance(vector, centroids[index])
        if distance < best_distance:
            best_index, best_distance = index, distance
    return best_index, best_distance


def _initial_centroids(rows: Sequence[Vector], k: int, rng: random.Random) -> list[Vector]:
    """k-means++ seeding.

    Picking k points at random routinely puts two centroids inside one natural group and
    none in another, and Lloyd's algorithm cannot recover from that — it converges to a
    local optimum that depends entirely on the draw. k-means++ spreads the initial
    centroids by sampling each new one in proportion to its squared distance from the
    nearest already chosen, which makes the result far less seed-sensitive.
    """
    centroids = [rows[rng.randrange(len(rows))]]
    while len(centroids) < k:
        weights = [_nearest(row, centroids)[1] for row in rows]
        total = sum(weights, start=Decimal(0))
        if total <= 0:
            # Every remaining point coincides with a centroid; no meaningful choice
            # left, so fill deterministically rather than sampling from a zero measure.
            for row in rows:
                if row not in centroids:
                    centroids.append(row)
                    break
            else:
                break
            continue
        target = Decimal(str(rng.random())) * total
        cumulative = Decimal(0)
        for row, weight in zip(rows, weights, strict=True):
            cumulative += weight
            if cumulative >= target:
                centroids.append(row)
                break
        else:  # pragma: no cover — cumulative reaches total by construction
            centroids.append(rows[-1])
    return centroids


def kmeans(rows: Sequence[Vector], k: int, *, seed: int = DEFAULT_SEED) -> Clustering:
    """Partition rows into k clusters with Lloyd's algorithm.

    An empty cluster is re-seeded to the point furthest from its centroid rather than
    dropped, so the returned clustering always has exactly k groups and ``labels`` can
    be indexed without a lookup table.
    """
    if k < 1 or len(rows) < k:
        raise ValueError(f"cannot form {k} clusters from {len(rows)} rows")

    rng = random.Random(seed)
    centroids = _initial_centroids(rows, k, rng)
    labels = [0] * len(rows)

    for _ in range(MAX_ITERATIONS):
        changed = False
        for index, row in enumerate(rows):
            label, _ = _nearest(row, centroids)
            if label != labels[index]:
                labels[index] = label
                changed = True

        new_centroids: list[Vector] = []
        for cluster in range(k):
            members = [row for row, label in zip(rows, labels, strict=True) if label == cluster]
            if not members:
                new_centroids.append(_furthest_point(rows, labels, centroids))
                continue
            new_centroids.append(
                tuple(
                    mean([member[column] for member in members]) or Decimal(0)
                    for column in range(len(rows[0]))
                )
            )
        centroids = new_centroids

        if not changed:
            break

    inertia = sum(
        (squared_distance(row, centroids[label]) for row, label in zip(rows, labels, strict=True)),
        start=Decimal(0),
    )
    return Clustering(
        labels=tuple(labels),
        centroids=tuple(centroids),
        silhouette=silhouette(rows, labels),
        k=k,
        inertia=inertia,
    )


def _furthest_point(
    rows: Sequence[Vector], labels: Sequence[int], centroids: Sequence[Vector]
) -> Vector:
    """The point worst served by its current centroid — the best place to re-seed."""
    best = rows[0]
    best_distance = Decimal(-1)
    for row, label in zip(rows, labels, strict=True):
        distance = squared_distance(row, centroids[label])
        if distance > best_distance:
            best, best_distance = row, distance
    return best


def silhouette(rows: Sequence[Vector], labels: Sequence[int]) -> Decimal | None:
    """Mean silhouette score: how much better each point fits its cluster than the next.

    Ranges from -1 (points are closer to another cluster) through 0 (clusters overlap
    entirely) to 1 (tight, well separated). Computed on a seeded subsample above
    :data:`SILHOUETTE_SAMPLE_CAP` rows, because it is quadratic and k selection would
    otherwise dominate the runtime of the whole engine.
    """
    distinct = sorted(set(labels))
    if len(distinct) < 2 or len(rows) < 3:
        return None

    indices = list(range(len(rows)))
    if len(indices) > SILHOUETTE_SAMPLE_CAP:
        rng = random.Random(DEFAULT_SEED)
        indices = sorted(rng.sample(indices, SILHOUETTE_SAMPLE_CAP))

    by_label: dict[int, list[int]] = {label: [] for label in distinct}
    for index in indices:
        by_label[labels[index]].append(index)

    scores: list[Decimal] = []
    for index in indices:
        own = by_label[labels[index]]
        if len(own) < 2:
            # A singleton cluster has no within-cluster distance to speak of. Scoring it
            # as 0 keeps it from inflating the mean either way.
            scores.append(Decimal(0))
            continue

        cohesion = _mean_distance(rows, index, [other for other in own if other != index])
        separation = min(
            _mean_distance(rows, index, by_label[label])
            for label in distinct
            if label != labels[index] and by_label[label]
        )
        widest = max(cohesion, separation)
        scores.append(Decimal(0) if widest == 0 else (separation - cohesion) / widest)

    return mean(scores)


def _mean_distance(rows: Sequence[Vector], index: int, others: Sequence[int]) -> Decimal:
    if not others:
        return Decimal(0)
    total = sum(
        (sqrt(squared_distance(rows[index], rows[other])) for other in others),
        start=Decimal(0),
    )
    return total / Decimal(len(others))


def _shuffle_columns(rows: Sequence[Vector], rng: random.Random) -> list[Vector]:
    """A null reference: each feature keeps its own distribution, independently of the rest.

    Shuffling every column separately destroys the *association between* features while
    leaving each feature's marginal distribution exactly as observed. That is the right
    null for "do these trades fall into kinds?", because a kind is a recurring
    combination — short *and* large *and* early. Comparing against uniform noise instead
    would flatter any sample whose features happen to be skewed.
    """
    columns = [[row[index] for row in rows] for index in range(len(rows[0]))]
    for column in columns:
        rng.shuffle(column)
    return [tuple(column[index] for column in columns) for index in range(len(rows))]


def structure_p_value(
    rows: Sequence[Vector],
    k: int,
    observed: Decimal,
    *,
    references: int = STRUCTURE_REFERENCES,
    seed: int = DEFAULT_SEED,
) -> Decimal:
    """How often shuffled data clusters as well as the real thing.

    This is the test that a bare silhouette threshold cannot replace. k-means partitions
    *any* data — hand it 300 uniformly random points and it returns six tidy groups with
    a silhouette around 0.29, well above any threshold low enough to be useful on real
    trades. The clusters are real partitions of real points; what they are not is kinds
    of trade.

    So the question is not "is the silhouette high?" but "is it higher than this
    algorithm produces on data with no structure in it?". The reference keeps each
    feature's marginal distribution and breaks only the associations between them, and
    the p-value is the fraction of references that clustered at least as tightly.

    Add-one smoothing in numerator and denominator: with 20 references the smallest
    attainable p-value is 1/21, never 0, because 20 draws cannot evidence "never".
    """
    rng = random.Random(seed)
    at_least_as_good = 0
    for index in range(references):
        reference = _shuffle_columns(rows, rng)
        clustering = kmeans(reference, k, seed=seed + index + 1)
        if clustering.silhouette is not None and clustering.silhouette >= observed:
            at_least_as_good += 1
    return Decimal(at_least_as_good + 1) / Decimal(references + 1)


def choose_clustering(
    rows: Sequence[Vector],
    *,
    min_k: int = MIN_K,
    max_k: int = MAX_K,
    seed: int = DEFAULT_SEED,
    min_silhouette: Decimal = MIN_SILHOUETTE,
    min_cluster_size: int = 5,
    references: int = STRUCTURE_REFERENCES,
    alpha: Decimal = STRUCTURE_ALPHA,
) -> Clustering | None:
    """Best k by silhouette, or ``None`` when the trades do not cluster.

    Returning ``None`` is a real answer and the important one. Most traders' histories
    are not made of four crisp behavioural groups, and a module that always returns
    clusters will always be believed — the descriptions read just as confidently whether
    or not there was anything there.

    Two gates. Clusterings containing a group too small to test are rejected, because a
    two-trade cluster cannot clear a significance threshold and surfacing it only invites
    someone to read meaning into it. Then the winning k must beat its own null reference:
    see :func:`structure_p_value`.
    """
    # With B references the smallest attainable p-value is 1/(B+1). If that already
    # exceeds alpha, no clustering can ever pass and the function silently becomes a
    # constant `None` — a configuration mistake that looks exactly like "this trader has
    # no patterns". Fail loudly instead.
    floor = Decimal(1) / Decimal(references + 1)
    if floor > alpha:
        raise ValueError(
            f"{references} references cannot reach alpha={alpha}: the smallest "
            f"attainable p-value is {floor}. Use at least "
            f"{int(1 / alpha) - 1} references, or raise alpha."
        )

    if len(rows) < min_k or not rows:
        return None

    best: Clustering | None = None
    for k in range(min_k, min(max_k, len(rows) - 1) + 1):
        candidate = kmeans(rows, k, seed=seed)
        if min(candidate.sizes()) < min_cluster_size:
            continue
        if candidate.silhouette is None:
            continue
        if best is None or (best.silhouette is not None and candidate.silhouette > best.silhouette):
            best = candidate

    if best is None or best.silhouette is None or best.silhouette < min_silhouette:
        return None

    p_value = structure_p_value(
        rows, best.k, best.silhouette, references=references, seed=seed
    )
    if p_value > alpha:
        return None
    return replace(best, structure_p_value=p_value)


def describe_cluster(
    clustering: Clustering,
    cluster: int,
    matrix_features: Sequence[Feature],
    *,
    threshold: Decimal = Decimal("0.5"),
    limit: int = 3,
) -> tuple[str, ...]:
    """Phrase what makes a cluster distinctive, in the trader's own vocabulary.

    Centroids live in standardised space, so a coordinate *is* a z-score: 1.4 means "one
    and a bit standard deviations above this trader's own average". Only coordinates
    beyond ``threshold`` are mentioned, strongest first — listing every feature would
    describe the cluster's average member rather than what sets it apart.
    """
    centroid = clustering.centroids[cluster]
    ranked = sorted(
        range(len(centroid)), key=lambda column: abs(centroid[column]), reverse=True
    )
    phrases: list[str] = []
    for column in ranked[:limit]:
        if abs(centroid[column]) < threshold:
            break
        feature = matrix_features[column]
        phrases.append(feature.higher_is if centroid[column] > 0 else feature.lower_is)
    return tuple(phrases)


def cluster_confidence(
    clustering: Clustering, row: Sequence[Decimal], label: int
) -> Decimal | None:
    """How firmly one trade belongs to its cluster, from 0 to 1.

    The ratio of the distance to the nearest *other* centroid against the sum of both
    distances. A trade sitting on the boundary scores near 0.5; one deep inside its
    cluster approaches 1. Surfaced as ``setup_confidence`` so an automatically applied
    label carries how sure the assignment was, rather than looking equally certain for
    a textbook example and a borderline case.
    """
    if clustering.k < 2:
        return None
    own = sqrt(squared_distance(row, clustering.centroids[label]))
    other = min(
        sqrt(squared_distance(row, centroid))
        for index, centroid in enumerate(clustering.centroids)
        if index != label
    )
    total = own + other
    if total == 0:
        return None
    return other / total


def trades_in_cluster(
    clustering: Clustering, trades: Sequence[TradeRecord], cluster: int
) -> tuple[TradeRecord, ...]:
    return tuple(trades[index] for index in clustering.members(cluster))
