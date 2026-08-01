"""Trades read off a price path, and the identities that holds true by construction.

This file replaces the tests for ``seed_demo._excursions``, a function that invented a
trade's maximum adverse and favourable excursion to be *consistent with* its outcome. It
was wrong twice, in opposite directions, and both times the wrongness surfaced as a
finding rather than as a failure:

* **Too generous.** ``mfe_r = abs(r) + uniform(0.05, 0.9)`` gave losers a favourable
  excursion of up to 1.8R, and the what-if simulator reported that a 1R target would have
  earned an extra $248,000 — because under that rule most losers became winners.
* **Too mean.** The correction drew losers from ``uniform(0, 0.45)`` and winners from
  ``r + uniform(0.05, 0.60)``. The smallest winner was around 0.6R, so the two ranges came
  out **disjoint**: across 1,447 trades not one loser in 714 ever reached 1R in your
  favour. MFE alone then classified win from loss perfectly, and the pattern engine
  published those clusters as established findings worth -$762,759 and +$687,586 a year
  (ADR 0020).

Both defects have the same root: a number that describes what the price did, produced
without a price series. So there is a price series now, and the excursions are read off it.
The identities are no longer asserted at the bottom of a generator — they are properties of
a path that a trade was placed on, and :func:`test_the_excursions_bracket_the_result` is
the test this file exists for.

What still needs testing is everything the path does *not* give away for free: that a
minute which could have hit both the stop and the target is resolved against the trader,
that the outcome-steering never fabricates the outcome it was asked for, and that the two
excursion distributions overlap — which is now a measured property of the market rather
than a shape anyone chose, and so could regress without anyone editing a distribution.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from app.scripts.market_path import (
    Candle,
    Placement,
    excursions,
    place_trade,
    session_path,
)

SEED = 20260731
TICK = 0.25
OPENS_AT = datetime(2026, 3, 2, 12, 30, tzinfo=UTC)
MINUTES = 465
RISK = Decimal("5")

#: Enough sessions that the excursion distributions have a shape worth asserting about.
#: The 1R-in-your-favour tail among losers is a few per cent, so a hundred trades would
#: leave the assertion measuring sampling noise.
SESSIONS = 60


class Trade:
    """A placement plus the three numbers the trade record would store."""

    __slots__ = ("long", "mae_r", "mfe_r", "placement", "realized_r")

    def __init__(self, placement: Placement, *, long: bool) -> None:
        self.placement = placement
        self.long = long
        sign = Decimal(1) if long else Decimal(-1)
        moved = (placement.exit_price - placement.entry_price) * sign
        self.realized_r = moved / RISK
        adverse, favourable = excursions(placement, long=long)
        self.mae_r = adverse / RISK
        self.mfe_r = favourable / RISK


def a_path(seed: int = SEED, start: Decimal = Decimal("5180.00")) -> list[Candle]:
    return session_path(
        random.Random(seed),
        opens_at=OPENS_AT,
        minutes=MINUTES,
        start_price=start,
        tick_size=TICK,
    )


@pytest.fixture(scope="module")
def path() -> list[Candle]:
    return a_path()


@pytest.fixture(scope="module")
def trades() -> list[Trade]:
    """Many sessions of placements, drawn the way the seed draws them.

    Deliberately not a single session: a path with an unusually strong drift makes one
    outcome easy and the other nearly unreachable, and a distribution measured on it
    describes that day rather than the generator.
    """
    rng = random.Random(SEED)
    placed = []
    price = Decimal("5180.00")
    for session in range(SESSIONS):
        path = a_path(SEED + session, price)
        price = path[-1].close
        cursor = 120
        for _ in range(rng.choice((3, 5, 6, 8, 9))):
            long = rng.random() < 0.55
            placement = place_trade(
                path,
                from_index=cursor,
                long=long,
                risk_points=RISK,
                target_r=Decimal(str(round(rng.uniform(1.2, 2.6), 2))),
                want_win=rng.random() < 0.5,
                max_hold=rng.randint(6, 40),
                tick_size=TICK,
            )
            if placement is None:
                continue
            cursor = placement.exit_index + rng.randint(3, 20)
            placed.append(Trade(placement, long=long))
    return placed


def test_the_excursions_bracket_the_result(trades: list[Trade]) -> None:
    """**The test this file exists for.**

    ``mae_r <= realized_r <= mfe_r`` are facts about what the words mean: you cannot exit
    better than the best price the trade ever offered, nor worse than the worst price it
    ever reached. Under the old generator they were asserted, and satisfying an assertion
    is not the same as being possible — both defects it shipped satisfied both identities.

    Here they hold because all three numbers are read off one path, which is why this test
    can no longer fail without the path itself being wrong.
    """
    assert trades, "no trades were placed"

    for trade in trades:
        assert trade.mae_r <= trade.realized_r, (
            f"exited at {trade.realized_r:.2f}R, worse than the {trade.mae_r:.2f}R "
            "the trade never reached"
        )
        assert trade.realized_r <= trade.mfe_r, (
            f"exited at {trade.realized_r:.2f}R, better than the {trade.mfe_r:.2f}R "
            "the trade ever offered"
        )


def test_entry_and_exit_are_prices_that_actually_traded(
    path: list[Candle], trades: list[Trade]
) -> None:
    """A fill outside its own candle is what `compute_excursions` refuses outright.

    It raises ``DomainError`` — "entry price must lie within the observed price range" —
    and before that the replay draws the entry marker floating above or below every
    candle, which reads as a rendering bug. The seed used to produce exactly this: an entry
    of 5175.96 against a bar spanning 5163.78-5165.13.
    """
    rng = random.Random(SEED)
    cursor = 120
    checked = 0
    for _ in range(12):
        long = rng.random() < 0.5
        placement = place_trade(
            path, from_index=cursor, long=long, risk_points=RISK,
            target_r=Decimal("2"), want_win=rng.random() < 0.5, max_hold=30,
            tick_size=TICK,
        )
        if placement is None:
            continue
        cursor = placement.exit_index + 5
        checked += 1

        entry_bar = path[placement.entry_index]
        exit_bar = path[placement.exit_index]
        assert entry_bar.low <= placement.entry_price <= entry_bar.high, (
            f"filled at {placement.entry_price} in a minute spanning "
            f"{entry_bar.low}-{entry_bar.high}"
        )
        assert exit_bar.low <= placement.exit_price <= exit_bar.high, (
            f"exited at {placement.exit_price} in a minute spanning "
            f"{exit_bar.low}-{exit_bar.high}"
        )

    assert checked >= 5, "too few placements to have tested anything"


def test_the_extremes_are_the_extremes_of_the_holding_period(
    path: list[Candle], trades: list[Trade]
) -> None:
    """MAE and MFE are the window's own high and low, not a summary of it.

    A generator that measured the extremes over the wrong window — the whole session, or
    the entry bar alone — would still satisfy the bracketing identities and still be
    wrong, so the window is pinned separately.
    """
    placement = place_trade(
        path, from_index=140, long=True, risk_points=RISK, target_r=Decimal("2"),
        want_win=True, max_hold=40, tick_size=TICK,
    )
    if placement is None:
        pytest.skip("this path offered no winning long from that minute")

    window = path[placement.entry_index : placement.exit_index + 1]
    assert placement.highest == max(candle.high for candle in window)
    assert placement.lowest == min(candle.low for candle in window)


def test_a_minute_that_spans_both_resolves_against_the_trader() -> None:
    """A candle whose range covers the stop *and* the target is ambiguous, and is scored
    as the loss.

    The minute records both extremes without recording their order, so nothing in the data
    says which was touched first. ADR 0008 makes the same conservative choice in the
    what-if simulator, and for the same reason: assuming the good fill flatters every
    scenario built on this history, and the flattering answer is the one nobody checks.
    """
    entry = Decimal("5000.00")
    spans_both = Candle(
        at=OPENS_AT + timedelta(minutes=1),
        open=entry,
        high=entry + Decimal("20"),  # past a 2R target at +10
        low=entry - Decimal("10"),  # and past the 5-point stop
        close=entry,
    )
    path = [
        Candle(at=OPENS_AT, open=entry, high=entry, low=entry, close=entry),
        spans_both,
        *(
            Candle(
                at=OPENS_AT + timedelta(minutes=i), open=entry, high=entry,
                low=entry, close=entry,
            )
            for i in range(2, 8)
        ),
    ]

    placement = place_trade(
        path, from_index=0, long=True, risk_points=RISK, target_r=Decimal("2"),
        want_win=False, max_hold=5, tick_size=TICK,
    )

    assert placement is not None
    assert placement.won is False
    assert placement.exit_price == entry - RISK, "the target was filled on an ambiguous bar"


def test_the_wanted_outcome_is_found_or_nothing_is(path: list[Candle]) -> None:
    """Steering picks *which* real trade is taken; it never invents one.

    This is what keeps the planted leak honest. The seed asks for a loss more often late
    in a session, and the only two legal answers are a placement that genuinely lost on
    the path and ``None``. A generator that forced the third answer — a trade whose prices
    are whatever the outcome required — is the defect this module was written to remove.
    """
    for from_index in range(120, 400, 7):
        for want_win in (True, False):
            placement = place_trade(
                path, from_index=from_index, long=True, risk_points=RISK,
                target_r=Decimal("1.8"), want_win=want_win, max_hold=25, tick_size=TICK,
            )
            if placement is None:
                continue
            assert placement.won is want_win
            assert placement.entry_price == path[placement.entry_index].open


def test_a_trade_at_the_end_of_the_session_is_skipped_not_squeezed(
    path: list[Candle],
) -> None:
    """With no room left to hold a position, ``None`` is the only honest answer."""
    assert (
        place_trade(
            path, from_index=len(path) - 2, long=True, risk_points=RISK,
            target_r=Decimal("2"), want_win=True, max_hold=40, tick_size=TICK,
        )
        is None
    )


def test_the_two_excursion_distributions_overlap(trades: list[Trade]) -> None:
    """Disjoint ranges make MFE a perfect win/loss classifier — the ADR 0020 defect.

    Now a measured property rather than a designed one, which is precisely why it is worth
    a test: nobody has to edit a distribution for it to regress. Widening the stop,
    shortening the hold, or raising the per-minute volatility all move it, and none of
    those changes look like they are about leakage.
    """
    winners = [t.mfe_r for t in trades if t.realized_r > 0]
    losers = [t.mfe_r for t in trades if t.realized_r <= 0]
    assert winners and losers

    floor = min(winners)
    overlapping = sum(1 for mfe in losers if mfe >= floor) / len(losers)

    assert overlapping > 0.05, (
        f"only {overlapping:.1%} of losers reach the winners' MFE range (floor {floor:.2f})"
        " — MFE alone separates win from loss, so any cluster built on it is the outcome"
    )


def test_a_losing_trade_sometimes_ran_a_full_unit_of_risk_in_your_favour(
    trades: list[Trade],
) -> None:
    """Present but uncommon. A history where it never happens is not a history; one where
    it is routine describes a trader who was up and chose to lose."""
    losers = [t.mfe_r for t in trades if t.realized_r <= 0]

    reached = sum(1 for mfe in losers if mfe >= 1) / len(losers)

    assert reached > 0.005, "no losing trade ever ran a full unit of risk in your favour"
    assert reached < 0.25, f"{reached:.1%} of losers reached 1R — that is not a loss"


def test_both_outcomes_are_reachable(trades: list[Trade]) -> None:
    """Guards the trivial pass. Every assertion above holds vacuously on a sample that is
    all winners or all losers, and a path generator with runaway drift produces one."""
    won = sum(1 for t in trades if t.realized_r > 0)

    assert 0.2 < won / len(trades) < 0.8, f"{won}/{len(trades)} trades won"


def test_the_session_is_one_continuous_series(path: list[Candle]) -> None:
    """Each minute opens where the last one closed.

    The old seed drew entries independently, so prices inside one session spanned 33 points
    on average and as much as 79. Bars generated to join them had to cover that ground, and
    the chart showed it. Continuity is the property that made the jumps impossible.
    """
    for earlier, later in pairwise(path):
        assert later.at - earlier.at == timedelta(minutes=1)
        assert later.open == earlier.close, (
            f"the price gapped from {earlier.close} to {later.open} at {later.at}"
        )


def test_every_candle_is_well_formed(path: list[Candle]) -> None:
    """The schema enforces these with check constraints; failing here is cheaper."""
    for candle in path:
        assert candle.high >= candle.low
        assert candle.high >= candle.open and candle.high >= candle.close
        assert candle.low <= candle.open and candle.low <= candle.close


def test_every_price_is_on_the_tick_grid(path: list[Candle]) -> None:
    """A price between two ticks never traded, and rounding it at display time hides that
    the trade was filled somewhere the instrument cannot quote."""
    tick = Decimal(str(TICK))
    for candle in path:
        for price in (candle.open, candle.high, candle.low, candle.close):
            assert price % tick == 0, f"{price} is not a multiple of {tick}"


def test_the_path_moves_enough_to_trade(path: list[Candle]) -> None:
    """A session whose range is under a stop distance cannot produce a trade at all, and
    every test above would pass by skipping everything."""
    span = max(c.high for c in path) - min(c.low for c in path)

    assert span > RISK * 2, f"the whole session moved {span} points"


def test_the_same_seed_produces_the_same_session() -> None:
    """The demo is reproducible from one number, so a re-seed is comparable to the last."""
    assert a_path(7) == a_path(7)
    assert a_path(7) != a_path(8)
