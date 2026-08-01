"""Generated bars have to contain the trades they are generated for.

A random walk started near the entry price produces a chart that looks entirely
convincing and does not contain the price the trade was filled at. Two things then break,
and only the second is visible:

* `compute_excursions` raises ``DomainError`` — "entry price must lie within the observed
  price range" — so the excursion backfill fails on data that renders fine.
* The replay draws the entry marker outside every candle, which reads as a rendering bug
  rather than as bars that disagree with the trade.

So the generator works from anchors, and this file tests the one property everything
downstream rests on: **the bar covering an anchor contains that anchor's price.** The rest
of the assertions here are the ones that make a chart a chart — highs above lows, no gaps,
no duplicate minutes.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from app.scripts.backfill_demo_bars import Anchor, session_bars

OPEN = datetime(2026, 3, 2, 12, 30, tzinfo=UTC)
CLOSE = datetime(2026, 3, 2, 20, 15, tzinfo=UTC)


def anchors() -> list[Anchor]:
    """Three trades in a session, including one that ran against the entry."""
    return [
        Anchor(datetime(2026, 3, 2, 14, 32, tzinfo=UTC), Decimal("5212.90")),
        Anchor(datetime(2026, 3, 2, 14, 45, tzinfo=UTC), Decimal("5207.90")),
        Anchor(datetime(2026, 3, 2, 15, 2, tzinfo=UTC), Decimal("5219.10")),
        Anchor(datetime(2026, 3, 2, 16, 10, tzinfo=UTC), Decimal("5180.25")),
        Anchor(datetime(2026, 3, 2, 16, 38, tzinfo=UTC), Decimal("5176.00")),
        Anchor(datetime(2026, 3, 2, 18, 5, tzinfo=UTC), Decimal("5244.75")),
    ]


@pytest.fixture(scope="module")
def bars() -> list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]]:
    return session_bars(
        random.Random(20260731), anchors(), opens_at=OPEN, closes_at=CLOSE
    )


def test_every_anchor_is_inside_the_bar_that_covers_it(
    bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]],
) -> None:
    """**The test this file exists for.**

    Without it a trade can be filled at a price that never traded, which the chart draws
    without complaint and the excursion engine refuses outright.
    """
    by_minute = {at: (high, low) for at, _, high, low, _ in bars}

    for anchor in anchors():
        minute = anchor.at.replace(second=0, microsecond=0)
        assert minute in by_minute, f"no bar covers {anchor.at}"
        high, low = by_minute[minute]
        assert low <= anchor.price <= high, (
            f"the bar at {minute} spans {low}-{high}, which does not contain "
            f"{anchor.price} — the trade was filled at a price that never traded"
        )


def test_the_session_is_continuous_and_minutely(
    bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]],
) -> None:
    """A duplicate minute violates the (instrument, timeframe, ts) key; a hole is a gap
    the replay would draw a straight line across."""
    stamps = [at for at, *_ in bars]

    assert len(stamps) == len(set(stamps)), "the same minute was emitted twice"
    assert stamps == sorted(stamps)
    for earlier, later in pairwise(stamps):
        assert later - earlier == timedelta(minutes=1)


def test_the_session_spans_the_whole_window(
    bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]],
) -> None:
    """Context bars before the first trade are what `build_window` reaches back for."""
    assert bars[0][0] == OPEN
    assert bars[-1][0] <= CLOSE
    assert bars[0][0] < anchors()[0].at, "no history before the session's first trade"
    assert bars[-1][0] > anchors()[-1].at, "nothing after the session's last trade"


def test_each_candle_is_well_formed(
    bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]],
) -> None:
    """The schema enforces these with check constraints; failing here is cheaper."""
    for at, open_price, high, low, close_price in bars:
        assert high >= low, f"inverted candle at {at}"
        assert high >= open_price and high >= close_price, f"high is not the max at {at}"
        assert low <= open_price and low <= close_price, f"low is not the min at {at}"


def test_the_path_is_not_a_straight_line(
    bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]],
) -> None:
    """Anchors alone would produce a piecewise-linear chart nobody would believe.

    Guards the opposite failure from the one above: a generator that satisfies every
    anchor by interpolating between them passes all the tests so far and draws a chart
    made of straight segments.
    """
    closes = [float(close) for *_, close in bars]
    straight = 0
    for first, second, third in zip(closes, closes[1:], closes[2:], strict=False):
        if abs((second - first) - (third - second)) < 1e-9:
            straight += 1

    assert straight < len(closes) * 0.2, "the path is mostly linear interpolation"


def test_an_empty_session_produces_nothing() -> None:
    """A day with no trades is not an error, and must not invent a session."""
    assert session_bars(random.Random(1), [], opens_at=OPEN, closes_at=CLOSE) == []


def test_the_same_seed_produces_the_same_session() -> None:
    """The demo is reproducible from one number, so a re-seed is comparable."""
    first = session_bars(random.Random(7), anchors(), opens_at=OPEN, closes_at=CLOSE)
    second = session_bars(random.Random(7), anchors(), opens_at=OPEN, closes_at=CLOSE)

    assert first == second
