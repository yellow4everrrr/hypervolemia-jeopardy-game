"""Shared fixtures.

Domain tests use real contract specifications (ES, MES, CL) rather than a toy
instrument with tick size 1.0. A tick size of 0.25 and a tick value of 12.50 is exactly
where off-by-a-factor-of-four errors hide, and a test that cannot catch them is not
testing the thing that matters.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.common.enums import AssetClass, Side
from app.domain.trading.execution import Execution
from app.domain.trading.instrument import InstrumentSpec

ES = InstrumentSpec(
    symbol="ESZ5",
    exchange="CME",
    asset_class=AssetClass.FUTURE,
    currency="USD",
    tick_size=Decimal("0.25"),
    tick_value=Decimal("12.50"),
    exchange_timezone="America/Chicago",
)

MES = InstrumentSpec(
    symbol="MESZ5",
    exchange="CME",
    asset_class=AssetClass.FUTURE,
    currency="USD",
    tick_size=Decimal("0.25"),
    tick_value=Decimal("1.25"),
    exchange_timezone="America/Chicago",
)

CL = InstrumentSpec(
    symbol="CLZ5",
    exchange="NYMEX",
    asset_class=AssetClass.FUTURE,
    currency="USD",
    tick_size=Decimal("0.01"),
    tick_value=Decimal("10.00"),
    exchange_timezone="America/New_York",
)

SPECS = {spec.symbol: spec for spec in (ES, MES, CL)}


@pytest.fixture
def specs() -> dict[str, InstrumentSpec]:
    return dict(SPECS)


@pytest.fixture
def es() -> InstrumentSpec:
    return ES


class ExecutionFactory:
    """Builds fills with sensible defaults and monotonically increasing ids."""

    def __init__(self, symbol: str = "ESZ5", account: str = "acct-1") -> None:
        self.symbol = symbol
        self.account = account
        self._counter = 0

    def __call__(
        self,
        side: Side | str,
        quantity: str | int,
        price: str,
        *,
        minute: int = 0,
        second: int = 0,
        commission: str = "0",
        fees: str = "0",
        sequence: int = 0,
        symbol: str | None = None,
        account: str | None = None,
        external_id: str | None = None,
        day: int = 5,
        hour: int = 14,
    ) -> Execution:
        self._counter += 1
        return Execution(
            external_id=external_id or f"fill-{self._counter}",
            account_key=account or self.account,
            instrument_symbol=symbol or self.symbol,
            side=Side(side) if isinstance(side, str) else side,
            quantity=Decimal(str(quantity)),
            price=Decimal(price),
            executed_at=datetime(2026, 3, day, hour, minute, second, tzinfo=UTC),
            commission=Decimal(commission),
            fees=Decimal(fees),
            sequence=sequence,
        )


@pytest.fixture
def fill() -> ExecutionFactory:
    return ExecutionFactory()


def requires_database() -> bool:
    return bool(os.getenv("LEDGERLINE_TEST_DATABASE_URL"))


db_test = pytest.mark.skipif(
    not requires_database(),
    reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
)
