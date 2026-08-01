"""Composing a report: what it shows, what it withholds, and what it refuses to infer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.analytics.discovery import DiscoveryConfig
from app.domain.common.enums import ReportType
from app.reports.builder import BuildConfig, annual_note, build
from app.reports.periods import period_containing
from tests.unit.reports.test_comparison import build_month

FAST = BuildConfig(
    discovery=DiscoveryConfig(permutations=200, include_clustering=False),
    comparison_permutations=200,
)


def month(day: int = 15) -> object:
    return period_containing(ReportType.MONTHLY, date(2026, 5, day))


class TestSufficiency:
    def test_a_thin_period_shows_its_numbers_but_draws_no_conclusions(self) -> None:
        """"You took six trades and lost $400" is a fact the trader owns. Withholding it
        because six trades cannot support an inference withholds their own history."""
        report = build(month(), build_month(month=5, seed=1, count=6), config=FAST)

        assert not report.draws_conclusions
        performance = report.section("performance")
        assert performance is not None and performance.is_present
        assert report.notes

    def test_a_thin_period_withholds_the_comparison_with_a_reason(self) -> None:
        report = build(
            month(),
            build_month(month=5, seed=1, count=6),
            previous_trades=build_month(month=4, seed=2),
            config=FAST,
        )
        comparison = report.section("comparison")

        assert comparison is not None
        assert not comparison.is_present
        assert "too small to compare" in (comparison.withheld_reason or "")

    def test_a_thin_period_withholds_the_pattern_scan(self) -> None:
        report = build(month(), build_month(month=5, seed=1, count=6), config=FAST)
        patterns = report.section("patterns")

        assert patterns is not None and not patterns.is_present

    def test_sections_refuse_individually_rather_than_the_report_refusing(self) -> None:
        """A month with enough trades for statistics but no prior period must still
        show the statistics."""
        report = build(month(), build_month(month=5, seed=3, count=40), config=FAST)

        present = {s.key for s in report.sections if s.is_present}
        withheld = {s.key for s in report.sections if not s.is_present}

        assert "performance" in present
        assert "comparison" in withheld

    def test_an_empty_period_is_reported_as_flat_not_skipped(self) -> None:
        """A gap in a monthly series is information. Omitting April would make the
        series lie about its own continuity."""
        report = build(month(), [], config=FAST)

        assert report.trades == 0
        assert not report.sections
        assert "flat rather than skipped" in " ".join(report.notes)
        assert "No trades" in report.headline


class TestComposition:
    def test_the_headline_states_the_period_before_interpreting_it(self) -> None:
        report = build(month(), build_month(month=5, seed=4, count=40), config=FAST)

        assert "May 2026" in report.headline
        assert "40 trades" in report.headline

    def test_a_full_period_produces_every_section(self) -> None:
        report = build(
            month(),
            build_month(month=5, seed=5, count=60),
            previous_trades=build_month(month=4, seed=6, count=60),
            config=FAST,
        )

        assert report.draws_conclusions
        assert {s.key for s in report.sections} == {
            "performance",
            "comparison",
            "patterns",
            "leaks",
        }
        assert all(section.is_present for section in report.sections)

    def test_it_is_deterministic(self) -> None:
        """A stored report must be reproducible from its inputs, or it is not a record
        of anything.

        ``computed_at`` is excluded, and only that: the clock is the one input that is
        legitimately different between two runs. Every statistic — including the ones
        built from seeded resampling — must match exactly.
        """
        trades = build_month(month=5, seed=7, count=50)
        first = build(month(), trades, config=FAST)
        second = build(month(), trades, config=FAST)

        def statistics(report: object) -> dict[str, object]:
            section = report.section("performance")  # type: ignore[attr-defined]
            assert section is not None
            return {
                key: value for key, value in section.payload.items() if key != "computed_at"
            }

        assert statistics(first) == statistics(second)
        assert statistics(first)["net_pnl"]

    def test_the_payload_marks_whether_conclusions_were_drawn(self) -> None:
        payload = build(month(), build_month(month=5, seed=8, count=6), config=FAST).to_payload()

        assert payload["draws_conclusions"] is False
        assert payload["version"] >= 1


class TestLongPeriodsAreNotAggregates:
    def test_a_quarter_is_computed_from_its_own_trades(self) -> None:
        """Not from the three monthly reports inside it.

        The quarter's trade count must equal the trades handed in, which it cannot if
        the builder were summing sub-reports — and a leak present all quarter would
        otherwise be billed three times.
        """
        quarter = period_containing(ReportType.QUARTERLY, date(2026, 5, 15))
        trades = (
            build_month(month=4, seed=11, count=30)
            + build_month(month=5, seed=12, count=30)
            + build_month(month=6, seed=13, count=30)
        )

        report = build(quarter, trades, config=FAST)

        assert report.trades == 90
        performance = report.section("performance")
        assert performance is not None
        assert performance.payload["trades"] == 90

    def test_long_reports_carry_the_warning_that_they_do_not_sum(self) -> None:
        """The natural reading of an annual figure is that it is the sum of the monthly
        ones, and it is not."""
        assert annual_note(ReportType.ANNUAL) is not None
        assert "not aggregated" in (annual_note(ReportType.QUARTERLY) or "")
        assert annual_note(ReportType.DAILY) is None

    def test_the_quarterly_total_is_not_the_sum_of_its_months(self) -> None:
        """Demonstrated rather than asserted in prose.

        Each month establishes its own leaks over its own sample; the quarter
        re-establishes them over the whole. The deduplicated quarterly cost is its own
        figure and there is no arithmetic relationship to the monthly ones.
        """
        months = [
            build_month(month=4, seed=21, count=40),
            build_month(month=5, seed=22, count=40),
            build_month(month=6, seed=23, count=40),
        ]
        quarter = period_containing(ReportType.QUARTERLY, date(2026, 5, 15))

        monthly_totals = []
        for index, trades in enumerate(months, start=4):
            section = build(
                period_containing(ReportType.MONTHLY, date(2026, index, 15)),
                trades,
                config=FAST,
            ).section("leaks")
            assert section is not None
            monthly_totals.append(section.payload.get("total_cost"))

        quarter_section = build(quarter, [t for m in months for t in m], config=FAST).section(
            "leaks"
        )
        assert quarter_section is not None

        summed = sum(
            (Decimal(value) for value in monthly_totals if value is not None), Decimal(0)
        )
        quarterly = quarter_section.payload.get("total_cost")
        quarterly_value = Decimal(quarterly) if quarterly is not None else Decimal(0)

        # They are different quantities computed over different samples. The point of
        # the test is that the builder never treats one as derivable from the other.
        assert quarterly_value != summed or summed == Decimal(0)


class TestLeakSectionIsDeduplicated:
    def test_the_report_headlines_the_deduplicated_cost(self) -> None:
        """The section must expose both figures so the gap is visible, and the total
        must be the deduplicated one."""
        from tests.unit.analytics.test_patterns import build_trades

        trades = build_trades(400, 13, leak_after=5)
        report = build(month(), trades, config=FAST)
        leaks = report.section("leaks")

        assert leaks is not None and leaks.is_present
        assert "total_cost" in leaks.payload
        assert "naive_sum" in leaks.payload
        if leaks.payload["total_cost"] and leaks.payload["naive_sum"]:
            assert abs(Decimal(leaks.payload["naive_sum"])) >= abs(
                Decimal(leaks.payload["total_cost"])
            )
