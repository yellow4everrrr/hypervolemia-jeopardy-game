"""Strategy builder and compliance endpoints.

The builder endpoints exist so a rule can be composed in the UI without the client
needing to know the field vocabulary or the expression grammar: ``/fields`` is the
vocabulary, ``/starter-rules`` is a working set to start from, and ``/validate`` reads
an expression back in English before it is saved.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.application.use_cases.evaluate_compliance import EvaluateCompliance
from app.core.errors import NotFoundError, ValidationError
from app.domain.rules.ast import parse
from app.domain.rules.context import AVAILABLE_FIELDS, STARTER_RULES, unknown_fields
from app.domain.rules.engine import SEVERITY_WEIGHT, Rule, measure_impact
from app.infrastructure.db.repositories.compliance import (
    SqlAlchemyComplianceRepository,
    SqlAlchemyStrategyRepository,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.interfaces.http.deps import CurrentUserDep, SessionDep
from app.interfaces.http.schemas.strategies import (
    RuleSummary,
    RuleValidationRequest,
    RuleValidationResponse,
    StrategyInput,
    StrategyList,
    StrategySummary,
)

router = APIRouter(prefix="/strategies", tags=["strategies"])
compliance_router = APIRouter(prefix="/compliance", tags=["compliance"])

#: How many sessions a compliance run covers by default. Bounded because the run loads
#: whole sessions and scores every trade in them; an unbounded default would turn a
#: dashboard render into a full-history rescore.
DEFAULT_SESSION_WINDOW = 90
MAX_SESSION_WINDOW = 750


@router.get("/fields", summary="Fields a rule may reference")
async def fields() -> dict[str, Any]:
    """The rule vocabulary, with a description per field.

    Published rather than documented: the rule builder renders this list, so a field
    added to the engine appears in the UI without a client release.
    """
    return {
        "fields": [
            {"name": name, "description": description}
            for name, description in sorted(AVAILABLE_FIELDS.items())
        ],
        "severity_weights": {
            severity.value: str(weight) for severity, weight in SEVERITY_WEIGHT.items()
        },
    }


@router.get("/starter-rules", summary="Suggested rules for a new strategy")
async def starter_rules() -> dict[str, Any]:
    """Templates offered during onboarding.

    Every one is checkable from data the journal imports automatically. A template that
    depended on manual input would score as unevaluable from day one and teach the
    trader that the compliance number does not mean anything.
    """
    return {
        "rules": [
            {**rule, "reads_as": parse(rule["expression"]).describe()} for rule in STARTER_RULES
        ]
    }


@router.post("/validate", summary="Check a rule expression")
async def validate_rule(payload: RuleValidationRequest) -> RuleValidationResponse:
    """Parse an expression and read it back in English."""
    node = parse(payload.expression)
    referenced = node.fields()
    unknown = unknown_fields(referenced)
    if unknown:
        raise ValidationError(
            f"rule references unknown field(s): {', '.join(sorted(unknown))}",
            details={"unknown_fields": sorted(unknown), "available": sorted(AVAILABLE_FIELDS)},
        )
    return RuleValidationResponse(
        is_valid=True, reads_as=node.describe(), fields=sorted(referenced)
    )


@router.get("", summary="List strategies")
async def list_strategies(
    user: CurrentUserDep,
    session: SessionDep,
    include_inactive: Annotated[bool, Query()] = False,
) -> StrategyList:
    repository = SqlAlchemyStrategyRepository(session)
    rows = await repository.list_strategies(user.id, include_inactive=include_inactive)
    return StrategyList(items=[_to_summary(strategy, rules) for strategy, rules in rows])


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a strategy")
async def create_strategy(
    payload: StrategyInput, user: CurrentUserDep, session: SessionDep
) -> StrategySummary:
    repository = SqlAlchemyStrategyRepository(session)
    strategy, rules = await repository.create_strategy(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        checklist=payload.checklist,
        rules=[rule.model_dump() for rule in payload.rules],
    )
    await SqlAlchemyUnitOfWork(session).commit()
    return _to_summary(strategy, rules)


@router.post("/{strategy_id}/revisions", summary="Revise a strategy")
async def revise_strategy(
    strategy_id: UUID, payload: StrategyInput, user: CurrentUserDep, session: SessionDep
) -> StrategySummary:
    """Create the next version of a strategy rather than editing it in place.

    Editing rules in place would silently rewrite history: yesterday's 92% compliance
    would be recomputed against rules that did not exist yesterday. Versioning keeps
    every past score reproducible against the rules that were actually in force.
    """
    repository = SqlAlchemyStrategyRepository(session)
    existing = await repository.get_strategy(user.id, strategy_id)
    if existing is None:
        raise NotFoundError(f"strategy {strategy_id} not found")

    strategy, rules = await repository.revise_strategy(
        user_id=user.id,
        strategy=existing[0],
        description=payload.description,
        checklist=payload.checklist,
        rules=[rule.model_dump() for rule in payload.rules],
    )
    await SqlAlchemyUnitOfWork(session).commit()
    return _to_summary(strategy, rules)


@router.get("/{strategy_id}", summary="One strategy with its rules")
async def get_strategy(
    strategy_id: UUID, user: CurrentUserDep, session: SessionDep
) -> StrategySummary:
    found = await SqlAlchemyStrategyRepository(session).get_strategy(user.id, strategy_id)
    if found is None:
        raise NotFoundError(f"strategy {strategy_id} not found")
    return _to_summary(*found)


@compliance_router.post("/evaluate", summary="Score trades against their strategy rules")
async def evaluate_compliance(
    user: CurrentUserDep,
    session: SessionDep,
    account_id: Annotated[UUID | None, Query()] = None,
    session_from: Annotated[date | None, Query()] = None,
    session_to: Annotated[date | None, Query()] = None,
    sessions: Annotated[int, Query(ge=1, le=MAX_SESSION_WINDOW)] = DEFAULT_SESSION_WINDOW,
    persist: Annotated[bool, Query(description="Store scores and per-rule verdicts")] = True,
) -> dict[str, Any]:
    """Run the rule engine over the caller's trades.

    ``sessions`` bounds the run by *trading days*, not trades, because a session is the
    unit the sequence-sensitive rules are defined over — half a day would misreport
    every trade in it.
    """
    if session_from and session_to and session_from > session_to:
        raise ValidationError("session_from must not be after session_to")

    use_case = EvaluateCompliance(
        repository=SqlAlchemyComplianceRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
    outcome = await use_case.execute(
        user_id=user.id,
        account_id=account_id,
        session_from=session_from,
        session_to=session_to,
        max_sessions=sessions,
        persist=persist,
    )
    return outcome.to_payload()


@compliance_router.get("/trades/{trade_id}", summary="Rule-by-rule verdict for one trade")
async def trade_compliance(
    trade_id: UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """The stored report for one trade.

    Read from ``rule_evaluations`` rather than recomputed so the trader sees the same
    verdict the score was built from, not a fresh evaluation against rules that may
    have been revised since.
    """
    from sqlalchemy import select

    from app.infrastructure.db.models.catalog import RuleEvaluation, StrategyRule
    from app.infrastructure.db.models.trading import Trade

    trade = (
        await session.execute(
            select(Trade).where(Trade.id == trade_id, Trade.user_id == user.id)
        )
    ).scalar_one_or_none()
    if trade is None:
        raise NotFoundError(f"trade {trade_id} not found")

    rows = (
        await session.execute(
            select(RuleEvaluation, StrategyRule)
            .join(StrategyRule, StrategyRule.id == RuleEvaluation.rule_id)
            .where(RuleEvaluation.trade_id == trade_id, RuleEvaluation.user_id == user.id)
            .order_by(StrategyRule.code)
        )
    ).all()

    return {
        "trade_id": str(trade_id),
        "strategy_id": str(trade.strategy_id) if trade.strategy_id else None,
        "compliance_score": (
            str(trade.compliance_score) if trade.compliance_score is not None else None
        ),
        "rules_total": len(rows),
        "rules_evaluated": sum(1 for evaluation, _ in rows if evaluation.evaluable),
        "violations": sum(
            1 for evaluation, _ in rows if evaluation.evaluable and not evaluation.passed
        ),
        "results": [
            {
                **evaluation.detail,
                "evaluated_at": evaluation.evaluated_at.isoformat(),
            }
            for evaluation, _ in rows
        ],
    }


@compliance_router.get("/rules/{rule_id}/impact", summary="What breaking one rule cost")
async def rule_impact(
    rule_id: UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Compare P&L on trades that broke a rule against trades that followed it.

    This is a *descriptive* comparison, not a causal one. Trades that broke a rule
    differ from trades that did not in ways beyond the rule itself — the same state of
    mind that widens a stop also picks worse entries — so the difference is what
    violation is associated with, not what it caused. The payload says so, because a
    number this persuasive needs its caveat attached to it rather than in a footnote.
    """
    from sqlalchemy import select

    from app.infrastructure.db.models.catalog import StrategyRule

    row = (
        await session.execute(
            select(StrategyRule).where(
                StrategyRule.id == rule_id, StrategyRule.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"rule {rule_id} not found")

    repository = SqlAlchemyComplianceRepository(session)
    outcomes = await repository.load_rule_outcomes(user.id, rule_id=rule_id)
    rule = Rule(
        rule_id=row.id,
        code=row.code,
        label=row.label,
        rule_type=row.rule_type,
        severity=row.severity,
        expression=row.expression,
        weight=row.weight,
        is_active=row.is_active,
    )
    impact = measure_impact(rule, outcomes)
    return {
        **impact.to_payload(),
        "unevaluable_trades": sum(1 for outcome, _ in outcomes if outcome.value == "unevaluable"),
        "interpretation": (
            "Descriptive comparison of trades that broke this rule against trades that "
            "followed it. Violations are not randomly assigned, so this measures "
            "association, not causation."
        ),
    }


def _to_summary(strategy: Any, rules: list[Any]) -> StrategySummary:
    """Project a strategy and its rules onto the wire without touching the ORM relationship.

    Every field is named explicitly rather than going through
    ``StrategySummary.model_validate(strategy)``. That call looks equivalent and is not:
    ``StrategySummary`` declares a ``rules`` field and sets ``from_attributes=True``, so
    Pydantic reads ``strategy.rules`` during validation — a **lazy relationship**. Under
    asyncio a lazy load raises ``MissingGreenlet`` rather than emitting a query, so
    ``GET /strategies`` returned a 500 for any user who actually had a strategy.

    The overwrite on the next line made it look safe: the previous version assigned the
    correct rules immediately after validating, so the relationship was never *used*. It
    was still read, and reading was the whole problem.

    Nothing caught this because every test built strategies through the repository inside
    the session that created them, where the relationship is already populated. The first
    request that loaded a strategy from a cold session was the demo — which is exactly the
    class of defect that only running the application finds (ADR 0016).
    """
    return StrategySummary(
        id=strategy.id,
        name=strategy.name,
        description=strategy.description,
        version=strategy.version,
        supersedes_id=strategy.supersedes_id,
        is_active=strategy.is_active,
        checklist=strategy.checklist,
        created_at=strategy.created_at,
        rules=[RuleSummary.model_validate(rule) for rule in rules],
    )
