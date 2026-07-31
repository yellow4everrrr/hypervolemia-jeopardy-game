"""Declarative rule expressions.

A trader's rules are stored as a small predicate AST — a JSON tree of comparisons and
boolean combinators — and evaluated by an interpreter here. **Nothing user-supplied is
ever executed.** The obvious alternative, storing a Python or JavaScript snippet and
running it, would turn every user's strategy definition into remote code execution
against our own servers.

The AST buys three other things beyond safety:

* **Inspectable.** A rule can be rendered back into English for the compliance report,
  because its structure is data rather than opaque source.
* **Diffable.** Strategy versions can be compared field by field, so "what changed
  between v2 and v3?" has a real answer.
* **Three-valued.** Evaluation returns pass, fail, *or* unevaluable. A rule about stop
  placement cannot be checked on a trade with no recorded stop, and scoring that as a
  violation would manufacture indiscipline out of missing data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.core.errors import ValidationError

#: Maximum nesting depth. A rule tree deeper than this is either generated or hostile,
#: and either way the recursive evaluator should not follow it.
MAX_DEPTH = 12


class Operator(StrEnum):
    """Comparisons a rule can make."""

    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    BETWEEN = "between"
    IN = "in"
    NOT_IN = "not_in"
    IS_SET = "is_set"
    IS_NOT_SET = "is_not_set"
    CONTAINS = "contains"


class Combinator(StrEnum):
    ALL = "all"
    ANY = "any"
    NONE = "none"


class Outcome(StrEnum):
    """Three-valued result.

    ``UNEVALUABLE`` is the important one: it keeps "I could not check this" distinct
    from "you broke this", which is the difference between a compliance score that
    means something and one that punishes incomplete data.
    """

    PASS = "pass"
    FAIL = "fail"
    UNEVALUABLE = "unevaluable"


@dataclass(frozen=True, slots=True)
class Evaluation:
    outcome: Outcome
    #: Human-readable account of what was compared, for the compliance report.
    detail: str
    observed: Any = None
    expected: Any = None

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS

    @property
    def failed(self) -> bool:
        return self.outcome is Outcome.FAIL


def parse(expression: dict[str, Any], *, depth: int = 0) -> Node:
    """Build a node tree from stored JSON, validating as it goes.

    Validation happens at parse time rather than evaluation time so a malformed rule is
    rejected when the trader saves it, not silently skipped months later while their
    compliance score quietly means nothing.
    """
    if depth > MAX_DEPTH:
        raise ValidationError(f"rule expression nests deeper than {MAX_DEPTH} levels")
    if not isinstance(expression, dict) or not expression:
        raise ValidationError("a rule expression must be a non-empty object")

    for combinator in Combinator:
        if combinator.value in expression:
            children = expression[combinator.value]
            if not isinstance(children, list) or not children:
                raise ValidationError(f"{combinator.value!r} needs a non-empty list of rules")
            return Group(
                combinator=combinator,
                children=tuple(parse(child, depth=depth + 1) for child in children),
            )

    if "field" not in expression:
        raise ValidationError(
            "a rule must be a comparison with a 'field', or a combinator "
            f"({', '.join(c.value for c in Combinator)})"
        )

    raw_operator = expression.get("op", Operator.EQ.value)
    try:
        operator = Operator(raw_operator)
    except ValueError as exc:
        raise ValidationError(
            f"unknown operator {raw_operator!r}",
            details={"allowed": [op.value for op in Operator]},
        ) from exc

    return Comparison(
        field=str(expression["field"]),
        operator=operator,
        value=expression.get("value"),
        label=expression.get("label"),
    )


@dataclass(frozen=True, slots=True)
class Node:
    """Base for the two node kinds."""

    def evaluate(self, context: dict[str, Any]) -> Evaluation:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def fields(self) -> set[str]:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Comparison(Node):
    field: str
    operator: Operator
    value: Any = None
    label: str | None = None

    def fields(self) -> set[str]:
        return {self.field}

    def describe(self) -> str:
        if self.label:
            return self.label
        readable = self.field.replace("_", " ")
        phrases = {
            Operator.EQ: f"{readable} is {self.value}",
            Operator.NE: f"{readable} is not {self.value}",
            Operator.LT: f"{readable} is below {self.value}",
            Operator.LTE: f"{readable} is at most {self.value}",
            Operator.GT: f"{readable} is above {self.value}",
            Operator.GTE: f"{readable} is at least {self.value}",
            Operator.BETWEEN: f"{readable} is between {self.value}",
            Operator.IN: f"{readable} is one of {self.value}",
            Operator.NOT_IN: f"{readable} is not one of {self.value}",
            Operator.IS_SET: f"{readable} is recorded",
            Operator.IS_NOT_SET: f"{readable} is not recorded",
            Operator.CONTAINS: f"{readable} contains {self.value}",
        }
        return phrases[self.operator]

    def evaluate(self, context: dict[str, Any]) -> Evaluation:
        present = self.field in context
        observed = context.get(self.field)

        if self.operator is Operator.IS_SET:
            outcome = Outcome.PASS if present and observed is not None else Outcome.FAIL
            return Evaluation(outcome, self.describe(), observed)
        if self.operator is Operator.IS_NOT_SET:
            outcome = Outcome.PASS if not present or observed is None else Outcome.FAIL
            return Evaluation(outcome, self.describe(), observed)

        if not present or observed is None:
            # The distinction the whole module turns on. A rule about stop distance
            # cannot be checked without a stop, and calling that a violation would
            # invent indiscipline out of a gap in the data.
            return Evaluation(
                Outcome.UNEVALUABLE,
                f"{self.field.replace('_', ' ')} was not recorded for this trade",
                None,
                self.value,
            )

        try:
            result = self._compare(observed)
        except (TypeError, InvalidOperation, ValueError) as exc:
            return Evaluation(
                Outcome.UNEVALUABLE,
                f"could not compare {self.field}: {exc}",
                observed,
                self.value,
            )

        return Evaluation(
            Outcome.PASS if result else Outcome.FAIL,
            self.describe(),
            observed,
            self.value,
        )

    def _compare(self, observed: Any) -> bool:
        if self.operator is Operator.IN:
            return bool(observed in _as_list(self.value))
        if self.operator is Operator.NOT_IN:
            return bool(observed not in _as_list(self.value))
        if self.operator is Operator.CONTAINS:
            return bool(self.value in _as_list(observed))
        if self.operator is Operator.BETWEEN:
            bounds = _as_list(self.value)
            if len(bounds) != 2:
                raise ValueError("'between' needs exactly two bounds")
            low, high = _coerce(observed, bounds[0]), _coerce(observed, bounds[1])
            value = _normalise(observed)
            return bool(low <= value <= high)

        expected = _coerce(observed, self.value)
        value = _normalise(observed)

        if self.operator is Operator.EQ:
            return bool(value == expected)
        if self.operator is Operator.NE:
            return bool(value != expected)
        if self.operator is Operator.LT:
            return bool(value < expected)
        if self.operator is Operator.LTE:
            return bool(value <= expected)
        if self.operator is Operator.GT:
            return bool(value > expected)
        return bool(value >= expected)  # GTE


@dataclass(frozen=True, slots=True)
class Group(Node):
    combinator: Combinator
    children: tuple[Node, ...]

    def fields(self) -> set[str]:
        return set().union(*(child.fields() for child in self.children))

    def describe(self) -> str:
        joiner = {Combinator.ALL: " and ", Combinator.ANY: " or ", Combinator.NONE: " nor "}
        inner = joiner[self.combinator].join(child.describe() for child in self.children)
        if self.combinator is Combinator.NONE:
            return f"neither {inner}"
        return f"({inner})" if len(self.children) > 1 else inner

    def evaluate(self, context: dict[str, Any]) -> Evaluation:
        """Combine children, propagating unevaluability only when it changes the answer.

        An ``any`` group with one passing child is a pass even if a sibling could not be
        checked — the rule is satisfied regardless. An ``all`` group with one failing
        child is a fail for the same reason. Unevaluable propagates only when the
        remaining children cannot settle it.
        """
        results = [child.evaluate(context) for child in self.children]
        passed = [result for result in results if result.outcome is Outcome.PASS]
        failed = [result for result in results if result.outcome is Outcome.FAIL]
        unknown = [result for result in results if result.outcome is Outcome.UNEVALUABLE]

        if self.combinator is Combinator.ALL:
            if failed:
                return Evaluation(
                    Outcome.FAIL, failed[0].detail, failed[0].observed, failed[0].expected
                )
            if unknown:
                return Evaluation(Outcome.UNEVALUABLE, unknown[0].detail)
            return Evaluation(Outcome.PASS, self.describe())

        if self.combinator is Combinator.ANY:
            if passed:
                return Evaluation(Outcome.PASS, passed[0].detail, passed[0].observed)
            if unknown:
                return Evaluation(Outcome.UNEVALUABLE, unknown[0].detail)
            return Evaluation(Outcome.FAIL, self.describe())

        # NONE: passes when nothing matched.
        if passed:
            return Evaluation(Outcome.FAIL, passed[0].detail, passed[0].observed)
        if unknown:
            return Evaluation(Outcome.UNEVALUABLE, unknown[0].detail)
        return Evaluation(Outcome.PASS, self.describe())


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list | tuple | set):
        return list(value)
    return [value]


def _normalise(value: Any) -> Any:
    """Make a context value comparable: times and decimals keep their own semantics."""
    if isinstance(value, bool | Decimal | int | time):
        return value
    if isinstance(value, float):
        # Should never happen — the whole system is Decimal — but coerce rather than
        # compare a float against a Decimal and raise.
        return Decimal(str(value))
    return value


def _coerce(observed: Any, expected: Any) -> Any:
    """Coerce the rule's stored value to the type of what it is being compared against.

    Rules are stored as JSON, so a threshold arrives as a string or an int while the
    observed value is a ``Decimal`` or a ``time``. Coercing here keeps the stored rule
    format simple and human-editable.
    """
    if isinstance(observed, bool):
        if isinstance(expected, str):
            return expected.lower() in {"true", "1", "yes"}
        return bool(expected)
    if isinstance(observed, Decimal):
        return Decimal(str(expected))
    if isinstance(observed, time) and isinstance(expected, str):
        hours, _, minutes = expected.partition(":")
        return time(int(hours), int(minutes or 0))
    if isinstance(observed, int) and isinstance(expected, str):
        return int(expected)
    return expected


def validate(expression: dict[str, Any]) -> set[str]:
    """Parse a rule and return the context fields it references.

    Called when a strategy is saved. The field set lets the API tell a trader that a
    rule references something the journal does not record, instead of accepting it and
    silently reporting it as unevaluable forever.
    """
    return parse(expression).fields()
