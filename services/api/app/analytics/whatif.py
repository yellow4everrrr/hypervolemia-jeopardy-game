"""Counterfactual re-simulation: what a different rule would have produced.

The question a trader actually wants answered is "what if I had used a 2R target?" —
and it is the question most easily answered dishonestly. Three failure modes, each
addressed here.

**Hindsight.** Re-simulating an exit against the excursion the trade actually reached
assumes the trader would have known where the high was. Excursions bound what was
*available*, never what was achievable, and every counterfactual built on them inherits
that. Stated in the output rather than buried in a footnote.

**Selection.** Sweeping twenty target multiples and reporting the best one is the
segmentation-cube problem in a new costume: the winner is the loudest coincidence in the
sweep. Every variant is tested against the baseline and the whole sweep is FDR-corrected
as one family, exactly as :mod:`app.analytics.discovery` does.

**Silent inapplicability.** A trade with no recorded stop cannot be re-simulated against
a different stop. Counting it as unchanged would dilute the effect toward zero and make
every counterfactual look safe. Such trades are *excluded and reported*, and a
simulation covering half the sample says so.

What this module does **not** do is claim causation. A trader who had used a 2R target
would have traded differently in ways the data cannot show — held winners with more
conviction, cut losers sooner, or abandoned the rule after three losses in a row. The
simulation re-prices the outcomes; it does not re-simulate the trader.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from app.analytics.montecarlo import DEFAULT_ITERATIONS, DEFAULT_SEED, bootstrap_mean
from app.analytics.significance import ComparisonResult, control_false_discovery_rate
from app.analytics.statistics import mean, stdev
from app.analytics.types import MIN_SAMPLE, Estimate, Interval, Reliability, TradeRecord

#: Bumped whenever the re-pricing logic changes, so a stored simulation records which
#: engine produced it.
SIMULATOR_VERSION = 1


@dataclass(frozen=True, slots=True)
class Scenario:
    """One counterfactual rule.

    Attributes:
        stop_r: Replace the stop with this distance in R. ``1`` is the trade's actual
            initial risk, so ``Decimal("0.5")`` is a stop half as wide.
        target_r: Exit at this R multiple when the trade reached it.
        max_trades_per_session: Skip trades beyond this index in a session.
        skip_after_consecutive_losses: Skip trades taken with this many losses behind
            them.
        only_hours: Restrict to these entry hours; others are skipped.
    """

    label: str
    stop_r: Decimal | None = None
    target_r: Decimal | None = None
    max_trades_per_session: int | None = None
    skip_after_consecutive_losses: int | None = None
    only_hours: tuple[int, ...] | None = None

    @property
    def is_filter_only(self) -> bool:
        """Whether the scenario only removes trades rather than re-pricing them.

        Filters need no excursion data, so they apply to the whole sample. Worth
        distinguishing because a filter simulation is far more trustworthy than an
        exit simulation — it makes no claim about a price path.
        """
        return self.stop_r is None and self.target_r is None

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "stop_r": str(self.stop_r) if self.stop_r is not None else None,
            "target_r": str(self.target_r) if self.target_r is not None else None,
            "max_trades_per_session": self.max_trades_per_session,
            "skip_after_consecutive_losses": self.skip_after_consecutive_losses,
            "only_hours": list(self.only_hours) if self.only_hours else None,
        }


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    """One trade after re-pricing, with why it changed — or why it could not."""

    original: TradeRecord
    simulated_r: Decimal | None
    simulated_pnl: Decimal | None
    #: ``repriced``, ``unchanged``, ``skipped`` (the scenario's filter removed it), or
    #: ``inapplicable`` (the trade lacks the data the scenario needs).
    outcome: str
    reason: str | None = None

    @property
    def counts_toward_pnl(self) -> bool:
        return self.outcome in {"repriced", "unchanged"}


@dataclass
class SimulationResult:
    """What a scenario would have produced, and how much to trust it."""

    scenario: Scenario
    baseline_pnl: Decimal
    simulated_pnl: Decimal
    baseline_expectancy: Estimate
    simulated_expectancy: Estimate
    trades: list[SimulatedTrade] = field(default_factory=list)
    comparison: ComparisonResult | None = None
    #: Bootstrap interval on the *per-trade* difference. The primary evidence: an
    #: interval spanning zero means resampling this history can produce an improvement
    #: or a loss, whatever the headline total says.
    delta_interval: Interval | None = None

    @property
    def repriced(self) -> int:
        return sum(1 for trade in self.trades if trade.outcome == "repriced")

    @property
    def skipped(self) -> int:
        return sum(1 for trade in self.trades if trade.outcome == "skipped")

    @property
    def inapplicable(self) -> int:
        return sum(1 for trade in self.trades if trade.outcome == "inapplicable")

    @property
    def evaluated(self) -> int:
        return sum(1 for trade in self.trades if trade.counts_toward_pnl)

    @property
    def coverage(self) -> Decimal | None:
        """Fraction of the sample the scenario could actually be applied to.

        A counterfactual that re-priced 40% of trades is a different claim from one
        that re-priced all of them, and the difference is invisible in the P&L alone.
        """
        if not self.trades:
            return None
        applicable = len(self.trades) - self.inapplicable
        return Decimal(applicable) / Decimal(len(self.trades))

    @property
    def difference(self) -> Decimal:
        return self.simulated_pnl - self.baseline_pnl

    @property
    def is_improvement(self) -> bool:
        return self.difference > 0

    @property
    def reliability(self) -> Reliability:
        return Reliability.for_sample(self.evaluated)

    @property
    def is_actionable(self) -> bool:
        """Whether this is a finding rather than an arithmetic exercise.

        Four conditions, and the interval is the one that does most of the work.

        A scenario that repriced a single trade out of 240 can produce a consistent
        sign and a tiny p-value while moving total P&L by a dollar. The bootstrap
        interval on the per-trade difference catches exactly that: resample the history
        and the improvement vanishes. This is the same bar
        :class:`app.analytics.segmentation.Segment` and
        :class:`app.analytics.discovery.ClusterFinding` clear, and for the same reason.

        Coverage matters too — a counterfactual applied to a third of the sample
        describes a subset, not this trader's history.
        """
        if self.reliability is not Reliability.RELIABLE:
            return False
        if self.coverage is None or self.coverage < Decimal("0.5"):
            return False
        if self.delta_interval is None or not self.delta_interval.excludes_zero:
            return False
        return bool(self.comparison and self.comparison.is_significant)

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.to_payload(),
            "baseline_pnl": str(self.baseline_pnl),
            "simulated_pnl": str(self.simulated_pnl),
            "difference": str(self.difference),
            "is_improvement": self.is_improvement,
            "baseline_expectancy": self.baseline_expectancy.to_payload(),
            "simulated_expectancy": self.simulated_expectancy.to_payload(),
            "trades": {
                "total": len(self.trades),
                "repriced": self.repriced,
                "skipped": self.skipped,
                "inapplicable": self.inapplicable,
                "evaluated": self.evaluated,
            },
            "coverage": str(self.coverage) if self.coverage is not None else None,
            "reliability": self.reliability.value,
            "is_actionable": self.is_actionable,
            "delta_interval": (
                [str(self.delta_interval.low), str(self.delta_interval.high)]
                if self.delta_interval is not None
                else None
            ),
            "p_value": (
                str(self.comparison.p_value)
                if self.comparison and self.comparison.p_value is not None
                else None
            ),
            "adjusted_p_value": (
                str(self.comparison.adjusted_p_value)
                if self.comparison and self.comparison.adjusted_p_value is not None
                else None
            ),
            "effect_size": (
                str(self.comparison.effect_size)
                if self.comparison and self.comparison.effect_size is not None
                else None
            ),
        }


def simulate(
    trades: Sequence[TradeRecord],
    scenario: Scenario,
    *,
    seed: int = DEFAULT_SEED,
    permutations: int | None = None,
) -> SimulationResult:
    """Re-price a history under one scenario.

    Trades are walked in session order because the filters are sequence-dependent —
    "skip anything past the sixth trade of a session" cannot be evaluated on a trade in
    isolation, and a skipped trade changes the *position* of everything after it.
    """
    simulated: list[SimulatedTrade] = []

    index_in_session = 0
    consecutive_losses = 0
    current_session = object()

    for trade in sorted(trades, key=lambda item: (item.opened_at, str(item.trade_id))):
        if trade.session_date != current_session:
            current_session = trade.session_date
            index_in_session = 0
            consecutive_losses = 0

        skip_reason = _filter_reason(
            scenario, index_in_session=index_in_session, losses=consecutive_losses, trade=trade
        )
        if skip_reason is not None:
            simulated.append(
                SimulatedTrade(trade, None, None, outcome="skipped", reason=skip_reason)
            )
            # A skipped trade never happened in the counterfactual, so it advances
            # neither the session index nor the losing streak. Advancing them would
            # simulate a trader who took the trade and ignored the result.
            continue

        outcome = _reprice(trade, scenario)
        simulated.append(outcome)

        index_in_session += 1
        if outcome.simulated_pnl is not None:
            if outcome.simulated_pnl < 0:
                consecutive_losses += 1
            elif outcome.simulated_pnl > 0:
                consecutive_losses = 0

    baseline_values = [
        item.original.net_pnl for item in simulated if item.outcome != "inapplicable"
    ]
    simulated_values = [
        item.simulated_pnl
        for item in simulated
        if item.counts_toward_pnl and item.simulated_pnl is not None
    ]

    deltas = [_delta(item) for item in simulated if item.outcome != "inapplicable"]
    comparison, delta_interval = _test_difference(
        deltas, seed=seed, iterations=permutations or DEFAULT_ITERATIONS
    )

    return SimulationResult(
        scenario=scenario,
        baseline_pnl=sum(baseline_values, start=Decimal(0)),
        simulated_pnl=sum(simulated_values, start=Decimal(0)),
        baseline_expectancy=_expectancy(baseline_values, seed),
        simulated_expectancy=_expectancy(simulated_values, seed),
        trades=simulated,
        comparison=comparison,
        delta_interval=delta_interval,
    )


def _test_difference(
    deltas: Sequence[Decimal], *, seed: int, iterations: int
) -> tuple[ComparisonResult | None, Interval | None]:
    """Is the improvement robust to which trades happened to occur?

    Choosing the null here is the whole inferential question, and the obvious choices
    are both wrong.

    A **two-sample permutation test** treats baseline and simulated as independent
    draws. They are not — they are the same trades measured twice, most of them
    identical on both sides, so pooling them inflates the reference variance with
    duplicates the design never contained.

    A **sign-flip paired test** assumes the sign of each difference is arbitrary under
    the null. It is not: a 2R target deterministically produces the deltas it produces.
    That null is trivially false the moment the rule touches a single trade, so the test
    returns "significant" for any scenario that changes anything at all — which is
    every scenario, and therefore no information.

    The question a trader is actually asking is whether the improvement would survive a
    *different sample of trades*. So the deltas are bootstrapped: resample trades with
    replacement, recompute the mean difference, and ask how often the sign flips. An
    improvement driven by two lucky trades collapses under resampling; one spread across
    the history does not.
    """
    if len(deltas) < MIN_SAMPLE:
        return None, None

    observed = mean(deltas)
    if observed is None:  # pragma: no cover — guarded above
        return None, None

    result = bootstrap_mean(deltas, iterations=iterations, seed=seed)
    if result is None:  # pragma: no cover — sample size guarded above
        return None, None

    # Two-sided bootstrap p-value from the share of resamples landing on the wrong side
    # of zero. Clamped to a floor of 1/iterations rather than allowed to reach zero:
    # a finite number of resamples can never rule chance out entirely, and p = 0 would
    # overstate what was measured.
    below = result.proportion_at_or_below_zero
    p_value = min(
        Decimal(1),
        max(
            Decimal(2) * min(below, Decimal(1) - below),
            Decimal(1) / Decimal(iterations),
        ),
    )

    spread = stdev(deltas)
    comparison = ComparisonResult(
        label_a="simulated",
        label_b="baseline",
        mean_a=observed,
        mean_b=Decimal(0),
        difference=observed,
        sample_a=len(deltas),
        sample_b=len(deltas),
        p_value=p_value,
        # Cohen's d for a paired design: the mean difference in units of its own
        # spread, not of a pooled between-group spread.
        effect_size=(observed / spread) if spread else None,
        permutations=result.iterations,
    )
    return comparison, result.interval


def _delta(item: SimulatedTrade) -> Decimal:
    """How much this trade's contribution changed under the scenario.

    A skipped trade contributes ``-net_pnl``: the counterfactual trader never took it,
    so its result is removed from the total. Treating a skip as a zero delta would say
    that not taking a losing trade changed nothing.
    """
    if item.outcome == "skipped":
        return -item.original.net_pnl
    if item.simulated_pnl is None:  # pragma: no cover — inapplicable is filtered out
        return Decimal(0)
    return item.simulated_pnl - item.original.net_pnl


def _filter_reason(
    scenario: Scenario,
    *,
    index_in_session: int,
    losses: int,
    trade: TradeRecord,
) -> str | None:
    if (
        scenario.max_trades_per_session is not None
        and index_in_session >= scenario.max_trades_per_session
    ):
        return f"beyond trade {scenario.max_trades_per_session} of the session"
    if (
        scenario.skip_after_consecutive_losses is not None
        and losses >= scenario.skip_after_consecutive_losses
    ):
        return f"taken after {losses} consecutive losses"
    if scenario.only_hours is not None:
        if trade.entry_hour is None:
            return "entry hour not recorded"
        if trade.entry_hour not in scenario.only_hours:
            return f"entry hour {trade.entry_hour} is outside the allowed hours"
    return None


def _reprice(trade: TradeRecord, scenario: Scenario) -> SimulatedTrade:
    """Work out what the trade would have returned under the scenario's exit rules.

    Requires excursions and a recorded stop. Without them the trade is *inapplicable* —
    not unchanged. Treating it as unchanged would let trades the scenario cannot touch
    dilute its measured effect toward zero, which flatters every counterfactual.
    """
    if scenario.is_filter_only:
        return SimulatedTrade(
            trade, trade.r_multiple, trade.net_pnl, outcome="unchanged"
        )

    if trade.r_multiple is None or trade.mae_r is None or trade.mfe_r is None:
        return SimulatedTrade(
            trade,
            None,
            None,
            outcome="inapplicable",
            reason="needs a recorded stop and excursion data",
        )

    # Risk per R in currency, derived from what the trade actually returned. Recovering
    # it this way rather than from a stored risk amount keeps the simulation consistent
    # with the R multiple the analytics engine computed.
    risk = _risk_per_r(trade)
    if risk is None:
        return SimulatedTrade(
            trade,
            None,
            None,
            outcome="inapplicable",
            reason="cannot recover the currency value of 1R for this trade",
        )

    stop_r = -abs(scenario.stop_r) if scenario.stop_r is not None else None
    target_r = scenario.target_r

    # A tighter stop only changes the outcome if the trade actually traded through it.
    # `mae_r` is negative by convention, so "went further against you than the new stop"
    # is a strictly-less-than comparison.
    if stop_r is not None and trade.mae_r <= stop_r:
        return SimulatedTrade(
            trade,
            stop_r,
            (stop_r * risk).quantize(Decimal("0.01")),
            outcome="repriced",
            reason=f"would have stopped out at {stop_r}R",
        )

    if target_r is not None and trade.mfe_r >= target_r:
        return SimulatedTrade(
            trade,
            target_r,
            (target_r * risk).quantize(Decimal("0.01")),
            outcome="repriced",
            reason=f"would have hit the {target_r}R target",
        )

    # Neither level was touched: the trade ends where it actually ended.
    return SimulatedTrade(trade, trade.r_multiple, trade.net_pnl, outcome="unchanged")


def _risk_per_r(trade: TradeRecord) -> Decimal | None:
    """Currency value of one R for this trade."""
    if trade.r_multiple is None or trade.r_multiple == 0:
        return None
    return trade.net_pnl / trade.r_multiple


def _expectancy(values: Sequence[Decimal], seed: int) -> Estimate:
    average = mean(values)
    if average is None:
        return Estimate.undefined(len(values), "no trades remained under this scenario")
    return Estimate.of(average, len(values), _interval(values, seed))


def _interval(values: Sequence[Decimal], seed: int) -> Interval | None:
    if len(values) < MIN_SAMPLE:
        return None
    result = bootstrap_mean(values, seed=seed)
    return result.interval if result else None


@dataclass
class SweepReport:
    """A family of scenarios, corrected together."""

    results: list[SimulationResult] = field(default_factory=list)
    sample_size: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> list[SimulationResult]:
        return sorted(
            (result for result in self.results if result.is_actionable),
            key=lambda result: result.difference,
            reverse=True,
        )

    @property
    def best(self) -> SimulationResult | None:
        """The largest *established* improvement, or ``None``.

        Deliberately not "the largest improvement". The best-performing variant in any
        sweep is the one that got luckiest, and returning it unqualified is how a
        simulator becomes a curve-fitting tool.
        """
        improvements = [result for result in self.actionable if result.is_improvement]
        return improvements[0] if improvements else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "engine_version": SIMULATOR_VERSION,
            "sample_size": self.sample_size,
            "scenarios_tested": len(self.results),
            "actionable": len(self.actionable),
            "best": self.best.to_payload() if self.best else None,
            "results": [result.to_payload() for result in self.results],
            "notes": list(self.notes),
            "interpretation": (
                "Counterfactual, not causal. Excursions are measured with hindsight, so "
                "these bound what was available rather than what was achievable, and a "
                "trader following a different rule would have traded differently in "
                "ways this cannot model."
            ),
        }


def sweep(
    trades: Sequence[TradeRecord],
    scenarios: Sequence[Scenario],
    *,
    seed: int = DEFAULT_SEED,
    permutations: int | None = None,
) -> SweepReport:
    """Run every scenario and correct the whole family together.

    The correction is the entire point. Twenty scenarios is twenty chances to clear
    p < 0.05, and the one that clears it will look specific and convincing. Testing them
    as a family means the more variants you try, the higher the bar each has to clear —
    which is the correct accounting and the reason a sweep cannot be used to shop for a
    result.
    """
    report = SweepReport(sample_size=len(trades))

    if len(trades) < MIN_SAMPLE:
        report.notes.append(
            f"{len(trades)} trades is too few to simulate against; at least "
            f"{MIN_SAMPLE} are needed before a comparison means anything."
        )
        return report

    report.results = [
        simulate(trades, scenario, seed=seed, permutations=permutations)
        for scenario in scenarios
    ]

    comparisons = [
        (index, result.comparison)
        for index, result in enumerate(report.results)
        if result.comparison is not None
    ]
    if comparisons:
        adjusted = control_false_discovery_rate([item[1] for item in comparisons])
        for (index, _), corrected in zip(comparisons, adjusted, strict=True):
            report.results[index] = replace(report.results[index], comparison=corrected)

    thin = [
        result
        for result in report.results
        if result.coverage is not None and result.coverage < Decimal("0.5")
    ]
    if thin:
        report.notes.append(
            f"{len(thin)} scenario(s) could only be applied to under half the sample — "
            "most trades lack the recorded stop or excursion data they need. Their "
            "results describe a subset, not this history."
        )
    if not report.actionable:
        report.notes.append(
            "No scenario produced a difference that survived correction across the "
            "sweep. Changing these rules would not measurably have changed the outcome "
            "at this sample size."
        )
    return report


def default_scenarios() -> list[Scenario]:
    """A standard sweep covering the changes traders most often consider.

    Kept small on purpose. Every added scenario raises the bar for all the others
    through the family-wide correction, so a sweep of fifty variants makes it nearly
    impossible for any of them to establish anything.
    """
    return [
        Scenario("Take profit at 1R", target_r=Decimal(1)),
        Scenario("Take profit at 2R", target_r=Decimal(2)),
        Scenario("Take profit at 3R", target_r=Decimal(3)),
        Scenario("Tighten the stop to 0.5R", stop_r=Decimal("0.5")),
        Scenario("Widen the stop to 1.5R", stop_r=Decimal("1.5")),
        Scenario(
            "Stop at 1R, target at 2R", stop_r=Decimal(1), target_r=Decimal(2)
        ),
        Scenario("At most 3 trades a session", max_trades_per_session=3),
        Scenario("At most 6 trades a session", max_trades_per_session=6),
        Scenario("Stop after 2 consecutive losses", skip_after_consecutive_losses=2),
    ]
