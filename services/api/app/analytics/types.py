"""Analytics input and output types.

The engine operates on :class:`TradeRecord` — a flat, immutable snapshot of everything
a statistic might need about one trade. It is deliberately *not* the ORM model and not
the domain aggregate: analytics must be runnable over a list built in a test, a what-if
simulation, or a resampled bootstrap draw, none of which have database rows behind them.

Two conventions run through every result type in this package:

**Undefined is `None`, never zero.** Profit factor with no losing trades, expectancy in
R for a sample with no recorded stops, Sharpe with one trade — these are undefined, and
the AI layer must be handed "not defined for this sample" rather than a number it will
confidently interpret.

**Every result carries its sample size.** A 71% win rate over 7 trades and over 700
trades are different claims. Nothing in this package reports a statistic without saying
how much evidence is behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.common.enums import Direction, SessionSegment

#: Below this many observations a statistic is reported but marked unreliable. Chosen
#: because the bootstrap confidence interval on an expectancy from fewer than 30 trades
#: is typically wider than the effect anyone is trying to measure — the number is not
#: magic, it is the point where the interval stops being informative.
MIN_RELIABLE_SAMPLE = 30

#: Below this, a statistic is not computed at all. Two trades have a standard deviation
#: and it means nothing.
MIN_SAMPLE = 5


class Reliability(StrEnum):
    """How much weight a result can bear.

    Surfaced in the payload the AI layer receives, so that "your Friday afternoon edge"
    can be phrased as an observation rather than a finding when the sample is thin.
    """

    INSUFFICIENT = "insufficient"
    PROVISIONAL = "provisional"
    RELIABLE = "reliable"

    @classmethod
    def for_sample(cls, size: int) -> Reliability:
        if size < MIN_SAMPLE:
            return cls.INSUFFICIENT
        if size < MIN_RELIABLE_SAMPLE:
            return cls.PROVISIONAL
        return cls.RELIABLE


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One trade, flattened for analysis.

    Attributes:
        net_pnl: Realized P&L after commissions and fees. Every money statistic uses
            this; gross is retained only to quantify what costs are consuming.
        r_multiple: Result in units of initial risk, or ``None`` when the trade has no
            recorded stop. R-based statistics silently skip ``None`` rather than
            treating it as zero — and report how many they skipped.
        mae_r / mfe_r: Excursions in R. Populated by the market-data engine
            (milestone 4); ``None`` until then, which the excursion statistics report
            honestly rather than filling in.
    """

    trade_id: UUID
    account_id: UUID
    opened_at: datetime
    closed_at: datetime | None
    direction: Direction
    net_pnl: Decimal
    gross_pnl: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    quantity: Decimal = Decimal(1)
    r_multiple: Decimal | None = None
    duration_seconds: int | None = None

    # --- Segmentation dimensions -------------------------------------------------
    session_date: date | None = None
    session_segment: SessionSegment | None = None
    entry_hour: int | None = None
    entry_weekday: int | None = None
    instrument_symbol: str | None = None
    instrument_root: str | None = None
    strategy: str | None = None
    setup: str | None = None
    market_condition: str | None = None

    # --- Excursions (milestone 4) -------------------------------------------------
    mae_r: Decimal | None = None
    mfe_r: Decimal | None = None

    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0

    @property
    def is_loser(self) -> bool:
        return self.net_pnl < 0

    @property
    def is_scratch(self) -> bool:
        """Exactly break-even after costs.

        Counted separately from wins and losses throughout. Folding scratches into
        either one shifts the win rate, and a scratch is a different event from a
        winner: it is a trade that cost nothing but the commission.
        """
        return self.net_pnl == 0

    @property
    def total_costs(self) -> Decimal:
        return self.commission + self.fees


@dataclass(frozen=True, slots=True)
class Interval:
    """A confidence interval, with the method that produced it.

    The method matters and is recorded: a bootstrap percentile interval and a normal
    approximation disagree materially on the fat-tailed, skewed distributions that
    trading results actually have, and a reader deserves to know which they are looking
    at.
    """

    low: Decimal
    high: Decimal
    confidence: Decimal = Decimal("0.95")
    method: str = "bootstrap_percentile"
    iterations: int | None = None

    @property
    def width(self) -> Decimal:
        return self.high - self.low

    def contains(self, value: Decimal) -> bool:
        return self.low <= value <= self.high

    @property
    def excludes_zero(self) -> bool:
        """Whether the interval is entirely on one side of zero.

        The honest version of "is this edge real?" — an expectancy interval spanning
        zero means the sample cannot distinguish the strategy from breakeven, however
        good the point estimate looks.
        """
        return self.low > 0 or self.high < 0


@dataclass(frozen=True, slots=True)
class Estimate:
    """A point estimate with its uncertainty and sample size."""

    value: Decimal | None
    sample_size: int
    interval: Interval | None = None
    reliability: Reliability = Reliability.INSUFFICIENT
    #: Set when the statistic could not be computed, explaining why in plain language.
    undefined_reason: str | None = None

    @classmethod
    def undefined(cls, sample_size: int, reason: str) -> Estimate:
        return cls(
            value=None,
            sample_size=sample_size,
            reliability=Reliability.for_sample(sample_size),
            undefined_reason=reason,
        )

    @classmethod
    def of(
        cls, value: Decimal, sample_size: int, interval: Interval | None = None
    ) -> Estimate:
        return cls(
            value=value,
            sample_size=sample_size,
            interval=interval,
            reliability=Reliability.for_sample(sample_size),
        )

    @property
    def is_defined(self) -> bool:
        return self.value is not None

    def to_payload(self) -> dict[str, Any]:
        """Serialise for storage and for the AI layer's evidence payload.

        Decimals become strings: this payload is stored as JSONB and read by a model,
        and a float here would reintroduce the imprecision the engine avoided.
        """
        payload: dict[str, Any] = {
            "value": str(self.value) if self.value is not None else None,
            "sample_size": self.sample_size,
            "reliability": self.reliability.value,
        }
        if self.undefined_reason:
            payload["undefined_reason"] = self.undefined_reason
        if self.interval is not None:
            payload["interval"] = {
                "low": str(self.interval.low),
                "high": str(self.interval.high),
                "confidence": str(self.interval.confidence),
                "method": self.interval.method,
                "excludes_zero": self.interval.excludes_zero,
            }
        return payload
