"""Composing a periodic report from the engines that already exist.

This module computes nothing itself. Every figure in a report comes from the analytics
engine, the pattern scan, the compliance engine or the leak attribution in
:mod:`app.reports.leaks`, and this file only decides what goes in and how it is framed.

That separation is deliberate and it is the main defence against a familiar failure: a
reporting layer that starts by formatting numbers and ends up re-deriving them, at which
point the monthly report's win rate and the dashboard's win rate disagree by a trade and
nobody can say which is right.

**An annual report is computed from the year's trades, never from twelve monthly
reports.** Aggregating sub-reports is the intuitive implementation and it is wrong in two
independent ways. A leak present all year is established once per month, so summing the
monthly costs bills it twelve times — the same double-counting :mod:`app.reports.leaks`
exists to prevent, reintroduced one level up. And significance does not aggregate at all:
twelve non-significant monthly differences do not combine into an annual finding, nor
does one significant month make an annual one. The year is its own sample.

**A report is a record of what was believed on a date.** It stores the evidence it was
built from, not a reference to live data. Re-running analytics next month would change
last month's report, and a report whose contents move is not a record of anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.analytics.discovery import DiscoveryConfig, PatternReport, discover_patterns
from app.analytics.engine import AnalyticsConfig, analyse
from app.analytics.types import MIN_SAMPLE, TradeRecord
from app.domain.common.enums import ReportType
from app.reports.comparison import PeriodComparison, compare_periods
from app.reports.leaks import LeakSummary, quantify
from app.reports.periods import MIN_SESSIONS_FOR_CONCLUSIONS, Period, previous

#: Bumped whenever the composition, the comparison set or the leak attribution changes.
#: Stored on every report: two reports built by different versions are not comparable,
#: and a reader looking at a year of monthly reports needs to know where the break is.
REPORT_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReportSection:
    """One block of a report, carrying its own sufficiency verdict.

    Sections refuse individually rather than the report refusing as a whole. A month with
    enough trades for performance statistics but too few for a pattern scan should show
    the statistics and say why the patterns are missing — dropping the whole report would
    withhold numbers the trader is entitled to, and rendering an empty pattern list would
    imply the scan found nothing.
    """

    key: str
    title: str
    payload: dict[str, Any]
    #: ``None`` when the section is complete; otherwise why it could not be produced.
    withheld_reason: str | None = None

    @property
    def is_present(self) -> bool:
        return self.withheld_reason is None

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "is_present": self.is_present,
            "withheld_reason": self.withheld_reason,
            **({"data": self.payload} if self.is_present else {}),
        }


@dataclass
class BuiltReport:
    """A complete periodic report, ready to store.

    ``draws_conclusions`` is separate from having data. A daily report on four trades
    shows the four trades honestly and concludes nothing, and that is the correct
    product: a "finding" from four trades is the loudest of four coin flips.
    """

    period: Period
    sessions: int
    trades: int
    sections: list[ReportSection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    draws_conclusions: bool = True
    version: int = REPORT_VERSION
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def section(self, key: str) -> ReportSection | None:
        return next((item for item in self.sections if item.key == key), None)

    @property
    def headline(self) -> str:
        """One sentence stating what the period was, before any interpretation."""
        if self.trades == 0:
            return f"No trades were taken in {self.period.label}."
        performance = self.section("performance")
        if performance is None or not performance.is_present:
            return f"{self.trades} trades over {self.sessions} sessions in {self.period.label}."
        net = performance.payload.get("net_pnl")
        return (
            f"{self.trades} trades over {self.sessions} sessions in "
            f"{self.period.label}, netting {net}."
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "period": self.period.to_payload(),
            "version": self.version,
            "generated_at": self.generated_at.isoformat(),
            "sessions": self.sessions,
            "trades": self.trades,
            "headline": self.headline,
            "draws_conclusions": self.draws_conclusions,
            "sections": [section.to_payload() for section in self.sections],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class BuildConfig:
    """Knobs for the expensive parts, so tests and workers can differ."""

    analytics: AnalyticsConfig | None = None
    discovery: DiscoveryConfig | None = None
    include_patterns: bool = True
    comparison_permutations: int | None = None


def build(
    period: Period,
    trades: Sequence[TradeRecord],
    *,
    previous_trades: Sequence[TradeRecord] = (),
    config: BuildConfig | None = None,
) -> BuiltReport:
    """Compose the report for one period.

    Pure: no database, no clock beyond ``generated_at``, no configuration from the
    environment. The same trades always produce the same report, which is what makes a
    stored report reproducible and its tests exact.
    """
    settings = config or BuildConfig()
    sessions = len({trade.session_date for trade in trades if trade.session_date is not None})
    required = MIN_SESSIONS_FOR_CONCLUSIONS.get(period.report_type, 1)

    report = BuiltReport(
        period=period,
        sessions=sessions,
        trades=len(trades),
        draws_conclusions=sessions >= required and len(trades) >= MIN_SAMPLE,
    )

    if not trades:
        report.notes.append(
            f"No trades were taken in {period.label}. The period is reported as flat "
            "rather than skipped, because a gap in a series is information."
        )
        return report

    if not report.draws_conclusions:
        report.notes.append(
            f"{period.label} covers {sessions} trading session(s) and {len(trades)} "
            f"trade(s). That is below the {required} sessions this report type needs "
            "before it will draw conclusions, so the numbers below are shown without "
            "interpretation — at this sample size the largest difference in any set is "
            "usually the noisiest one, not the most important."
        )

    report.sections.append(_performance_section(trades, settings))
    report.sections.append(
        _comparison_section(period, trades, previous_trades, report.draws_conclusions, settings)
    )

    patterns = _pattern_scan(trades, report.draws_conclusions, settings)
    report.sections.append(_pattern_section(patterns, report.draws_conclusions))
    report.sections.append(_leak_section(patterns, trades, report.draws_conclusions))

    return report


def _performance_section(
    trades: Sequence[TradeRecord], settings: BuildConfig
) -> ReportSection:
    """The deterministic numbers. Always present when there are trades.

    Shown even when the report draws no conclusions: "you took six trades and lost $400"
    is a fact, and withholding it because six trades cannot support an inference would
    be withholding the trader's own history from them.
    """
    analytics = analyse(trades, settings.analytics)
    payload = analytics.to_payload()
    return ReportSection(
        key="performance",
        title="Performance",
        payload={
            "net_pnl": str(analytics.core.pnl.net_pnl),
            "trades": analytics.sample_size,
            **payload,
        },
    )


def _comparison_section(
    period: Period,
    trades: Sequence[TradeRecord],
    previous_trades: Sequence[TradeRecord],
    draws_conclusions: bool,
    settings: BuildConfig,
) -> ReportSection:
    prior = previous(period)

    if not draws_conclusions:
        return ReportSection(
            key="comparison",
            title=f"Against {prior.label}",
            payload={},
            withheld_reason=(
                "this period is too small to compare against the one before it; a "
                "difference computed from it would be measuring sample size, not change"
            ),
        )

    if not previous_trades:
        return ReportSection(
            key="comparison",
            title=f"Against {prior.label}",
            payload={},
            withheld_reason=(
                f"there are no trades in {prior.label} to compare against — this is the "
                "first period with activity"
            ),
        )

    comparison: PeriodComparison = compare_periods(
        period,
        prior,
        trades,
        previous_trades,
        permutations=settings.comparison_permutations,
    )
    return ReportSection(
        key="comparison",
        title=f"Against {prior.label}",
        payload=comparison.to_payload(),
    )


def _pattern_scan(
    trades: Sequence[TradeRecord], draws_conclusions: bool, settings: BuildConfig
) -> PatternReport | None:
    if not draws_conclusions or not settings.include_patterns:
        return None
    return discover_patterns(trades, settings.discovery)


def _pattern_section(patterns: PatternReport | None, draws_conclusions: bool) -> ReportSection:
    if patterns is None:
        return ReportSection(
            key="patterns",
            title="Patterns",
            payload={},
            withheld_reason=(
                "the period is too small for a pattern scan"
                if not draws_conclusions
                else "pattern scanning was not run for this report"
            ),
        )
    return ReportSection(key="patterns", title="Patterns", payload=patterns.to_payload())


def _leak_section(
    patterns: PatternReport | None,
    trades: Sequence[TradeRecord],
    draws_conclusions: bool,
) -> ReportSection:
    """What the established leaks cost, counted once.

    The headline figure of any periodic report, and the one most easily inflated — see
    :mod:`app.reports.leaks` for why adding the detectors' estimates produces a number
    several times too large.
    """
    if patterns is None:
        return ReportSection(
            key="leaks",
            title="What your leaks cost",
            payload={},
            withheld_reason=(
                "leak cost is attributed from the pattern scan, which was not run for "
                "this period"
            ),
        )

    summary: LeakSummary = quantify(
        patterns.behaviours,
        net_pnl_by_trade={str(trade.trade_id): trade.net_pnl for trade in trades},
    )
    return ReportSection(
        key="leaks", title="What your leaks cost", payload=summary.to_payload()
    )


def annual_note(report_type: ReportType) -> str | None:
    """The warning that belongs on every long-period report.

    Stated in the payload rather than assumed, because the natural reading of an annual
    figure is that it is the sum of the monthly ones, and it is not.
    """
    if report_type not in (ReportType.QUARTERLY, ReportType.ANNUAL):
        return None
    return (
        "This report is computed from the whole period's trades, not aggregated from "
        "the shorter reports inside it. Its figures will not equal their sum: a leak "
        "present throughout the period is counted once here and once per shorter "
        "report, and significance does not add up — several inconclusive months do not "
        "combine into a finding, and one conclusive month does not make the year one."
    )


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
