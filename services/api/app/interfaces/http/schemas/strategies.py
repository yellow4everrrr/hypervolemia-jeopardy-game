"""API schemas for strategies, rules and compliance.

Rule expressions are validated here as *structure*, not just as JSON. A rule that
references a field the journal does not record is rejected at save time, because the
alternative is a rule that reads as unevaluable forever and quietly shrinks the
denominator of every compliance score the trader looks at.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import ValidationError
from app.domain.common.enums import RuleSeverity, RuleType
from app.domain.rules.ast import parse
from app.domain.rules.context import unknown_fields


class RuleInput(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]
    label: Annotated[str, Field(min_length=1, max_length=200)]
    description: str | None = None
    rule_type: RuleType
    severity: RuleSeverity = RuleSeverity.MINOR
    weight: Annotated[Decimal, Field(ge=0, le=100)] = Decimal(1)
    expression: dict[str, Any]
    is_active: bool = True

    @field_validator("expression")
    @classmethod
    def expression_is_well_formed(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Parse the predicate tree and check every field it names.

        Raised as a domain :class:`ValidationError` rather than a pydantic one so the
        response carries the list of available fields — a typo should come back with
        the vocabulary, not just a rejection.
        """
        referenced = parse(value).fields()
        unknown = unknown_fields(referenced)
        if unknown:
            raise ValidationError(
                f"rule references unknown field(s): {', '.join(sorted(unknown))}",
                details={"unknown_fields": sorted(unknown)},
            )
        return value


class StrategyInput(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: str | None = None
    checklist: dict[str, Any] = Field(default_factory=dict)
    rules: Annotated[list[RuleInput], Field(min_length=1, max_length=100)]

    @field_validator("rules")
    @classmethod
    def codes_are_unique(cls, value: list[RuleInput]) -> list[RuleInput]:
        codes = [rule.code for rule in value]
        duplicates = {code for code in codes if codes.count(code) > 1}
        if duplicates:
            raise ValidationError(
                f"duplicate rule code(s): {', '.join(sorted(duplicates))}",
                details={"duplicates": sorted(duplicates)},
            )
        return value


class RuleSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    label: str
    description: str | None
    rule_type: RuleType
    severity: RuleSeverity
    weight: Decimal
    expression: dict[str, Any]
    is_active: bool


class StrategySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    version: int
    supersedes_id: UUID | None
    is_active: bool
    checklist: dict[str, Any]
    created_at: datetime
    rules: list[RuleSummary] = Field(default_factory=list)


class StrategyList(BaseModel):
    items: list[StrategySummary]


class RuleValidationRequest(BaseModel):
    expression: dict[str, Any]


class RuleValidationResponse(BaseModel):
    """What a rule means, echoed back in English.

    The rule builder shows this before saving. A trader who writes an expression whose
    plain reading surprises them has caught a bug in their own rule, which is the whole
    point of keeping rules as inspectable data.
    """

    is_valid: bool
    reads_as: str
    fields: list[str]
