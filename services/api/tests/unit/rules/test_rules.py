"""Tests for the rule AST, context building and compliance scoring.

The recurring theme is the three-valued outcome. Most of these tests exist to pin down
the difference between "you broke this rule" and "I could not check this rule" — which
is what makes a compliance score mean anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.core.errors import ValidationError
from app.domain.common.enums import Direction, RuleSeverity, RuleType
from app.domain.rules.ast import Outcome, parse, validate
from app.domain.rules.context import (
    AVAILABLE_FIELDS,
    STARTER_RULES,
    TradeFacts,
    build_context,
    unknown_fields,
)
from app.domain.rules.engine import (
    CRITICAL_VIOLATION_CEILING,
    Rule,
    evaluate,
    measure_impact,
)

NOW = datetime(2026, 3, 5, 20, 0, tzinfo=UTC)
TRADE = UUID("00000000-0000-7000-8000-000000000001")


def rule(
    expression: dict,
    *,
    code: str = "r",
    severity: RuleSeverity = RuleSeverity.MINOR,
    weight: str = "1",
) -> Rule:
    return Rule(
        rule_id=UUID(int=abs(hash(code)) % (2**128)),
        code=code,
        label=code.replace("_", " "),
        rule_type=RuleType.RISK,
        severity=severity,
        expression=expression,
        weight=Decimal(weight),
    )


# --- Comparisons ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("operator", "value", "observed", "expected"),
    [
        ("eq", 5, 5, Outcome.PASS),
        ("eq", 5, 6, Outcome.FAIL),
        ("ne", 5, 6, Outcome.PASS),
        ("lt", 10, 5, Outcome.PASS),
        ("lte", 5, 5, Outcome.PASS),
        ("gt", 5, 10, Outcome.PASS),
        ("gte", 5, 4, Outcome.FAIL),
    ],
)
def test_numeric_comparisons(operator: str, value: int, observed: int, expected: Outcome) -> None:
    node = parse({"field": "trades_today", "op": operator, "value": value})
    assert node.evaluate({"trades_today": observed}).outcome is expected


def test_decimal_thresholds_are_coerced_from_stored_strings() -> None:
    """Rules are stored as JSON, so a threshold arrives as a string."""
    node = parse({"field": "risk_pct_of_equity", "op": "lte", "value": "0.01"})

    assert node.evaluate({"risk_pct_of_equity": Decimal("0.008")}).outcome is Outcome.PASS
    assert node.evaluate({"risk_pct_of_equity": Decimal("0.02")}).outcome is Outcome.FAIL


def test_between_operator() -> None:
    node = parse({"field": "entry_hour", "op": "between", "value": [9, 11]})
    assert node.evaluate({"entry_hour": 10}).outcome is Outcome.PASS
    assert node.evaluate({"entry_hour": 14}).outcome is Outcome.FAIL


def test_in_and_not_in() -> None:
    node = parse({"field": "setup", "op": "in", "value": ["breakout", "pullback"]})
    assert node.evaluate({"setup": "breakout"}).outcome is Outcome.PASS
    assert node.evaluate({"setup": "gap_fill"}).outcome is Outcome.FAIL


def test_time_comparison_coerces_a_string() -> None:
    from datetime import time

    node = parse({"field": "entry_time", "op": "gte", "value": "09:30"})
    assert node.evaluate({"entry_time": time(10, 0)}).outcome is Outcome.PASS
    assert node.evaluate({"entry_time": time(9, 0)}).outcome is Outcome.FAIL


def test_is_set_is_answerable_even_when_absent() -> None:
    """"Was a stop recorded?" has an answer either way, unlike "how far away was it?"."""
    node = parse({"field": "stop_price", "op": "is_set"})
    assert node.evaluate({}).outcome is Outcome.FAIL
    assert node.evaluate({"stop_price": Decimal(100)}).outcome is Outcome.PASS


# --- The three-valued outcome ---------------------------------------------------


def test_a_missing_field_is_unevaluable_not_failed() -> None:
    """The distinction the whole module turns on.

    A rule about stop distance cannot be checked on a trade with no stop, and scoring
    that as a violation would invent indiscipline out of missing data.
    """
    node = parse({"field": "risk_pct_of_equity", "op": "lte", "value": "0.01"})
    result = node.evaluate({})

    assert result.outcome is Outcome.UNEVALUABLE
    assert "not recorded" in result.detail


def test_all_group_fails_fast_on_a_real_violation() -> None:
    """An unevaluable sibling cannot rescue a rule that was definitively broken."""
    node = parse(
        {
            "all": [
                {"field": "trades_today", "op": "lt", "value": 3},
                {"field": "risk_amount", "op": "lte", "value": "100"},
            ]
        }
    )
    assert node.evaluate({"trades_today": 9}).outcome is Outcome.FAIL


def test_all_group_is_unevaluable_when_only_unknowns_remain() -> None:
    node = parse(
        {
            "all": [
                {"field": "trades_today", "op": "lt", "value": 10},
                {"field": "risk_amount", "op": "lte", "value": "100"},
            ]
        }
    )
    assert node.evaluate({"trades_today": 2}).outcome is Outcome.UNEVALUABLE


def test_any_group_passes_on_one_satisfied_child() -> None:
    """The rule is satisfied regardless of what could not be checked."""
    node = parse(
        {
            "any": [
                {"field": "consecutive_losses", "op": "lt", "value": 2},
                {"field": "minutes_since_last_trade", "op": "gte", "value": 10},
            ]
        }
    )
    assert node.evaluate({"consecutive_losses": 0}).outcome is Outcome.PASS


def test_none_group_fails_when_something_matches() -> None:
    node = parse({"none": [{"field": "emotion", "op": "eq", "value": "revengeful"}]})
    assert node.evaluate({"emotion": "revengeful"}).outcome is Outcome.FAIL
    assert node.evaluate({"emotion": "calm"}).outcome is Outcome.PASS


# --- Parsing and validation -----------------------------------------------------


def test_malformed_expressions_are_rejected_at_parse_time() -> None:
    """A bad rule must fail when saved, not silently skip for months."""
    with pytest.raises(ValidationError, match="non-empty object"):
        parse({})
    with pytest.raises(ValidationError, match="must be a comparison"):
        parse({"nonsense": 1})
    with pytest.raises(ValidationError, match="unknown operator"):
        parse({"field": "net_pnl", "op": "approximately", "value": 1})
    with pytest.raises(ValidationError, match="non-empty list"):
        parse({"all": []})


def test_deep_nesting_is_refused() -> None:
    expression: dict = {"field": "net_pnl", "op": "gt", "value": 0}
    for _ in range(20):
        expression = {"all": [expression]}
    with pytest.raises(ValidationError, match="nests deeper"):
        parse(expression)


def test_validate_reports_referenced_fields() -> None:
    fields = validate(
        {
            "all": [
                {"field": "has_stop", "op": "eq", "value": True},
                {"field": "risk_pct_of_equity", "op": "lte", "value": "0.01"},
            ]
        }
    )
    assert fields == {"has_stop", "risk_pct_of_equity"}


def test_unknown_fields_are_detectable() -> None:
    """So a typo is an error at save time rather than a permanently unevaluable rule."""
    assert unknown_fields({"has_stop", "moon_phase"}) == {"moon_phase"}


def test_rules_render_back_to_english() -> None:
    """Structure is data, so the compliance report can explain itself."""
    node = parse({"field": "trades_today", "op": "lt", "value": 6})
    assert node.describe() == "trades today is below 6"


# --- Context --------------------------------------------------------------------


def test_context_omits_unknown_fields_rather_than_nulling_them() -> None:
    context = build_context(TradeFacts(direction=Direction.LONG))

    assert "risk_amount" not in context
    assert "r_multiple" not in context
    # Presence flags are always answerable.
    assert context["has_stop"] is False


def test_stop_widening_is_distinguished_from_stop_moving() -> None:
    """Moving a stop closer is risk reduction; moving it further is the leak."""
    tightened = build_context(
        TradeFacts(
            direction=Direction.LONG,
            stop_price=Decimal(4990),
            final_stop_price=Decimal(4995),
        )
    )
    widened = build_context(
        TradeFacts(
            direction=Direction.LONG,
            stop_price=Decimal(4990),
            final_stop_price=Decimal(4980),
        )
    )

    assert tightened["stop_moved"] is True
    assert tightened["stop_widened"] is False
    assert widened["stop_widened"] is True


def test_stop_widening_inverts_for_shorts() -> None:
    widened = build_context(
        TradeFacts(
            direction=Direction.SHORT,
            stop_price=Decimal(5010),
            final_stop_price=Decimal(5020),
        )
    )
    assert widened["stop_widened"] is True


def test_risk_as_a_fraction_of_equity() -> None:
    context = build_context(
        TradeFacts(
            direction=Direction.LONG,
            risk_amount=Decimal(500),
            account_equity=Decimal(50_000),
        )
    )
    assert context["risk_pct_of_equity"] == Decimal("0.01")


def test_daily_loss_limit_breach_is_derived() -> None:
    context = build_context(
        TradeFacts(
            direction=Direction.LONG,
            session_pnl_before=Decimal(-1200),
            daily_loss_limit=Decimal(1000),
        )
    )
    assert context["is_after_daily_loss_limit"] is True


def test_every_starter_rule_parses_and_references_known_fields() -> None:
    """A template rule that referenced a nonexistent field would teach the wrong lesson."""
    for template in STARTER_RULES:
        referenced = validate(template["expression"])
        assert not unknown_fields(referenced), template["code"]


def test_available_fields_is_the_documented_contract() -> None:
    assert "has_stop" in AVAILABLE_FIELDS
    assert all(isinstance(description, str) for description in AVAILABLE_FIELDS.values())


# --- Compliance scoring ---------------------------------------------------------


def test_a_fully_compliant_trade_scores_one() -> None:
    report = evaluate(
        trade_id=TRADE,
        strategy_id=None,
        rules=[
            rule({"field": "has_stop", "op": "eq", "value": True}, code="stop"),
            rule({"field": "trades_today", "op": "lt", "value": 6}, code="count"),
        ],
        context={"has_stop": True, "trades_today": 2},
        now=NOW,
    )

    assert report.score == Decimal(1)
    assert report.violations == ()


def test_unevaluable_rules_leave_the_denominator() -> None:
    """Scoring on what could be checked, and saying how much that was.

    Counting them as passes would inflate every score; as failures would punish the
    trader for the journal's own missing data.
    """
    report = evaluate(
        trade_id=TRADE,
        strategy_id=None,
        rules=[
            rule({"field": "has_stop", "op": "eq", "value": True}, code="stop"),
            rule({"field": "risk_pct_of_equity", "op": "lte", "value": "0.01"}, code="risk"),
        ],
        context={"has_stop": True},
        now=NOW,
    )

    assert report.score == Decimal(1)
    assert len(report.unevaluable) == 1
    assert report.coverage == Decimal("0.5")


def test_score_is_none_when_nothing_could_be_checked() -> None:
    """Zero would claim total indiscipline; the truth is the data cannot say."""
    report = evaluate(
        trade_id=TRADE,
        strategy_id=None,
        rules=[rule({"field": "risk_amount", "op": "lte", "value": "100"})],
        context={},
        now=NOW,
    )
    assert report.score is None


def test_severity_is_weighted() -> None:
    """A failed critical rule costs far more than a failed advisory."""
    advisory_failed = evaluate(
        trade_id=TRADE,
        strategy_id=None,
        rules=[
            rule(
                {"field": "a", "op": "eq", "value": True},
                code="a",
                severity=RuleSeverity.ADVISORY,
            ),
            rule({"field": "b", "op": "eq", "value": True}, code="b", severity=RuleSeverity.MAJOR),
        ],
        context={"a": False, "b": True},
        now=NOW,
    )
    major_failed = evaluate(
        trade_id=TRADE,
        strategy_id=None,
        rules=[
            rule(
                {"field": "a", "op": "eq", "value": True},
                code="a",
                severity=RuleSeverity.ADVISORY,
            ),
            rule({"field": "b", "op": "eq", "value": True}, code="b", severity=RuleSeverity.MAJOR),
        ],
        context={"a": True, "b": False},
        now=NOW,
    )

    assert advisory_failed.score is not None and major_failed.score is not None
    assert advisory_failed.score > major_failed.score


def test_a_critical_violation_caps_the_score() -> None:
    """Discipline is not an average.

    Nine passed advisories cannot make blowing a hard risk limit into a 90% trade.
    """
    rules = [
        rule({"field": f"ok{i}", "op": "eq", "value": True}, code=f"ok{i}") for i in range(9)
    ]
    rules.append(
        rule(
            {"field": "is_after_daily_loss_limit", "op": "eq", "value": False},
            code="loss_limit",
            severity=RuleSeverity.CRITICAL,
        )
    )
    context = {f"ok{i}": True for i in range(9)} | {"is_after_daily_loss_limit": True}

    report = evaluate(
        trade_id=TRADE, strategy_id=None, rules=rules, context=context, now=NOW
    )

    assert report.has_critical_violation
    assert report.score is not None
    assert report.score <= CRITICAL_VIOLATION_CEILING


def test_inactive_rules_are_skipped() -> None:
    disabled = Rule(
        rule_id=UUID(int=1),
        code="off",
        label="off",
        rule_type=RuleType.RISK,
        severity=RuleSeverity.MAJOR,
        expression={"field": "has_stop", "op": "eq", "value": True},
        is_active=False,
    )
    report = evaluate(
        trade_id=TRADE, strategy_id=None, rules=[disabled], context={"has_stop": False}, now=NOW
    )
    assert report.results == ()


def test_report_payload_explains_each_verdict() -> None:
    report = evaluate(
        trade_id=TRADE,
        strategy_id=None,
        rules=[rule({"field": "trades_today", "op": "lt", "value": 6}, code="count")],
        context={"trades_today": 9},
        now=NOW,
    )
    payload = report.to_payload()

    assert payload["violations"] == 1
    assert payload["results"][0]["outcome"] == "fail"
    assert payload["results"][0]["observed"] == 9


# --- Rule impact ----------------------------------------------------------------


def test_rule_impact_quantifies_what_breaking_it_costs() -> None:
    """What turns a compliance report into a reason to change behaviour.

    "You violated this 3 times" is a scolding. "Those 3 trades averaged -$180 against
    +$62 for the rest, costing $726" is an argument.
    """
    outcomes = [
        (Outcome.FAIL, Decimal(-180)),
        (Outcome.FAIL, Decimal(-180)),
        (Outcome.FAIL, Decimal(-180)),
        (Outcome.PASS, Decimal(62)),
        (Outcome.PASS, Decimal(62)),
    ]
    impact = measure_impact(rule({"field": "a", "op": "eq", "value": True}), outcomes)

    assert impact.violations == 3
    assert impact.mean_pnl_when_violated == Decimal(-180)
    assert impact.mean_pnl_when_followed == Decimal(62)
    assert impact.difference == Decimal(-242)
    assert impact.total_cost == Decimal(-726)
    assert impact.is_costly


def test_unevaluable_trades_are_excluded_from_impact() -> None:
    """Including them would contaminate the comparison with trades the rule never applied to."""
    outcomes = [
        (Outcome.FAIL, Decimal(-100)),
        (Outcome.PASS, Decimal(100)),
        (Outcome.UNEVALUABLE, Decimal(9999)),
    ]
    impact = measure_impact(rule({"field": "a", "op": "eq", "value": True}), outcomes)

    assert impact.violations == 1
    assert impact.compliant_trades == 1
    assert impact.mean_pnl_when_followed == Decimal(100)


def test_entry_time_is_the_exchange_clock_not_utc() -> None:
    """A rule about the open means the exchange's clock.

    Trades are stored in UTC, so reading the hour straight off `entry_at` reports a
    09:30 New York entry as 14:30 — which passes "no trades before 09:30" for a trade
    taken exactly at the open, and fails "no trades after 12:00" for one taken at
    breakfast. Neither error is visible in the report: the rule simply returns the
    wrong verdict with full confidence.
    """
    entry = datetime(2026, 3, 5, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    context = build_context(
        TradeFacts(
            direction=Direction.LONG,
            entry_at=entry.astimezone(UTC),
            exchange_timezone="America/New_York",
        )
    )

    assert context["entry_time"] == time(9, 30)
    assert parse({"field": "entry_time", "op": "gte", "value": "09:30"}).evaluate(
        context
    ).outcome is Outcome.PASS
    assert parse({"field": "entry_time", "op": "lt", "value": "09:30"}).evaluate(
        context
    ).outcome is Outcome.FAIL


def test_entry_time_agrees_with_entry_hour() -> None:
    """Two fields describing the same instant must not disagree about the timezone.

    `entry_hour` is denormalised as exchange-local by the ingestion pipeline. If
    `entry_time` were derived from UTC, a single context would carry both 9 and 14 for
    one trade, and which one a rule saw would depend on how it happened to be written.
    """
    entry = datetime(2026, 11, 3, 14, 45, tzinfo=ZoneInfo("America/Chicago"))
    context = build_context(
        TradeFacts(
            direction=Direction.LONG,
            entry_at=entry.astimezone(UTC),
            exchange_timezone="America/Chicago",
            entry_hour=14,
        )
    )

    assert context["entry_time"].hour == context["entry_hour"] == 14


def test_entry_time_is_omitted_when_the_zone_is_unknown() -> None:
    """Better unevaluable than confidently wrong.

    Without a zone there is no answer to "what time was this for the trader?", so the
    field is absent and a rule against it reads as uncheckable — the same treatment a
    missing stop gets.
    """
    facts = TradeFacts(
        direction=Direction.LONG, entry_at=datetime(2026, 3, 5, 14, 30, tzinfo=UTC)
    )

    context = build_context(facts)

    assert "entry_time" not in context
    assert parse({"field": "entry_time", "op": "gte", "value": "09:30"}).evaluate(
        context
    ).outcome is Outcome.UNEVALUABLE


def test_an_unusable_timezone_does_not_fail_the_run() -> None:
    """A stale IANA name on one instrument must not take down a whole compliance run."""
    context = build_context(
        TradeFacts(
            direction=Direction.LONG,
            entry_at=datetime(2026, 3, 5, 14, 30, tzinfo=UTC),
            exchange_timezone="Mars/Olympus_Mons",
        )
    )

    assert "entry_time" not in context


def test_entry_time_follows_daylight_saving() -> None:
    """The same wall-clock entry sits at different UTC offsets across a DST boundary.

    Deriving from a fixed offset would drift by an hour for half the year — enough to
    move every trade in and out of an opening-range rule twice a year.
    """
    ny = ZoneInfo("America/New_York")
    winter = datetime(2026, 1, 14, 9, 30, tzinfo=ny).astimezone(UTC)
    summer = datetime(2026, 7, 14, 9, 30, tzinfo=ny).astimezone(UTC)

    # Different UTC hours (14:30 vs 13:30) for the same local time.
    assert winter.hour != summer.hour

    for moment in (winter, summer):
        context = build_context(
            TradeFacts(
                direction=Direction.LONG, entry_at=moment, exchange_timezone="America/New_York"
            )
        )
        assert context["entry_time"] == time(9, 30)
