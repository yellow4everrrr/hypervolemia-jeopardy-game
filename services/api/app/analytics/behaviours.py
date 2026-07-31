"""Named behavioural detectors: the leaks that have a name before they have a number.

Clustering finds groups nobody thought to look for. This module looks for the specific
destructive patterns that show up in most traders' histories, and it is the more useful
half — a cluster described as "short trades entered late, sized large" needs
interpretation, whereas "you lose money on the trades you take within ten minutes of a
loss" is already an instruction.

Every detector works the same way: **split the sample on a behaviour, compare outcomes,
and let the permutation test decide whether the split means anything.** No detector
reports a finding on its own; :mod:`app.analytics.discovery` collects them all and
FDR-adjusts the whole scan together, because running eight tests and reporting the two
that came back at p < 0.05 is how a journal manufactures leaks.

**The rule every detector must obey: the split may not be a function of the value being
compared.** Grouping trades by ``r_multiple / mfe_r`` and then comparing their
``r_multiple`` is not a test — the answer is built into the question, and it returns the
smallest p-value the permutation count allows on *every* sample, including pure noise.
That is worse than a useless detector, because Benjamini–Hochberg is a step-up
procedure: one guaranteed-tiny p-value raises the acceptance threshold for every other
test in the family, so a single circular detector manufactures findings out of its
honest neighbours. Questions of the form "did I exit too early?" are answered here as
estimates (:class:`CaptureSummary`), never as members of the test family.

What distinguishes these from the segmentation cube is *sequence*. Hour of day and
instrument are properties of a trade. Revenge trading, escalating size while down, and
overtrading a session are properties of a trade's **position in a sequence**, which no
amount of slicing a flat table will surface.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.analytics.montecarlo import DEFAULT_SEED, bootstrap_mean
from app.analytics.significance import ComparisonResult, compare_samples
from app.analytics.statistics import mean, median
from app.analytics.types import MIN_SAMPLE, Estimate, Interval, Reliability, TradeRecord

#: Trades taken within this long of the previous exit count as "immediately after".
#: Ten minutes is not a law of nature; it is short enough that the previous trade is
#: plainly still in mind and long enough to catch more than instant re-entries.
REVENGE_WINDOW_MINUTES = 10

#: A session's trades beyond this index are "late in the session" for the overtrading
#: detector. Six is the median self-imposed limit across the starter rule set.
OVERTRADING_THRESHOLD = 6


@dataclass(frozen=True, slots=True)
class SequencedTrade:
    """A trade with its position in the session worked out.

    The same before-this-trade semantics the compliance engine uses: a trade is judged
    against the state of the day as it stood when the decision was made, not as it
    ended up.
    """

    trade: TradeRecord
    index_in_session: int
    session_pnl_before: Decimal
    consecutive_losses: int
    minutes_since_last_exit: int | None
    previous_was_loss: bool


class Unit(StrEnum):
    """What the compared values are measured in.

    Carried explicitly because it decides two things: whether a difference can be
    turned into an estimated cost (only currency can), and which direction counts as
    a leak — more dollars is better, more seconds spent in a loser is not.
    """

    CURRENCY = "currency"
    R_MULTIPLE = "r_multiple"
    SECONDS = "seconds"


@dataclass(frozen=True, slots=True)
class BehaviourFinding:
    """One detector's verdict, with everything needed to judge it.

    Attributes:
        polarity: ``leak`` when the behaviour is associated with worse outcomes,
            ``edge`` when better. Set from the observed difference, not assumed.
        comparison: The permutation test. ``is_significant`` stays ``None`` until the
            whole scan has been FDR-adjusted, so a finding cannot be read as
            established before the correction is applied.
        estimated_cost: Difference in mean P&L times the number of affected trades —
            what the behaviour is associated with over the sample. Not a forecast, and
            ``None`` for any detector not measured in currency.
    """

    kind: str
    label: str
    description: str
    affected: int
    unaffected: int
    mean_when_present: Decimal | None
    mean_when_absent: Decimal | None
    expectancy_when_present: Estimate
    comparison: ComparisonResult | None
    estimated_cost: Decimal | None
    detail: dict[str, Any]
    unit: Unit = Unit.CURRENCY
    #: Whether a larger value is the better outcome. False for holding time, where the
    #: comparison is "losers are held longer" and longer is the problem.
    higher_is_better: bool = True

    @property
    def difference(self) -> Decimal | None:
        if self.mean_when_present is None or self.mean_when_absent is None:
            return None
        return self.mean_when_present - self.mean_when_absent

    @property
    def polarity(self) -> str:
        difference = self.difference
        if difference is None or difference == 0:
            return "edge"
        worse = difference < 0 if self.higher_is_better else difference > 0
        return "leak" if worse else "edge"

    @property
    def reliability(self) -> Reliability:
        return Reliability.for_sample(min(self.affected, self.unaffected))

    @property
    def is_actionable(self) -> bool:
        """A finding, rather than an observation.

        Needs a testable sample on both sides and significance after the scan-wide
        adjustment. Without the second condition this is a list of the largest
        differences, which any random split of any sample will also produce.
        """
        if self.reliability is Reliability.INSUFFICIENT:
            return False
        return bool(self.comparison and self.comparison.is_significant)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "polarity": self.polarity,
            "affected": self.affected,
            "unaffected": self.unaffected,
            "unit": self.unit.value,
            "mean_when_present": _s(self.mean_when_present),
            "mean_when_absent": _s(self.mean_when_absent),
            "difference": _s(self.difference),
            "estimated_cost": _s(self.estimated_cost),
            "reliability": self.reliability.value,
            "is_actionable": self.is_actionable,
            "p_value": _s(self.comparison.p_value) if self.comparison else None,
            "adjusted_p_value": (
                _s(self.comparison.adjusted_p_value) if self.comparison else None
            ),
            "effect_size": _s(self.comparison.effect_size) if self.comparison else None,
            "effect_magnitude": self.comparison.effect_magnitude if self.comparison else None,
            "expectancy_when_present": self.expectancy_when_present.to_payload()
            if hasattr(self.expectancy_when_present, "to_payload")
            else None,
            "detail": self.detail,
        }


def _interval(values: Sequence[Decimal], seed: int) -> Interval | None:
    """Bootstrap interval, or ``None`` when the sample is too small to resample."""
    if len(values) < MIN_SAMPLE:
        return None
    result = bootstrap_mean(values, seed=seed)
    return result.interval if result else None


def _entry_order(trade: TradeRecord) -> tuple[datetime, str]:
    """Stable ordering within a session: entry time, then id to break exact ties."""
    return (trade.opened_at, str(trade.trade_id))


def sequence_trades(trades: Sequence[TradeRecord]) -> list[SequencedTrade]:
    """Work out each trade's position within its session.

    Grouped by session date and walked in entry order, accumulating state *before* each
    trade. Trades without a session date are grouped under ``None`` together rather than
    dropped — they still have an order, and excluding them would quietly shrink the
    sample the tests run on.
    """
    by_session: dict[Any, list[TradeRecord]] = {}
    for trade in trades:
        by_session.setdefault(trade.session_date, []).append(trade)

    sequenced: list[SequencedTrade] = []
    for session_trades in by_session.values():
        ordered = sorted(session_trades, key=_entry_order)
        running = Decimal(0)
        losses = 0
        previous_exit: datetime | None = None
        previous_loss = False

        for index, trade in enumerate(ordered):
            minutes = None
            if previous_exit is not None:
                minutes = max(0, int((trade.opened_at - previous_exit).total_seconds() // 60))

            sequenced.append(
                SequencedTrade(
                    trade=trade,
                    index_in_session=index,
                    session_pnl_before=running,
                    consecutive_losses=losses,
                    minutes_since_last_exit=minutes,
                    previous_was_loss=previous_loss,
                )
            )

            running += trade.net_pnl
            if trade.is_loser:
                losses += 1
                previous_loss = True
            elif trade.is_winner:
                losses = 0
                previous_loss = False
            previous_exit = trade.closed_at or previous_exit

    return sequenced


def _split(
    sequenced: Sequence[SequencedTrade], predicate: Callable[[SequencedTrade], bool | None]
) -> tuple[list[TradeRecord], list[TradeRecord]]:
    """Partition into present / absent, discarding trades the predicate cannot judge.

    ``None`` means "cannot tell for this trade" — the first trade of a session has no
    previous exit, so it is neither an example nor a counter-example of revenge trading.
    Forcing it into the absent group would dilute the comparison with trades the
    behaviour could not have applied to.
    """
    present: list[TradeRecord] = []
    absent: list[TradeRecord] = []
    for item in sequenced:
        verdict = predicate(item)
        if verdict is None:
            continue
        (present if verdict else absent).append(item.trade)
    return present, absent


def _finding(
    kind: str,
    label: str,
    description: str,
    present: Sequence[TradeRecord],
    absent: Sequence[TradeRecord],
    *,
    detail: dict[str, Any] | None = None,
    permutations: int | None = None,
    seed: int = DEFAULT_SEED,
    value: Callable[[TradeRecord], Decimal] = lambda trade: trade.net_pnl,
    unit: Unit = Unit.CURRENCY,
    higher_is_better: bool = True,
) -> BehaviourFinding:
    """Run the comparison and package it. Shared by every detector."""
    present_values = [value(trade) for trade in present]
    absent_values = [value(trade) for trade in absent]

    mean_present = mean(present_values)
    mean_absent = mean(absent_values)

    comparison: ComparisonResult | None = None
    if len(present) >= MIN_SAMPLE and len(absent) >= MIN_SAMPLE:
        kwargs: dict[str, Any] = {"label_a": "present", "label_b": "absent", "seed": seed}
        if permutations is not None:
            kwargs["permutations"] = permutations
        comparison = compare_samples(present_values, absent_values, **kwargs)

    # Only a currency comparison has a cost. Multiplying a difference in seconds by a
    # trade count produces a number with no meaning that someone would nonetheless read
    # as dollars.
    cost: Decimal | None = None
    if unit is Unit.CURRENCY and mean_present is not None and mean_absent is not None:
        cost = (mean_present - mean_absent) * Decimal(len(present))

    expectancy = (
        Estimate.of(mean_present, len(present), _interval(present_values, seed))
        if mean_present is not None
        else Estimate.undefined(len(present), "no trades exhibited this behaviour")
    )

    return BehaviourFinding(
        kind=kind,
        label=label,
        description=description,
        affected=len(present),
        unaffected=len(absent),
        mean_when_present=mean_present,
        mean_when_absent=mean_absent,
        expectancy_when_present=expectancy,
        comparison=comparison,
        estimated_cost=cost,
        detail=detail or {},
        unit=unit,
        higher_is_better=higher_is_better,
    )


# --- The detectors ---------------------------------------------------------------


def revenge_trading(
    sequenced: Sequence[SequencedTrade], *, window: int = REVENGE_WINDOW_MINUTES, **kwargs: Any
) -> BehaviourFinding:
    """Trades taken immediately after a loss, against everything else.

    The canonical tilt pattern: a loss, then a fast re-entry to make it back. The
    comparison holds the *behaviour* constant rather than the setup, so what it measures
    is the cost of the timing, not of the idea.
    """

    def predicate(item: SequencedTrade) -> bool | None:
        if item.minutes_since_last_exit is None:
            return None
        return item.previous_was_loss and item.minutes_since_last_exit <= window

    present, absent = _split(sequenced, predicate)
    return _finding(
        "revenge_trading",
        f"Re-entering within {window} minutes of a loss",
        f"Trades opened within {window} minutes of closing a losing trade, compared "
        "with every other trade that followed a completed trade.",
        present,
        absent,
        detail={"window_minutes": window},
        **kwargs,
    )


def overtrading(
    sequenced: Sequence[SequencedTrade], *, threshold: int = OVERTRADING_THRESHOLD, **kwargs: Any
) -> BehaviourFinding:
    """Late-session trades against the first few of the day.

    Tests whether the trades taken after the plan is exhausted resemble the ones taken
    while it was still being followed.
    """
    present, absent = _split(sequenced, lambda item: item.index_in_session >= threshold)
    return _finding(
        "overtrading",
        f"Trades beyond the {threshold}th of a session",
        f"Trades taken after the first {threshold} of a session, compared with the "
        "first few.",
        present,
        absent,
        detail={"threshold": threshold},
        **kwargs,
    )


def trading_while_down(sequenced: Sequence[SequencedTrade], **kwargs: Any) -> BehaviourFinding:
    """Trades opened while the session is already in the red."""
    present, absent = _split(sequenced, lambda item: item.session_pnl_before < 0)
    return _finding(
        "trading_while_down",
        "Trading while down on the session",
        "Trades opened when the session was already negative, compared with trades "
        "opened flat or up.",
        present,
        absent,
        **kwargs,
    )


def size_escalation(sequenced: Sequence[SequencedTrade], **kwargs: Any) -> BehaviourFinding:
    """Trades sized above the trader's own median while down on the session.

    Deliberately conjunctive. Size alone is confounded — a trader may legitimately size
    up on their best setup — but *sizing up while losing* is the pattern that turns a
    bad day into an account event.
    """
    sizes = [item.trade.quantity for item in sequenced]
    typical = median(sizes)
    if typical is None:
        return _finding(
            "size_escalation",
            "Increasing size while down",
            "Not computable without position sizes.",
            [],
            [],
            **kwargs,
        )

    def predicate(item: SequencedTrade) -> bool | None:
        return item.session_pnl_before < 0 and item.trade.quantity > typical

    present, absent = _split(sequenced, predicate)
    return _finding(
        "size_escalation",
        "Increasing size while down on the session",
        f"Trades larger than the usual {typical} contracts taken while the session was "
        "negative, compared with every other trade.",
        present,
        absent,
        detail={"median_size": str(typical)},
        **kwargs,
    )


def after_a_losing_streak(
    sequenced: Sequence[SequencedTrade], *, streak: int = 2, **kwargs: Any
) -> BehaviourFinding:
    """Trades taken with two or more losses immediately behind them."""
    present, absent = _split(sequenced, lambda item: item.consecutive_losses >= streak)
    return _finding(
        "after_losing_streak",
        f"Trading after {streak} consecutive losses",
        f"Trades opened with {streak} or more losses immediately preceding them.",
        present,
        absent,
        detail={"streak": streak},
        **kwargs,
    )


def holding_losers_too_long(
    sequenced: Sequence[SequencedTrade], **kwargs: Any
) -> BehaviourFinding:
    """Do losers get held longer than winners?

    The asymmetry that quietly reverses an edge: cutting winners at target while giving
    losers room "to come back" inverts the payoff the strategy was built on.

    The comparison is deliberately the other way round from every other detector here —
    it groups **by outcome** and compares **holding time**. Splitting on duration and
    comparing P&L would be close to circular, because the duration threshold would have
    to come from somewhere, and deriving it from winners' holding times builds the
    conclusion into the split. Grouping by an outcome that is already known and
    comparing a variable that is not the outcome has no such problem.
    """
    winners = [
        item.trade
        for item in sequenced
        if item.trade.is_winner and item.trade.duration_seconds is not None
    ]
    losers = [
        item.trade
        for item in sequenced
        if item.trade.is_loser and item.trade.duration_seconds is not None
    ]

    return _finding(
        "holding_losers_too_long",
        "Holding losers longer than winners",
        "Time spent in losing trades compared with time spent in winning trades. A "
        "positive difference means losers are given more room than winners are.",
        losers,
        winners,
        value=lambda trade: Decimal(trade.duration_seconds or 0),
        unit=Unit.SECONDS,
        higher_is_better=False,
        **kwargs,
    )


def first_trade_of_the_session(
    sequenced: Sequence[SequencedTrade], **kwargs: Any
) -> BehaviourFinding:
    """The opening trade against the rest of the day.

    Included because it frequently comes back as an *edge* rather than a leak, and a
    detector suite that can only find faults is one nobody keeps reading.
    """
    present, absent = _split(sequenced, lambda item: item.index_in_session == 0)
    return _finding(
        "first_trade_of_session",
        "The first trade of the session",
        "The opening trade of each session, compared with every subsequent trade.",
        present,
        absent,
        **kwargs,
    )


#: Every detector, run as one family. Adding one here widens the scan, which the
#: false-discovery-rate correction accounts for automatically — the more places you
#: look, the higher the bar each finding has to clear.
#:
#: Before adding one, check that its split is not a function of the value it compares.
#: A detector that cannot fail to be significant poisons every other test in this tuple.
DETECTORS: tuple[Callable[..., BehaviourFinding], ...] = (
    revenge_trading,
    overtrading,
    trading_while_down,
    size_escalation,
    after_a_losing_streak,
    holding_losers_too_long,
    first_trade_of_the_session,
)


@dataclass(frozen=True, slots=True)
class CaptureSummary:
    """How much of the available move the trader actually takes.

    This answers "am I cutting winners early?", and it is deliberately **not** a
    hypothesis test. Splitting trades by how much of their excursion they captured and
    then comparing what they captured is circular: the split contains the answer, so it
    returns the smallest possible p-value on every sample, noise included. Worse, being
    part of an FDR family, that rigged p-value would lower the bar for every honest test
    beside it.

    Reported instead as an estimate with a confidence interval. "Your winners reached
    2.1R on average and you captured 0.9R" is a real claim about the exits, it carries
    its own uncertainty, and nothing about it pretends to be a discovery.
    """

    winners: int
    mean_mfe_r: Decimal | None
    mean_captured_r: Decimal | None
    mean_capture_ratio: Estimate
    #: R per winning trade left on the table, on average.
    forgone_r: Decimal | None

    @property
    def is_computable(self) -> bool:
        return self.mean_capture_ratio.value is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "winners": self.winners,
            "mean_mfe_r": _s(self.mean_mfe_r),
            "mean_captured_r": _s(self.mean_captured_r),
            "mean_capture_ratio": self.mean_capture_ratio.to_payload(),
            "forgone_r": _s(self.forgone_r),
            "interpretation": (
                "Descriptive. Excursion is measured with hindsight — the best price the "
                "trade reached was not knowable at the exit — so this bounds what was "
                "available, not what was achievable."
            ),
        }


def summarise_capture(
    trades: Sequence[TradeRecord], *, seed: int = DEFAULT_SEED
) -> CaptureSummary:
    """Mean capture ratio across winning trades, with a bootstrap interval.

    Restricted to winners. A loser's capture ratio is negative or undefined and mixing
    the two produces an average of two different questions.
    """
    eligible = [
        trade
        for trade in trades
        if trade.is_winner
        and trade.mfe_r is not None
        and trade.r_multiple is not None
        and trade.mfe_r > 0
    ]
    if not eligible:
        return CaptureSummary(
            winners=0,
            mean_mfe_r=None,
            mean_captured_r=None,
            mean_capture_ratio=Estimate.undefined(
                0, "needs winning trades with recorded excursions and stops"
            ),
            forgone_r=None,
        )

    ratios = [
        trade.r_multiple / trade.mfe_r  # type: ignore[operator]
        for trade in eligible
    ]
    mean_mfe = mean([trade.mfe_r for trade in eligible])  # type: ignore[misc]
    mean_captured = mean([trade.r_multiple for trade in eligible])  # type: ignore[misc]
    mean_ratio = mean(ratios)

    return CaptureSummary(
        winners=len(eligible),
        mean_mfe_r=mean_mfe,
        mean_captured_r=mean_captured,
        mean_capture_ratio=(
            Estimate.of(mean_ratio, len(ratios), _interval(ratios, seed))
            if mean_ratio is not None
            else Estimate.undefined(len(ratios), "capture ratio is undefined")
        ),
        forgone_r=(
            mean_mfe - mean_captured
            if mean_mfe is not None and mean_captured is not None
            else None
        ),
    )


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
