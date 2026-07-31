"""The what-if simulator, tested for the ways a counterfactual flatters itself.

A simulator that re-prices trades under a different rule is the easiest place in this
product to produce a persuasive lie. Three specific failures are pinned here:

* counting trades the scenario *cannot* apply to as unchanged, which dilutes every
  effect toward zero and makes each counterfactual look safe;
* reporting the best variant in a sweep without correcting for having tried many;
* re-pricing against an excursion the trader could not have known about, and calling
  the result achievable.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.analytics.montecarlo import DEFAULT_SEED, bootstrap_mean
from app.analytics.types import Direction, TradeRecord
from app.analytics.whatif import (
    Scenario,
    SweepReport,
    _test_difference,
    default_scenarios,
    simulate,
    sweep,
)
from app.core.ids import uuid7

BASE = datetime(2026, 2, 2, 14, 30, tzinfo=UTC)


def trade(
    *,
    r_multiple: str | None,
    mae_r: str | None = "-0.4",
    mfe_r: str | None = "1.8",
    risk: str = "200",
    minute: int = 0,
    session: date = date(2026, 2, 2),
    hour: int = 9,
) -> TradeRecord:
    """One trade, with its R multiple and the excursions it reached.

    ``net_pnl`` is derived from ``r_multiple`` so that 1R is always ``risk`` in
    currency — the same relationship the analytics engine computes.
    """
    r = Decimal(r_multiple) if r_multiple is not None else None
    # The timestamp has to follow the session date, or trades from different sessions
    # sort into each other and the sequence-dependent filters see a scrambled order.
    opened = datetime.combine(session, BASE.timetz()) + timedelta(minutes=minute)
    return TradeRecord(
        trade_id=uuid7(),
        account_id=uuid7(),
        opened_at=opened,
        closed_at=opened + timedelta(minutes=20),
        direction=Direction.LONG,
        net_pnl=(r * Decimal(risk)) if r is not None else Decimal(0),
        quantity=Decimal(1),
        r_multiple=r,
        mae_r=Decimal(mae_r) if mae_r is not None else None,
        mfe_r=Decimal(mfe_r) if mfe_r is not None else None,
        duration_seconds=1200,
        session_date=session,
        entry_hour=hour,
        entry_weekday=session.isoweekday(),
    )


# --- Re-pricing -------------------------------------------------------------------


def test_a_target_is_only_taken_when_the_trade_reached_it() -> None:
    """The core re-pricing rule: a 2R target on a trade that peaked at 1.2R is fiction."""
    reached = trade(r_multiple="0.5", mfe_r="2.4")
    did_not = trade(r_multiple="0.5", mfe_r="1.2")

    result = simulate([reached, did_not], Scenario("2R target", target_r=Decimal(2)))

    outcomes = {item.original.trade_id: item for item in result.trades}
    assert outcomes[reached.trade_id].outcome == "repriced"
    assert outcomes[reached.trade_id].simulated_r == Decimal(2)
    assert outcomes[did_not.trade_id].outcome == "unchanged"
    assert outcomes[did_not.trade_id].simulated_pnl == did_not.net_pnl


def test_a_tighter_stop_only_fires_when_the_trade_traded_through_it() -> None:
    deep = trade(r_multiple="1.5", mae_r="-0.8")
    shallow = trade(r_multiple="1.5", mae_r="-0.2")

    result = simulate([deep, shallow], Scenario("0.5R stop", stop_r=Decimal("0.5")))

    outcomes = {item.original.trade_id: item for item in result.trades}
    # The winner that first went 0.8R against would have been stopped out for -0.5R.
    assert outcomes[deep.trade_id].simulated_r == Decimal("-0.5")
    assert outcomes[deep.trade_id].simulated_pnl == Decimal("-100.00")
    assert outcomes[shallow.trade_id].outcome == "unchanged"


def test_the_stop_is_evaluated_before_the_target() -> None:
    """A trade that hit the stop first never reached the target, whatever its MFE.

    Excursions record both extremes without recording which came first, so the only
    safe reading is the pessimistic one.
    """
    result = simulate(
        [trade(r_multiple="2.0", mae_r="-1.2", mfe_r="2.5")],
        Scenario("1R stop, 2R target", stop_r=Decimal(1), target_r=Decimal(2)),
    )

    assert result.trades[0].simulated_r == Decimal(-1)


def test_currency_is_recovered_from_the_trades_own_r_multiple() -> None:
    """1R must mean the same thing in the simulation as in the original trade."""
    result = simulate(
        [trade(r_multiple="0.5", risk="750", mfe_r="3.0")],
        Scenario("2R target", target_r=Decimal(2)),
    )

    assert result.trades[0].simulated_pnl == Decimal("1500.00")


# --- Inapplicability --------------------------------------------------------------


def test_a_trade_without_excursions_is_inapplicable_not_unchanged() -> None:
    """The failure that makes every counterfactual look safe.

    Counting a trade the scenario cannot touch as "unchanged" adds it to both sides of
    the comparison and pulls the measured effect toward zero.
    """
    result = simulate(
        [trade(r_multiple="1.0", mae_r=None, mfe_r=None)],
        Scenario("2R target", target_r=Decimal(2)),
    )

    assert result.trades[0].outcome == "inapplicable"
    assert result.inapplicable == 1
    assert result.evaluated == 0


def test_a_trade_without_a_stop_is_inapplicable() -> None:
    result = simulate(
        [trade(r_multiple=None)], Scenario("2R target", target_r=Decimal(2))
    )

    assert result.trades[0].outcome == "inapplicable"


def test_coverage_reports_how_much_of_the_sample_was_simulated() -> None:
    """A counterfactual over 40% of trades is a different claim from one over all."""
    trades = [trade(r_multiple="1.0", minute=index) for index in range(3)] + [
        trade(r_multiple="1.0", mae_r=None, mfe_r=None, minute=index + 3)
        for index in range(7)
    ]

    result = simulate(trades, Scenario("2R target", target_r=Decimal(2)))

    assert result.coverage == Decimal("0.3")
    assert not result.is_actionable


def test_a_filter_scenario_applies_to_every_trade() -> None:
    """Filters make no claim about a price path, so they need no excursion data."""
    trades = [
        trade(r_multiple="1.0", mae_r=None, mfe_r=None, minute=index * 10)
        for index in range(8)
    ]

    result = simulate(trades, Scenario("max 3", max_trades_per_session=3))

    assert result.inapplicable == 0
    assert result.coverage == Decimal(1)
    assert result.skipped == 5


# --- Sequence-dependent filters ---------------------------------------------------


def test_the_session_limit_counts_position_within_the_session() -> None:
    trades = [
        trade(r_multiple="1.0", minute=index * 10, session=date(2026, 2, 2))
        for index in range(5)
    ] + [
        trade(r_multiple="1.0", minute=index * 10, session=date(2026, 2, 3))
        for index in range(5)
    ]

    result = simulate(trades, Scenario("max 2", max_trades_per_session=2))

    # Two kept per session, three skipped per session.
    assert result.skipped == 6
    assert result.evaluated == 4


def test_a_skipped_trade_does_not_advance_the_session_counter() -> None:
    """The counterfactual trader never took it, so it cannot occupy a slot.

    Advancing the index on a skipped trade would simulate someone who took the trade
    and ignored the result — which is not the rule being tested.
    """
    trades = [
        trade(r_multiple="1.0", minute=index * 10, hour=9 if index % 2 == 0 else 14)
        for index in range(6)
    ]

    result = simulate(
        trades,
        Scenario("mornings, max 2", only_hours=(9,), max_trades_per_session=2),
    )

    # Three morning trades survive the hour filter; the session cap keeps the first two.
    assert result.evaluated == 2


def test_a_skipped_trade_does_not_extend_a_losing_streak() -> None:
    """Same reasoning: a trade that never happened cannot have lost."""
    trades = [
        trade(r_multiple="-1.0", minute=0, hour=14),
        trade(r_multiple="-1.0", minute=10, hour=14),
        trade(r_multiple="1.0", minute=20, hour=9),
    ]

    result = simulate(
        trades,
        Scenario(
            "mornings only, stop after 2 losses",
            only_hours=(9,),
            skip_after_consecutive_losses=2,
        ),
    )

    # The two losses were filtered out by the hour rule, so no streak ever formed and
    # the morning trade is taken.
    assert result.evaluated == 1
    assert result.simulated_pnl == Decimal("200")


def test_stopping_after_consecutive_losses_removes_the_rest_of_the_session() -> None:
    trades = [
        trade(r_multiple="-1.0", minute=0),
        trade(r_multiple="-1.0", minute=10),
        trade(r_multiple="-1.0", minute=20),
        trade(r_multiple="-1.0", minute=30),
    ]

    result = simulate(trades, Scenario("stop after 2", skip_after_consecutive_losses=2))

    assert result.evaluated == 2
    assert result.skipped == 2
    assert result.simulated_pnl == Decimal("-400")


def test_the_streak_resets_across_sessions() -> None:
    trades = [
        trade(r_multiple="-1.0", minute=0, session=date(2026, 2, 2)),
        trade(r_multiple="-1.0", minute=10, session=date(2026, 2, 2)),
        trade(r_multiple="-1.0", minute=0, session=date(2026, 2, 3)),
    ]

    result = simulate(trades, Scenario("stop after 2", skip_after_consecutive_losses=2))

    # The third trade opens a new session, so the streak does not carry over.
    assert result.evaluated == 3


# --- The sweep and its correction --------------------------------------------------


def build_noise(count: int, seed: int) -> list[TradeRecord]:
    """A history in which no exit rule can have an edge, by construction.

    Getting this generator right took three attempts, and the failures are instructive
    because each produced a confident, wrong "finding".

    Drawing ``r_multiple`` and ``mfe_r`` independently lets a -2R loser record a +2.5R
    favourable excursion — physically impossible, since it would have been a winner —
    and a target scenario then reprices almost every trade to a win.

    Clamping the excursions to contain the outcome (``mfe >= max(r, 0)``) fixes the
    impossibility but introduces a worse one: MAE becomes a *function* of the result,
    so every loser has a deep adverse excursion and every winner a shallow one.
    Tightening the stop is then guaranteed to help, on any sample, forever.

    The honest null is a **driftless random walk**. Each trade is a simulated price
    path; MFE and MAE are its running extremes and the R multiple is where it finished.
    All three are then jointly consistent, and the optional stopping theorem says no
    stop or target placement changes the expectation. Anything this sweep reports as a
    finding here is a false one.
    """
    rng = random.Random(seed)
    trades: list[TradeRecord] = []
    session = date(2026, 2, 2)

    for index in range(count):
        if index % 8 == 0:
            session += timedelta(days=1)

        position = 0.0
        high = 0.0
        low = 0.0
        for _ in range(60):
            position += rng.gauss(0, 0.2)
            high = max(high, position)
            low = min(low, position)

        trades.append(
            trade(
                r_multiple=str(round(position, 3)),
                mae_r=str(round(low, 3)),
                mfe_r=str(round(high, 3)),
                minute=(index % 8) * 20,
                session=session,
            )
        )
    return trades


@pytest.mark.parametrize("seed", [1, 3, 5])
def test_a_sweep_over_noise_establishes_nothing(seed: int) -> None:
    """The test that stops the simulator becoming a curve-fitting tool.

    Nine scenarios is nine chances to clear p < 0.05, and the winner will look
    specific. Correcting the family is what prevents it being reported.

    **These seeds are illustrative, not a guarantee, and the distinction matters.**
    Benjamini-Hochberg bounds the expected *proportion* of false discoveries; it does
    not promise zero on every sample, and a test asserting zero for arbitrary seeds is
    asserting something no correction provides. Measured over 25 noise sweeps, 2 of
    them established one scenario each — about 8%, which is what a 5% bound looks like
    at that sample size. The property actually worth testing is the one below: that the
    correction is what suppresses the raw significance.
    """
    report = sweep(build_noise(240, seed), default_scenarios(), permutations=400)

    assert report.results
    assert report.best is None


def test_the_correction_is_what_suppresses_the_noise_findings() -> None:
    """The mechanism, asserted directly rather than inferred from an outcome.

    On this sample three of the nine scenarios clear a raw p < 0.05 — exactly the
    "your 1.5R stop is worth $8,400" result an uncorrected sweep would publish — and
    none survives the family-wide adjustment.

    Testing it this way rather than through ``best is None`` makes the test sharp: it
    fails if the correction stops being applied, which an outcome-level assertion can
    miss whenever the raw noise happens to be quiet.
    """
    report = sweep(build_noise(240, 1), default_scenarios(), permutations=400)

    raw = [
        result
        for result in report.results
        if result.comparison is not None
        and result.comparison.p_value is not None
        and result.comparison.p_value < Decimal("0.05")
    ]
    survived = [
        result
        for result in report.results
        if result.comparison is not None and result.comparison.is_significant
    ]

    assert len(raw) >= 3, "the sample no longer produces the raw noise this test needs"
    assert survived == []


def test_the_best_scenario_is_the_best_established_one_not_the_largest() -> None:
    """`best` deliberately ignores an unproven improvement, however large."""
    report = sweep(build_noise(240, 4), default_scenarios(), permutations=400)

    improvements = [
        result for result in report.results if result.difference > 0
    ]
    assert improvements  # some variant always looks better on any sample
    assert report.best is None  # none of them survived correction


def test_the_sweep_reports_when_nothing_survived() -> None:
    report = sweep(build_noise(200, 7), default_scenarios(), permutations=300)

    assert any("survived correction" in note for note in report.notes)


def test_every_scenario_in_a_sweep_is_adjusted() -> None:
    """An unadjusted comparison refuses to call itself significant — check none remain."""
    report = sweep(build_noise(200, 5), default_scenarios()[:4], permutations=300)

    for result in report.results:
        if result.comparison is not None and result.comparison.p_value is not None:
            assert result.comparison.adjusted_p_value is not None


def test_thin_coverage_is_called_out_in_the_notes() -> None:
    trades = [
        trade(r_multiple="1.0", mae_r=None, mfe_r=None, minute=index * 5)
        for index in range(40)
    ] + [trade(r_multiple="1.0", minute=(index + 40) * 5) for index in range(5)]

    report = sweep(trades, [Scenario("2R", target_r=Decimal(2))], permutations=200)

    assert any("under half the sample" in note for note in report.notes)


def test_too_few_trades_reports_why_rather_than_simulating() -> None:
    report = sweep([trade(r_multiple="1.0")], default_scenarios(), permutations=100)

    assert report.results == []
    assert any("too few" in note for note in report.notes)


# --- Honesty of the payload -------------------------------------------------------


def test_the_payload_states_that_this_is_not_causal() -> None:
    """The caveat travels with the number, not in documentation nobody reads."""
    payload = sweep(build_noise(100, 9), default_scenarios()[:2], permutations=200).to_payload()

    assert "hindsight" in payload["interpretation"]
    assert "would have traded differently" in payload["interpretation"]


def test_the_payload_separates_repriced_from_skipped_from_inapplicable() -> None:
    trades = [trade(r_multiple="1.0", mfe_r="3.0", minute=index * 10) for index in range(4)] + [
        trade(r_multiple="1.0", mae_r=None, mfe_r=None, minute=(index + 4) * 10)
        for index in range(2)
    ]

    payload = simulate(
        trades, Scenario("2R, max 3", target_r=Decimal(2), max_trades_per_session=3)
    ).to_payload()

    counts = payload["trades"]
    assert counts["total"] == 6
    assert counts["repriced"] + counts["skipped"] + counts["inapplicable"] <= counts["total"]
    # The filter runs before the re-pricing, so the trades missing excursion data were
    # already skipped and never reached the step that would have found them
    # inapplicable. A trade the counterfactual never takes has no data requirements.
    assert counts["skipped"] == 3
    assert counts["inapplicable"] == 0


def test_an_empty_report_is_still_a_valid_payload() -> None:
    assert SweepReport().to_payload()["scenarios_tested"] == 0


def test_simulation_is_deterministic() -> None:
    trades = build_noise(150, 11)
    scenarios = default_scenarios()[:3]

    first = sweep(trades, scenarios, permutations=200).to_payload()
    second = sweep(trades, scenarios, permutations=200).to_payload()

    assert first == second


def build_with_a_real_edge(count: int, seed: int) -> list[TradeRecord]:
    """A random walk, plus one genuinely exploitable regularity.

    Every path that reaches +1R is dragged back to a loss before it closes. A 1R target
    therefore converts a reliable stream of losers into a reliable stream of winners —
    a real effect a simulator ought to find, and the complement of the null battery
    above. Without this test, an engine that reported nothing under all circumstances
    would pass every other test in this file.
    """
    rng = random.Random(seed)
    trades: list[TradeRecord] = []
    session = date(2026, 2, 2)

    for index in range(count):
        if index % 8 == 0:
            session += timedelta(days=1)

        position = 0.0
        high = 0.0
        low = 0.0
        for _ in range(60):
            position += rng.gauss(0, 0.2)
            high = max(high, position)
            low = min(low, position)

        if high >= 1.0:
            # The regularity: a run to +1R always gives it all back and then some.
            position = -abs(rng.gauss(0.8, 0.3))
            low = min(low, position)

        trades.append(
            trade(
                r_multiple=str(round(position, 3)),
                mae_r=str(round(low, 3)),
                mfe_r=str(round(high, 3)),
                minute=(index % 8) * 20,
                session=session,
            )
        )
    return trades


def test_a_real_edge_is_found() -> None:
    """The complement of the null battery: silence must not be the only answer."""
    report = sweep(build_with_a_real_edge(240, 21), default_scenarios(), permutations=400)

    best = report.best
    assert best is not None
    assert best.scenario.target_r == Decimal(1)
    assert best.difference > 0
    assert best.delta_interval is not None and best.delta_interval.excludes_zero


def test_a_real_edge_reports_its_per_trade_interval() -> None:
    """The headline total is not the evidence; the per-trade interval is."""
    report = sweep(build_with_a_real_edge(240, 22), default_scenarios(), permutations=400)
    best = report.best
    assert best is not None

    payload = best.to_payload()
    low, high = payload["delta_interval"]
    assert Decimal(low) > 0 and Decimal(high) > Decimal(low)


def test_each_scenario_keeps_its_own_adjusted_p_value() -> None:
    """The sweep must not hand a scenario another scenario's significance.

    Same defect as the pattern scan carried: the family-wide correction returns results
    ranked by p-value, and pairing them back positionally scrambles which scenario
    earned which q-value. A scenario that changed nothing then inherits the strongest
    result in the sweep and is reported as an established improvement.
    """
    report = sweep(build_noise(240, 9), default_scenarios(), permutations=300)

    tested = [
        result
        for result in report.results
        if result.comparison is not None and result.comparison.p_value is not None
    ]
    assert tested, "the sweep produced no testable scenario to check"

    for result in tested:
        comparison = result.comparison
        assert comparison is not None
        assert comparison.adjusted_p_value is not None, result.scenario.label
        assert comparison.adjusted_p_value >= comparison.p_value, result.scenario.label


def test_a_scenario_that_changes_nothing_is_not_significant() -> None:
    """The null case, which scored at the significance floor.

    A rule that reprices no trades — a 3R target on a history where nothing reached 3R —
    produces a delta of exactly zero on every trade. That is the *least* possible
    evidence against the null, and the sweep reported it as p = 0.0001, the smallest
    p-value the test can emit.

    The cause was measuring one tail and subtracting for the other.
    `proportion_at_or_below_zero` counts resamples landing exactly on zero, so with every
    resample at zero it is 1 and `1 - below` is 0 — read by a two-sided formula as "no
    resample fell on the far side", the signature of an overwhelming effect rather than
    of no effect at all.

    This mattered beyond one confusing row. The sweep is a single FDR-corrected family,
    so two scenarios with no effect and p = 0.0001 sat at the top of the sorted p-values
    and shifted the Benjamini-Hochberg threshold for every real test beside them.
    """
    comparison, interval = _test_difference(
        [Decimal(0)] * 200, seed=DEFAULT_SEED, iterations=2_000
    )

    assert comparison is not None
    assert comparison.p_value == Decimal(1), (
        f"a scenario with zero effect scored p = {comparison.p_value}; "
        "no evidence must not read as maximum evidence"
    )
    assert interval is not None
    assert interval.low == 0 and interval.high == 0


def test_a_real_improvement_is_still_significant() -> None:
    """Guards the fix against over-correcting.

    Making the degenerate case return 1 is only right if a genuine, consistent
    improvement still clears the bar. A uniform +50 on every trade is as strong as an
    effect gets.
    """
    comparison, interval = _test_difference(
        [Decimal(50)] * 200, seed=DEFAULT_SEED, iterations=2_000
    )

    assert comparison is not None
    assert comparison.p_value <= Decimal("0.01")
    assert interval is not None
    assert interval.low > 0


def test_a_mixed_sample_lands_between_the_two() -> None:
    """Noise centred on zero is not significant either, by a different route.

    Here the resample distribution genuinely straddles zero rather than collapsing onto
    it, so both tails are substantial and the p-value is large without the atom being
    involved at all.
    """
    deltas = [Decimal(value) for value in (30, -28, 25, -32, 27, -26) * 40]
    comparison, _ = _test_difference(deltas, seed=DEFAULT_SEED, iterations=2_000)

    assert comparison is not None
    assert comparison.p_value > Decimal("0.05")


def test_both_bootstrap_tails_include_zero() -> None:
    """The property the p-value depends on, asserted where it is produced.

    The two tails sum to more than one exactly when resamples land on zero. Asserting it
    here means a later "simplification" of `bootstrap_mean` back to a single tail fails
    in the module that owns the definition, not three layers up in a sweep result.
    """
    result = bootstrap_mean([Decimal(0)] * 100, iterations=500, seed=DEFAULT_SEED)

    assert result is not None
    assert result.proportion_at_or_below_zero == Decimal(1)
    assert result.proportion_at_or_above_zero == Decimal(1)
