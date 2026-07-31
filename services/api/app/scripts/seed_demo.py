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

**Excursions must respect their own arithmetic.** The first version set
``mfe_r = abs(r) + uniform(0.05, 0.9)``, which gives a trade that lost 0.9R a maximum
favourable excursion of up to 1.8R — a loser that supposedly ran nearly two units of risk
in your favour before failing. The what-if simulator then reported that a 1R profit target
would have earned an extra $248,000, because under that rule most of the losers became
winners. The engine was right; the history was impossible. See :func:`_excursions`.

Idempotent only in the sense that it refuses to run twice: it creates a user and exits if
that user already exists, rather than layering a second history on top of the first.

Usage::

    LEDGERLINE_DATABASE_URL=... python -m app.scripts.seed_demo
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text

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

#: Bars written for one trade, so the replay screen has a chart. One trade rather than all
#: of them because 1,400 trades of minute bars is a quarter of a million rows for a demo.
#:
#: The bars go on the **most recently opened** trade, and that is a deliberate coupling
#: rather than an arbitrary pick: the blotter orders newest-first, so anything that opens
#: "the first trade in the list" — a reviewer clicking the top row, the route smoke test
#: fetching ``?limit=1`` — lands on the one trade that has a chart. Attaching them to the
#: oldest trade instead put an empty chart behind the most obvious click in the app.
REPLAY_BARS = 180


def _excursions(
    rng: random.Random, realized_r: Decimal
) -> tuple[Decimal, Decimal]:
    """Maximum adverse and favourable excursion, in R, consistent with the outcome.

    Two identities constrain these and are not negotiable, because they are facts about
    what the words mean rather than modelling choices:

    * ``mfe_r >= max(realized_r, 0)`` — you cannot exit better than the best price the
      trade ever offered.
    * ``mae_r <= min(realized_r, 0)`` — you cannot exit worse than the worst price it
      ever reached.

    Within those bounds the *distribution* still has to be plausible, and that is what the
    first version of this script got wrong. Giving losers a large favourable excursion is
    arithmetically legal and behaviourally absurd: it describes a trader who was up two
    units of risk and chose to lose one. It also silently rigs the exit simulator, since
    every counterfactual profit target fills on a trade whose MFE cleared it.

    So a winner ran a little past where it was closed, and a loser mostly did not run at
    all.
    """
    if realized_r > 0:
        # Exited near, but not at, the high. A large excess here is what a trader means
        # by "I left money on the table", and it should be the exception.
        mfe = realized_r + Decimal(str(round(rng.uniform(0.05, 0.60), 2)))
        mae = Decimal(str(round(-rng.uniform(0.10, 0.80), 2)))
    else:
        # A losing trade that ticked briefly in your favour before failing. Capped well
        # under 1R: a trade that reaches a full unit of risk in profit and still loses is
        # a real event, but it is not the common one.
        mfe = Decimal(str(round(rng.uniform(0.0, 0.45), 2)))
        mae = realized_r - Decimal(str(round(rng.uniform(0.0, 0.20), 2)))

    assert mfe >= max(realized_r, Decimal(0)), "MFE below the realized result"
    assert mae <= min(realized_r, Decimal(0)), "MAE above the realized result"
    return mae, mfe


def _segment(index: int) -> SessionSegment:
    if index == 0:
        return SessionSegment.RTH_OPEN
    return SessionSegment.RTH_MORNING if index < 4 else SessionSegment.RTH_AFTERNOON


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
        es = Instrument(
            id=uuid7(), symbol="ESU6", root_symbol="ES", exchange="CME",
            asset_class=AssetClass.FUTURE, tick_size=Decimal("0.25"),
            tick_value=Decimal("12.50"), currency="USD",
            exchange_timezone="America/Chicago",
        )
        nq = Instrument(
            id=uuid7(), symbol="NQU6", root_symbol="NQ", exchange="CME",
            asset_class=AssetClass.FUTURE, tick_size=Decimal("0.25"),
            tick_value=Decimal("5.00"), currency="USD",
            exchange_timezone="America/Chicago",
        )
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
        #: runs chronologically, so the last created is the most recent. See REPLAY_BARS.
        replay_trade: tuple[Trade, Instrument] | None = None

        while sessions_made < SESSIONS:
            if day.weekday() >= 5:
                day += timedelta(days=1)
                continue
            sessions_made += 1

            for index in range(rng.choice(SESSION_LENGTHS)):
                late = index >= LEAK_AFTER
                won = rng.random() < (WIN_RATE_LATE if late else WIN_RATE_EARLY)
                instrument = es if rng.random() < 0.7 else nq
                direction = Direction.LONG if rng.random() < 0.55 else Direction.SHORT
                quantity = Decimal(rng.choice([1, 1, 2, 2, 3]))

                risk = Decimal("250") * quantity
                realized_r = Decimal(
                    str(round(rng.uniform(0.6, 2.4) if won else -rng.uniform(0.4, 1.05), 2))
                )
                net = (risk * realized_r).quantize(Decimal("0.01"))
                gross = net + Decimal("4.10") * quantity
                mae_r, mfe_r = _excursions(rng, realized_r)

                opened = datetime.combine(
                    day, datetime.min.time(), tzinfo=UTC
                ).replace(hour=14, minute=32) + timedelta(
                    minutes=34 * index + rng.randint(0, 12)
                )
                held = rng.randint(90, 2400)
                entry = Decimal("5180.00") + Decimal(str(round(rng.uniform(-40, 40), 2)))
                exit_price = entry + (
                    net / (Decimal("12.50") * quantity) * Decimal("0.25")
                )

                trade = Trade(
                    id=uuid7(), user_id=user.id, account_id=account.id,
                    instrument_id=instrument.id,
                    origin_execution_id=f"fill-{sessions_made}-{index}",
                    direction=direction, status=TradeStatus.CLOSED, opened_at=opened,
                    closed_at=opened + timedelta(seconds=held), duration_seconds=held,
                    quantity_opened=quantity, quantity_closed=quantity,
                    max_position_size=quantity,
                    avg_entry_price=entry.quantize(Decimal("0.01")),
                    avg_exit_price=exit_price.quantize(Decimal("0.01")),
                    gross_pnl=gross, commission=Decimal("2.50") * quantity,
                    fees=Decimal("1.60") * quantity, net_pnl=net,
                    realized_r=realized_r,
                    initial_stop_price=(
                        entry - Decimal("5")
                        if direction is Direction.LONG
                        else entry + Decimal("5")
                    ),
                    planned_risk_amount=risk,
                    mae_r=mae_r, mfe_r=mfe_r,
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

            day += timedelta(days=1)

        await session.flush()
        assert replay_trade is not None
        await _seed_replay(session, rng, user, account, *replay_trade)
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
    """Executions and minute bars for one trade, so the replay chart has something to draw.

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

    # The column is nullable, but every trade this script writes sets it. Asserting is
    # better than a cast: if the seed ever produces a trade without an entry price, the
    # bars would be generated around a meaningless number and the chart would look fine.
    assert trade.avg_entry_price is not None
    price = float(trade.avg_entry_price) - 6
    start = trade.opened_at - timedelta(minutes=45)
    for minute in range(REPLAY_BARS):
        open_price = price
        close_price = price + rng.gauss(0, 0.8)
        session.add(  # type: ignore[attr-defined]
            MarketBar(
                instrument_id=instrument.id, timeframe="1m", source="demo",
                ts=start + timedelta(minutes=minute),
                open=Decimal(str(round(open_price, 2))),
                high=Decimal(
                    str(round(max(open_price, close_price) + abs(rng.gauss(0, 0.6)), 2))
                ),
                low=Decimal(
                    str(round(min(open_price, close_price) - abs(rng.gauss(0, 0.6)), 2))
                ),
                close=Decimal(str(round(close_price, 2))),
                volume=Decimal(rng.randint(200, 5000)),
            )
        )
        price = close_price


if __name__ == "__main__":
    asyncio.run(main())
