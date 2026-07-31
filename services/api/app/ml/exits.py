"""Choosing where to take profit, without letting the choosing become the result.

This was deferred out of milestones 10 and 11 twice, with the same stated reason each
time: the naive implementation is precisely the curve-fitting ADR 0008 refuses.

That naive version is worth spelling out, because it is what every "optimal exit" feature
does. Sweep stop over a dozen values and target over a dozen more, re-price the trader's
history under all 144 combinations, and report the best cell. On a trader with no exit
edge whatsoever the best of 144 draws from a noisy distribution will look excellent, be
specific — "your optimal stop is 1.75R" — and be entirely an artefact of having looked
144 times. Measured here on pure noise, that naive search advertised between +$1,854 and
+$5,089.

The what-if sweep already refuses this by correcting a *family of nine* scenarios and
returning the largest **established** improvement. A grid is the same problem an order of
magnitude worse, and correction alone does not fix it: with 144 comparisons a genuine
moderate edge cannot clear the adjusted threshold either, so the honest sweep answers
"nothing" to every trader whether or not they have an edge.

**So the grid is not the hypothesis. The selection procedure is.**

Rather than asking "which cell is best?", this asks: *if I pick the best cell on data I
have, does that choice beat what the trader already does on data I have not?* That is one
question, tested once, and it is the question a trader actually has — they are not going
to trade the grid, they are going to trade whatever the procedure picks.

Mechanically, per fold: select the best rule on the training sessions, apply **that** rule
to the test sessions, and record the per-trade difference against what the trader really
did. The reported improvement is the out-of-sample performance of the selection, never the
in-sample performance of the winner.

Two further guards, because a positive out-of-sample mean is still not sufficient:

* **Selection stability.** If fold one picks a 1R target and fold two picks 3R, the
  procedure is not finding structure — it is fitting each window's noise, and its average
  delta coming out positive is luck. A recommendation nobody could have followed
  consistently is not a recommendation.
* **A session-block bootstrap interval** on the deltas, exactly as in
  :mod:`app.ml.skill`, because a mean over folds is a point estimate and this codebase has
  been wrong twice by trusting one.

Only targets are searched, and stops are not
--------------------------------------------

This is the most important thing in the file. It was found by measurement, after the first
version searched both and reported a rule on **one noise sample in three** — on a
driftless random walk, where the optional stopping theorem proves no exit rule can help at
all.

The cause is not in the selection. It is in the re-pricing, and it applies to the shipped
what-if simulator too. Over 4,000 driftless-walk trades, a 0.5R stop reported **+8.28 per
trade** with no selection involved whatsoever — one fixed rule, no grid, no choosing::

    of 2,786 trades that traded through -0.5R:
       mean actual final R  = -0.619
       stopped at           = -0.500

An excursion says price *reached* a level. It does not say a fill was available there.
Conditioning on "the recorded extreme passed the stop" selects the paths that overshot it,
and the overshoot is precisely the amount by which assuming a perfect fill flatters the
counterfactual. Observing the path more finely shrinks the gap but never closes it — 0.094
at 60 samples per trade, 0.030 at 3,840 — because barrier overshoot decays only with the
square root of the sampling interval.

**The direction of the error depends on the order type, and that is what makes targets
usable.** A stop is a market order: it fills at the trigger *or worse*, so assuming the
trigger price is optimistic. A target is a limit order: it fills at the limit *or better*,
so assuming the limit price is conservative. The same measurement shows the mirror image —
a 1R target on the same driftless data reports **-4.94 per trade**, biased against itself.

A target improvement that survives this procedure therefore survives despite a bias
working against it, while a stop improvement of the same size would be indistinguishable
from the bias working for it. No fix is available from excursion data: the overshoot at
the moment of crossing is not recoverable from the pair (extreme, outcome), and any
slippage constant chosen to cancel it would be a number invented to produce the answer.

The trader's own stop is left exactly where they put it. Every candidate here changes only
where profit is taken.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.analytics.montecarlo import DEFAULT_SEED
from app.analytics.statistics import mean
from app.analytics.types import Interval, TradeRecord
from app.analytics.whatif import Scenario, SimulatedTrade, _delta, _reprice
from app.core.logging import get_logger
from app.ml.skill import _index_at

logger = get_logger(__name__)

#: Candidate targets, in R. Deliberately coarse: a finer grid does not find a better rule,
#: it finds a more specific-sounding one, and the difference between a 2R and a 2.1R target
#: is far below what a few hundred trades can resolve.
TARGET_CANDIDATES: tuple[Decimal, ...] = (
    Decimal(1),
    Decimal("1.5"),
    Decimal(2),
    Decimal(3),
    Decimal(4),
)

#: **Stops are deliberately not searched**, and the reason is a measurement rather than a
#: preference. See the module docstring: re-pricing a tightened stop from excursion data is
#: systematically optimistic, by an amount comparable to any improvement it would report.
#:
#: Kept as a named constant because the obvious future change is to "just add stops to the
#: grid", and this is where whoever tries should find out why not.
STOPS_ARE_NOT_SEARCHED = True

#: Sessions in the first training window, and how far each fold advances. Larger than the
#: model layer's because selecting an exit rule needs enough trades in the training window
#: for the selection itself to mean anything.
MIN_TRAIN_SESSIONS = 25
STEP_SESSIONS = 10

#: Below this many out-of-sample trades no recommendation is made at all.
MIN_SCORED = 40

#: The fraction of folds that must agree on a rule before it is called stable. Two thirds
#: is a judgement: unanimity is too strict on five folds, and a bare majority is a coin
#: flip wearing a number.
STABILITY_THRESHOLD = Decimal("0.6")


@dataclass(frozen=True, slots=True)
class ExitRule:
    """One candidate exit.

    Only the target varies. The trader's own stop is left exactly where they put it, so
    every candidate here is "take profit at N R" rather than a change to their risk.
    """

    target_r: Decimal

    @property
    def label(self) -> str:
        return f"take profit at {self.target_r}R"

    def to_scenario(self) -> Scenario:
        return Scenario(label=self.label, stop_r=None, target_r=self.target_r)

    def to_payload(self) -> dict[str, Any]:
        return {"target_r": str(self.target_r), "label": self.label}


def candidates() -> list[ExitRule]:
    return [ExitRule(target_r=target) for target in TARGET_CANDIDATES]


@dataclass(frozen=True, slots=True)
class FoldResult:
    """What one fold selected, and how that selection then did."""

    fold: int
    chosen: ExitRule
    train_sessions: int
    test_trades: int
    #: Per-trade differences against what the trader actually did, out of sample.
    deltas: tuple[tuple[date | None, Decimal], ...]

    @property
    def mean_delta(self) -> Decimal | None:
        return mean([value for _, value in self.deltas])


@dataclass(frozen=True, slots=True)
class ExitRecommendation:
    """The out-of-sample verdict on the selection procedure.

    ``rule`` is what the procedure would pick from the whole history — the thing a trader
    would actually adopt. It is *not* where the reported improvement comes from: that comes
    from the folds, which never saw the data they were measured on.
    """

    rule: ExitRule | None
    folds: tuple[FoldResult, ...]
    improvement: Decimal | None
    interval: Interval | None
    stability: Decimal | None
    scored_trades: int
    refusal: str | None

    @property
    def is_established(self) -> bool:
        return self.refusal is None

    def to_payload(self) -> dict[str, Any]:
        return {
            "rule": self.rule.to_payload() if self.rule else None,
            "is_established": self.is_established,
            "refusal": self.refusal,
            "improvement_per_trade": (
                str(self.improvement) if self.improvement is not None else None
            ),
            "interval": (
                [str(self.interval.low), str(self.interval.high)] if self.interval else None
            ),
            "stability": str(self.stability) if self.stability is not None else None,
            "scored_trades": self.scored_trades,
            "folds": [
                {
                    "fold": fold.fold,
                    "chosen": fold.chosen.to_payload(),
                    "test_trades": fold.test_trades,
                    "mean_delta": str(fold.mean_delta) if fold.mean_delta is not None else None,
                }
                for fold in self.folds
            ],
            "interpretation": (
                "The improvement reported here is the out-of-sample performance of the "
                "selection procedure, not the in-sample performance of the best rule. "
                "The best cell of a grid always looks good on the data it was chosen "
                "from; what matters is whether choosing that way beats what you already "
                "do on trades the choice never saw."
            ),
        }


def _sessions_of(trades: Sequence[TradeRecord]) -> list[date]:
    return sorted({t.session_date for t in trades if t.session_date is not None})


def _repriced(trades: Sequence[TradeRecord], rule: ExitRule) -> list[SimulatedTrade]:
    """Apply a rule to every trade, with no statistics attached.

    Calls the simulator's own re-pricing so a rule evaluated here and the same rule run
    through ``/simulator/sweep`` cannot disagree — but deliberately stops short of
    ``simulate()``, which also runs a bootstrap and a significance test.

    That distinction is the design rather than an optimisation detail. **Selection needs a
    point estimate; only the final out-of-sample result is a hypothesis.** Bootstrapping
    inside the ranking loop computes a confidence interval for each of 25 candidates per
    fold — hundreds of intervals whose only use is to be sorted, none of which is ever
    reported. The first version did exactly that and a single run took minutes.

    These candidates carry no session filters, so the sequence-dependent bookkeeping in
    ``simulate`` has nothing to do and each trade re-prices independently.
    """
    scenario = rule.to_scenario()
    return [_reprice(trade, scenario) for trade in trades]


def _score(trades: Sequence[TradeRecord], rule: ExitRule) -> Decimal | None:
    """Mean per-trade difference from applying a rule. ``None`` when inapplicable."""
    priced = _repriced(trades, rule)
    applicable = [item for item in priced if item.outcome != "inapplicable"]
    if not applicable:
        return None
    # Coverage matters as much here as in the sweep: a rule that could only be applied to
    # a fifth of the training window was selected on a fifth of the evidence.
    if Decimal(len(applicable)) / Decimal(len(priced)) < Decimal("0.5"):
        return None
    return mean([_delta(item) for item in applicable])


def select(trades: Sequence[TradeRecord]) -> ExitRule | None:
    """The best candidate on this sample. **In-sample by construction.**

    Never used to report a result — only to make a choice that is then measured elsewhere.
    Ties break toward the earlier candidate, and the grid orders wider stops and no target
    first, so a tie resolves toward interfering with the trader's exits less.
    """
    best: tuple[Decimal, ExitRule] | None = None
    for rule in candidates():
        score = _score(trades, rule)
        if score is None:
            continue
        if best is None or score > best[0]:
            best = (score, rule)
    return best[1] if best else None


def _per_trade_deltas(
    trades: Sequence[TradeRecord], rule: ExitRule
) -> list[tuple[date | None, Decimal]]:
    """Differences against what actually happened, one per applicable trade.

    Tagged with the session so the interval below can resample whole days.
    """
    return [
        (item.original.session_date, _delta(item))
        for item in _repriced(trades, rule)
        if item.outcome != "inapplicable"
    ]


def recommend(
    trades: Sequence[TradeRecord],
    *,
    seed: int = DEFAULT_SEED,
    min_train_sessions: int = MIN_TRAIN_SESSIONS,
    step_sessions: int = STEP_SESSIONS,
    draws: int = 1_000,
) -> ExitRecommendation:
    """Walk the selection procedure forward and judge it out of sample."""
    ordered = sorted(
        (t for t in trades if t.session_date is not None),
        key=lambda t: (t.opened_at, str(t.trade_id)),
    )
    sessions = _sessions_of(ordered)

    if len(sessions) < min_train_sessions + 1:
        return ExitRecommendation(
            None, (), None, None, None, 0,
            refusal=(
                f"choosing an exit rule needs at least {min_train_sessions + 1} trading "
                f"sessions and this history has {len(sessions)}; below that the rule "
                "would be chosen and judged on the same trades"
            ),
        )

    by_session: dict[date, list[TradeRecord]] = {}
    for trade in ordered:
        assert trade.session_date is not None
        by_session.setdefault(trade.session_date, []).append(trade)

    folds: list[FoldResult] = []
    all_deltas: list[tuple[date | None, Decimal]] = []
    cursor = min_train_sessions
    fold_number = 0

    while cursor < len(sessions):
        train_sessions = sessions[:cursor]
        test_sessions = sessions[cursor : cursor + step_sessions]
        cursor += step_sessions
        fold_number += 1

        train = [t for s in train_sessions for t in by_session[s]]
        test = [t for s in test_sessions for t in by_session[s]]
        if not train or not test:
            continue

        chosen = select(train)
        if chosen is None:
            continue

        # The only measurement that counts: the rule picked on the training window,
        # applied to sessions it has never seen.
        deltas = _per_trade_deltas(test, chosen)
        if not deltas:
            continue

        folds.append(
            FoldResult(
                fold=fold_number,
                chosen=chosen,
                train_sessions=len(train_sessions),
                test_trades=len(deltas),
                deltas=tuple(deltas),
            )
        )
        all_deltas.extend(deltas)

    if len(all_deltas) < MIN_SCORED:
        return ExitRecommendation(
            None, tuple(folds), None, None, None, len(all_deltas),
            refusal=(
                f"only {len(all_deltas)} trades could be scored out of sample, and at "
                f"least {MIN_SCORED} are needed before an exit rule means anything"
            ),
        )

    improvement = mean([value for _, value in all_deltas])
    interval = _block_interval(all_deltas, draws=draws, seed=seed)
    stability = _stability(folds)
    final = select(ordered)

    refusal = _refusal(improvement, interval, stability, folds)

    logger.info(
        "exits.recommended",
        folds=len(folds),
        scored=len(all_deltas),
        established=refusal is None,
        stability=str(stability) if stability is not None else None,
    )

    return ExitRecommendation(
        rule=final if refusal is None else None,
        folds=tuple(folds),
        improvement=improvement,
        interval=interval,
        stability=stability,
        scored_trades=len(all_deltas),
        refusal=refusal,
    )


def _stability(folds: Sequence[FoldResult]) -> Decimal | None:
    """How often the folds agreed on the same rule.

    A procedure that picks a different rule every window is fitting noise, and its average
    delta coming out positive is luck rather than evidence. A trader could not have
    followed it in any case: there was never one rule to follow.
    """
    if not folds:
        return None
    counts: dict[Decimal, int] = {}
    for fold in folds:
        counts[fold.chosen.target_r] = counts.get(fold.chosen.target_r, 0) + 1
    return Decimal(max(counts.values())) / Decimal(len(folds))


def _block_interval(
    deltas: Sequence[tuple[date | None, Decimal]], *, draws: int, seed: int
) -> Interval | None:
    """Session-block bootstrap, for the reason given in :mod:`app.ml.skill`.

    Trades within a session are not independent, so resampling them individually would
    yield an interval roughly half the width the evidence supports.
    """
    import random

    grouped: dict[date | None, list[Decimal]] = {}
    for session, value in deltas:
        grouped.setdefault(session, []).append(value)
    blocks = list(grouped.values())
    if len(blocks) < 10:
        return None

    rng = random.Random(seed)
    resampled: list[Decimal] = []
    for _ in range(draws):
        drawn: list[Decimal] = []
        for _ in range(len(blocks)):
            drawn.extend(blocks[rng.randrange(len(blocks))])
        resampled_mean = mean(drawn)
        if resampled_mean is not None:
            resampled.append(resampled_mean)

    if len(resampled) < draws // 2:
        return None

    resampled.sort()
    return Interval(
        low=resampled[_index_at(Decimal("0.025"), len(resampled))],
        high=resampled[_index_at(Decimal("0.975"), len(resampled))],
        confidence=Decimal("0.95"),
        method="session_block_bootstrap",
        iterations=len(resampled),
    )


def _refusal(
    improvement: Decimal | None,
    interval: Interval | None,
    stability: Decimal | None,
    folds: Sequence[FoldResult],
) -> str | None:
    """Plain language for why no exit rule is recommended.

    Ordered so the most fundamental problem is heard first, and phrased so each answer
    tells a trader something different about what to do next.
    """
    if improvement is None:
        return "no out-of-sample difference could be measured"

    if improvement <= 0:
        return (
            "picking the best target from your past trades did not beat what you "
            f"already do when applied to later ones ({improvement:.2f} per trade). The "
            "grid finds a winner on any history; this one did not survive contact with "
            "data it had not seen"
        )

    if len(folds) < 2:
        return (
            "there is only one out-of-sample window, so there is no way to tell a rule "
            "that works from one that happened to suit a single stretch of trading"
        )

    if stability is not None and stability < STABILITY_THRESHOLD:
        chosen = ", ".join(sorted({fold.chosen.label for fold in folds}))
        return (
            f"the best target changed between windows ({chosen}), so the procedure is "
            "fitting each stretch rather than finding something stable. There was never "
            "one rule here for you to have followed"
        )

    if interval is None:
        return (
            "there are too few trading sessions to put a confidence interval on the "
            "improvement"
        )

    if interval.low <= 0:
        return (
            f"the improvement averaged {improvement:.2f} per trade out of sample, but "
            f"resampling your sessions puts it anywhere from {interval.low:.2f} to "
            f"{interval.high:.2f} — the range includes zero, so it cannot be "
            "distinguished from luck yet"
        )

    return None
