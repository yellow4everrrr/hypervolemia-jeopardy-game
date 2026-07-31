"""Comparing a period against the one before it, honestly.

Every trading journal ships this feature and almost all of them ship it wrong. The
standard implementation computes a metric for both periods, subtracts, and renders an
arrow. On a trader taking twenty trades a month that arrow is noise with a direction:
the win rate will move four or five points between any two months of an unchanged
process, and a green arrow will appear about half the time.

The damage is specific. A trader shown "win rate ↑ 4 points" concludes that whatever they
changed last month worked, and keeps doing it. A trader shown "expectancy ↓ $30"
concludes something is broken and changes a process that was fine. Both conclusions are
drawn from nothing, and a *monthly* report delivers them twelve times a year.

**So a change is not reported as a change until it beats its own null.** This is the same
rule as the segmentation cube ([ADR 0004](../../../docs/adr/0004-analytics-honesty.md)),
the pattern scan (ADR 0006) and the what-if sweep (ADR 0008), applied to the one place
where the comparison is the entire point of the feature.

Two consequences worth stating plainly, because they make the feature look worse and are
correct anyway:

* **Almost nothing will be significant on a monthly report.** Two months of twenty trades
  cannot distinguish a four-point win-rate change from chance. The honest rendering is
  "48% this month against 44% last month — with 21 and 19 trades, chance produces a gap
  that size most of the time", which tells the trader something true.
* **Every extra metric compared is another chance to clear the threshold.** Comparing
  eight metrics at p < 0.05 finds roughly one "change" per report on an unchanged trader,
  so the comparison set is a single FDR-corrected family, kept short on purpose.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from app.analytics.montecarlo import DEFAULT_SEED
from app.analytics.significance import (
    ComparisonResult,
    compare_samples,
    control_false_discovery_rate,
)
from app.analytics.types import MIN_SAMPLE, TradeRecord
from app.reports.periods import Period


@dataclass(frozen=True, slots=True)
class ComparedMetric:
    """One metric the period comparison tests, and how to say it out loud.

    ``unit`` exists because the mean of a per-trade value carries the full precision of
    the division that produced it, and every consumer of these numbers was printing that
    precision verbatim. A monthly report read "average P&L per trade was
    178.88686131386861313868613 against 144.43359375" — arithmetically exact, and
    unreadable. Worse, the sentence is not only rendered: it is stored on the report row
    and handed to the AI layer as ground truth, so the noise propagated to everything
    downstream of it.

    Rounding for *display* is not the same as rounding the computation. The comparison,
    the permutation test and the stored value all keep full precision; only the sentence
    and the payload's display fields are quantised, at the last possible moment.
    """

    key: str
    label: str
    extract: Callable[[TradeRecord], Decimal | None]
    unit: str


#: How many decimal places each unit is worth reading to, and what to append. Seconds are
#: whole because a tenth of a second of average holding time is not a fact about trading.
_DISPLAY: dict[str, tuple[int, str]] = {
    "currency": (2, ""),
    "ratio": (4, ""),
    "r": (3, "R"),
    "seconds": (0, "s"),
    "count": (2, ""),
}


def display_value(value: Decimal | None, unit: str) -> str:
    """A metric as a person would read it. Never used as an input to anything."""
    if value is None:
        return "not available"
    places, suffix = _DISPLAY.get(unit, (2, ""))
    quantum = Decimal(1).scaleb(-places)
    return f"{value.quantize(quantum)}{suffix}"


#: The metrics compared across periods. Short deliberately — every addition raises the
#: bar for all the others through the family-wide correction, and a report that compares
#: thirty metrics establishes none of them.
#:
#: Each entry maps a trade to the per-trade value whose *mean* is the metric. That
#: restriction is what makes the permutation test valid: shuffling period labels tests
#: whether the two sets of per-trade values came from one distribution. A metric that is
#: not a mean of per-trade values — profit factor, max drawdown, Sharpe — cannot be
#: tested this way and is deliberately absent rather than tested wrongly.
COMPARED_METRICS: tuple[ComparedMetric, ...] = (
    ComparedMetric(
        "net_pnl_per_trade", "average P&L per trade", lambda trade: trade.net_pnl,
        unit="currency",
    ),
    ComparedMetric(
        "win_rate", "win rate",
        lambda trade: Decimal(1) if trade.is_winner else Decimal(0),
        unit="ratio",
    ),
    ComparedMetric(
        "r_multiple", "average R multiple", lambda trade: trade.r_multiple, unit="r"
    ),
    ComparedMetric(
        "duration_seconds",
        "average holding time",
        lambda trade: Decimal(trade.duration_seconds)
        if trade.duration_seconds is not None
        else None,
        unit="seconds",
    ),
    ComparedMetric(
        "quantity", "average position size", lambda trade: trade.quantity, unit="count"
    ),
)


@dataclass(frozen=True, slots=True)
class MetricChange:
    """One metric, this period against last.

    Attributes:
        comparison: The permutation test, FDR-adjusted across the whole comparison set.
            ``None`` when either period had too few trades to test at all.
        is_established: Whether the change survived correction. **This is what the
            report renders an arrow for**; ``difference`` alone never is.
    """

    key: str
    label: str
    current: Decimal | None
    previous: Decimal | None
    current_sample: int
    previous_sample: int
    comparison: ComparisonResult | None
    #: Display unit, carried from :data:`COMPARED_METRICS`. Affects how the value is
    #: written, never how it is computed or compared.
    unit: str = "currency"

    @property
    def display_current(self) -> str:
        return display_value(self.current, self.unit)

    @property
    def display_previous(self) -> str:
        return display_value(self.previous, self.unit)

    @property
    def difference(self) -> Decimal | None:
        if self.current is None or self.previous is None:
            return None
        return self.current - self.previous

    @property
    def is_established(self) -> bool:
        return bool(self.comparison and self.comparison.is_significant)

    @property
    def direction(self) -> str:
        """``up``, ``down`` or ``flat`` — and ``unchanged`` when nothing was established.

        The distinction matters more than it looks. ``flat`` says the measured values
        matched; ``unchanged`` says the measurement could not tell them apart. A UI that
        collapses the two shows a trader a settled fact where there is an open question.
        """
        if not self.is_established:
            return "unchanged"
        difference = self.difference
        if difference is None or difference == 0:
            return "flat"
        return "up" if difference > 0 else "down"

    def narrate(self) -> str:
        """Plain language, including the null result — especially the null result."""
        if self.current is None or self.previous is None:
            return f"{self.label} could not be compared: one of the periods has no value for it"

        if self.is_established:
            movement = "higher" if (self.difference or Decimal(0)) > 0 else "lower"
            return (
                f"{self.label} was {movement} than the previous period "
                f"({self.display_current} against {self.display_previous}), by more than "
                f"chance explains across {self.current_sample} and "
                f"{self.previous_sample} trades"
            )

        if self.comparison is None:
            return (
                f"{self.label} was {self.display_current} against "
                f"{self.display_previous}, but with "
                f"{self.current_sample} and {self.previous_sample} trades there is not "
                "enough data to compare the two"
            )

        return (
            f"{self.label} was {self.display_current} against "
            f"{self.display_previous} — with "
            f"{self.current_sample} and {self.previous_sample} trades, chance produces a "
            "gap that size often enough that this is not a change"
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            # Full precision is preserved alongside the rounded form rather than
            # replaced by it: the exact value is what a later computation would need, and
            # the display value is what a person should read. A UI given only the first
            # prints 26 digits; given only the second it cannot recompute anything.
            "current": _s(self.current),
            "previous": _s(self.previous),
            "difference": _s(self.difference),
            "unit": self.unit,
            "display_current": self.display_current,
            "display_previous": self.display_previous,
            "current_sample": self.current_sample,
            "previous_sample": self.previous_sample,
            "is_established": self.is_established,
            "direction": self.direction,
            "narrative": self.narrate(),
        }
        if self.comparison is not None:
            payload["p_value"] = _s(self.comparison.p_value)
            payload["adjusted_p_value"] = _s(self.comparison.adjusted_p_value)
            payload["effect_size"] = _s(self.comparison.effect_size)
        return payload


@dataclass(frozen=True, slots=True)
class PeriodComparison:
    """Every compared metric, corrected as one family."""

    current: Period
    previous: Period
    changes: tuple[MetricChange, ...]

    @property
    def established(self) -> tuple[MetricChange, ...]:
        return tuple(change for change in self.changes if change.is_established)

    def to_payload(self) -> dict[str, Any]:
        return {
            "current_period": self.current.to_payload(),
            "previous_period": self.previous.to_payload(),
            "changes": [change.to_payload() for change in self.changes],
            "established_count": len(self.established),
            "interpretation": (
                "A metric is reported as changed only when the difference survives a "
                "permutation test corrected across every metric compared. On a normal "
                "month almost nothing will be established, and that is the accurate "
                "result: two small samples cannot distinguish a real shift from the "
                "variation an unchanged process produces."
            ),
        }


def compare_periods(
    current: Period,
    previous: Period,
    current_trades: Sequence[TradeRecord],
    previous_trades: Sequence[TradeRecord],
    *,
    permutations: int | None = None,
    seed: int = DEFAULT_SEED,
) -> PeriodComparison:
    """Test every compared metric, then correct the whole set together."""
    changes: list[MetricChange] = []
    comparisons: list[ComparisonResult] = []
    #: Index into ``changes`` for each entry in ``comparisons``, so corrected results go
    #: back onto the metric that earned them.
    tested_positions: list[int] = []

    for metric in COMPARED_METRICS:
        extract = metric.extract
        current_values = [value for value in map(extract, current_trades) if value is not None]
        previous_values = [value for value in map(extract, previous_trades) if value is not None]

        comparison: ComparisonResult | None = None
        if len(current_values) >= MIN_SAMPLE and len(previous_values) >= MIN_SAMPLE:
            kwargs: dict[str, Any] = {
                "label_a": current.label,
                "label_b": previous.label,
                "seed": seed,
            }
            if permutations is not None:
                kwargs["permutations"] = permutations
            comparison = compare_samples(current_values, previous_values, **kwargs)
            comparisons.append(comparison)
            tested_positions.append(len(changes))

        changes.append(
            MetricChange(
                key=metric.key,
                label=metric.label,
                current=comparison.mean_a
                if comparison is not None
                else _mean_or_none(current_values),
                previous=comparison.mean_b
                if comparison is not None
                else _mean_or_none(previous_values),
                current_sample=len(current_values),
                previous_sample=len(previous_values),
                comparison=comparison,
                unit=metric.unit,
            )
        )

    # One family across every metric compared. Correcting them separately — or not at
    # all — would surface roughly one "change" per report on a trader who changed
    # nothing, every month, forever.
    #
    # The corrected results are spliced back rather than discarded. ``MetricChange`` is
    # frozen, so a caller that ignores the return value silently keeps
    # ``adjusted_p_value = None`` on every metric — at which point ``is_significant``
    # refuses to answer and nothing is ever established. That fails safe here, but the
    # same mistake made against mutable state would have failed *open*.
    if tested_positions:
        adjusted = control_false_discovery_rate(comparisons)
        for position, corrected in zip(tested_positions, adjusted, strict=True):
            changes[position] = replace(changes[position], comparison=corrected)

    return PeriodComparison(current=current, previous=previous, changes=tuple(changes))


def _mean_or_none(values: Sequence[Decimal]) -> Decimal | None:
    from app.analytics.statistics import mean

    return mean(values)


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
