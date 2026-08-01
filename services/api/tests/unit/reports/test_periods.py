"""Period arithmetic and the scheduler's notion of what is due."""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.common.enums import ReportType
from app.reports.periods import (
    Period,
    due_periods,
    is_complete,
    period_containing,
    previous,
)


class TestBoundaries:
    def test_a_week_runs_monday_to_sunday(self) -> None:
        """Sunday-start would move a Sunday session into the following week, and for
        futures — where the trading week opens Sunday evening — that is the wrong side."""
        period = period_containing(ReportType.WEEKLY, date(2026, 3, 5))  # a Thursday

        assert period.start == date(2026, 3, 2)
        assert period.end == date(2026, 3, 8)
        assert period.start.weekday() == 0

    def test_a_sunday_belongs_to_the_week_that_is_ending(self) -> None:
        period = period_containing(ReportType.WEEKLY, date(2026, 3, 8))

        assert period.start == date(2026, 3, 2)

    @pytest.mark.parametrize(
        ("session", "start", "end"),
        [
            (date(2026, 2, 14), date(2026, 2, 1), date(2026, 2, 28)),
            (date(2024, 2, 14), date(2024, 2, 1), date(2024, 2, 29)),
            (date(2026, 12, 31), date(2026, 12, 1), date(2026, 12, 31)),
        ],
    )
    def test_month_ends_land_on_the_real_last_day(
        self, session: date, start: date, end: date
    ) -> None:
        """Including the leap year, and including December, where naive month+1
        arithmetic overflows the year."""
        period = period_containing(ReportType.MONTHLY, session)

        assert (period.start, period.end) == (start, end)

    @pytest.mark.parametrize(
        ("month", "start", "end"),
        [
            (1, date(2026, 1, 1), date(2026, 3, 31)),
            (5, date(2026, 4, 1), date(2026, 6, 30)),
            (9, date(2026, 7, 1), date(2026, 9, 30)),
            (11, date(2026, 10, 1), date(2026, 12, 31)),
        ],
    )
    def test_quarters_cover_the_calendar_exactly(
        self, month: int, start: date, end: date
    ) -> None:
        period = period_containing(ReportType.QUARTERLY, date(2026, month, 15))

        assert (period.start, period.end) == (start, end)

    def test_a_year_is_january_to_december(self) -> None:
        period = period_containing(ReportType.ANNUAL, date(2026, 7, 31))

        assert (period.start, period.end) == (date(2026, 1, 1), date(2026, 12, 31))

    def test_periods_are_inclusive_at_both_ends(self) -> None:
        period = period_containing(ReportType.MONTHLY, date(2026, 4, 10))

        assert period.contains(date(2026, 4, 1))
        assert period.contains(date(2026, 4, 30))
        assert not period.contains(date(2026, 5, 1))

    def test_a_compliance_report_has_no_calendar_period(self) -> None:
        """It is a report *shape*, not a period, so callers must supply dates."""
        with pytest.raises(ValueError, match="not a calendar period"):
            period_containing(ReportType.COMPLIANCE, date(2026, 4, 10))

    def test_a_backwards_period_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="cannot end before it starts"):
            Period(ReportType.DAILY, date(2026, 4, 10), date(2026, 4, 9))


class TestPrevious:
    @pytest.mark.parametrize(
        ("report_type", "session", "expected_start"),
        [
            (ReportType.DAILY, date(2026, 3, 1), date(2026, 2, 28)),
            (ReportType.WEEKLY, date(2026, 3, 4), date(2026, 2, 23)),
            (ReportType.MONTHLY, date(2026, 1, 15), date(2025, 12, 1)),
            (ReportType.QUARTERLY, date(2026, 1, 15), date(2025, 10, 1)),
            (ReportType.ANNUAL, date(2026, 6, 1), date(2025, 1, 1)),
        ],
    )
    def test_it_steps_back_one_period_across_year_boundaries(
        self, report_type: ReportType, session: date, expected_start: date
    ) -> None:
        assert previous(period_containing(report_type, session)).start == expected_start

    def test_the_previous_period_is_adjacent_with_no_gap(self) -> None:
        """A gap would silently exclude trades from every comparison."""
        current = period_containing(ReportType.MONTHLY, date(2026, 5, 10))
        prior = previous(current)

        assert (current.start - prior.end).days == 1


class TestCompleteness:
    def test_an_unfinished_period_is_not_complete(self) -> None:
        """A month report generated on the 9th describes nine days while being labelled
        with the month, and every figure in it reads as a claim about the whole."""
        period = period_containing(ReportType.MONTHLY, date(2026, 4, 9))

        assert not is_complete(period, today=date(2026, 4, 9))

    def test_a_period_is_complete_only_after_its_last_day_has_passed(self) -> None:
        period = period_containing(ReportType.MONTHLY, date(2026, 4, 9))

        assert not is_complete(period, today=date(2026, 4, 30))
        assert is_complete(period, today=date(2026, 5, 1))


class TestDuePeriods:
    def test_nothing_is_due_without_a_trading_history(self) -> None:
        assert due_periods(
            today=date(2026, 5, 1),
            earliest_session=None,
            latest_session=None,
            already_generated=set(),
        ) == []

    def test_no_reports_are_generated_before_the_first_trade(self) -> None:
        """A trader who started in March gets no January report. Generating empty
        reports back to the epoch would bury the real ones."""
        due = due_periods(
            today=date(2026, 5, 1),
            earliest_session=date(2026, 3, 2),
            latest_session=date(2026, 4, 28),
            already_generated=set(),
            types=(ReportType.MONTHLY,),
        )

        assert [item.start for item in due] == [date(2026, 3, 1), date(2026, 4, 1)]

    def test_the_current_incomplete_period_is_never_offered(self) -> None:
        due = due_periods(
            today=date(2026, 5, 12),
            earliest_session=date(2026, 3, 2),
            latest_session=date(2026, 5, 11),
            already_generated=set(),
            types=(ReportType.MONTHLY,),
        )

        assert date(2026, 5, 1) not in [item.start for item in due]

    def test_it_is_idempotent(self) -> None:
        """The scheduler is re-run after every failure, so a second run must produce
        nothing new."""
        kwargs = {
            "today": date(2026, 5, 1),
            "earliest_session": date(2026, 3, 2),
            "latest_session": date(2026, 4, 28),
            "types": (ReportType.MONTHLY,),
        }
        first = due_periods(already_generated=set(), **kwargs)  # type: ignore[arg-type]
        generated = {(item.report_type, item.start) for item in first}
        second = due_periods(already_generated=generated, **kwargs)  # type: ignore[arg-type]

        assert first
        assert second == []

    def test_a_month_with_no_trades_inside_the_history_is_still_due(self) -> None:
        """A trader active in March and May still gets an April report saying it was
        flat. Silently omitting it would make the monthly series lie about its own
        continuity."""
        due = due_periods(
            today=date(2026, 6, 1),
            earliest_session=date(2026, 3, 2),
            latest_session=date(2026, 5, 20),
            already_generated=set(),
            types=(ReportType.MONTHLY,),
        )

        assert date(2026, 4, 1) in [item.start for item in due]

    def test_every_requested_type_is_covered_for_the_same_history(self) -> None:
        due = due_periods(
            today=date(2027, 1, 2),
            earliest_session=date(2026, 11, 3),
            latest_session=date(2026, 12, 30),
            already_generated=set(),
            types=(ReportType.MONTHLY, ReportType.QUARTERLY, ReportType.ANNUAL),
        )
        kinds = {item.report_type for item in due}

        assert kinds == {ReportType.MONTHLY, ReportType.QUARTERLY, ReportType.ANNUAL}

    def test_results_are_ordered_oldest_first(self) -> None:
        """Generation order matters: a report is a record of what was believed at a
        point in time, and producing December before November inverts that."""
        due = due_periods(
            today=date(2027, 1, 2),
            earliest_session=date(2026, 9, 1),
            latest_session=date(2026, 12, 30),
            already_generated=set(),
            types=(ReportType.MONTHLY,),
        )

        assert [item.start for item in due] == sorted(item.start for item in due)
