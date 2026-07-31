"""Loading rules and trades for compliance scoring, and storing the verdicts.

Two things here are less obvious than they look.

**Sessions are loaded whole.** A naive ``LIMIT 500`` on trades would slice a trading
day in half, and the rule engine would then be told that the trader's fourth trade of
the day was their first. Sequence-sensitive rules ("at most six trades a session",
"pause after two losses") would score against a fiction. So the session boundary is
resolved first, with a grouped count, and trades are then loaded by session date.

**Equity is looked up as of the trade, not as of now.** ``risk_pct_of_equity`` compares
what was risked against the account balance *at the time*. Using today's balance would
grade a $500 risk taken on a $10,000 account against the $80,000 the account holds
after two good years, and every early trade would look disciplined.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.evaluate_compliance import TradeForCompliance
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.domain.common.enums import TradeStatus
from app.domain.rules.ast import Outcome
from app.domain.rules.context import TradeFacts
from app.domain.rules.engine import ComplianceReport, Rule
from app.infrastructure.db.models.broker import Account, AccountBalanceSnapshot
from app.infrastructure.db.models.catalog import (
    MarketCondition,
    RuleEvaluation,
    Setup,
    Strategy,
    StrategyRule,
)
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade

logger = get_logger(__name__)

#: How far back an equity snapshot may be and still describe the account at the moment
#: of a trade. Beyond this the balance is treated as unknown, which makes
#: ``risk_pct_of_equity`` unevaluable rather than wrong.
MAX_SNAPSHOT_STALENESS_DAYS = 7


class SqlAlchemyComplianceRepository:
    """Reads the inputs the rule engine needs and writes back what it decided."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Reading ---------------------------------------------------------------

    async def load_rules(self, user_id: UUID) -> dict[UUID, list[Rule]]:
        """Every active rule the user has, grouped by strategy.

        Loaded in one query rather than per trade: a year of trading is a handful of
        strategies and a few thousand trades, so the rule set is small, static within a
        run, and belongs in memory.

        Superseded strategy versions are deliberately **included**. A trade taken under
        version 1 has to keep being scored against version 1's rules — filtering to
        active strategies here would silently rescore a trader's whole history against
        rules that did not exist when they took the trades.
        """
        statement = (
            select(StrategyRule)
            .join(Strategy, Strategy.id == StrategyRule.strategy_id)
            .where(
                StrategyRule.user_id == user_id,
                StrategyRule.is_active.is_(True),
                Strategy.deleted_at.is_(None),
            )
            .order_by(StrategyRule.strategy_id, StrategyRule.code)
        )
        rows = (await self._session.execute(statement)).scalars().all()

        grouped: dict[UUID, list[Rule]] = {}
        for row in rows:
            grouped.setdefault(row.strategy_id, []).append(
                Rule(
                    rule_id=row.id,
                    code=row.code,
                    label=row.label,
                    rule_type=row.rule_type,
                    severity=row.severity,
                    expression=row.expression,
                    weight=row.weight,
                    is_active=row.is_active,
                )
            )
        return grouped

    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
        max_sessions: int | None = None,
    ) -> list[TradeForCompliance]:
        """Load closed trades with everything a rule can reference.

        Only closed trades. An open position has no exit price, no realized R and no
        final stop, so most rules would report unevaluable and the ones that did fire
        would be scoring a decision the trader has not finished making.
        """
        if session_from is None and max_sessions is not None:
            session_from = await self._session_floor(
                user_id,
                account_id=account_id,
                session_to=session_to,
                max_sessions=max_sessions,
            )
            if session_from is None:
                return []

        statement: Select[Any] = (
            select(
                Trade,
                Instrument.root_symbol,
                Instrument.exchange_timezone,
                Setup.name,
                MarketCondition.name,
                Account.max_daily_loss,
                Account.starting_balance,
            )
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .join(Account, Account.id == Trade.account_id)
            .outerjoin(Setup, Setup.id == Trade.setup_id)
            .outerjoin(MarketCondition, MarketCondition.id == Trade.market_condition_id)
            .where(Trade.user_id == user_id, Trade.status == TradeStatus.CLOSED)
            .order_by(Trade.opened_at)
        )
        if account_id is not None:
            statement = statement.where(Trade.account_id == account_id)
        if session_from is not None:
            statement = statement.where(Trade.session_date >= session_from)
        if session_to is not None:
            statement = statement.where(Trade.session_date <= session_to)

        rows = (await self._session.execute(statement)).all()
        if not rows:
            return []

        equity = await self._equity_timeline(
            user_id, account_ids={row[0].account_id for row in rows}
        )

        return [
            TradeForCompliance(
                trade_id=trade.id,
                strategy_id=trade.strategy_id,
                session_date=trade.session_date,
                closed_at=trade.closed_at,
                net_pnl=trade.net_pnl,
                facts=TradeFacts(
                    direction=trade.direction,
                    instrument=root_symbol,
                    quantity=trade.max_position_size,
                    entry_price=trade.avg_entry_price,
                    exit_price=trade.avg_exit_price,
                    entry_at=trade.opened_at,
                    exchange_timezone=exchange_timezone,
                    entry_hour=trade.entry_hour,
                    weekday=trade.entry_weekday,
                    session_segment=trade.session_segment,
                    duration_seconds=trade.duration_seconds,
                    stop_price=trade.initial_stop_price,
                    target_price=trade.initial_target_price,
                    final_stop_price=trade.final_stop_price,
                    risk_amount=trade.planned_risk_amount,
                    # An explicit None check, not ``or``: an account whose equity is
                    # exactly zero is blown, and falling back to its opening balance
                    # would measure that trade's risk against money it no longer had.
                    account_equity=_first_known(
                        equity.equity_at(trade.account_id, trade.opened_at), starting_balance
                    ),
                    planned_r_multiple=trade.planned_r_multiple,
                    net_pnl=trade.net_pnl,
                    r_multiple=trade.realized_r,
                    mae_r=trade.mae_r,
                    mfe_r=trade.mfe_r,
                    setup=setup_name,
                    market_condition=condition_name,
                    confidence=trade.confidence,
                    emotion=trade.emotion.value if trade.emotion else None,
                    daily_loss_limit=max_daily_loss,
                ),
            )
            for (
                trade,
                root_symbol,
                exchange_timezone,
                setup_name,
                condition_name,
                max_daily_loss,
                starting_balance,
            ) in rows
        ]

    async def _session_floor(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None,
        session_to: date | None,
        max_sessions: int,
    ) -> date | None:
        """The earliest session date to include, counting back ``max_sessions`` days.

        Counting *sessions* rather than trades is what keeps every loaded day complete.
        """
        statement = (
            select(Trade.session_date)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.session_date.is_not(None),
            )
            .group_by(Trade.session_date)
            .order_by(Trade.session_date.desc())
            .limit(max_sessions)
        )
        if account_id is not None:
            statement = statement.where(Trade.account_id == account_id)
        if session_to is not None:
            statement = statement.where(Trade.session_date <= session_to)

        dates = [
            value
            for value in (await self._session.execute(statement)).scalars().all()
            if value is not None
        ]
        return min(dates) if dates else None

    async def _equity_timeline(
        self, user_id: UUID, *, account_ids: set[UUID]
    ) -> _EquityTimeline:
        """Balance snapshots for the accounts involved, indexed for as-of lookup.

        One query for the whole run instead of a correlated subquery per trade: at
        100k trades the latter is 100k index scans for a number that changes daily.
        """
        statement = (
            select(
                AccountBalanceSnapshot.account_id,
                AccountBalanceSnapshot.as_of,
                AccountBalanceSnapshot.equity,
            )
            .where(
                AccountBalanceSnapshot.user_id == user_id,
                AccountBalanceSnapshot.account_id.in_(account_ids),
                AccountBalanceSnapshot.equity.is_not(None),
            )
            .order_by(AccountBalanceSnapshot.account_id, AccountBalanceSnapshot.as_of)
        )
        rows = (await self._session.execute(statement)).all()

        timeline: dict[UUID, tuple[list[datetime], list[Decimal]]] = {}
        for account, as_of, equity in rows:
            times, values = timeline.setdefault(account, ([], []))
            times.append(as_of)
            values.append(equity)
        return _EquityTimeline(timeline)

    async def load_rule_outcomes(
        self, user_id: UUID, *, rule_id: UUID
    ) -> list[tuple[Outcome, Decimal]]:
        """Stored verdicts for one rule, paired with what the trade made or lost.

        Read from ``rule_evaluations`` rather than recomputed, so the impact figures a
        trader sees match the report they were shown at the time.
        """
        statement = (
            select(RuleEvaluation.passed, RuleEvaluation.evaluable, Trade.net_pnl)
            .join(Trade, Trade.id == RuleEvaluation.trade_id)
            .where(RuleEvaluation.user_id == user_id, RuleEvaluation.rule_id == rule_id)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            (
                Outcome.UNEVALUABLE
                if not evaluable
                else (Outcome.PASS if passed else Outcome.FAIL),
                net_pnl,
            )
            for passed, evaluable, net_pnl in rows
        ]

    # --- Writing ---------------------------------------------------------------

    async def save_reports(self, user_id: UUID, reports: list[ComplianceReport]) -> int:
        """Upsert one row per (trade, rule).

        Upsert rather than insert because compliance is recomputed whenever a trade is
        enriched — a stop is inferred, a setup is corrected — and history should
        converge on the current verdict rather than accumulate every past guess.
        """
        rows: list[dict[str, Any]] = [
            {
                "id": uuid7(),
                "user_id": user_id,
                "trade_id": report.trade_id,
                "rule_id": result.rule.rule_id,
                "passed": result.outcome is Outcome.PASS,
                "evaluable": result.outcome is not Outcome.UNEVALUABLE,
                "detail": result.to_payload(),
                "evaluated_at": report.evaluated_at,
            }
            for report in reports
            for result in report.results
        ]
        if not rows:
            return 0

        statement = insert(RuleEvaluation).values(rows)
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_rule_evaluations_trade_rule",
                set_={
                    column: getattr(statement.excluded, column)
                    for column in ("passed", "evaluable", "detail", "evaluated_at")
                },
            )
        )
        logger.info("compliance.reports_saved", user_id=str(user_id), rows=len(rows))
        return len(rows)

    async def save_scores(self, scores: dict[UUID, Decimal | None]) -> int:
        """Write each trade's compliance score.

        ``None`` is written as ``NULL`` deliberately: a trade whose rules could not be
        checked has no score, and storing zero would be indistinguishable from total
        indiscipline in every aggregate built on the column.
        """
        if not scores:
            return 0
        await self._session.execute(
            update(Trade),
            [
                {"id": trade_id, "compliance_score": score}
                for trade_id, score in scores.items()
            ],
        )
        return len(scores)


def _first_known(*candidates: Decimal | None) -> Decimal | None:
    """First candidate that is not ``None``, preserving a legitimate zero."""
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


class _EquityTimeline:
    """As-of equity lookup over pre-sorted snapshots."""

    def __init__(self, timeline: dict[UUID, tuple[list[datetime], list[Decimal]]]) -> None:
        self._timeline = timeline

    def equity_at(self, account_id: UUID, moment: datetime) -> Decimal | None:
        entry = self._timeline.get(account_id)
        if entry is None:
            return None
        times, values = entry
        index = bisect_right(times, moment)
        if index == 0:
            # Every snapshot postdates the trade. Extrapolating backwards from a later
            # balance would fold the trade's own result into the equity it is measured
            # against.
            return None
        age = moment - times[index - 1]
        if age.days > MAX_SNAPSHOT_STALENESS_DAYS:
            return None
        return values[index - 1]


class SqlAlchemyStrategyRepository:
    """Strategies and their rules.

    Strategies are versioned rather than edited in place. A compliance score is only
    meaningful against the rules that were actually in force when the trade was taken,
    so changing a rule creates a new version and leaves the old one — and every score
    computed under it — intact.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_strategies(self, user_id: UUID, *, include_inactive: bool = False) -> list[
        tuple[Strategy, list[StrategyRule]]
    ]:
        statement = (
            select(Strategy)
            .where(Strategy.user_id == user_id, Strategy.deleted_at.is_(None))
            .order_by(Strategy.name, Strategy.version.desc())
        )
        if not include_inactive:
            statement = statement.where(Strategy.is_active.is_(True))

        strategies = (await self._session.execute(statement)).scalars().all()
        if not strategies:
            return []

        rules = (
            (
                await self._session.execute(
                    select(StrategyRule)
                    .where(StrategyRule.strategy_id.in_([s.id for s in strategies]))
                    .order_by(StrategyRule.code)
                )
            )
            .scalars()
            .all()
        )
        by_strategy: dict[UUID, list[StrategyRule]] = {}
        for rule in rules:
            by_strategy.setdefault(rule.strategy_id, []).append(rule)
        return [(strategy, by_strategy.get(strategy.id, [])) for strategy in strategies]

    async def get_strategy(
        self, user_id: UUID, strategy_id: UUID
    ) -> tuple[Strategy, list[StrategyRule]] | None:
        strategy = (
            await self._session.execute(
                select(Strategy).where(
                    Strategy.id == strategy_id,
                    Strategy.user_id == user_id,
                    Strategy.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if strategy is None:
            return None
        rules = (
            (
                await self._session.execute(
                    select(StrategyRule)
                    .where(StrategyRule.strategy_id == strategy.id)
                    .order_by(StrategyRule.code)
                )
            )
            .scalars()
            .all()
        )
        return strategy, list(rules)

    async def create_strategy(
        self,
        *,
        user_id: UUID,
        name: str,
        description: str | None,
        rules: Sequence[dict[str, Any]],
        checklist: dict[str, Any] | None = None,
    ) -> tuple[Strategy, list[StrategyRule]]:
        version = await self._next_version(user_id, name)
        strategy = Strategy(
            id=uuid7(),
            user_id=user_id,
            name=name,
            description=description,
            version=version,
            checklist=checklist or {},
        )
        self._session.add(strategy)
        created = [self._build_rule(user_id, strategy.id, rule) for rule in rules]
        self._session.add_all(created)
        await self._session.flush()
        return strategy, created

    async def revise_strategy(
        self,
        *,
        user_id: UUID,
        strategy: Strategy,
        rules: Sequence[dict[str, Any]],
        description: str | None = None,
        checklist: dict[str, Any] | None = None,
    ) -> tuple[Strategy, list[StrategyRule]]:
        """Supersede a strategy with a new version carrying the revised rules.

        The previous version is deactivated but kept, and the new one points back at it
        through ``supersedes_id``, so "what changed, and when?" stays answerable.
        """
        strategy.is_active = False
        successor = Strategy(
            id=uuid7(),
            user_id=user_id,
            name=strategy.name,
            description=description if description is not None else strategy.description,
            version=await self._next_version(user_id, strategy.name),
            supersedes_id=strategy.id,
            checklist=checklist if checklist is not None else dict(strategy.checklist),
        )
        self._session.add(successor)
        created = [self._build_rule(user_id, successor.id, rule) for rule in rules]
        self._session.add_all(created)
        await self._session.flush()
        return successor, created

    async def _next_version(self, user_id: UUID, name: str) -> int:
        highest = (
            await self._session.execute(
                select(func.max(Strategy.version)).where(
                    Strategy.user_id == user_id, Strategy.name == name
                )
            )
        ).scalar_one_or_none()
        return int(highest or 0) + 1

    def _build_rule(
        self, user_id: UUID, strategy_id: UUID, payload: dict[str, Any]
    ) -> StrategyRule:
        return StrategyRule(
            id=uuid7(),
            user_id=user_id,
            strategy_id=strategy_id,
            code=payload["code"],
            label=payload["label"],
            description=payload.get("description"),
            rule_type=payload["rule_type"],
            severity=payload["severity"],
            weight=payload.get("weight", Decimal(1)),
            expression=payload["expression"],
            is_active=payload.get("is_active", True),
        )
