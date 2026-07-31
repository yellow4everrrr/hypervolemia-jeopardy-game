"""Score trades against their strategy's rules and persist the verdicts.

Building the session context is the subtle part. Rules like "no more than six trades a
session" and "wait ten minutes after two losses" are about a trade's *position in the
sequence*, not the trade itself — so each trade must be evaluated against the state of
the session as it was **immediately before** that trade was taken. Evaluating against
end-of-day state would mark the first trade of the day as the seventh.

That is also why the repository loads *whole sessions*. A limit that cuts a session in
half would tell the rule engine that the trader's fourth trade was their first, and the
compliance score would silently be wrong rather than obviously missing.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from app.analytics.statistics import mean
from app.core.logging import get_logger
from app.domain.rules.ast import Outcome
from app.domain.rules.context import TradeFacts, build_context
from app.domain.rules.engine import ComplianceReport, Rule, RuleImpact, evaluate, measure_impact

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TradeForCompliance:
    """One trade, plus the strategy it was taken under."""

    trade_id: UUID
    strategy_id: UUID | None
    facts: TradeFacts
    session_date: date | None
    closed_at: datetime | None
    net_pnl: Decimal


class ComplianceRepository(Protocol):
    async def load_rules(self, user_id: UUID) -> dict[UUID, list[Rule]]: ...

    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
        max_sessions: int | None = None,
    ) -> list[TradeForCompliance]: ...

    async def save_reports(self, user_id: UUID, reports: list[ComplianceReport]) -> int: ...

    async def save_scores(self, scores: dict[UUID, Decimal | None]) -> int: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...


@dataclass
class ComplianceOutcome:
    """What a compliance run found.

    ``trades_without_strategy`` and ``trades_without_rules`` are kept apart on purpose:
    the first is a labelling gap the trader can close, the second means a strategy
    exists but has nothing checkable attached to it. Rolling them together would hide
    which of the two is suppressing the coverage number.
    """

    trades_evaluated: int = 0
    trades_scored: int = 0
    total_violations: int = 0
    critical_violations: int = 0
    trades_without_strategy: int = 0
    trades_without_rules: int = 0
    mean_score: Decimal | None = None
    reports: list[ComplianceReport] = field(default_factory=list)
    impacts: list[RuleImpact] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "trades_evaluated": self.trades_evaluated,
            "trades_scored": self.trades_scored,
            "total_violations": self.total_violations,
            "critical_violations": self.critical_violations,
            "trades_without_strategy": self.trades_without_strategy,
            "trades_without_rules": self.trades_without_rules,
            "mean_score": str(self.mean_score) if self.mean_score is not None else None,
            "impacts": [impact.to_payload() for impact in self.impacts],
        }


class EvaluateCompliance:
    """Score every trade against the rules of the strategy it was taken under."""

    def __init__(self, *, repository: ComplianceRepository, uow: UnitOfWork) -> None:
        self._repository = repository
        self._uow = uow

    async def execute(
        self,
        *,
        user_id: UUID,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
        max_sessions: int | None = 90,
        persist: bool = True,
        now: datetime | None = None,
    ) -> ComplianceOutcome:
        moment = now or datetime.now(UTC)
        rules_by_strategy = await self._repository.load_rules(user_id)
        trades = await self._repository.load_trades(
            user_id,
            account_id=account_id,
            session_from=session_from,
            session_to=session_to,
            max_sessions=max_sessions,
        )

        outcome = ComplianceOutcome()
        if not trades:
            return outcome

        reports: list[ComplianceReport] = []
        scores: dict[UUID, Decimal | None] = {}
        #: Per rule, the outcome and the P&L of every trade it was checked against —
        #: the raw material for "what did breaking this cost?".
        by_rule: dict[UUID, tuple[Rule, list[tuple[Outcome, Decimal]]]] = {}

        for trade in _with_session_context(trades):
            if trade.strategy_id is None:
                # A trade with no strategy has no rules to be judged against. Scoring it
                # against someone else's rules, or against an implicit default, would be
                # inventing a plan the trader never declared.
                outcome.trades_without_strategy += 1
                continue

            rules = rules_by_strategy.get(trade.strategy_id, [])
            if not rules:
                outcome.trades_without_rules += 1
                continue

            report = evaluate(
                trade_id=trade.trade_id,
                strategy_id=trade.strategy_id,
                rules=rules,
                context=build_context(trade.facts),
                now=moment,
            )
            reports.append(report)
            scores[trade.trade_id] = report.score

            for result in report.results:
                entry = by_rule.setdefault(result.rule.rule_id, (result.rule, []))
                entry[1].append((result.outcome, trade.net_pnl))

            outcome.trades_evaluated += 1
            outcome.total_violations += len(report.violations)
            if report.has_critical_violation:
                outcome.critical_violations += 1
            if report.score is not None:
                outcome.trades_scored += 1

        outcome.mean_score = mean([value for value in scores.values() if value is not None])
        outcome.reports = reports
        outcome.impacts = sorted(
            (measure_impact(rule, outcomes) for rule, outcomes in by_rule.values()),
            key=lambda impact: (impact.total_cost is None, impact.total_cost or Decimal(0)),
        )

        if persist and reports:
            await self._repository.save_reports(user_id, reports)
            await self._repository.save_scores(scores)
            await self._uow.commit()

        logger.info(
            "compliance.evaluated",
            user_id=str(user_id),
            evaluated=outcome.trades_evaluated,
            violations=outcome.total_violations,
            critical=outcome.critical_violations,
            mean_score=str(outcome.mean_score) if outcome.mean_score is not None else None,
        )
        return outcome


def _with_session_context(trades: list[TradeForCompliance]) -> list[TradeForCompliance]:
    """Fill in each trade's position within its session.

    Trades are grouped by session date and walked in order, accumulating the counts and
    running P&L that sequence-sensitive rules depend on. Each trade sees the state as it
    was *before* it was taken — the first trade of a session sees zero prior trades,
    not the day's total.
    """
    by_session: dict[date | None, list[TradeForCompliance]] = defaultdict(list)
    for trade in trades:
        by_session[trade.session_date].append(trade)

    enriched: list[TradeForCompliance] = []
    for session_trades in by_session.values():
        ordered = sorted(
            session_trades,
            key=lambda trade: (
                trade.facts.entry_at or datetime.min.replace(tzinfo=UTC),
                str(trade.trade_id),
            ),
        )
        running_pnl = Decimal(0)
        consecutive_losses = 0
        previous_close: datetime | None = None

        for index, trade in enumerate(ordered):
            minutes_since = None
            if previous_close is not None and trade.facts.entry_at is not None:
                minutes_since = max(
                    0, int((trade.facts.entry_at - previous_close).total_seconds() // 60)
                )

            enriched.append(
                dataclasses.replace(
                    trade,
                    facts=dataclasses.replace(
                        trade.facts,
                        trades_today=index,
                        session_pnl_before=running_pnl,
                        consecutive_losses=consecutive_losses,
                        minutes_since_last_trade=minutes_since,
                    ),
                )
            )

            running_pnl += trade.net_pnl
            if trade.net_pnl < 0:
                consecutive_losses += 1
            elif trade.net_pnl > 0:
                consecutive_losses = 0
            # A scratch (exactly zero) neither continues nor breaks a losing streak: it
            # is not a loss, and calling it a recovery would let a flat trade reset the
            # "pause after two losses" rule the trader wrote to protect themselves.
            previous_close = trade.closed_at or previous_close

    return enriched
