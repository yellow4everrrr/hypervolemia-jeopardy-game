"""Pattern discovery: the named detectors and the discovered clusters, tested as one.

The single most important line in this module is the one that pools every p-value —
behavioural and cluster alike — into a **single family** before adjusting.

Running eight behavioural detectors and testing five clusters is thirteen chances to
find something at p < 0.05. On a trader with no leaks at all, the expected number of
"findings" from that scan is about 0.65, and whichever one comes back will look
specific and convincing. Correcting each half separately would still leave two
families and roughly double the false discovery rate the trader was promised.

So the scan is one family. A behavioural detector's finding has to clear a bar that is
higher because clusters were also examined, and vice versa. That is the correct
accounting, and it is also the unintuitive one — adding a detector makes existing
findings *harder* to establish, which is exactly why it has to be enforced here rather
than left to whoever adds the next detector.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.analytics.behaviours import (
    DETECTORS,
    BehaviourFinding,
    CaptureSummary,
    sequence_trades,
    summarise_capture,
)
from app.analytics.clustering import (
    STRUCTURE_REFERENCES,
    Clustering,
    choose_clustering,
    cluster_confidence,
    describe_cluster,
    trades_in_cluster,
)
from app.analytics.features import (
    FEATURES,
    MIN_TRADES_FOR_CLUSTERING,
    Feature,
    FeatureMatrix,
    build_matrix,
)
from app.analytics.montecarlo import DEFAULT_SEED, bootstrap_mean
from app.analytics.performance import count_trades
from app.analytics.significance import (
    ComparisonResult,
    compare_samples,
    control_false_discovery_rate,
)
from app.analytics.statistics import mean
from app.analytics.types import MIN_SAMPLE, Estimate, Interval, Reliability, TradeRecord
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Bumped whenever a detector, feature or threshold changes. Stored on every persisted
#: pattern so a rescan can tell which rows were produced by which engine.
DISCOVERY_VERSION = 1


@dataclass(frozen=True, slots=True)
class ClusterFinding:
    """One discovered group of behaviourally similar trades.

    Attributes:
        traits: The cluster described in the trader's vocabulary — "held longer than
            usual", "sized larger than usual". Derived from centroid coordinates, which
            are z-scores against this trader's own averages, not universal norms.
        comparison: This cluster's trades against every other trade in the sample.
    """

    cluster: int
    size: int
    traits: tuple[str, ...]
    centre: dict[str, Decimal]
    expectancy: Estimate
    mean_pnl: Decimal | None
    mean_pnl_elsewhere: Decimal | None
    win_rate: Decimal | None
    comparison: ComparisonResult | None
    trade_ids: tuple[str, ...]

    @property
    def difference(self) -> Decimal | None:
        if self.mean_pnl is None or self.mean_pnl_elsewhere is None:
            return None
        return self.mean_pnl - self.mean_pnl_elsewhere

    @property
    def polarity(self) -> str:
        difference = self.difference
        return "leak" if difference is not None and difference < 0 else "edge"

    @property
    def reliability(self) -> Reliability:
        return Reliability.for_sample(self.size)

    @property
    def is_actionable(self) -> bool:
        if self.reliability is not Reliability.RELIABLE:
            return False
        if self.expectancy.interval is None or not self.expectancy.interval.excludes_zero:
            return False
        return bool(self.comparison and self.comparison.is_significant)

    @property
    def label(self) -> str:
        """A description a trader can act on, assembled from the retained traits.

        Deliberately a list rather than a sentence. The trait phrases come from the
        feature set and are a mix of grammatical shapes — "held longer than usual" is a
        past participle, "gave back most of the available move" is a finite verb, "long"
        is an adjective — so any connecting phrase is wrong for some of them. An earlier
        version read "Trades that are gave back most of the available move".
        """
        if not self.traits:
            return f"Cluster {self.cluster + 1}"
        return "Trades: " + ", ".join(self.traits)

    def to_payload(self) -> dict[str, Any]:
        return {
            "cluster": self.cluster,
            "label": self.label,
            "traits": list(self.traits),
            "size": self.size,
            "centre": {key: str(value) for key, value in self.centre.items()},
            "polarity": self.polarity,
            "mean_pnl": _s(self.mean_pnl),
            "mean_pnl_elsewhere": _s(self.mean_pnl_elsewhere),
            "difference": _s(self.difference),
            "win_rate": _s(self.win_rate),
            "expectancy": self.expectancy.to_payload()
            if hasattr(self.expectancy, "to_payload")
            else None,
            "reliability": self.reliability.value,
            "is_actionable": self.is_actionable,
            "p_value": _s(self.comparison.p_value) if self.comparison else None,
            "adjusted_p_value": (
                _s(self.comparison.adjusted_p_value) if self.comparison else None
            ),
            "effect_size": _s(self.comparison.effect_size) if self.comparison else None,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    seed: int = DEFAULT_SEED
    permutations: int | None = None
    include_clustering: bool = True
    include_behaviours: bool = True
    features: tuple[Feature, ...] = FEATURES
    min_trades_for_clustering: int = MIN_TRADES_FOR_CLUSTERING
    #: Null references drawn for the cluster-structure test. Lower is faster and more
    #: permissive; the floor on the achievable p-value is 1/(references + 1).
    references: int = STRUCTURE_REFERENCES


@dataclass
class PatternReport:
    """Everything the scan found, and everything it looked at but could not establish.

    ``tests_performed`` is not decoration. It is the denominator the trader needs to
    interpret the rest: two findings out of thirteen tests is a different claim from
    two out of two, and a report that hides how many places it looked is presenting a
    filtered view of its own search as though it were the whole search.
    """

    behaviours: list[BehaviourFinding] = field(default_factory=list)
    clusters: list[ClusterFinding] = field(default_factory=list)
    sample_size: int = 0
    clustered_trades: int = 0
    tests_performed: int = 0
    clustering: Clustering | None = None
    matrix: FeatureMatrix | None = None
    #: Exit quality, reported as an estimate rather than tested. See
    #: :class:`app.analytics.behaviours.CaptureSummary` for why it is kept out of the
    #: hypothesis-test family.
    capture: CaptureSummary | None = None
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    notes: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[BehaviourFinding | ClusterFinding]:
        """Only what survived the correction, worst first.

        Sorted by effect on the account rather than by p-value: the most statistically
        certain finding is not the most expensive one, and the trader is trying to
        decide what to change.
        """
        survivors: list[BehaviourFinding | ClusterFinding] = [
            item for item in self.behaviours if item.is_actionable
        ]
        survivors.extend(item for item in self.clusters if item.is_actionable)
        return sorted(survivors, key=_impact)

    def to_payload(self) -> dict[str, Any]:
        return {
            "computed_at": self.computed_at.isoformat(),
            "engine_version": DISCOVERY_VERSION,
            "sample": {
                "trades": self.sample_size,
                "clustered": self.clustered_trades,
                "tests_performed": self.tests_performed,
                "findings": len(self.findings),
            },
            "behaviours": [item.to_payload() for item in self.behaviours],
            "capture": self.capture.to_payload() if self.capture else None,
            "clusters": [item.to_payload() for item in self.clusters],
            "clustering": (
                {
                    "k": self.clustering.k,
                    "silhouette": _s(self.clustering.silhouette),
                    "sizes": list(self.clustering.sizes()),
                    "features": [feature.name for feature in self.matrix.features]
                    if self.matrix
                    else [],
                    "coverage": _s(self.matrix.coverage) if self.matrix else None,
                    "dropped_features": list(self.matrix.dropped_features)
                    if self.matrix
                    else [],
                }
                if self.clustering and self.matrix
                else None
            ),
            "notes": list(self.notes),
        }


def discover_patterns(
    trades: Sequence[TradeRecord], config: DiscoveryConfig | None = None
) -> PatternReport:
    """Run the whole scan and correct it as one family."""
    settings = config or DiscoveryConfig()
    report = PatternReport(sample_size=len(trades))

    if len(trades) < MIN_SAMPLE:
        report.notes.append(
            f"{len(trades)} trades is too few to look for patterns in; "
            f"at least {MIN_SAMPLE} are needed before any comparison means anything."
        )
        return report

    if settings.include_behaviours:
        report.behaviours = _run_behaviours(trades, settings)
        report.capture = summarise_capture(trades, seed=settings.seed)

    if settings.include_clustering:
        clusters, clustering, matrix = _run_clustering(trades, settings)
        report.clusters = clusters
        report.clustering = clustering
        report.matrix = matrix
        report.clustered_trades = matrix.size if matrix else 0
        if matrix and matrix.dropped_features:
            report.notes.append(
                "Features not used because too few trades carried them: "
                + ", ".join(matrix.dropped_features)
            )
        if settings.include_clustering and clustering is None:
            report.notes.append(
                "These trades do not fall into distinct behavioural groups. That is a "
                "result, not a failure — it means the differences between them are "
                "smaller than the differences within them."
            )

    _adjust_as_one_family(report)
    report.tests_performed = sum(
        1 for item in report.behaviours if item.comparison is not None
    ) + sum(1 for item in report.clusters if item.comparison is not None)

    logger.info(
        "patterns.scanned",
        trades=len(trades),
        tests=report.tests_performed,
        findings=len(report.findings),
        clusters=len(report.clusters),
    )
    return report


def _run_behaviours(
    trades: Sequence[TradeRecord], config: DiscoveryConfig
) -> list[BehaviourFinding]:
    sequenced = sequence_trades(trades)
    findings: list[BehaviourFinding] = []
    for detector in DETECTORS:
        kwargs: dict[str, Any] = {"seed": config.seed}
        if config.permutations is not None:
            kwargs["permutations"] = config.permutations
        findings.append(detector(sequenced, **kwargs))
    return findings


def _run_clustering(
    trades: Sequence[TradeRecord], config: DiscoveryConfig
) -> tuple[list[ClusterFinding], Clustering | None, FeatureMatrix | None]:
    if len(trades) < config.min_trades_for_clustering:
        return [], None, None

    matrix = build_matrix(trades, features=config.features)
    if matrix.size < config.min_trades_for_clustering:
        return [], None, matrix

    clustering = choose_clustering(
        matrix.rows, seed=config.seed, references=config.references
    )
    if clustering is None:
        return [], None, matrix

    findings: list[ClusterFinding] = []
    for cluster in range(clustering.k):
        members = trades_in_cluster(clustering, matrix.trades, cluster)
        others = tuple(
            trade
            for index, trade in enumerate(matrix.trades)
            if clustering.labels[index] != cluster
        )

        inside = [trade.net_pnl for trade in members]
        outside = [trade.net_pnl for trade in others]

        comparison: ComparisonResult | None = None
        if len(inside) >= MIN_SAMPLE and len(outside) >= MIN_SAMPLE:
            kwargs: dict[str, Any] = {
                "label_a": f"cluster_{cluster}",
                "label_b": "rest",
                "seed": config.seed,
            }
            if config.permutations is not None:
                kwargs["permutations"] = config.permutations
            comparison = compare_samples(inside, outside, **kwargs)

        mean_inside = mean(inside)
        findings.append(
            ClusterFinding(
                cluster=cluster,
                size=len(members),
                traits=describe_cluster(clustering, cluster, matrix.features),
                centre=matrix.to_real_units(clustering.centroids[cluster]),
                expectancy=(
                    Estimate.of(mean_inside, len(inside), _interval(inside, config.seed))
                    if mean_inside is not None
                    else Estimate.undefined(len(inside), "cluster is empty")
                ),
                mean_pnl=mean_inside,
                mean_pnl_elsewhere=mean(outside),
                win_rate=count_trades(members).win_rate,
                comparison=comparison,
                trade_ids=tuple(str(trade.trade_id) for trade in members),
            )
        )

    return findings, clustering, matrix


def _adjust_as_one_family(report: PatternReport) -> None:
    """FDR-correct every test in the scan together, then write the results back.

    The dataclasses are frozen, so adjusted results are spliced in with ``replace``
    rather than mutated. That is deliberate: a finding that was never adjusted keeps
    ``adjusted_p_value = None`` and its ``is_significant`` refuses to answer, so
    forgetting to call this function cannot silently produce unadjusted findings.
    """
    indexed: list[tuple[str, int, ComparisonResult]] = []
    for position, behaviour in enumerate(report.behaviours):
        if behaviour.comparison is not None:
            indexed.append(("behaviour", position, behaviour.comparison))
    for position, cluster in enumerate(report.clusters):
        if cluster.comparison is not None:
            indexed.append(("cluster", position, cluster.comparison))

    if not indexed:
        return

    adjusted = control_false_discovery_rate([item[2] for item in indexed])

    for (kind, position, _), result in zip(indexed, adjusted, strict=True):
        if kind == "behaviour":
            report.behaviours[position] = replace(report.behaviours[position], comparison=result)
        else:
            report.clusters[position] = replace(report.clusters[position], comparison=result)


def propose_setups(report: PatternReport, *, min_confidence: Decimal = Decimal("0.55")) -> list[
    dict[str, Any]
]:
    """Turn discovered clusters into setup candidates a trader can accept or rename.

    This is the bridge from "we found a group" to ``trades.setup_id``. Nothing is
    applied automatically: an auto-assigned label is a claim about what the trader was
    thinking, and the journal does not get to make that claim unilaterally. Each
    proposal carries per-trade confidence so a borderline member can be reviewed rather
    than inheriting the certainty of a textbook one.
    """
    if report.clustering is None or report.matrix is None:
        return []

    proposals: list[dict[str, Any]] = []
    for finding in report.clusters:
        assignments = []
        for index in report.clustering.members(finding.cluster):
            confidence = cluster_confidence(
                report.clustering, report.matrix.rows[index], finding.cluster
            )
            if confidence is None or confidence < min_confidence:
                continue
            assignments.append(
                {
                    "trade_id": str(report.matrix.trades[index].trade_id),
                    "confidence": str(confidence),
                }
            )

        proposals.append(
            {
                "cluster": finding.cluster,
                "suggested_name": finding.label,
                "traits": list(finding.traits),
                "size": finding.size,
                "confident_members": len(assignments),
                "polarity": finding.polarity,
                "is_actionable": finding.is_actionable,
                "assignments": assignments,
            }
        )
    return proposals


def _interval(values: Sequence[Decimal], seed: int) -> Interval | None:
    if len(values) < MIN_SAMPLE:
        return None
    result = bootstrap_mean(values, seed=seed)
    return result.interval if result else None


def _impact(finding: BehaviourFinding | ClusterFinding) -> Decimal:
    """Sort key: most negative effect on the account first."""
    if isinstance(finding, BehaviourFinding):
        return finding.estimated_cost if finding.estimated_cost is not None else Decimal(0)
    difference = finding.difference
    return difference * Decimal(finding.size) if difference is not None else Decimal(0)


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
