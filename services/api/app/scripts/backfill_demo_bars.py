"""Generate minute bars for a seeded demo history, consistent with the trades in it.

The seed writes bars for exactly one trade, so 1,399 of 1,403 trades have a replay screen
that is honestly empty and no screenshots at all. This fills the rest in.

**The bars have to agree with the trades, and that is the whole difficulty.** A random walk
near the entry price looks convincing and is wrong: nothing makes it pass through the price
the trade was actually filled at. The consequence is not cosmetic. `compute_excursions`
raises ``DomainError`` when the entry price falls outside the observed range, so bars that
do not contain their own trade turn the excursion backfill into an error — and before that,
the replay draws an entry marker floating above or below every candle, which looks like a
rendering bug rather than a data one.

So the path is built from anchors rather than from noise:

* the entry price at ``opened_at`` and the exit price at ``closed_at``, exactly;
* the adverse and favourable extremes implied by the trade's recorded ``mae_r``/``mfe_r``,
  placed inside the holding period;
* a Brownian bridge between consecutive anchors, whose deviation is scaled to vanish at
  both ends, so the path wanders plausibly and still lands on each anchor to the tick.

**One session at a time, not one trade at a time.** Bars are keyed by
``(instrument, timeframe, ts)``. Generating a window per trade collides on that key the
moment two trades in a session overlap — and with up to nine trades a session, they do.
A session-wide path also means consecutive trades share the price history between them,
which is what makes the replay of the third trade of the day show where the second one
ended.

**Ordering within a trade is a choice, and it is recorded as one.** ADR 0008 notes that
excursions capture both extremes without capturing which came first, so nothing in the
data says whether a winner dipped before it ran. This picks the reading that matches how
the seed narrates a trade — a winner goes against you first, a loser ticks your way first
— and nothing downstream may depend on it.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from uuid import UUID

from sqlalchemy import insert, select

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.domain.common.enums import Direction
from app.infrastructure.db.bulk import batched
from app.infrastructure.db.models.marketdata import MarketBar
from app.infrastructure.db.models.trading import Trade
from app.infrastructure.db.session import session_scope

logger = get_logger(__name__)

#: Same seed as the trade history, so the whole demo is reproducible from one number.
SEED = 20260731

#: The session the demo trades live in, in UTC. Trades run 14:32 to 19:55, and the margin
#: on either side is what gives the replay window its context bars — `build_window` asks
#: for 120 bars before the entry, and the first trade of a session opens at 14:32.
SESSION_OPEN = time(12, 30)
SESSION_CLOSE = time(20, 15)

#: Per-minute noise, in points, for the bridge between anchors and for the wicks. Small
#: relative to a trade's risk so the anchors remain the shape of the chart rather than
#: being buried in noise.
DRIFT_POINTS = 0.55
WICK_POINTS = 0.45


class Anchor:
    """A price the generated path must pass through at a given minute."""

    __slots__ = ("at", "price")

    def __init__(self, at: datetime, price: Decimal) -> None:
        self.at = at
        self.price = price


def _extremes(trade: Trade) -> tuple[Decimal | None, Decimal | None]:
    """Adverse and favourable prices implied by the recorded excursions in R.

    Returns ``(None, None)`` when the trade has no stop, since without one there is no
    risk unit and ``mae_r``/``mfe_r`` mean nothing. Those trades still get entry and exit
    anchors — the path simply has no required extremes between them.
    """
    entry, stop = trade.avg_entry_price, trade.initial_stop_price
    if entry is None or stop is None or trade.mae_r is None or trade.mfe_r is None:
        return None, None

    risk = abs(entry - stop)
    if risk <= 0:
        return None, None

    # Stored as a signed pair — adverse negative, favourable positive — but only the
    # magnitudes matter here; the direction decides which side of entry each sits on.
    adverse = abs(trade.mae_r) * risk
    favourable = abs(trade.mfe_r) * risk

    if trade.direction is Direction.LONG:
        return entry - adverse, entry + favourable
    return entry + adverse, entry - favourable


def anchors_for(trade: Trade) -> list[Anchor]:
    """Entry, exit, and the two extremes, in the order the path should visit them."""
    entry, exit_price = trade.avg_entry_price, trade.avg_exit_price
    if entry is None or exit_price is None or trade.closed_at is None:
        return []

    points = [Anchor(trade.opened_at, entry)]

    adverse, favourable = _extremes(trade)
    held = (trade.closed_at - trade.opened_at).total_seconds()
    if adverse is not None and favourable is not None and held >= 120:
        # A third and two-thirds of the way through, so both extremes sit strictly inside
        # the holding period and never coincide with the entry or exit minute.
        first = trade.opened_at + timedelta(seconds=held / 3)
        second = trade.opened_at + timedelta(seconds=held * 2 / 3)
        won = trade.net_pnl > 0
        points.append(Anchor(first, adverse if won else favourable))
        points.append(Anchor(second, favourable if won else adverse))

    points.append(Anchor(trade.closed_at, exit_price))
    return points


def _bridge(
    rng: random.Random, start: Anchor, end: Anchor, step: timedelta
) -> Iterator[tuple[datetime, Decimal]]:
    """Prices from ``start`` up to but excluding ``end``, landing exactly on ``end``.

    A straight line between the anchors with a deviation whose scale is
    ``sqrt(f * (1 - f))`` — zero at both ends, widest in the middle. That is what lets the
    path wander without drifting off the anchor it has to hit.
    """
    span = (end.at - start.at).total_seconds()
    if span <= 0:
        return

    at = start.at
    while at < end.at:
        fraction = (at - start.at).total_seconds() / span
        base = float(start.price) + (float(end.price) - float(start.price)) * fraction
        spread = (fraction * (1 - fraction)) ** 0.5
        drift = rng.gauss(0, DRIFT_POINTS) * spread * (span / 60) ** 0.5
        yield at, Decimal(str(round(base + drift, 2)))
        at += step


def session_bars(
    rng: random.Random,
    anchors: list[Anchor],
    *,
    opens_at: datetime,
    closes_at: datetime,
    step: timedelta = timedelta(minutes=1),
) -> list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]]:
    """One session of OHLC, passing through every anchor.

    Pure: no database, no clock. The invariant worth testing is that the bar covering an
    anchor contains that anchor's price between its low and its high — everything the
    replay draws depends on it, and nothing about a chart that violates it looks wrong.
    """
    if not anchors:
        return []

    ordered = sorted(anchors, key=lambda a: a.at)
    # Open and close the session on the first and last known prices, so the path exists
    # before the first trade and after the last rather than starting at the entry.
    ends = [
        Anchor(opens_at, ordered[0].price + Decimal(str(round(rng.gauss(0, 3), 2)))),
        *ordered,
        Anchor(closes_at, ordered[-1].price + Decimal(str(round(rng.gauss(0, 3), 2)))),
    ]

    path: list[tuple[datetime, Decimal]] = []
    for start, end in pairwise(ends):
        path.extend(_bridge(rng, start, end, step))
    path.append((ends[-1].at, ends[-1].price))

    by_minute: dict[datetime, list[Decimal]] = {}
    for anchor in ordered:
        by_minute.setdefault(anchor.at.replace(second=0, microsecond=0), []).append(
            anchor.price
        )

    bars = []
    for (at, open_price), (_, close_price) in pairwise(path):
        high = max(open_price, close_price) + Decimal(
            str(round(abs(rng.gauss(0, WICK_POINTS)), 2))
        )
        low = min(open_price, close_price) - Decimal(
            str(round(abs(rng.gauss(0, WICK_POINTS)), 2))
        )
        # The bar covering an anchor must contain it. Widening the wick is the honest fix:
        # the alternative is a chart on which the trade was filled at a price that never
        # traded.
        for price in by_minute.get(at, ()):
            high = max(high, price)
            low = min(low, price)
        bars.append((at, open_price, high, low, close_price))
    return bars


def _session_bounds(session: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(session, SESSION_OPEN, tzinfo=UTC),
        datetime.combine(session, SESSION_CLOSE, tzinfo=UTC),
    )


async def write_bars(session: object) -> tuple[int, int]:
    """Generate and insert bars for every session in the history. Returns (bars, sessions).

    Takes an open session so the seed can call it inside its own transaction and a
    standalone run can open one. Skips minutes that already have a bar, which is what
    makes a re-run safe rather than a duplicate-key error.
    """
    rng = random.Random(SEED)
    rows = (
        await session.execute(  # type: ignore[attr-defined]
            select(Trade)
            .where(Trade.closed_at.is_not(None))
            .order_by(Trade.instrument_id, Trade.opened_at)
        )
    ).scalars().all()

    grouped: dict[tuple[UUID, date], list[Trade]] = {}
    for trade in rows:
        day = trade.session_date or trade.opened_at.date()
        grouped.setdefault((trade.instrument_id, day), []).append(trade)

    existing = {
        (instrument_id, ts)
        for instrument_id, ts in (
            await session.execute(  # type: ignore[attr-defined]
                select(MarketBar.instrument_id, MarketBar.ts)
            )
        ).all()
    }

    payload: list[dict[str, object]] = []
    for (instrument_id, day), trades in sorted(grouped.items(), key=lambda kv: kv[0][1]):
        anchors = [a for trade in trades for a in anchors_for(trade)]
        if not anchors:
            continue
        opens_at, closes_at = _session_bounds(day)
        for at, open_price, high, low, close_price in session_bars(
            rng, anchors, opens_at=opens_at, closes_at=closes_at
        ):
            if (instrument_id, at) in existing:
                continue
            payload.append(
                {
                    "instrument_id": instrument_id,
                    "timeframe": "1m",
                    "ts": at,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close_price,
                    "volume": Decimal(rng.randint(150, 4200)),
                    "source": "demo",
                }
            )

    written = 0
    # Batched against the bind-parameter ceiling: nine columns over two hundred thousand
    # rows is far past the 32,767 a single statement can carry.
    for chunk in batched(payload):
        await session.execute(insert(MarketBar), chunk)  # type: ignore[attr-defined]
        written += len(chunk)
    return written, len(grouped)


async def main() -> None:
    configure_logging(get_settings())
    async with session_scope() as session:
        written, sessions = await write_bars(session)
        logger.info("backfill.bars_written", bars=written, sessions=sessions)


if __name__ == "__main__":
    asyncio.run(main())
