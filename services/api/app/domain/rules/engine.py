"""Compliance scoring: how well a trade followed the strategy it was taken under.

Turns "did I follow my plan?" from a feeling into a number with a rule-by-rule audit
trail. Two design decisions carry most of the weight:

**Unevaluable rules are excluded from the denominator, not counted as passes or
failures.** A strategy with ten rules where six cannot be checked scores on the four
that could, and reports that it did. Counting unevaluable as a pass would inflate every
score; counting it as a failure would punish a trader for the journal's own missing
data. Both are worse than saying so.

**Severity is weighted, and one critical violation caps the score.** Averaging a
critical breach against nine passed advisories produces 90% compliance for a trade that
broke the single rule that mattered. Trading discipline is not an average — blowing
through a hard risk limit is categorically different from entering half a point late.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.money import safe_divide
from app.domain.common.enums import RuleSeverity, RuleType
from app.domain.rules.ast import Evaluation, Node, Outcome, parse

#: Relative weight per severity. Ratios chosen so that a single major violation cannot
#: be offset by passing several advisories.
SEVERITY_WEIGHT: dict[RuleSeverity, Decimal] = {
    RuleSeverity.ADVISORY: Decimal("0.5"),
    RuleSeverity.MINOR: Decimal("1"),
    RuleSeverity.MAJOR: Decimal("3"),
    RuleSeverity.CRITICAL: Decimal("8"),
}

#: A trade that breaches a critical rule cannot score above this, however many other
#: rules it satisfied. Blowing a hard risk limit is not offset by good entry timing.
CRITICAL_VIOLATION_CEILING = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class Rule:
    """One machine-evaluable rule, as the engine sees it."""

    rule_id: UUID
    code: str
    label: str
    rule_type: RuleType
    severity: RuleSeverity
    expression: dict[str, Any]
    weight: Decimal = Decimal(1)
    is_active: bool = True

    @property
    def effective_weight(self) -> Decimal:
        return self.weight * SEVERITY_WEIGHT[self.severity]

    def node(self) -> Node:
        return parse(self.expression)


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule: Rule
    evaluation: Evaluation

    @property
    def outcome(self) -> Outcome:
        return self.evaluation.outcome

    @property
    def is_violation(self) -> bool:
        return self.evaluation.outcome is Outcome.FAIL

    def to_payload(self) -> dict[str, Any]:
        return {
            "rule_id": str(self.rule.rule_id),
            "code": self.rule.code,
            "label": self.rule.label,
            "rule_type": self.rule.rule_type.value,
            "severity": self.rule.severity.value,
            "outcome": self.evaluation.outcome.value,
            "detail": self.evaluation.detail,
            "observed": _stringify(self.evaluation.observed),
            "expected": _stringify(self.evaluation.expected),
        }


@dataclass(frozen=True, slots=True)
class ComplianceReport:
    """Rule-by-rule verdict for one trade."""

    trade_id: UUID
    strategy_id: UUID | None
    results: tuple[RuleResult, ...]
    evaluated_at: datetime

    @property
    def evaluated(self) -> tuple[RuleResult, ...]:
        """Rules that could actually be checked — the scoring denominator."""
        return tuple(
            result for result in self.results if result.outcome is not Outcome.UNEVALUABLE
        )

    @property
    def violations(self) -> tuple[RuleResult, ...]:
        return tuple(result for result in self.results if result.is_violation)

    @property
    def unevaluable(self) -> tuple[RuleResult, ...]:
        return tuple(
            result for result in self.results if result.outcome is Outcome.UNEVALUABLE
        )

    @property
    def has_critical_violation(self) -> bool:
        return any(
            result.rule.severity is RuleSeverity.CRITICAL for result in self.violations
        )

    @property
    def score(self) -> Decimal | None:
        """Weighted fraction of checkable rules that passed, 0–1.

        ``None`` when nothing could be checked. A score of zero would claim total
        indiscipline; the honest answer is that the journal lacks the data to say.
        """
        checkable = self.evaluated
        if not checkable:
            return None

        total = sum((result.rule.effective_weight for result in checkable), start=Decimal(0))
        earned = sum(
            (
                result.rule.effective_weight
                for result in checkable
                if result.outcome is Outcome.PASS
            ),
            start=Decimal(0),
        )
        raw = safe_divide(earned, total)
        if raw is None:  # pragma: no cover — total is positive by construction
            return None
        return min(raw, CRITICAL_VIOLATION_CEILING) if self.has_critical_violation else raw

    @property
    def coverage(self) -> Decimal | None:
        """Fraction of rules that could be checked at all.

        Reported beside the score, because 100% compliance on two of twelve rules is a
        different claim from 100% on all twelve.
        """
        if not self.results:
            return None
        return safe_divide(Decimal(len(self.evaluated)), Decimal(len(self.results)))

    def to_payload(self) -> dict[str, Any]:
        return {
            "trade_id": str(self.trade_id),
            "strategy_id": str(self.strategy_id) if self.strategy_id else None,
            "evaluated_at": self.evaluated_at.isoformat(),
            "score": str(self.score) if self.score is not None else None,
            "coverage": str(self.coverage) if self.coverage is not None else None,
            "rules_total": len(self.results),
            "rules_evaluated": len(self.evaluated),
            "rules_unevaluable": len(self.unevaluable),
            "violations": len(self.violations),
            "has_critical_violation": self.has_critical_violation,
            "results": [result.to_payload() for result in self.results],
        }


def evaluate(
    *,
    trade_id: UUID,
    strategy_id: UUID | None,
    rules: list[Rule],
    context: dict[str, Any],
    now: datetime,
) -> ComplianceReport:
    """Score one trade against a strategy's rules.

    ``context`` is the flat fact set the trade exposes — see
    :func:`app.domain.rules.context.build_context`. Rules reference its keys by name,
    and a key that is absent makes the rule unevaluable rather than failed.
    """
    results = [
        RuleResult(rule=rule, evaluation=rule.node().evaluate(context))
        for rule in rules
        if rule.is_active
    ]
    return ComplianceReport(
        trade_id=trade_id,
        strategy_id=strategy_id,
        results=tuple(results),
        evaluated_at=now,
    )


@dataclass(frozen=True, slots=True)
class RuleImpact:
    """What breaking one rule has historically cost.

    The number that turns a compliance report into a reason to change behaviour.
    "You violated your max-size rule 14 times" is a scolding; "those 14 trades averaged
    −$180 against +$62 for the rest, a difference of $3,388" is an argument.
    """

    rule_code: str
    rule_label: str
    severity: RuleSeverity
    violations: int
    compliant_trades: int
    mean_pnl_when_violated: Decimal | None
    mean_pnl_when_followed: Decimal | None
    total_cost: Decimal | None

    @property
    def difference(self) -> Decimal | None:
        if self.mean_pnl_when_violated is None or self.mean_pnl_when_followed is None:
            return None
        return self.mean_pnl_when_violated - self.mean_pnl_when_followed

    @property
    def is_costly(self) -> bool:
        difference = self.difference
        return difference is not None and difference < 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "rule_label": self.rule_label,
            "severity": self.severity.value,
            "violations": self.violations,
            "compliant_trades": self.compliant_trades,
            "mean_pnl_when_violated": _stringify(self.mean_pnl_when_violated),
            "mean_pnl_when_followed": _stringify(self.mean_pnl_when_followed),
            "difference": _stringify(self.difference),
            "total_cost": _stringify(self.total_cost),
            "is_costly": self.is_costly,
        }


def measure_impact(
    rule: Rule,
    outcomes: list[tuple[Outcome, Decimal]],
) -> RuleImpact:
    """Compare P&L on trades that broke a rule against trades that followed it.

    Unevaluable trades are excluded from both groups — including them in either would
    contaminate the comparison with trades the rule never actually applied to.
    """
    from app.analytics.statistics import mean

    violated = [pnl for outcome, pnl in outcomes if outcome is Outcome.FAIL]
    followed = [pnl for outcome, pnl in outcomes if outcome is Outcome.PASS]

    mean_violated = mean(violated)
    mean_followed = mean(followed)
    cost = None
    if mean_violated is not None and mean_followed is not None and violated:
        cost = (mean_violated - mean_followed) * Decimal(len(violated))

    return RuleImpact(
        rule_code=rule.code,
        rule_label=rule.label,
        severity=rule.severity,
        violations=len(violated),
        compliant_trades=len(followed),
        mean_pnl_when_violated=mean_violated,
        mean_pnl_when_followed=mean_followed,
        total_cost=cost,
    )


def _stringify(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list | tuple):
        return [_stringify(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
