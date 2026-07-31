"""Fixtures for analytics tests.

Trades are built by a small factory so each test states only what it cares about. Where
a test checks a formula, the expected value is computed by hand in the docstring — an
assertion against the implementation's own output would pass no matter how wrong the
formula is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.analytics.types import TradeRecord
from app.core.ids import uuid7
from app.domain.common.enums import Direction

ACCOUNT_ID = uuid7()
BASE_TIME = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


def make_trade(
    net_pnl: str | Decimal,
    *,
    index: int = 0,
    r_multiple: str | Decimal | None = None,
    duration_seconds: int | None = 600,
    session_date: date | None = None,
    entry_hour: int | None = 9,
    entry_weekday: int | None = 1,
    direction: Direction = Direction.LONG,
    instrument_root: str | None = "ES",
    setup: str | None = None,
    strategy: str | None = None,
    market_condition: str | None = None,
    commission: str = "0",
    fees: str = "0",
    mae_r: str | None = None,
    mfe_r: str | None = None,
    closed: bool = True,
) -> TradeRecord:
    """Build one trade. ``index`` spaces trades an hour apart so ordering is stable."""
    opened = BASE_TIME + timedelta(hours=index)
    net = Decimal(net_pnl) if isinstance(net_pnl, str) else net_pnl
    return TradeRecord(
        trade_id=uuid7(),
        account_id=ACCOUNT_ID,
        opened_at=opened,
        closed_at=opened + timedelta(seconds=duration_seconds or 0) if closed else None,
        direction=direction,
        net_pnl=net,
        gross_pnl=net + Decimal(commission) + Decimal(fees),
        commission=Decimal(commission),
        fees=Decimal(fees),
        r_multiple=(
            Decimal(r_multiple) if isinstance(r_multiple, str) else r_multiple
        ),
        duration_seconds=duration_seconds,
        session_date=session_date or (BASE_TIME + timedelta(hours=index)).date(),
        entry_hour=entry_hour,
        entry_weekday=entry_weekday,
        instrument_root=instrument_root,
        instrument_symbol=f"{instrument_root}Z5" if instrument_root else None,
        setup=setup,
        strategy=strategy,
        market_condition=market_condition,
        mae_r=Decimal(mae_r) if mae_r else None,
        mfe_r=Decimal(mfe_r) if mfe_r else None,
    )


def make_trades(pnls: list[str], **kwargs: object) -> list[TradeRecord]:
    """Build a sequence of trades from a list of P&L strings."""
    return [make_trade(value, index=index, **kwargs) for index, value in enumerate(pnls)]  # type: ignore[arg-type]


@pytest.fixture
def simple_trades() -> list[TradeRecord]:
    """Five trades: +100, -50, +200, -50, +100.

    Net +300, three winners, two losers, gross profit 400, gross loss 100.
    Hand-computed throughout the tests that use it.
    """
    return make_trades(["100", "-50", "200", "-50", "100"])


@pytest.fixture
def r_trades() -> list[TradeRecord]:
    """Trades with R multiples: +2R, -1R, +3R, -1R, +1R. Mean R = 0.8."""
    values = [("200", "2"), ("-100", "-1"), ("300", "3"), ("-100", "-1"), ("100", "1")]
    return [
        make_trade(pnl, index=index, r_multiple=r)
        for index, (pnl, r) in enumerate(values)
    ]


@pytest.fixture
def losing_trades() -> list[TradeRecord]:
    return make_trades(["-100", "-50", "50", "-75", "-25"])


@pytest.fixture
def many_trades() -> list[TradeRecord]:
    """A 200-trade sample with a genuine edge, deterministic and reproducible.

    A repeating pattern rather than random draws: seeded randomness would still make a
    test's expected values depend on the RNG's implementation, while a fixed cycle makes
    every aggregate exactly hand-checkable.
    """
    pattern = ["150", "-100", "200", "-100", "-50", "300", "-100", "100", "-50", "150"]
    return make_trades([pattern[index % len(pattern)] for index in range(200)])
