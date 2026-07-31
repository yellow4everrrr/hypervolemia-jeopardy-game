"""Period boundaries, and what is due.

Reports are the only part of the product where the *calendar* decides what gets grouped
together, which introduces a class of error nothing upstream has to deal with.

**A period is a sample, not a natural unit.** A trader does not change on the first of
the month. Grouping by calendar month is a convention chosen because humans think in
months, and everything computed inside one inherits that arbitrariness: "your best month"
is the largest of twelve draws from the same process far more often than it is a month
the trader did something differently.

The module is deliberately small and pure. Everything here is date arithmetic with no
knowledge of trades, so the rules that decide which trades belong to a period can be
tested exhaustively without a database — and so the boundary logic lives in exactly one
place rather than being re-derived in each report type.

**Session dates, not timestamps.** Trades carry a ``session_date`` already resolved
through the exchange's timezone by milestone 1. Periods are therefore compared against
that date and never against ``opened_at``, which would put a 23:30 UTC Sunday fill in the
wrong week — and would do it silently, since the resulting report still looks complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.domain.common.enums import ReportType

#: Report types this module knows how to bound. ``COMPLIANCE`` is a report *shape* rather
#: than a calendar period — it is generated over whatever window the caller asks for —
#: so it is deliberately absent and callers must supply explicit dates for it.
PERIODIC_TYPES: tuple[ReportType, ...] = (
    ReportType.DAILY,
    ReportType.WEEKLY,
    ReportType.MONTHLY,
    ReportType.QUARTERLY,
    ReportType.ANNUAL,
)

#: How many trading sessions a period needs before its report attempts any comparison.
#: Below this the report still renders — the trader is owed their numbers — but it draws
#: no conclusions, and says why.
MIN_SESSIONS_FOR_CONCLUSIONS: dict[ReportType, int] = {
    ReportType.DAILY: 1,
    ReportType.WEEKLY: 3,
    ReportType.MONTHLY: 8,
    ReportType.QUARTERLY: 20,
    ReportType.ANNUAL: 60,
}


@dataclass(frozen=True, slots=True)
class Period:
    """A closed date range, inclusive at both ends.

    Inclusive-end rather than half-open because these values are shown to a trader and
    stored in a table whose constraint reads ``period_end >= period_start``. A
    half-open convention would put 1 February in January's report on the screen the one
    time somebody read the boundary wrong, and the error would be invisible in aggregate.
    """

    report_type: ReportType
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("a period cannot end before it starts")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, session: date) -> bool:
        return self.start <= session <= self.end

    @property
    def label(self) -> str:
        """Human label, in the form the report headline uses."""
        if self.report_type is ReportType.DAILY:
            return self.start.isoformat()
        if self.report_type is ReportType.MONTHLY:
            return self.start.strftime("%B %Y")
        if self.report_type is ReportType.QUARTERLY:
            return f"Q{(self.start.month - 1) // 3 + 1} {self.start.year}"
        if self.report_type is ReportType.ANNUAL:
            return str(self.start.year)
        return f"{self.start.isoformat()} to {self.end.isoformat()}"

    def to_payload(self) -> dict[str, str]:
        return {
            "type": self.report_type.value,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
        }


def period_containing(report_type: ReportType, session: date) -> Period:
    """The period of the given type that a session date falls in.

    Weeks run Monday to Sunday (ISO). Choosing Sunday-start instead would move a Sunday
    session into the following week's report, and for futures — where the trading week
    opens on Sunday evening — that is the wrong side of the boundary.
    """
    if report_type is ReportType.DAILY:
        return Period(report_type, session, session)

    if report_type is ReportType.WEEKLY:
        start = session - timedelta(days=session.weekday())
        return Period(report_type, start, start + timedelta(days=6))

    if report_type is ReportType.MONTHLY:
        start = session.replace(day=1)
        return Period(report_type, start, _last_day_of_month(start))

    if report_type is ReportType.QUARTERLY:
        first_month = ((session.month - 1) // 3) * 3 + 1
        start = date(session.year, first_month, 1)
        end = _last_day_of_month(date(session.year, first_month + 2, 1))
        return Period(report_type, start, end)

    if report_type is ReportType.ANNUAL:
        return Period(report_type, date(session.year, 1, 1), date(session.year, 12, 31))

    raise ValueError(f"{report_type.value} is not a calendar period; supply explicit dates")


def previous(period: Period) -> Period:
    """The period of the same type immediately before this one.

    Used for the period-over-period comparison. Deliberately the *adjacent* period rather
    than "the same month last year": a year-ago comparison spans a different market
    regime and a different trader, and the difference between them is not attributable to
    anything the report can name.
    """
    return period_containing(period.report_type, period.start - timedelta(days=1))


def _last_day_of_month(first: date) -> date:
    if first.month == 12:
        return date(first.year, 12, 31)
    return date(first.year, first.month + 1, 1) - timedelta(days=1)


def is_complete(period: Period, *, today: date) -> bool:
    """Whether the period has finished.

    An incomplete period must not be reported on as though it were finished. A month
    report generated on the 9th describes nine days while being labelled with the month,
    and every figure in it — expectancy, drawdown, the comparison against last month —
    reads as a claim about the whole. The scheduler only offers completed periods.
    """
    return period.end < today


def due_periods(
    *,
    today: date,
    earliest_session: date | None,
    latest_session: date | None,
    already_generated: set[tuple[ReportType, date]],
    types: tuple[ReportType, ...] = PERIODIC_TYPES,
) -> list[Period]:
    """Completed periods that contain trading activity and have no report yet.

    Three filters, and each removes a distinct kind of useless report:

    * **Completed only** — see :func:`is_complete`.
    * **Within the trading history** — a trader who started in March gets no report for
      January. Generating empty reports for every period since the epoch would bury the
      real ones, and "you took no trades" is not a finding.
    * **Not already generated** — keyed on ``(type, period_start)``, matching the unique
      constraint on the table, so the scheduler is idempotent. Re-running it produces no
      duplicates, which matters because it will be re-run after every failure.

    Note this returns periods *spanned* by the history, not periods that certainly
    contain trades: a trader who traded in March and May still gets an April report, and
    that report will say the month was flat. That is deliberate — a gap in a monthly
    series is information, and silently omitting April would make the series lie about
    its own continuity.
    """
    if earliest_session is None or latest_session is None:
        return []

    due: list[Period] = []
    for report_type in types:
        cursor = period_containing(report_type, earliest_session)
        final = period_containing(report_type, latest_session)
        while cursor.start <= final.start:
            if is_complete(cursor, today=today) and (
                report_type,
                cursor.start,
            ) not in already_generated:
                due.append(cursor)
            cursor = period_containing(report_type, cursor.end + timedelta(days=1))

    due.sort(key=lambda item: (item.start, item.report_type.value))
    return due
