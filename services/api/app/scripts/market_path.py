"""A continuous intraday price path, and trades placed on top of it.

This inverts how the demo used to be built, and the inversion is the point.

The old seed drew each trade's entry from ``5180 + uniform(-40, 40)``, independently of
every other trade. Nothing tied one trade to the next, so entry prices inside a single
session spanned 33 points on average and as much as 79 — and once bars existed to join
them, the chart had to cover that ground, which it did in visible jumps. The bars were
being asked to explain prices that no price series had produced.

Worse, it made the excursions a modelling problem. With no path, ``mae_r`` and ``mfe_r``
had to be *invented* to be consistent with the outcome, and that function was wrong twice:
first generous enough that a 1R target looked worth $248,000, then mean enough that MFE
became a perfect win/loss label and the pattern engine published the outcome back as a
$762,759 "finding" (ADR 0020).

Here the path comes first and everything else is read off it:

* **Entry** is the price on the path at the entry minute.
* **Exit** is the stop or the target, whichever the path reaches first — or the price at
  the time stop if it reaches neither.
* **MAE and MFE are measured** from the highs and lows the path actually printed between
  those two moments. They are no longer a distribution anyone has to get right, and the
  identities ``mae_r <= r <= mfe_r`` hold because the path enforces them, not because a
  docstring asks for them.

**The planted leak survives, and still has to be earned.** The scan must find that trades
after the sixth of a session perform worse, so the outcome cannot be left entirely to the
path. `place_trade` searches forward for an entry minute whose stop-or-target race ends
the way the session's leak design wants, within a bounded window. The trade is real — a
real entry, a real exit, real excursions — but *which* real trade is chosen is steered.
That is a fair description of a trader with a bias: the market is not rigged, the entries
are.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

#: Per-minute volatility in points, and the drift applied across a session. Chosen so a
#: session's range lands in the tens of points, which is where index futures live.
MINUTE_SIGMA = 0.9
SESSION_DRIFT = 6.0

#: How far a minute's high and low sit beyond its open and close.
WICK_SIGMA = 0.55

#: Minutes to search forward for an entry that produces the wanted outcome. Wide enough
#: that both outcomes are usually reachable, narrow enough that the trade stays near the
#: time the session's schedule asked for.
ENTRY_SEARCH = 45


@dataclass(frozen=True, slots=True)
class Candle:
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class Placement:
    """A trade read off the path, with its excursions measured rather than assigned."""

    entry_index: int
    exit_index: int
    entry_price: Decimal
    exit_price: Decimal
    stop_price: Decimal
    #: Highest and lowest the path printed while the position was open.
    highest: Decimal
    lowest: Decimal
    #: True when the target was reached before the stop.
    won: bool


def _tick(value: float, tick_size: float) -> Decimal:
    """Round to the instrument's tick. A price off the grid never traded."""
    ticks = round(value / tick_size)
    return Decimal(str(round(ticks * tick_size, 4)))


def session_path(
    rng: random.Random,
    *,
    opens_at: datetime,
    minutes: int,
    start_price: Decimal,
    tick_size: float,
) -> list[Candle]:
    """One session of minute candles from a random walk with drift.

    A walk rather than a bridge between known points: nothing here has to arrive
    anywhere, because the trades will be placed on whatever it does.
    """
    drift = rng.gauss(0, SESSION_DRIFT) / max(minutes, 1)
    price = float(start_price)

    candles = []
    for index in range(minutes):
        open_price = price
        close_price = price + drift + rng.gauss(0, MINUTE_SIGMA)
        high = max(open_price, close_price) + abs(rng.gauss(0, WICK_SIGMA))
        low = min(open_price, close_price) - abs(rng.gauss(0, WICK_SIGMA))
        candles.append(
            Candle(
                at=opens_at + timedelta(minutes=index),
                open=_tick(open_price, tick_size),
                high=_tick(high, tick_size),
                low=_tick(low, tick_size),
                close=_tick(close_price, tick_size),
            )
        )
        price = close_price
    return candles


def _race(
    path: list[Candle],
    entry_index: int,
    *,
    long: bool,
    stop: Decimal,
    target: Decimal,
    max_hold: int,
) -> tuple[int, Decimal, bool]:
    """Walk forward until the stop or the target is touched. Returns (index, price, won).

    The stop is checked before the target within a minute. A candle whose range spans
    both is ambiguous — the minute records the extremes but not their order — and
    resolving it in favour of the loss is the same conservative choice the what-if
    simulator makes (ADR 0008). Assuming the good fill would flatter every scenario built
    on this data.
    """
    last = min(entry_index + max_hold, len(path) - 1)
    for index in range(entry_index + 1, last + 1):
        candle = path[index]
        if long:
            if candle.low <= stop:
                return index, stop, False
            if candle.high >= target:
                return index, target, True
        else:
            if candle.high >= stop:
                return index, stop, False
            if candle.low <= target:
                return index, target, True
    # Neither was reached inside the holding limit: the trader closed it out at the
    # market, and whether that counts as a win is decided by the close against the entry.
    close = path[last].close
    entry = path[entry_index].open
    return last, close, (close > entry) if long else (close < entry)


def place_trade(
    path: list[Candle],
    *,
    from_index: int,
    long: bool,
    risk_points: Decimal,
    target_r: Decimal,
    want_win: bool,
    max_hold: int,
    tick_size: float,
) -> Placement | None:
    """Find an entry near ``from_index`` whose stop-or-target race ends as wanted.

    Returns ``None`` when no entry in the search window produces the wanted outcome,
    which the caller should treat as "no trade here" rather than forcing one — a forced
    trade is one whose prices do not come from the path, which is the whole thing this
    module exists to prevent.
    """
    limit = min(from_index + ENTRY_SEARCH, len(path) - max_hold - 1)
    for entry_index in range(from_index, max(limit, from_index + 1)):
        if entry_index + 2 >= len(path):
            break
        entry = path[entry_index].open
        sign = Decimal(1) if long else Decimal(-1)
        stop = _tick(float(entry - risk_points * sign), tick_size)
        target = _tick(float(entry + risk_points * target_r * sign), tick_size)

        exit_index, exit_price, won = _race(
            path, entry_index, long=long, stop=stop, target=target, max_hold=max_hold
        )
        if won is not want_win:
            continue

        window = path[entry_index : exit_index + 1]
        return Placement(
            entry_index=entry_index,
            exit_index=exit_index,
            entry_price=entry,
            exit_price=exit_price,
            stop_price=stop,
            highest=max(candle.high for candle in window),
            lowest=min(candle.low for candle in window),
            won=won,
        )
    return None


def excursions(placement: Placement, *, long: bool) -> tuple[Decimal, Decimal]:
    """Adverse and favourable excursion in points, measured from the path.

    Signed the way the trade record stores them: adverse negative, favourable positive.
    Nothing is drawn from a distribution here — these are the extremes the price series
    printed while the position was open, which is what makes
    ``mae_r <= realized_r <= mfe_r`` true by construction rather than by assertion.
    """
    entry = placement.entry_price
    if long:
        return placement.lowest - entry, placement.highest - entry
    return entry - placement.highest, entry - placement.lowest
