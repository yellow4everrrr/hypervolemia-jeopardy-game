"""Synthetic histories for the ML tests.

Two generators, and the distinction between them is the whole test strategy:

* :func:`noise_history` — outcomes drawn independently of every feature. Nothing here is
  predictable, so anything the engine reports as deployable is a false positive.
* :func:`edged_history` — one genuine, exploitable regularity. Needed because an engine
  that refused everything would pass every null test in the suite while being useless.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.analytics.types import TradeRecord
from app.domain.common.enums import Direction

BASE = datetime(2025, 1, 6, 14, 30, tzinfo=UTC)
ACCOUNT = uuid4()

#: Outcome rule: given (direction, index within session, rng) decide whether it won.
OutcomeRule = Callable[[Direction, int, random.Random], bool]


def _weekdays(count: int) -> list[datetime]:
    """Consecutive weekday sessions.

    Weekends are skipped so that ``entry_weekday`` spans a realistic range. A generator
    that produced Saturdays would give the model a feature level that never occurs in
    real futures data.
    """
    days: list[datetime] = []
    offset = 0
    while len(days) < count:
        candidate = BASE + timedelta(days=offset)
        offset += 1
        if candidate.weekday() < 5:
            days.append(candidate)
    return days


def build_history(
    *,
    sessions: int,
    per_session: int,
    seed: int,
    rule: OutcomeRule | None = None,
    win_rate: float = 0.45,
    with_r: bool = True,
) -> list[TradeRecord]:
    rng = random.Random(seed)
    trades: list[TradeRecord] = []

    for day in _weekdays(sessions):
        for index in range(per_session):
            opened = day + timedelta(minutes=17 * index)
            direction = Direction.LONG if rng.random() < 0.5 else Direction.SHORT
            won = rule(direction, index, rng) if rule else rng.random() < win_rate
            trades.append(
                TradeRecord(
                    trade_id=uuid4(),
                    account_id=ACCOUNT,
                    opened_at=opened,
                    closed_at=opened + timedelta(minutes=5),
                    direction=direction,
                    net_pnl=Decimal("120") if won else Decimal("-100"),
                    r_multiple=(Decimal("1.2") if won else Decimal("-1.0")) if with_r else None,
                    quantity=Decimal(rng.choice([1, 1, 2, 3])),
                    session_date=opened.date(),
                    entry_hour=opened.hour,
                    entry_weekday=opened.isoweekday(),
                    duration_seconds=300,
                    mae_r=Decimal("-0.5") if won else Decimal("-1.0"),
                    mfe_r=Decimal("2.0") if won else Decimal("0.2"),
                )
            )
    return trades


def noise_history(*, seed: int, sessions: int = 60, per_session: int = 4) -> list[TradeRecord]:
    """Outcomes independent of every feature the model can see."""
    return build_history(sessions=sessions, per_session=per_session, seed=seed)


def edged_history(*, seed: int, sessions: int = 60, per_session: int = 4) -> list[TradeRecord]:
    """Long trades genuinely win far more often than short ones.

    A crude edge on purpose. The positive control's job is to prove the engine can find
    something when something is there — a subtle edge would make a failure ambiguous
    between "the gate is too strict" and "the signal was too weak".
    """

    def rule(direction: Direction, _index: int, rng: random.Random) -> bool:
        return rng.random() < (0.68 if direction is Direction.LONG else 0.25)

    return build_history(sessions=sessions, per_session=per_session, seed=seed, rule=rule)
