"""Pattern detection, tested primarily for what it refuses to find.

The valuable property of this engine is not that it discovers patterns — any clustering
library discovers patterns, and most of them are noise. It is that it declines to, on
data where there is nothing to find. So the centrepiece here is a battery of null tests:
pure noise in, nothing out.

Two bugs found during development are pinned by name below, because both were invisible
in the output and both produced confident, plausible-looking findings:

* a detector whose split was a function of the value it compared, which returned the
  minimum possible p-value on *every* sample and — via the step-up FDR procedure —
  dragged its honest neighbours over the significance line with it;
* a silhouette threshold that accepted the partition k-means imposes on uniform noise.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.analytics.behaviours import (
    DETECTORS,
    Unit,
    overtrading,
    revenge_trading,
    sequence_trades,
    summarise_capture,
    trading_while_down,
)
from app.analytics.clustering import (
    choose_clustering,
    cluster_confidence,
    describe_cluster,
    kmeans,
    silhouette,
    structure_p_value,
)
from app.analytics.discovery import DiscoveryConfig, discover_patterns, propose_setups
from app.analytics.features import build_matrix
from app.analytics.types import Direction, TradeRecord
from app.core.ids import uuid7

#: Small permutation counts keep the suite fast. They cost resolution in the p-value,
#: never correctness — a coarser p-value is a *more* conservative test, so a finding
#: that survives here would survive at full resolution too.
FAST = DiscoveryConfig(permutations=300, references=20)

BASE = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def build_trades(
    count: int,
    seed: int,
    *,
    leak_after: int | None = None,
    leak_size: Decimal = Decimal(-260),
    per_session: int = 8,
    structured: bool = False,
) -> list[TradeRecord]:
    """Synthetic history with optional injected structure.

    P&L is drawn from a stream independent of every other attribute, so unless
    ``leak_after`` is set there is genuinely nothing for a detector to find, and any
    finding is a false one.
    """
    shape = random.Random(seed)
    pnl_draw = random.Random(seed + 7_919)
    trades: list[TradeRecord] = []
    session = date(2026, 1, 5)
    session_start = BASE

    for index in range(count):
        position = index % per_session
        if position == 0:
            session += timedelta(days=1)
            session_start += timedelta(days=1)

        if structured:
            scalp = index % 2 == 0
            duration = int(shape.gauss(120, 30)) if scalp else int(shape.gauss(5400, 600))
            quantity = shape.randint(4, 6) if scalp else shape.randint(1, 2)
            hour = shape.choice([9, 10]) if scalp else shape.choice([14, 15])
        else:
            duration = shape.randint(60, 6000)
            quantity = shape.randint(1, 6)
            hour = 9 + position // 2

        pnl = Decimal(str(round(pnl_draw.gauss(20, 200), 2)))
        if leak_after is not None and position >= leak_after:
            pnl += leak_size

        opened = session_start + timedelta(minutes=position * 20)
        trades.append(
            TradeRecord(
                trade_id=uuid7(),
                account_id=uuid7(),
                opened_at=opened,
                closed_at=opened + timedelta(seconds=max(duration, 10)),
                direction=Direction.LONG if shape.random() < 0.5 else Direction.SHORT,
                net_pnl=pnl,
                quantity=Decimal(quantity),
                duration_seconds=max(duration, 10),
                session_date=session,
                entry_hour=hour,
                entry_weekday=session.isoweekday(),
                r_multiple=pnl / Decimal(200),
                mae_r=Decimal(str(round(-abs(shape.gauss(0.5, 0.3)), 3))),
                mfe_r=Decimal(str(round(abs(shape.gauss(1.2, 0.6)) + 0.1, 3))),
            )
        )
    return trades


# --- The null battery ------------------------------------------------------------


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_a_scan_of_pure_noise_finds_nothing(seed: int) -> None:
    """The single most important test in this file.

    Every detector runs, every p-value is computed, and after the family-wide correction
    nothing survives. A journal that fails this test is not a weaker journal — it is one
    that invents leaks, and a trader acting on an invented leak is worse off than one
    who never opened it.
    """
    report = discover_patterns(build_trades(320, seed), FAST)

    assert report.tests_performed >= 5
    assert report.findings == []


def test_detectors_do_not_split_on_the_value_they_compare() -> None:
    """Guards the circularity bug directly.

    A detector whose grouping is derived from the quantity it then compares cannot fail
    to be significant. This runs every detector on data where the compared value is
    independent of everything else, and asserts none of them returns a p-value pinned at
    the permutation floor.
    """
    trades = build_trades(300, 99)
    sequenced = sequence_trades(trades)
    floor = Decimal(1) / Decimal(301)  # the smallest p-value 300 permutations can give

    for detector in DETECTORS:
        finding = detector(sequenced, permutations=300, seed=99)
        if finding.comparison is None or finding.comparison.p_value is None:
            continue
        assert finding.comparison.p_value > floor, (
            f"{finding.kind} returned the minimum possible p-value on independent data, "
            "which means its split is a function of the value it compares"
        )


def test_capture_efficiency_is_an_estimate_not_a_test() -> None:
    """"Am I cutting winners early?" cannot be asked as a two-group comparison.

    Grouping trades by captured-fraction and comparing what they captured is circular.
    It is reported as an estimate with an interval instead, and — crucially — it is not
    a member of the FDR family, so it cannot lower the bar for the real tests.
    """
    trades = build_trades(300, 5)
    capture = summarise_capture(trades)

    assert capture.is_computable
    assert capture.mean_capture_ratio.interval is not None
    assert capture.forgone_r is not None
    assert "cutting_winners_early" not in {detector.__name__ for detector in DETECTORS}


def test_uniform_noise_does_not_cluster() -> None:
    """k-means partitions anything; the structure test is what stops it being reported.

    Uniformly random trades reach a silhouette around 0.29 — above any fixed threshold
    low enough to be useful — and produce confidently worded descriptions. Only the
    comparison against a shuffled null rejects them.
    """
    matrix = build_matrix(build_trades(140, 4))

    assert matrix.size >= 120
    assert choose_clustering(matrix.rows, references=20) is None


def test_real_structure_does_cluster() -> None:
    """The complement: the engine must still find groups that genuinely exist.

    The signal here sits in three features (duration, size, hour) while five others —
    the excursion set — are pure noise, which is the realistic case and holds the
    silhouette well below what a clean two-blob dataset would score. It still separates
    the scalps from the swings, and it still beats its null.
    """
    matrix = build_matrix(build_trades(120, 4, structured=True))
    clustering = choose_clustering(matrix.rows, references=20)

    assert clustering is not None
    assert clustering.k == 2
    assert clustering.silhouette is not None and clustering.silhouette > Decimal("0.25")
    assert clustering.structure_p_value is not None
    assert clustering.structure_p_value <= Decimal("0.05")

    # Scalps and swings, described in the trader's own terms.
    descriptions = {describe_cluster(clustering, index, matrix.features) for index in range(2)}
    flattened = {phrase for group in descriptions for phrase in group}
    assert "held briefly" in flattened
    assert "held longer than usual" in flattened


# --- Detection of things that are really there ------------------------------------


def test_an_injected_leak_is_found() -> None:
    """A real effect must survive the same correction that filters the noise out."""
    report = discover_patterns(build_trades(400, 11, leak_after=5), FAST)

    kinds = {getattr(finding, "kind", None) for finding in report.findings}
    assert "overtrading" in kinds

    overtrading_finding = next(
        item for item in report.behaviours if item.kind == "overtrading"
    )
    assert overtrading_finding.polarity == "leak"
    assert overtrading_finding.estimated_cost is not None
    assert overtrading_finding.estimated_cost < 0
    assert overtrading_finding.is_actionable


def test_findings_are_ranked_by_cost_not_by_p_value() -> None:
    """The trader is deciding what to change, not what is most certain."""
    report = discover_patterns(build_trades(400, 11, leak_after=5), FAST)
    costs = [
        finding.estimated_cost
        for finding in report.findings
        if getattr(finding, "estimated_cost", None) is not None
    ]

    assert costs == sorted(costs)


# --- Determinism ------------------------------------------------------------------


def test_the_same_trades_always_produce_the_same_scan() -> None:
    """A trader who reloads and sees different patterns has learned they mean nothing."""
    trades = build_trades(300, 21, leak_after=5)

    first = discover_patterns(trades, FAST)
    second = discover_patterns(trades, FAST)

    assert [item.kind for item in first.behaviours] == [item.kind for item in second.behaviours]
    assert [item.comparison.p_value for item in first.behaviours if item.comparison] == [
        item.comparison.p_value for item in second.behaviours if item.comparison
    ]


def test_clustering_is_deterministic() -> None:
    matrix = build_matrix(build_trades(200, 3, structured=True))

    assert kmeans(matrix.rows, 2).labels == kmeans(matrix.rows, 2).labels


# --- Unit behaviour ---------------------------------------------------------------


def test_holding_time_is_compared_in_seconds_with_no_cost_attached() -> None:
    """Multiplying a difference in seconds by a trade count would read as dollars."""
    finding = next(
        item
        for item in discover_patterns(build_trades(300, 8), FAST).behaviours
        if item.kind == "holding_losers_too_long"
    )

    assert finding.unit is Unit.SECONDS
    assert finding.estimated_cost is None
    assert finding.higher_is_better is False


def test_a_longer_hold_on_losers_is_a_leak_not_an_edge() -> None:
    """Polarity has to follow the unit: more seconds in a loser is not an improvement."""
    trades = build_trades(240, 13)
    # Force losers to be held far longer than winners.
    stretched = [
        TradeRecord(**{**_as_dict(trade), "duration_seconds": 6000})
        if trade.is_loser
        else TradeRecord(**{**_as_dict(trade), "duration_seconds": 300})
        for trade in trades
    ]

    finding = next(
        item
        for item in discover_patterns(stretched, FAST).behaviours
        if item.kind == "holding_losers_too_long"
    )

    assert finding.difference is not None and finding.difference > 0
    assert finding.polarity == "leak"


# --- Sequencing -------------------------------------------------------------------


def test_session_state_is_as_of_before_the_trade() -> None:
    """Same rule as the compliance engine: judge the decision, not the outcome."""
    trades = build_trades(24, 2, per_session=8)
    sequenced = sequence_trades(trades)
    first_of_session = [item for item in sequenced if item.index_in_session == 0]

    assert len(first_of_session) == 3
    for item in first_of_session:
        assert item.session_pnl_before == 0
        assert item.consecutive_losses == 0
        assert item.minutes_since_last_exit is None


def test_trades_that_cannot_be_judged_are_excluded_not_counted_as_absent() -> None:
    """The first trade of a session is neither an example nor a counter-example.

    Revenge trading needs a previous exit to measure from. Forcing the openers into the
    "did not do it" group would dilute the comparison with trades where the behaviour
    was impossible.
    """
    trades = build_trades(80, 6, per_session=8)
    sequenced = sequence_trades(trades)
    finding = revenge_trading(sequenced, permutations=200)

    # Ten sessions, so ten openers with no previous exit to measure against.
    assert finding.affected + finding.unaffected == len(trades) - 10


def test_trading_while_down_excludes_the_trades_own_result() -> None:
    """Otherwise a losing trade would define itself as having been taken while down."""
    trades = build_trades(160, 17, per_session=8)
    sequenced = sequence_trades(trades)
    finding = trading_while_down(sequenced, permutations=200)

    assert finding.affected + finding.unaffected == len(trades)
    for item in sequenced:
        if item.index_in_session == 0:
            assert item.session_pnl_before == 0


# --- Small samples ----------------------------------------------------------------


def test_too_few_trades_reports_why_rather_than_guessing() -> None:
    report = discover_patterns(build_trades(3, 1), FAST)

    assert report.findings == []
    assert report.notes
    assert "too few" in report.notes[0]


def test_clustering_is_skipped_below_the_minimum() -> None:
    report = discover_patterns(build_trades(20, 1), FAST)

    assert report.clusters == []
    assert report.clustering is None


def test_no_setups_are_proposed_without_a_clustering() -> None:
    assert propose_setups(discover_patterns(build_trades(20, 1), FAST)) == []


# --- Feature handling -------------------------------------------------------------


def test_missing_features_are_dropped_rather_than_imputed() -> None:
    """Filling a missing R multiple with a zero invents a trade that did not happen."""
    trades = [
        TradeRecord(**{**_as_dict(trade), "mae_r": None, "mfe_r": None, "r_multiple": None})
        for trade in build_trades(120, 4)
    ]

    matrix = build_matrix(trades)

    assert "mae_r" in matrix.dropped_features
    assert "mfe_r" in matrix.dropped_features
    assert "capture_efficiency" in matrix.dropped_features
    # No trade is lost: the features went, the sample stayed.
    assert matrix.size == 120
    assert matrix.coverage == 1


def test_a_constant_feature_is_dropped() -> None:
    """It carries no information and would divide by zero when standardised."""
    trades = [
        TradeRecord(**{**_as_dict(trade), "quantity": Decimal(1)})
        for trade in build_trades(120, 4)
    ]

    assert "quantity" in build_matrix(trades).dropped_features


def test_standardised_columns_have_zero_mean_and_unit_spread() -> None:
    from app.analytics.statistics import mean, stdev

    matrix = build_matrix(build_trades(200, 4))

    for column in range(len(matrix.features)):
        values = [row[column] for row in matrix.rows]
        assert abs(mean(values) or Decimal(0)) < Decimal("0.0000001")
        spread = stdev(values)
        assert spread is not None and abs(spread - 1) < Decimal("0.0000001")


def test_centroids_convert_back_to_real_units() -> None:
    """A z-score is not something to show a trader."""
    matrix = build_matrix(build_trades(120, 4, structured=True))
    clustering = choose_clustering(matrix.rows, references=20)
    assert clustering is not None

    centres = [matrix.to_real_units(centroid) for centroid in clustering.centroids]
    durations = sorted(centre["duration_seconds"] for centre in centres)

    assert durations[0] < Decimal(600)  # the scalps
    assert durations[-1] > Decimal(3600)  # the swings


# --- Clustering internals ---------------------------------------------------------


def test_silhouette_is_undefined_for_a_single_cluster() -> None:
    assert silhouette([(Decimal(1),), (Decimal(2),)], [0, 0]) is None


def test_structure_p_value_never_returns_zero() -> None:
    """Twenty references cannot evidence "never"."""
    matrix = build_matrix(build_trades(120, 4, structured=True))
    clustering = kmeans(matrix.rows, 2)
    assert clustering.silhouette is not None

    p_value = structure_p_value(matrix.rows, 2, clustering.silhouette, references=20)

    assert p_value >= Decimal(1) / Decimal(21)


def test_cluster_confidence_is_lower_on_the_boundary() -> None:
    """An automatic label should not look as certain for a borderline trade."""
    matrix = build_matrix(build_trades(120, 4, structured=True))
    clustering = choose_clustering(matrix.rows, references=20)
    assert clustering is not None

    scores = [
        cluster_confidence(clustering, row, label)
        for row, label in zip(matrix.rows, clustering.labels, strict=True)
    ]
    known = [score for score in scores if score is not None]

    assert known
    assert all(Decimal("0.5") <= score <= 1 for score in known)
    assert min(known) < max(known)


def test_too_few_references_to_reach_alpha_is_an_error() -> None:
    """Otherwise the function silently becomes a constant `None`.

    With six references the smallest attainable p-value is 1/7, so at alpha 0.05 no
    clustering could ever pass — and the output would be indistinguishable from a trader
    who genuinely has no patterns.
    """
    matrix = build_matrix(build_trades(120, 4, structured=True))

    with pytest.raises(ValueError, match="cannot reach alpha"):
        choose_clustering(matrix.rows, references=6)


def test_kmeans_refuses_more_clusters_than_points() -> None:
    with pytest.raises(ValueError, match="cannot form"):
        kmeans([(Decimal(1),), (Decimal(2),)], 5)


# --- Reporting --------------------------------------------------------------------


def test_the_report_says_how_many_places_it_looked() -> None:
    """Two findings out of thirteen tests is a different claim from two out of two."""
    report = discover_patterns(build_trades(320, 3), FAST)
    payload = report.to_payload()

    assert payload["sample"]["tests_performed"] == report.tests_performed
    assert payload["sample"]["tests_performed"] > 0
    assert payload["sample"]["findings"] == len(report.findings)


def test_an_unadjusted_finding_refuses_to_call_itself_significant() -> None:
    """Forgetting the correction must not silently produce findings."""
    sequenced = sequence_trades(build_trades(300, 11, leak_after=5))
    raw = overtrading(sequenced, permutations=300)

    assert raw.comparison is not None
    assert raw.comparison.p_value is not None
    assert raw.comparison.adjusted_p_value is None
    assert raw.comparison.is_significant is None
    assert not raw.is_actionable


def test_setup_proposals_carry_per_trade_confidence() -> None:
    report = discover_patterns(build_trades(120, 4, structured=True), FAST)
    proposals = propose_setups(report)

    assert proposals
    for proposal in proposals:
        assert proposal["suggested_name"]
        assert proposal["confident_members"] <= proposal["size"]
        for assignment in proposal["assignments"]:
            assert Decimal(assignment["confidence"]) >= Decimal("0.55")


def _as_dict(trade: TradeRecord) -> dict:
    """Field mapping for rebuilding a frozen record with one attribute changed."""
    return {
        field: getattr(trade, field)
        for field in (
            "trade_id",
            "account_id",
            "opened_at",
            "closed_at",
            "direction",
            "net_pnl",
            "gross_pnl",
            "commission",
            "fees",
            "quantity",
            "r_multiple",
            "duration_seconds",
            "session_date",
            "session_segment",
            "entry_hour",
            "entry_weekday",
            "instrument_symbol",
            "instrument_root",
            "strategy",
            "setup",
            "market_condition",
            "mae_r",
            "mfe_r",
        )
    }


def test_each_finding_keeps_its_own_adjusted_p_value() -> None:
    """Guards against the correction attaching q-values to the wrong findings.

    Benjamini–Hochberg only ever scales a p-value upward, so for every finding the
    adjusted value must be at least its own raw one. If the scan pairs findings with
    somebody else's q-value, a high-p detector receives a low-q and this fails.

    That is not a cosmetic mix-up. It shipped, and its effect was to publish a
    pure-noise detector as significant using the significance earned by an unrelated
    one — manufactured by the exact mechanism built to prevent manufacturing.
    """
    report = discover_patterns(build_trades(400, 11, leak_after=5), FAST)

    tested = [
        item
        for item in report.behaviours
        if item.comparison is not None and item.comparison.p_value is not None
    ]
    assert tested, "the scan produced no testable behaviour to check"

    for finding in tested:
        comparison = finding.comparison
        assert comparison is not None
        assert comparison.adjusted_p_value is not None, finding.kind
        assert comparison.adjusted_p_value >= comparison.p_value, finding.kind
