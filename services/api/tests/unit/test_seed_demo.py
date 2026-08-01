"""The demo history has to be possible, and it has to reach the thresholds it exercises.

Every screen in this application is read against the seeded history, so a defect here does
not look like a defect — it looks like a finding, or like a working system with nothing to
report. Both have happened:

* The first version put a fixed five trades in every session against an
  ``OVERTRADING_THRESHOLD`` of six, so the one detector capable of finding the planted leak
  never had a sample to test it on. The scan came back clean on data built to be dirty.
* Excursions were invented rather than measured, twice wrongly, and the engines that read
  them published the outcome back as findings worth hundreds of thousands of dollars a
  year (ADR 0020).

The second class is gone by construction — prices now come from
:mod:`app.scripts.market_path`, and :mod:`tests.unit.test_market_path` covers it. What is
left here are the seed's own dials: numbers that are correct only in relation to each
other, where changing one in isolation produces a history that still seeds, still renders,
and quietly stops demonstrating something.
"""

from __future__ import annotations

import random
import statistics
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.analytics.behaviours import OVERTRADING_THRESHOLD
from app.domain.common.enums import SessionSegment
from app.scripts.market_path import session_path
from app.scripts.seed_demo import (
    FIRST_SESSION,
    FIRST_TRADE_INDEX,
    INSTRUMENTS,
    LEAK_AFTER,
    OVERNIGHT_SIGMA,
    SEED,
    SESSION_LENGTHS,
    SESSION_MINUTES,
    SESSION_OPEN,
    SESSIONS,
    START_PRICE,
    TICK_SIZE,
    WIN_RATE_EARLY,
    WIN_RATE_LATE,
    _segment,
    plan_session,
)


def test_sessions_run_past_the_overtrading_threshold() -> None:
    """**The original defect.** Five trades a session against a threshold of six meant the
    detector that finds the planted leak never saw a session long enough to test."""
    assert max(SESSION_LENGTHS) > OVERTRADING_THRESHOLD

    over = sum(1 for n in SESSION_LENGTHS if n > OVERTRADING_THRESHOLD) / len(
        SESSION_LENGTHS
    )
    assert over > 0.15, (
        f"only {over:.0%} of sessions exceed the threshold — too few for the detector to "
        "establish anything, which reads as a clean scan rather than as thin data"
    )


def test_the_leak_is_inside_the_sessions_that_are_long_enough_to_have_one() -> None:
    """A split at a trade index no session reaches plants nothing at all."""
    assert max(SESSION_LENGTHS) > LEAK_AFTER

    exposed = sum(max(n - LEAK_AFTER, 0) for n in SESSION_LENGTHS)
    assert exposed > 0, "no session is long enough to contain a late trade"


def test_the_leak_is_large_enough_to_be_found_and_small_enough_to_be_earned() -> None:
    """The scan has to establish it through a permutation test, not stumble over it.

    A gap of a couple of points would need more history than the seed builds; a gap of
    fifty would be visible in any grouping and prove nothing about the machinery.
    """
    gap = WIN_RATE_EARLY - WIN_RATE_LATE

    assert 0.10 < gap < 0.40, f"a {gap:.0%} win-rate gap is not a leak worth planting"
    assert 0 < WIN_RATE_LATE < WIN_RATE_EARLY < 1


def test_one_r_means_the_same_money_on_every_instrument() -> None:
    """**The test this file exists for now.**

    The stop is stated in points, and points are worth different amounts on different
    contracts: ES is $50 a point, NQ $20. Copying one stop distance across both leaves 1R
    meaning $250 on one instrument and $100 on the other — and every R-denominated
    statistic in the application, from expectancy to the what-if simulator's deltas,
    averages across both without knowing they are different units.

    Nothing downstream can detect this. The trades are individually consistent; only the
    unit is inconsistent, and a unit is not a column.
    """
    risks = {
        symbol: (tick_value / TICK_SIZE) * stop
        for symbol, _, tick_value, stop in INSTRUMENTS
    }

    assert len(risks) > 1, "with one instrument this test proves nothing"
    assert max(risks.values()) / min(risks.values()) < Decimal("1.1"), (
        f"a one-lot risks {dict(risks)} — 1R is not the same money on each, so every "
        "R-denominated statistic averages two different units"
    )


def test_every_stop_lands_on_the_tick_grid() -> None:
    """A stop between two ticks cannot be resting in the market."""
    for symbol, _, _, stop in INSTRUMENTS:
        assert stop % TICK_SIZE == 0, f"{symbol}'s {stop}-point stop is off the grid"
        assert stop > 0


def test_the_replay_has_its_context_bars() -> None:
    """`build_window` reaches back 120 bars before the entry.

    A session that opens at its first trade renders a chart that begins at the entry
    marker, which looks like a truncated chart rather than like a session boundary.
    """
    assert FIRST_TRADE_INDEX >= 120
    assert FIRST_TRADE_INDEX < SESSION_MINUTES


class Placed:
    """One trade of a replayed history: where in its session it sat, and what it made."""

    __slots__ = ("day", "entry_minute", "late", "net", "seq")

    def __init__(
        self, day: date, seq: int, entry_minute: int, net: float, *, late: bool
    ) -> None:
        self.day = day
        self.seq = seq
        self.entry_minute = entry_minute
        self.net = net
        self.late = late


class History:
    __slots__ = ("asked", "late_asked", "trades")

    def __init__(self) -> None:
        self.trades: list[Placed] = []
        self.asked = 0
        self.late_asked = 0


@pytest.fixture(scope="module")
def history() -> History:
    """The shipped history, replayed in memory by calling :func:`plan_session` itself.

    Calling the seed's own function rather than re-implementing its loop is the point. The
    properties below are statistical and sequential, so a test that reproduced the loop
    would be measuring its copy — and the copy would keep passing while the seed drifted
    away from it. That is not a theoretical worry here: the defect
    :func:`test_the_session_is_one_timeline` was written for is *exactly* a change to how
    the loop advances its cursor, which a replica would have absorbed silently.

    What is reproduced is only the scaffolding `main` puts around it — the session dates,
    the paths, and the per-bar volume draws, which have to be spent for the random stream
    to match the history that actually ships. Full length, not a sample: the leak's
    detectability depends on the sample size, so a shortened replay answers a different
    question. About two and a half seconds.
    """
    rng = random.Random(SEED)
    price = {symbol: START_PRICE for symbol, *_ in INSTRUMENTS}
    values = {symbol: tick_value for symbol, _, tick_value, _ in INSTRUMENTS}
    result = History()
    day = FIRST_SESSION
    built = 0

    while built < SESSIONS:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        built += 1

        paths = {}
        for symbol, *_ in INSTRUMENTS:
            paths[symbol] = session_path(
                rng,
                opens_at=datetime.combine(day, SESSION_OPEN, tzinfo=UTC),
                minutes=SESSION_MINUTES,
                start_price=price[symbol]
                + Decimal(str(round(rng.gauss(0, OVERNIGHT_SIGMA), 2))),
                tick_size=float(TICK_SIZE),
            )
            price[symbol] = paths[symbol][-1].close

        length = rng.choice(SESSION_LENGTHS)
        planned = plan_session(rng, paths, length=length)
        result.asked += length
        result.late_asked += max(length - LEAK_AFTER, 0)

        for trade in planned:
            moved = (
                trade.placement.exit_price - trade.placement.entry_price
            ) * (Decimal(1) if trade.long else Decimal(-1))
            net = moved * (values[trade.symbol] / TICK_SIZE) * trade.quantity
            result.trades.append(
                Placed(
                    day,
                    trade.index,
                    trade.placement.entry_index,
                    float(net),
                    late=trade.index >= LEAK_AFTER,
                )
            )

        # The seed draws a volume for every bar it writes, so the stream only matches the
        # shipped history if this replay spends those draws too.
        for symbol, *_ in INSTRUMENTS:
            for _ in paths[symbol]:
                rng.randint(150, 4200)
        day += timedelta(days=1)

    return result


def test_the_session_is_long_enough_for_the_trades_it_asks_for(
    history: History,
) -> None:
    """**The failure this guards is silent and it lands on the leak.**

    Trades in a session are sequential — each searches forward from where the last one
    exited — so a session has a finite budget of them, and `place_trade` returns ``None``
    rather than forcing one when the budget runs out. Nothing reports that. The seed just
    writes fewer trades than it asked for, and the ones it drops are always the *last* of
    a session: exactly the trades past :data:`LEAK_AFTER` the planted leak is made of.

    Measured rather than derived from the constants. An earlier version of this test
    asserted ``SESSION_MINUTES - FIRST_TRADE_INDEX > max(SESSION_LENGTHS) * 40`` — a
    per-trade budget I had guessed, which ignored that the trades are split across two
    instruments with a cursor each, and which failed at 345 > 360 on a configuration that
    was fine.
    """
    dropped = history.asked - len(history.trades)
    late_placed = sum(1 for trade in history.trades if trade.late)
    late_dropped = history.late_asked - late_placed

    assert dropped / history.asked < 0.12, (
        f"{dropped}/{history.asked} trades could not be placed — the session has run out "
        "of room, and the trades it drops are the late ones the leak is made of"
    )
    assert late_dropped / history.late_asked < 0.30, (
        f"{late_dropped}/{history.late_asked} *late* trades were dropped — the leak is "
        "being thinned by the session running short, not by its design"
    )


def test_the_session_is_one_timeline(history: History) -> None:
    """**The test this file exists for.**

    The planted leak is a split on *position in the session*, and two different things can
    mean "position": the order the seed placed the trades in, and the order they opened in.
    The seed decides the first. The detector computes the second — `behaviours.py` sorts a
    session's trades by ``opened_at`` and enumerates. Nothing checks that they agree.

    They did not. The seed kept a separate minute cursor per instrument, so ES and NQ ran
    independent clocks and a trade placed eighth in the loop could open before one placed
    third. The leak was planted at one index and read at another, and the dilution was
    enough to lose it: the effect was in the database at -$43.33 a trade against +$21.33,
    worth an estimated -$6,207 a year, and the scan returned nothing.

    That failure is invisible from every direction. The history seeds, the charts render,
    the excursions reconcile, every test of the leak's *design* passes, and the one screen
    the demo exists to populate reports no findings. There is no error to read — only a
    scan that comes back clean on data built to be dirty, which is the same false negative
    ADR 0016 was written about.

    The fix was one session clock instead of two, which is also just what a trader does:
    one position at a time. This asserts the consequence directly.
    """
    by_day: dict[date, list[Placed]] = {}
    for trade in history.trades:
        by_day.setdefault(trade.day, []).append(trade)

    assert len(by_day) > 200, "the replay did not build the history it was asked for"

    for day, trades in by_day.items():
        minutes = [trade.entry_minute for trade in trades]
        assert minutes == sorted(minutes), (
            f"on {day} the trades were placed in an order the clock disagrees with "
            f"({minutes}) — the detector sorts a session by opened_at, so the leak is "
            "planted at one index and read at another"
        )
        assert len(set(minutes)) == len(minutes), (
            f"on {day} two trades opened in the same minute — the trader is in two "
            "positions at once, and the session stops being a sequence"
        )


def test_the_planted_leak_is_strong_enough_to_be_found(history: History) -> None:
    """A leak that is present but not *findable* is a demo that quietly proves nothing.

    Necessary, not sufficient. The scan runs seven tests and corrects them as one family,
    so a finding needs roughly ``p < 0.05/7 = 0.0071`` to survive, which is about
    ``t = 2.45``; below that the leak certainly will not be returned. Above it, this
    statistic does not promise the scan will return it — a Welch t on per-trade P&L is not
    the permutation test the detector runs, and when the sequencing defect above was live
    this measured t > 4 on a history whose scan came back empty. That is why the sequencing
    has its own test rather than being inferred from this one.

    Split on ``seq`` rather than on ``late``: ``seq`` is the position the detector will
    work out, and a dropped trade shifts every later one down a place.
    """
    early = [trade.net for trade in history.trades if trade.seq < LEAK_AFTER]
    late = [trade.net for trade in history.trades if trade.seq >= LEAK_AFTER]

    assert len(late) > 90, f"only {len(late)} late trades — too thin to establish anything"

    difference = statistics.mean(early) - statistics.mean(late)
    standard_error = (
        statistics.variance(early) / len(early) + statistics.variance(late) / len(late)
    ) ** 0.5

    assert difference / standard_error > 2.45, (
        f"the late-session penalty is worth ${difference:.2f} a trade but measures only "
        f"t={difference / standard_error:.2f} against the spread around it — the scan "
        "corrects seven tests as one family and will not return it, so the demo's one "
        "planted finding will not appear on the screen built to show it"
    )


def test_the_session_segments_cover_the_day() -> None:
    """Trades are labelled by their position in the session, and the analytics group on it,
    so a segment that never occurs is a grouping that never has a sample."""
    segments = {_segment(index) for index in range(max(SESSION_LENGTHS))}

    assert segments == {
        SessionSegment.RTH_OPEN,
        SessionSegment.RTH_MORNING,
        SessionSegment.RTH_AFTERNOON,
    }
