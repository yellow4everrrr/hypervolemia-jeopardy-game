"""Replay window and marker computation.

A replay is a chart window plus a set of overlays that let a trader re-watch their own
decision. Both are computed here, once, and stored — so a trade reviewed today and
again in a year shows the same chart, the same context, and the same markers. A replay
that silently re-derives its window would change as surrounding data arrives, which
makes "why did I take this?" unanswerable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.core.errors import DomainError
from app.domain.common.enums import Direction, Timeframe

#: Bars of context before entry. Enough to see the setup that produced the trade — an
#: entry with no preceding structure is unreviewable.
DEFAULT_BARS_BEFORE = 120

#: Bars after exit. Enough to answer "what happened next?", which is where the
#: management lesson usually is.
DEFAULT_BARS_AFTER = 60

#: Execution timeframe chosen from holding period, so a 40-second scalp is not replayed
#: on a chart where the whole trade is one candle.
_TIMEFRAME_BY_DURATION: tuple[tuple[int, Timeframe], ...] = (
    (120, Timeframe.S15),
    (900, Timeframe.M1),
    (3_600, Timeframe.M2),
    (14_400, Timeframe.M5),
    (86_400, Timeframe.M15),
    (604_800, Timeframe.H1),
)

#: The higher timeframe shown beside it, for context.
_HIGHER_TIMEFRAME = {
    Timeframe.S15: Timeframe.M5,
    Timeframe.M1: Timeframe.M15,
    Timeframe.M2: Timeframe.M30,
    Timeframe.M5: Timeframe.H1,
    Timeframe.M15: Timeframe.H4,
    Timeframe.H1: Timeframe.D1,
}


class MarkerKind(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    SCALE_IN = "scale_in"
    SCALE_OUT = "scale_out"
    STOP = "stop"
    TARGET = "target"
    MAE = "mae"
    MFE = "mfe"


@dataclass(frozen=True, slots=True)
class Marker:
    """One overlay on the replay chart."""

    kind: MarkerKind
    at: datetime | None
    price: Decimal
    label: str
    quantity: Decimal | None = None
    #: Horizontal lines (stop, target) have no single moment; they span the trade.
    spans_trade: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "at": self.at.isoformat() if self.at else None,
            "price": str(self.price),
            "label": self.label,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "spans_trade": self.spans_trade,
        }


@dataclass(frozen=True, slots=True)
class ReplayWindow:
    """Everything needed to render one trade's replay."""

    instrument_symbol: str
    primary_timeframe: Timeframe
    higher_timeframe: Timeframe
    window_start: datetime
    window_end: datetime
    trade_start: datetime
    trade_end: datetime
    bars_before: int
    bars_after: int
    markers: tuple[Marker, ...] = ()
    #: The rectangle from entry to stop, shaded as the amount at risk. Absent when the
    #: trade has no recorded stop — an invented risk box would misrepresent the plan.
    risk_box: dict[str, Any] | None = None

    @property
    def duration(self) -> timedelta:
        return self.trade_end - self.trade_start

    def to_payload(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument_symbol,
            "primary_timeframe": self.primary_timeframe.value,
            "higher_timeframe": self.higher_timeframe.value,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "trade_start": self.trade_start.isoformat(),
            "trade_end": self.trade_end.isoformat(),
            "bars_before": self.bars_before,
            "bars_after": self.bars_after,
            "markers": [marker.to_payload() for marker in self.markers],
            "risk_box": self.risk_box,
        }


def choose_timeframe(duration_seconds: int) -> tuple[Timeframe, Timeframe]:
    """Pick execution and context timeframes from a holding period.

    A 40-second scalp replayed on 5-minute candles is one candle and teaches nothing;
    a three-day swing on 15-second candles is 17,000 bars and renders like static.
    """
    for threshold, timeframe in _TIMEFRAME_BY_DURATION:
        if duration_seconds <= threshold:
            return timeframe, _HIGHER_TIMEFRAME[timeframe]
    return Timeframe.H1, Timeframe.D1


def build_window(
    *,
    instrument_symbol: str,
    opened_at: datetime,
    closed_at: datetime | None,
    duration_seconds: int | None,
    bars_before: int = DEFAULT_BARS_BEFORE,
    bars_after: int = DEFAULT_BARS_AFTER,
    now: datetime | None = None,
) -> ReplayWindow:
    """Compute the chart window for one trade.

    An open trade's window runs to ``now``; a closed trade's to its exit plus context.
    """
    if opened_at.tzinfo is None:
        raise DomainError("opened_at must be timezone-aware")

    trade_end = closed_at or now or opened_at
    if trade_end < opened_at:
        raise DomainError("a trade cannot close before it opens")

    held = duration_seconds
    if held is None:
        held = int((trade_end - opened_at).total_seconds())

    primary, higher = choose_timeframe(max(held, 1))
    step = timedelta(seconds=primary.seconds)

    return ReplayWindow(
        instrument_symbol=instrument_symbol,
        primary_timeframe=primary,
        higher_timeframe=higher,
        window_start=opened_at - step * bars_before,
        window_end=trade_end + step * bars_after,
        trade_start=opened_at,
        trade_end=trade_end,
        bars_before=bars_before,
        bars_after=bars_after,
    )


@dataclass(frozen=True, slots=True)
class LegInput:
    """One fill on the trade, as the replay needs it."""

    at: datetime
    price: Decimal
    quantity: Decimal
    is_entry: bool


def build_markers(
    *,
    direction: Direction,
    legs: list[LegInput],
    stop_price: Decimal | None = None,
    target_price: Decimal | None = None,
    mae_price: Decimal | None = None,
    mfe_price: Decimal | None = None,
) -> tuple[Marker, ...]:
    """Overlays for one trade.

    Scale-ins and scale-outs get their own marker kinds rather than being drawn as more
    entries and exits: seeing *where* size was added is most of the point of replaying a
    scaled trade.
    """
    entries = [leg for leg in legs if leg.is_entry]
    exits = [leg for leg in legs if not leg.is_entry]
    markers: list[Marker] = []

    for index, leg in enumerate(sorted(entries, key=lambda leg: leg.at)):
        first = index == 0
        markers.append(
            Marker(
                kind=MarkerKind.ENTRY if first else MarkerKind.SCALE_IN,
                at=leg.at,
                price=leg.price,
                label=(
                    f"{'Long' if direction is Direction.LONG else 'Short'} {leg.quantity}"
                    if first
                    else f"Add {leg.quantity}"
                ),
                quantity=leg.quantity,
            )
        )

    ordered_exits = sorted(exits, key=lambda leg: leg.at)
    for index, leg in enumerate(ordered_exits):
        last = index == len(ordered_exits) - 1
        markers.append(
            Marker(
                kind=MarkerKind.EXIT if last else MarkerKind.SCALE_OUT,
                at=leg.at,
                price=leg.price,
                label=f"Exit {leg.quantity}" if last else f"Trim {leg.quantity}",
                quantity=leg.quantity,
            )
        )

    if stop_price is not None:
        markers.append(
            Marker(MarkerKind.STOP, None, stop_price, "Initial stop", spans_trade=True)
        )
    if target_price is not None:
        markers.append(
            Marker(MarkerKind.TARGET, None, target_price, "Target", spans_trade=True)
        )
    if mae_price is not None:
        markers.append(Marker(MarkerKind.MAE, None, mae_price, "Worst excursion", spans_trade=True))
    if mfe_price is not None:
        markers.append(Marker(MarkerKind.MFE, None, mfe_price, "Best excursion", spans_trade=True))

    return tuple(markers)


def build_risk_box(
    *,
    direction: Direction,
    entry_price: Decimal,
    stop_price: Decimal | None,
    opened_at: datetime,
    closed_at: datetime | None,
) -> dict[str, Any] | None:
    """The shaded rectangle from entry to stop.

    ``None`` without a recorded stop. Drawing a default-sized box would put an invented
    plan on the chart and let the trader review a trade they did not take.
    """
    if stop_price is None:
        return None

    risk_points = (entry_price - stop_price) * direction.signed_multiplier
    if risk_points <= 0:
        # A stop on the wrong side is a data error, not a tiny risk.
        return None

    return {
        "from": opened_at.isoformat(),
        "to": (closed_at or opened_at).isoformat(),
        "entry_price": str(entry_price),
        "stop_price": str(stop_price),
        "risk_points": str(risk_points),
        "direction": direction.value,
    }
