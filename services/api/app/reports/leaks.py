"""Quantifying what a trader's leaks cost, without billing the same dollars twice.

This module exists because of one sentence in ADR 0006, on pattern detection:

    "Detectors are **not** independent of each other. An injected late-session leak fires
    overtrading, trading-while-down and after-a-losing-streak together, because a trader
    down on the day late in the session is in all three states at once."

Milestone 8 recorded that and handled it by ranking rather than summing. A report cannot
duck it the same way, because the headline figure a periodic report exists to produce is
exactly the sum: *what did my mistakes cost me this month?*

Adding the detectors' estimates produces a number that is wrong in the worst possible
direction. Three detectors firing on the same forty trades each estimate the full cost of
those forty trades, so the total is roughly three times the money that was actually lost.
A trader reading "your leaks cost you $18,000 this quarter" when the figure is $6,000
will restructure their trading around a fiction — and the error is invisible, because
every input to it was computed correctly.

**The fix is to attribute cost per trade, not per detector.** Each behaviour names the
trades it was present on. The union of those trades is the set that any leak touched; the
cost attributed to each trade is counted once, and the overlap is reported rather than
hidden, because "three of your leaks are the same forty trades seen three ways" is more
useful than any of the three individually.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.analytics.behaviours import BehaviourFinding, Unit
from app.analytics.statistics import divide


@dataclass(frozen=True, slots=True)
class LeakAttribution:
    """One established leak, with what it uniquely accounts for.

    Attributes:
        estimated_cost: The detector's own estimate — what this behaviour cost across
            every trade it appeared on, ignoring other detectors.
        exclusive_cost: What it cost on trades **no other established leak touched**.
            The part of the estimate that would survive fixing every other leak first.
        shared_trades: How many of its trades another established leak also claims.
    """

    kind: str
    label: str
    affected: int
    estimated_cost: Decimal | None
    exclusive_trades: int
    exclusive_cost: Decimal | None
    shared_trades: int

    @property
    def is_wholly_shared(self) -> bool:
        """Whether every trade this leak names is also named by another.

        Such a leak is not necessarily spurious — it may be the better description of the
        same behaviour — but it adds nothing to the total, and presenting it as a separate
        item to fix would have the trader chase the same forty trades twice.
        """
        return self.affected > 0 and self.exclusive_trades == 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "affected": self.affected,
            "estimated_cost": _s(self.estimated_cost),
            "exclusive_trades": self.exclusive_trades,
            "exclusive_cost": _s(self.exclusive_cost),
            "shared_trades": self.shared_trades,
            "is_wholly_shared": self.is_wholly_shared,
        }


@dataclass(frozen=True, slots=True)
class LeakSummary:
    """What every established leak cost together, counted once.

    Attributes:
        total_cost: The deduplicated figure. This is the number the report headlines and
            the only one the AI layer may cite as "what your leaks cost".
        naive_sum: What adding the detectors' estimates would have produced. Carried
            **specifically so the gap is visible**: a reader who sees the two side by
            side understands that overlap is real, and a future change that starts
            summing has an obviously wrong number to explain.
        attributions: Per-leak breakdown, ordered by exclusive cost.
        trades_touched: Size of the union — how many trades any established leak
            appeared on.
    """

    total_cost: Decimal | None
    naive_sum: Decimal | None
    attributions: tuple[LeakAttribution, ...]
    trades_touched: int
    trades_considered: int
    #: Set when nothing could be quantified, in plain language.
    undefined_reason: str | None = None

    @property
    def overstatement_factor(self) -> Decimal | None:
        """How many times over the naive sum would have billed the same money.

        Reported because it is the single most compelling argument for this module
        existing. On overlapping detectors it lands near the number of detectors that
        fired.
        """
        if self.total_cost is None or self.naive_sum is None or self.total_cost == 0:
            return None
        return divide(self.naive_sum, self.total_cost)

    @property
    def coverage(self) -> Decimal | None:
        if self.trades_considered == 0:
            return None
        return Decimal(self.trades_touched) / Decimal(self.trades_considered)

    def to_payload(self) -> dict[str, Any]:
        return {
            "total_cost": _s(self.total_cost),
            "naive_sum": _s(self.naive_sum),
            "overstatement_factor": _s(self.overstatement_factor),
            "trades_touched": self.trades_touched,
            "trades_considered": self.trades_considered,
            "coverage": _s(self.coverage),
            "leaks": [item.to_payload() for item in self.attributions],
            "undefined_reason": self.undefined_reason,
            "interpretation": (
                "total_cost counts each trade once, however many leaks it belongs to. "
                "naive_sum is what adding the individual estimates would have given; "
                "the gap between them is overlap, not disagreement. Costs are what "
                "these behaviours were associated with over the period, not a forecast "
                "and not a recoverable amount."
            ),
        }


def quantify(
    findings: Sequence[BehaviourFinding],
    *,
    net_pnl_by_trade: dict[str, Decimal],
) -> LeakSummary:
    """Deduplicate the cost of every established leak across the trades they share.

    Only findings that are **actionable** (significant after the scan-wide correction)
    and whose polarity is ``leak`` are counted. Including unestablished ones would put
    the loudest coincidence in the scan into the trader's headline figure, which is the
    behaviour ADR 0004 exists to prevent — and a report is the place it would do the most
    damage, because reports are read as settled fact.

    Args:
        net_pnl_by_trade: Realized P&L per trade id over the period. Attribution needs
            per-trade money; a detector's mean difference cannot be split across its
            trades without it.
    """
    leaks = [
        finding
        for finding in findings
        if finding.is_actionable
        and finding.polarity == "leak"
        and finding.unit is Unit.CURRENCY
        and finding.affected_trade_ids
    ]

    considered = len(net_pnl_by_trade)
    if not leaks:
        return LeakSummary(
            total_cost=None,
            naive_sum=None,
            attributions=(),
            trades_touched=0,
            trades_considered=considered,
            undefined_reason=(
                "no behaviour was established as a leak over this period, so there is "
                "nothing to attribute a cost to"
            ),
        )

    # Per-trade cost share: a detector's estimated cost spread evenly over the trades it
    # named. Even spreading is a choice and a defensible one — the detector's evidence is
    # a difference in *means*, which says nothing about which of its trades carried more
    # of it, and any weighting by outcome would attribute the most cost to the worst
    # trades regardless of whether the behaviour caused them.
    share_by_trade: dict[str, Decimal] = {}
    claims: dict[str, int] = {}
    for finding in leaks:
        cost = finding.estimated_cost
        if cost is None or not finding.affected_trade_ids:
            continue
        per_trade = cost / Decimal(len(finding.affected_trade_ids))
        for trade_id in finding.affected_trade_ids:
            claims[trade_id] = claims.get(trade_id, 0) + 1
            # A trade claimed by several leaks keeps the largest single claim rather
            # than their sum. The alternative — averaging — would let a weak third
            # detector dilute a strong finding, and summing is the bug this file exists
            # to prevent.
            existing = share_by_trade.get(trade_id)
            if existing is None or abs(per_trade) > abs(existing):
                share_by_trade[trade_id] = per_trade

    if not share_by_trade:
        return LeakSummary(
            total_cost=None,
            naive_sum=None,
            attributions=(),
            trades_touched=0,
            trades_considered=considered,
            undefined_reason="no established leak carried a cost measured in currency",
        )

    total = sum(share_by_trade.values(), Decimal(0))
    naive = sum(
        (finding.estimated_cost for finding in leaks if finding.estimated_cost is not None),
        Decimal(0),
    )

    attributions: list[LeakAttribution] = []
    for finding in leaks:
        exclusive_ids = [
            trade_id for trade_id in finding.affected_trade_ids if claims.get(trade_id) == 1
        ]
        exclusive_cost: Decimal | None = None
        if finding.estimated_cost is not None and finding.affected_trade_ids:
            per_trade = finding.estimated_cost / Decimal(len(finding.affected_trade_ids))
            exclusive_cost = per_trade * Decimal(len(exclusive_ids))

        attributions.append(
            LeakAttribution(
                kind=finding.kind,
                label=finding.label,
                affected=finding.affected,
                estimated_cost=finding.estimated_cost,
                exclusive_trades=len(exclusive_ids),
                exclusive_cost=exclusive_cost,
                shared_trades=finding.affected - len(exclusive_ids),
            )
        )

    # Most negative exclusive cost first: the leak with the most money nobody else can
    # claim is the one worth fixing first.
    attributions.sort(
        key=lambda item: (item.exclusive_cost is None, item.exclusive_cost or Decimal(0))
    )

    return LeakSummary(
        total_cost=total,
        naive_sum=naive,
        attributions=tuple(attributions),
        trades_touched=len(share_by_trade),
        trades_considered=considered,
    )


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
