"""Seed a demo trading history: a year of sessions, one planted leak, bars for a replay.

Exists so the application can be *run* — by CI, by a reviewer, by anyone who wants to see
the screens with something on them. Milestones 1 through 13 all passed their tests before
anyone rendered a page against real data, and six defects were waiting there
([ADR 0016](../../../../docs/adr/0016-running-it-is-a-test-stage.md)).

Two properties matter more than volume, and both were got wrong on the first attempt.

**The data must reach the thresholds the engine actually uses.** The first version put a
fixed five trades in every session against an ``OVERTRADING_THRESHOLD`` of six, so the one
detector capable of finding the planted leak never had a sample to test. The scan came
back clean on data built to be dirty — a false negative that looked like a working system.
Session lengths are drawn from :data:`SESSION_LENGTHS` and reach nine.

**The prices must come from a price series.** Every number a trade records — entry, exit,
stop, both excursions — is now read off a continuous minute path built by
:mod:`app.scripts.market_path`, one path per instrument per session. The earlier versions
drew each of them separately, and being separately plausible is not the same as being
jointly possible:

* Entries were ``5180 + uniform(-40, 40)``, drawn independently per trade, so prices
  inside a single session spanned 33 points on average and as much as 79. Once bars
  existed to join them the chart had to cover that ground, and did so in visible jumps.
* Excursions had to be *invented* to agree with the outcome, and that function was wrong
  twice in opposite directions — first generous enough that a 1R target looked worth
  $248,000, then mean enough that MFE became a perfect win/loss label and the pattern
  engine published the outcome back as a $762,759 "finding" (ADR 0020).

Both classes of defect come from the same root, and placing trades on a path removes it:
MAE and MFE are now **measured** from the highs and lows the path printed while the
position was open, so ``mae_r <= realized_r <= mfe_r`` holds because the data enforces it
rather than because an assertion asks for it.

**The planted leak still has to be earned.** The outcome is not left to the path — the
placement searches forward for an entry whose stop-or-target race ends the way the
session's leak design wants. The trade is real; which real trade gets taken is steered.
That is a fair description of a trader with a bias, and it keeps the leak a property of
trade *sequence* rather than of any price column.

Idempotent only in the sense that it refuses to run twice: it creates a user and exits if
that user already exists, rather than layering a second history on top of the first.

Usage::

    LEDGERLINE_DATABASE_URL=... python -m app.scripts.seed_demo
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import insert, select, text

from app.application.use_cases.generate_report import GenerateReport
from app.core.config import get_settings
from app.core.ids import uuid7
from app.core.logging import configure_logging, get_logger
from app.domain.common.enums import (
    AccountType,
    AssetClass,
    Direction,
    ExecutionRole,
    ReportType,
    RuleSeverity,
    RuleType,
    SessionSegment,
    Side,
    TradeStatus,
)
from app.domain.rules.context import STARTER_RULES
from app.infrastructure.db.bulk import batched
from app.infrastructure.db.models.broker import Account
from app.infrastructure.db.models.catalog import Strategy, StrategyRule
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.marketdata import MarketBar
from app.infrastructure.db.models.trading import Execution, Trade, TradeExecution
from app.infrastructure.db.repositories.reports import SqlAlchemyReportRepository
from app.infrastructure.db.session import dispose_engine, session_scope
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.reports.periods import period_containing
from app.scripts.market_path import (
    Candle,
    Placement,
    excursions,
    place_trade,
    session_path,
)

logger = get_logger(__name__)

#: Seeded so two runs produce byte-identical histories. A demo that changes under you is
#: a demo you cannot file a bug against.
SEED = 20260731

DEMO_CLERK_USER_ID = "demo"
SESSIONS = 250
FIRST_SESSION = date(2025, 8, 4)

#: Trades per session, drawn per day rather than fixed, and reaching past
#: ``OVERTRADING_THRESHOLD`` (6) so the detector that finds the planted leak has a sample.
SESSION_LENGTHS = (3, 4, 4, 5, 5, 6, 6, 7, 8, 9)

#: The planted leak: past this many trades in a session, the edge is gone. Chosen to sit
#: at the detector's threshold. It is a split on trade *sequence*, not on outcome, so the
#: scan has to earn it through a permutation test rather than find it by construction.
LEAK_AFTER = 6
WIN_RATE_EARLY = 0.55
WIN_RATE_LATE = 0.31

#: The session every path spans, in UTC: 11:30 to 20:15, which is two hours of Globex
#: followed by the cash session to the 16:15 ET close.
#:
#: The overnight hours are not decoration. A replay reaches back 120 bars before the entry,
#: so a path that opens at its first trade renders a chart starting at the entry marker,
#: and the trades themselves need room to run — they are placed sequentially, each
#: searching forward from where the last one exited, so the session has a finite budget of
#: them and `place_trade` returns ``None`` when it runs out. See
#: :func:`tests.unit.test_seed_demo.test_the_session_is_long_enough_for_the_trades_it_asks_for`
#: for why that matters more than it sounds: the trades a short session drops are always
#: the *last* of the day, which are exactly the ones the planted leak is made of.
SESSION_OPEN = time(11, 30)
SESSION_MINUTES = 525

#: The first minute a trade may be placed at, as an offset into the session — 13:30 UTC,
#: the cash open, leaving exactly the 120 context bars ``build_window`` asks for. It is
#: also what makes :func:`_segment`'s ``RTH_OPEN`` label true rather than nominal.
FIRST_TRADE_INDEX = 120

#: Where the first session opens, and how far the price gaps between sessions. The gap is
#: what stops a year of history from being one continuous intraday walk; it is drawn per
#: instrument, so the two paths separate over time rather than moving in lockstep.
START_PRICE = Decimal("5180.00")
OVERNIGHT_SIGMA = 8.0

#: The instruments the demo trades, as ``(symbol, root, tick_value, stop_distance)``.
#:
#: The stop is stated in **points** rather than dollars because the path is in points: it
#: is the distance the price has to travel, and the race between it and the target is what
#: decides the trade. The dollar risk falls out of the tick value.
#:
#: The two distances are not the same number and must not be. ES is $50 a point and NQ
#: $20, so an equal dollar risk buys 5 points on one and 12.5 on the other. Copying one
#: distance to the other instrument would leave 1R meaning $250 on ES and $100 on NQ, and
#: every R-denominated statistic in the application averages across both.
TICK_SIZE = Decimal("0.25")
INSTRUMENTS = (
    ("ESU6", "ES", Decimal("12.50"), Decimal("5")),
    ("NQU6", "NQ", Decimal("5.00"), Decimal("12.5")),
)

#: Share of trades taken on the first instrument. Concentrated rather than even, because a
#: trader with one setup does not split their year down the middle, and because the
#: per-instrument analytics need one symbol with a sample worth grouping on.
ES_SHARE = 0.7

#: Minutes to wait after an exit before the next entry may be searched for. Sequential
#: trades, not simultaneous ones: the session's third trade begins where its second ended,
#: which is what the leak is a split on.
COOLDOWN = (3, 20)

#: How long a position may be held before it is closed at the market, in minutes.
MAX_HOLD = (6, 40)

#: Profit target, as a multiple of the stop distance.
#:
#: Narrow on purpose. This range was 1.2–2.6, and widening a target does not make a demo
#: more realistic — it makes the per-trade P&L noisier, and the noise lands on the
#: detector rather than on the chart. Measured across seven seeded histories, the planted
#: overtrading leak cleared p<0.01 on five of seven at 1.2–2.6 and on seven of seven at
#: 1.4–2.0; on the shipped seed it was one of the two that failed, so the scan came back
#: without the one finding the history was built to contain.
#:
#: A trader running one setup does not vary their target by a factor of two anyway. The
#: leak has to be *found*, not merely planted, and how hard it is to find is a property of
#: the variance around it as much as of its size.
TARGET_R = (1.4, 2.0)


def _segment(index: int) -> SessionSegment:
    if index == 0:
        return SessionSegment.RTH_OPEN
    return SessionSegment.RTH_MORNING if index < 4 else SessionSegment.RTH_AFTERNOON


@dataclass(frozen=True, slots=True)
class Planned:
    """One trade of a session, decided but not yet turned into rows."""

    #: Position in the session, counting trades that were actually placed. The planted leak
    #: is a split on this, and it is the same number `behaviours.py` arrives at by sorting
    #: the session on ``opened_at`` — see :func:`plan_session` for why that is not free.
    index: int
    symbol: str
    quantity: Decimal
    long: bool
    placement: Placement


def plan_session(
    rng: random.Random, paths: dict[str, list[Candle]], *, length: int
) -> list[Planned]:
    """Decide a day's trades against already-generated paths. No database, no clock.

    Extracted from :func:`main` so it can be tested. That is not a tidiness point: the
    properties that matter about a session are statistical and sequential — how strong the
    planted leak is, how many trades a day has room for, whether the day is a single
    ordered timeline — and none of them can be checked by inspecting the constants or by
    reading one trade. A test that re-implemented this loop would be testing its own copy,
    and the copy would stay correct while the seed drifted.

    **One cursor, not one per instrument.** The cursor is the session's clock: each trade
    searches forward from where the previous one exited, whichever contract that was, so a
    trader holds one position at a time. Running a cursor per instrument is the obvious
    thing and it silently breaks the demo — the two contracts get independent clocks, a
    trade placed eighth can open before one placed third, and since the detector recovers
    a trade's position by sorting on ``opened_at`` the leak ends up planted at one index
    and read at another. It cost the scan its only planted finding, with nothing anywhere
    reporting an error.
    """
    planned: list[Planned] = []
    cursor = FIRST_TRADE_INDEX

    for requested in range(length):
        late = requested >= LEAK_AFTER
        want_win = rng.random() < (WIN_RATE_LATE if late else WIN_RATE_EARLY)
        symbol, _, _, stop = (
            INSTRUMENTS[0] if rng.random() < ES_SHARE else INSTRUMENTS[1]
        )
        quantity = Decimal(rng.choice([1, 1, 2, 2, 3]))
        long = rng.random() < 0.55

        placement = place_trade(
            paths[symbol],
            from_index=cursor,
            long=long,
            risk_points=stop,
            target_r=Decimal(str(round(rng.uniform(*TARGET_R), 2))),
            want_win=want_win,
            max_hold=rng.randint(*MAX_HOLD),
            tick_size=float(TICK_SIZE),
        )
        # No entry in the window produced the wanted outcome, or the session ran out of
        # room. Skipping is the only honest answer: forcing one means a price that is not
        # on the path, which is the whole thing `market_path` exists to prevent.
        if placement is None:
            continue
        cursor = placement.exit_index + rng.randint(*COOLDOWN)
        planned.append(
            Planned(
                index=len(planned),
                symbol=symbol,
                quantity=quantity,
                long=long,
                placement=placement,
            )
        )

    return planned


async def main() -> None:
    configure_logging(get_settings())
    rng = random.Random(SEED)

    async with session_scope() as session:
        existing = (
            await session.execute(
                select(User).where(User.clerk_user_id == DEMO_CLERK_USER_ID)
            )
        ).scalar_one_or_none()
        if existing is not None:
            logger.warning(
                "seed_demo.already_seeded",
                detail="a demo user exists; drop the database to reseed",
            )
            return

        user = User(
            id=uuid7(),
            clerk_user_id=DEMO_CLERK_USER_ID,
            email="demo@ledgerline.app",
            display_name="Demo Trader",
        )
        es, nq = (
            Instrument(
                id=uuid7(), symbol=symbol, root_symbol=root, exchange="CME",
                asset_class=AssetClass.FUTURE, tick_size=TICK_SIZE,
                tick_value=tick_value, currency="USD",
                exchange_timezone="America/Chicago",
            )
            for symbol, root, tick_value, _ in INSTRUMENTS
        )
        risk_points = {
            instrument.id: points
            for instrument, (*_, points) in zip((es, nq), INSTRUMENTS, strict=True)
        }
        session.add_all([user, es, nq])
        await session.flush()

        account = Account(
            id=uuid7(), user_id=user.id, broker_account_id="DEMO-1",
            name="Tradovate Live", account_type=AccountType.LIVE,
            starting_balance=Decimal("50000"),
        )
        session.add(account)
        await session.flush()

        strategy_id = await _seed_strategy(session, user)

        day = FIRST_SESSION
        sessions_made = 0
        #: Reassigned every iteration, so it holds the last trade created — and the loop
        #: runs chronologically, so the last created is the most recent.
        replay_trade: tuple[Trade, Instrument] | None = None

        #: One price path per instrument per session, plus the last close each carried
        #: into the night.
        #:
        #: The cursor is **one clock for the session, not one per instrument**, and that is
        #: load-bearing rather than a simplification. It is what makes the day a sequence:
        #: each trade searches forward from where the previous one exited, whichever
        #: contract that was. A trader holds one position at a time.
        #:
        #: With a cursor each, the two instruments run independent clocks and a trade taken
        #: eighth in the loop can open before one taken third. That matters because the
        #: planted leak is a split on *position in the session*, and the detector works out
        #: that position by ordering on ``opened_at`` (`behaviours.py`), not by asking the
        #: seed. The two disagreed, so the leak was planted at one index and read at
        #: another — diluted by roughly a third, and invisible in every test of its design.
        paths: dict[str, list[Candle]] = {}
        instruments = dict(zip((s for s, *_ in INSTRUMENTS), (es, nq), strict=True))
        closes = dict.fromkeys(instruments, START_PRICE)
        bars: list[dict[str, object]] = []

        while sessions_made < SESSIONS:
            if day.weekday() >= 5:
                day += timedelta(days=1)
                continue
            sessions_made += 1

            opens_at = datetime.combine(day, SESSION_OPEN, tzinfo=UTC)
            for symbol, instrument in instruments.items():
                gap = Decimal(str(round(rng.gauss(0, OVERNIGHT_SIGMA), 2)))
                paths[symbol] = session_path(
                    rng,
                    opens_at=opens_at,
                    minutes=SESSION_MINUTES,
                    start_price=closes[symbol] + gap,
                    tick_size=float(instrument.tick_size),
                )
                closes[symbol] = paths[symbol][-1].close

            for planned in plan_session(rng, paths, length=rng.choice(SESSION_LENGTHS)):
                index, placement = planned.index, planned.placement
                instrument = instruments[planned.symbol]
                path = paths[planned.symbol]
                quantity, long = planned.quantity, planned.long

                point_value = instrument.tick_value / instrument.tick_size
                stop_distance = risk_points[instrument.id]
                risk = (point_value * stop_distance * quantity).quantize(Decimal("0.01"))

                opened = path[placement.entry_index].at
                closed = path[placement.exit_index].at
                held = max(int((closed - opened).total_seconds()), 60)

                points = (placement.exit_price - placement.entry_price) * (
                    Decimal(1) if long else Decimal(-1)
                )
                net = (points * point_value * quantity).quantize(Decimal("0.01"))
                gross = net + Decimal("4.10") * quantity
                realized_r = (net / risk).quantize(Decimal("0.01"))

                # Measured off the path, not drawn from a distribution. This is what makes
                # mae_r <= realized_r <= mfe_r true by construction — see ADR 0020 for the
                # two ways inventing them went wrong.
                adverse, favourable = excursions(placement, long=long)
                mae_r = (adverse * point_value * quantity / risk).quantize(Decimal("0.01"))
                mfe_r = (favourable * point_value * quantity / risk).quantize(Decimal("0.01"))

                trade = Trade(
                    id=uuid7(), user_id=user.id, account_id=account.id,
                    instrument_id=instrument.id,
                    origin_execution_id=f"fill-{sessions_made}-{index}",
                    direction=Direction.LONG if long else Direction.SHORT,
                    status=TradeStatus.CLOSED, opened_at=opened,
                    closed_at=closed, duration_seconds=held,
                    quantity_opened=quantity, quantity_closed=quantity,
                    max_position_size=quantity,
                    avg_entry_price=placement.entry_price,
                    avg_exit_price=placement.exit_price,
                    gross_pnl=gross, commission=Decimal("2.50") * quantity,
                    fees=Decimal("1.60") * quantity, net_pnl=net,
                    realized_r=realized_r,
                    initial_stop_price=placement.stop_price,
                    planned_risk_amount=risk,
                    mae_r=mae_r, mfe_r=mfe_r,
                    mae_price=placement.lowest if long else placement.highest,
                    mfe_price=placement.highest if long else placement.lowest,
                    session_date=day, entry_hour=opened.hour,
                    entry_weekday=day.isoweekday(),
                    session_segment=_segment(index),
                    # Every trade is attributed to the one strategy. Without this the
                    # compliance engine reports `trades_without_strategy` for all of
                    # them and scores nothing — the rules exist but never meet a trade.
                    strategy_id=strategy_id,
                )
                session.add(trade)
                replay_trade = (trade, instrument)

            for symbol, instrument in instruments.items():
                bars.extend(
                    {
                        "instrument_id": instrument.id, "timeframe": "1m", "ts": candle.at,
                        "open": candle.open, "high": candle.high, "low": candle.low,
                        "close": candle.close,
                        "volume": Decimal(rng.randint(150, 4200)), "source": "demo",
                    }
                    for candle in paths[symbol]
                )

            day += timedelta(days=1)

        await session.flush()
        assert replay_trade is not None
        await _seed_replay(session, rng, user, account, *replay_trade)
        await session.flush()

        # The same candles the trades were placed on, so the bars cannot disagree with
        # them. Bars used to be generated *after* the trades and fitted to them — first by
        # a random walk near the entry, which produced a chart that did not contain its own
        # trade (an entry of 5175.96 against a bar spanning 5163.78-5165.13, which renders
        # without complaint and which `compute_excursions` refuses outright), then by a
        # Brownian bridge through the recorded prices, which was correct but was still
        # reverse-engineering a path from its own conclusions. Here there is only one price
        # series and both the trades and the chart read from it.
        #
        # `backfill_demo_bars` is kept for histories seeded before this, which have trades
        # and no bars and cannot be re-derived — it is no longer on this path.
        for chunk in batched(bars):
            await session.execute(insert(MarketBar), chunk)
        await session.flush()
        await _seed_reports(session, user)

        total, net_total = (
            await session.execute(text("SELECT count(*), sum(net_pnl) FROM trades"))
        ).one()
        logger.info(
            "seed_demo.complete",
            user=DEMO_CLERK_USER_ID,
            trades=total,
            sessions=SESSIONS,
            net=str(net_total),
            replay_trade=str(replay_trade[0].id),
            bars=len(bars),
        )

    await dispose_engine()


async def _seed_strategy(session: object, user: User) -> UUID:
    """A strategy built from the published starter rules.

    Seeded rather than left empty because the compliance engine is otherwise invisible:
    with no strategy there is nothing to score, and a screen that says "no strategies yet"
    demonstrates nothing about a milestone's worth of rule evaluation.

    The starter rules are used verbatim, from the same constant the API publishes, so the
    demo cannot drift from what a new user is actually offered. They also interact with
    the planted leak in a way worth seeing: sessions run to nine trades against a
    ``max_trades_per_session`` rule set at six, so the compliance report and the pattern
    scan independently flag the same behaviour — one as a broken rule, one as a
    statistically established leak.
    """
    strategy = Strategy(
        id=uuid7(),
        user_id=user.id,
        name="Opening range continuation",
        description=(
            "Trade the first clean pullback after the open in the direction of the "
            "opening drive. Stop beyond the pullback low, target the measured move."
        ),
        version=1,
        is_active=True,
    )
    session.add(strategy)  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]

    for rule in STARTER_RULES:
        session.add(  # type: ignore[attr-defined]
            StrategyRule(
                id=uuid7(),
                user_id=user.id,
                strategy_id=strategy.id,
                rule_type=RuleType(rule["rule_type"]),
                code=rule["code"],
                label=rule["label"],
                severity=RuleSeverity(rule["severity"]),
                expression=rule["expression"],
            )
        )
    return strategy.id


#: Months built at seed time. **Two consecutive** months, because the reports screen is
#: built around a period-over-period comparison and one month has nothing to compare
#: against — it renders an honest but empty page that no smoke test can distinguish from a
#: broken one.
#:
#: Deliberately not the full schedule. `run_schedule` would build every completed daily,
#: weekly, monthly, quarterly and annual period in the history — around three hundred
#: reports, each running the analytics engine and a pattern scan — which is minutes of
#: work to demonstrate a screen that shows one report at a time.
REPORT_MONTHS = (date(2025, 12, 15), date(2026, 1, 15))


async def _seed_reports(session: object, user: User) -> None:
    """Build two consecutive monthly reports so the comparison has both sides."""
    use_case = GenerateReport(
        repository=SqlAlchemyReportRepository(session),  # type: ignore[arg-type]
        uow=SqlAlchemyUnitOfWork(session),  # type: ignore[arg-type]
    )
    for day_in_month in REPORT_MONTHS:
        await use_case.execute(
            user_id=user.id,
            period=period_containing(ReportType.MONTHLY, day_in_month),
            persist=True,
        )


async def _seed_replay(
    session: object,
    rng: random.Random,
    user: User,
    account: Account,
    trade: Trade,
    instrument: Instrument,
) -> None:
    """Executions for one trade, so the replay has fills to mark and not just a price.

    Every trade has bars — they are the path it was placed on — but only this one has
    executions, and the blotter orders newest-first, so anything that opens "the first
    trade in the list" lands on it: a reviewer clicking the top row, the route smoke test
    fetching ``?limit=1``.

    Bars are written at **1m** even though the replay window will ask for 2m. That is
    deliberate: it is the exact mismatch that returned an empty chart for a year of
    history, and leaving it in place means the demo exercises the aggregation path rather
    than tiptoeing around it.
    """
    entry_id, exit_id = uuid7(), uuid7()
    session.add_all(  # type: ignore[attr-defined]
        [
            Execution(
                id=entry_id, user_id=user.id, account_id=account.id,
                instrument_id=instrument.id, broker_execution_id="x-entry",
                side=Side.BUY, quantity=trade.quantity_opened,
                price=trade.avg_entry_price, executed_at=trade.opened_at, sequence=0,
            ),
            Execution(
                id=exit_id, user_id=user.id, account_id=account.id,
                instrument_id=instrument.id, broker_execution_id="x-exit",
                side=Side.SELL, quantity=trade.quantity_closed,
                price=trade.avg_exit_price, executed_at=trade.closed_at, sequence=1,
            ),
        ]
    )
    await session.flush()  # type: ignore[attr-defined]

    session.add_all(  # type: ignore[attr-defined]
        [
            TradeExecution(
                trade_id=trade.id, execution_id=entry_id, leg_index=0,
                role=ExecutionRole.ENTRY, quantity=trade.quantity_opened,
                price=trade.avg_entry_price, executed_at=trade.opened_at,
            ),
            TradeExecution(
                trade_id=trade.id, execution_id=exit_id, leg_index=1,
                role=ExecutionRole.EXIT, quantity=trade.quantity_closed,
                price=trade.avg_exit_price, executed_at=trade.closed_at,
            ),
        ]
    )

if __name__ == "__main__":
    asyncio.run(main())
