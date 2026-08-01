"""The compliance use case, against a fake repository.

The interesting behaviour is not the scoring — that is tested exhaustively against the
engine — but the *session context*: whether each trade is judged against the state of
the day as it was before that trade was taken.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.application.use_cases.evaluate_compliance import (
    ComplianceRepository,
    EvaluateCompliance,
    TradeForCompliance,
)
from app.core.ids import uuid7
from app.domain.common.enums import Direction, RuleSeverity, RuleType
from app.domain.rules.ast import Outcome
from app.domain.rules.context import TradeFacts
from app.domain.rules.engine import ComplianceReport, Rule

STRATEGY = uuid7()
SESSION = date(2026, 3, 5)


def rule(code: str, expression: dict, severity: RuleSeverity = RuleSeverity.MAJOR) -> Rule:
    return Rule(
        rule_id=uuid7(),
        code=code,
        label=code.replace("_", " "),
        rule_type=RuleType.RISK,
        severity=severity,
        expression=expression,
    )


def trade(
    *,
    minute: int,
    net_pnl: str,
    strategy_id: UUID | None = STRATEGY,
    quantity: str = "1",
    stop: str | None = "5000",
    session_date: date = SESSION,
) -> TradeForCompliance:
    entry = datetime(2026, 3, 5, 14, minute, tzinfo=UTC)
    return TradeForCompliance(
        trade_id=uuid7(),
        strategy_id=strategy_id,
        session_date=session_date,
        closed_at=entry,
        net_pnl=Decimal(net_pnl),
        facts=TradeFacts(
            direction=Direction.LONG,
            quantity=Decimal(quantity),
            entry_at=entry,
            stop_price=Decimal(stop) if stop is not None else None,
            net_pnl=Decimal(net_pnl),
        ),
    )


class FakeRepository:
    """Records what was persisted so the use case's writes can be asserted on."""

    def __init__(self, rules: list[Rule], trades: list[TradeForCompliance]) -> None:
        self._rules = rules
        self._trades = trades
        self.saved_reports: list[ComplianceReport] = []
        self.saved_scores: dict[UUID, Decimal | None] = {}

    async def load_rules(self, user_id: UUID) -> dict[UUID, list[Rule]]:
        return {STRATEGY: self._rules}

    async def load_trades(self, user_id: UUID, **kwargs: object) -> list[TradeForCompliance]:
        return self._trades

    async def save_reports(self, user_id: UUID, reports: list[ComplianceReport]) -> int:
        self.saved_reports.extend(reports)
        return len(reports)

    async def save_scores(self, scores: dict[UUID, Decimal | None]) -> int:
        self.saved_scores.update(scores)
        return len(scores)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def build(rules: list[Rule], trades: list[TradeForCompliance]) -> tuple[
    EvaluateCompliance, FakeRepository, FakeUnitOfWork
]:
    repository = FakeRepository(rules, trades)
    uow = FakeUnitOfWork()
    # The port is a structural Protocol, so nothing checks the fake against it at
    # runtime. Comparing method names catches a port that grows a method the fakes —
    # and therefore these tests — never exercise.
    assert {name for name in vars(ComplianceRepository) if not name.startswith("_")} <= {
        name for name in dir(repository) if not name.startswith("_")
    }
    return EvaluateCompliance(repository=repository, uow=uow), repository, uow


@pytest.mark.asyncio
async def test_first_trade_of_a_session_sees_no_prior_trades() -> None:
    """The bug this guards against marks every trade of the day as the last one."""
    max_three = rule("max_three", {"field": "trades_today", "op": "lt", "value": 3})
    trades = [trade(minute=m, net_pnl="100") for m in (0, 10, 20, 30, 40)]
    use_case, _, _ = build([max_three], trades)

    outcome = await use_case.execute(user_id=uuid7(), now=datetime.now(UTC))

    assert outcome.trades_evaluated == 5
    outcomes = [report.results[0].outcome for report in outcome.reports]
    # Trades 0, 1 and 2 are compliant; the fourth and fifth are the violations.
    assert outcomes == [Outcome.PASS] * 3 + [Outcome.FAIL] * 2


@pytest.mark.asyncio
async def test_session_pnl_is_the_state_before_the_trade() -> None:
    no_trading_while_down = rule(
        "not_while_down", {"field": "session_pnl_before", "op": "gte", "value": 0}
    )
    trades = [
        trade(minute=0, net_pnl="-500"),
        trade(minute=10, net_pnl="200"),
        trade(minute=20, net_pnl="100"),
    ]
    use_case, _, _ = build([no_trading_while_down], trades)

    outcome = await use_case.execute(user_id=uuid7())

    # The first trade was taken flat, so it passes even though it lost. The second was
    # taken 500 down and fails. The third was taken 300 down and fails too.
    assert [report.results[0].outcome for report in outcome.reports] == [
        Outcome.PASS,
        Outcome.FAIL,
        Outcome.FAIL,
    ]


@pytest.mark.asyncio
async def test_sessions_do_not_bleed_into_each_other() -> None:
    max_two = rule("max_two", {"field": "trades_today", "op": "lt", "value": 2})
    trades = [
        trade(minute=0, net_pnl="10", session_date=date(2026, 3, 5)),
        trade(minute=10, net_pnl="10", session_date=date(2026, 3, 5)),
        trade(minute=20, net_pnl="10", session_date=date(2026, 3, 6)),
    ]
    use_case, _, _ = build([max_two], trades)

    outcome = await use_case.execute(user_id=uuid7())

    # The third trade opens a new session and must be its own first trade.
    assert all(report.results[0].outcome is Outcome.PASS for report in outcome.reports)


@pytest.mark.asyncio
async def test_consecutive_losses_reset_on_a_winner() -> None:
    pause = rule("pause", {"field": "consecutive_losses", "op": "lt", "value": 2})
    trades = [
        trade(minute=0, net_pnl="-100"),
        trade(minute=10, net_pnl="-100"),
        trade(minute=20, net_pnl="300"),  # taken after two losses — violation
        trade(minute=30, net_pnl="-100"),  # streak was reset by the winner — compliant
    ]
    use_case, _, _ = build([pause], trades)

    outcome = await use_case.execute(user_id=uuid7())

    assert [report.results[0].outcome for report in outcome.reports] == [
        Outcome.PASS,
        Outcome.PASS,
        Outcome.FAIL,
        Outcome.PASS,
    ]


@pytest.mark.asyncio
async def test_trades_without_a_strategy_are_not_scored() -> None:
    """Scoring an unlabelled trade would invent a plan the trader never declared."""
    always = rule("always", {"field": "has_stop", "op": "eq", "value": True})
    trades = [
        trade(minute=0, net_pnl="100"),
        trade(minute=10, net_pnl="100", strategy_id=None),
    ]
    use_case, repository, _ = build([always], trades)

    outcome = await use_case.execute(user_id=uuid7())

    assert outcome.trades_evaluated == 1
    assert outcome.trades_without_strategy == 1
    assert len(repository.saved_scores) == 1


@pytest.mark.asyncio
async def test_a_strategy_with_no_rules_is_counted_separately() -> None:
    trades = [trade(minute=0, net_pnl="100")]
    use_case, _, _ = build([], trades)

    outcome = await use_case.execute(user_id=uuid7())

    assert outcome.trades_without_rules == 1
    assert outcome.trades_without_strategy == 0


@pytest.mark.asyncio
async def test_unevaluable_rules_leave_the_score_undefined() -> None:
    needs_a_target = rule("target", {"field": "planned_r_multiple", "op": "gte", "value": "1.5"})
    trades = [trade(minute=0, net_pnl="100")]
    use_case, repository, _ = build([needs_a_target], trades)

    outcome = await use_case.execute(user_id=uuid7())

    assert outcome.trades_evaluated == 1
    assert outcome.trades_scored == 0
    assert outcome.mean_score is None
    assert list(repository.saved_scores.values()) == [None]


@pytest.mark.asyncio
async def test_impacts_rank_the_most_expensive_rule_first() -> None:
    stop_rule = rule("has_stop", {"field": "has_stop", "op": "eq", "value": True})
    size_rule = rule("small_size", {"field": "quantity", "op": "lte", "value": 2})
    trades = [
        trade(minute=0, net_pnl="100", stop="5000", quantity="1"),
        trade(minute=10, net_pnl="-900", stop=None, quantity="10"),
        trade(minute=20, net_pnl="150", stop="5000", quantity="1"),
        trade(minute=30, net_pnl="-500", stop=None, quantity="1"),
    ]
    use_case, _, _ = build([size_rule, stop_rule], trades)

    outcome = await use_case.execute(user_id=uuid7())

    # Oversizing cost roughly 817; trading without a stop cost 1650. Ranked by cost,
    # not by the order the rules were written in.
    assert [impact.rule_code for impact in outcome.impacts] == ["has_stop", "small_size"]
    worst = outcome.impacts[0]
    assert worst.violations == 2
    assert worst.compliant_trades == 2
    # (-700 mean when violated) − (125 mean when followed) × 2 violations.
    assert worst.total_cost == Decimal("-1650")
    assert worst.is_costly


@pytest.mark.asyncio
async def test_nothing_is_persisted_when_persist_is_false() -> None:
    always = rule("always", {"field": "has_stop", "op": "eq", "value": True})
    use_case, repository, uow = build([always], [trade(minute=0, net_pnl="100")])

    await use_case.execute(user_id=uuid7(), persist=False)

    assert repository.saved_reports == []
    assert repository.saved_scores == {}
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_an_empty_history_commits_nothing() -> None:
    use_case, _, uow = build([], [])

    outcome = await use_case.execute(user_id=uuid7())

    assert outcome.trades_evaluated == 0
    assert outcome.mean_score is None
    assert uow.commits == 0
