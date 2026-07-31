"""The evidence bundle: every fact the coach is allowed to know.

ADR 0002 says the model interprets statistics and never produces them. This module is
where that becomes a data structure rather than an instruction.

An :class:`EvidenceBundle` is a flat, keyed set of values computed by Python — from the
analytics engine, the compliance engine, and the pattern scan. It is the model's
*entire* factual world. Anything not in the bundle is something the coach cannot say,
and that is the intended trade: the coach cannot improvise a breakdown nobody
implemented.

Each value carries more than a number. ``sample_size`` and ``interval`` travel with the
figure so the model cannot quote an expectancy without the evidence of how much data
stands behind it, and ``display`` is the pre-formatted string the trader will actually
read — the model never formats a number, because formatting is where a value quietly
becomes a different value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

#: Units the renderer knows how to format. The unit is part of the evidence, not a
#: guess made at render time — 0.34 as a ratio and 0.34 as a dollar amount are
#: different claims.
CURRENCY = "currency"
RATIO = "ratio"
PERCENT = "percent"
COUNT = "count"
R_MULTIPLE = "r_multiple"
SECONDS = "seconds"
TEXT = "text"


@dataclass(frozen=True, slots=True)
class EvidenceValue:
    """One computed fact, with everything needed to quote it responsibly."""

    key: str
    value: Decimal | int | str | None
    unit: str
    label: str
    #: Observations behind the figure. ``None`` for values where it is meaningless
    #: (a rule's configured threshold has no sample).
    sample_size: int | None = None
    interval: tuple[Decimal, Decimal] | None = None
    reliability: str | None = None
    #: Set when the value is undefined, explaining why in plain language. A model shown
    #: ``None`` with no reason will guess at one.
    undefined_reason: str | None = None

    @property
    def is_defined(self) -> bool:
        return self.value is not None

    @property
    def display(self) -> str:
        """How the value is rendered into the coach's prose.

        The single place a number becomes text. Doing this in Python rather than
        letting the model format it means a value cannot change on its way to the page:
        no dropped minus sign, no ratio quoted as a percentage, no silent rounding that
        turns 0.4951 into "roughly half" and then into "half".
        """
        if self.value is None:
            return "not defined for this sample"
        if isinstance(self.value, str):
            return self.value
        if self.unit == CURRENCY:
            amount = Decimal(self.value).quantize(Decimal("0.01"))
            return f"-${abs(amount):,}" if amount < 0 else f"${amount:,}"
        if self.unit == PERCENT:
            return f"{(Decimal(self.value) * 100).quantize(Decimal('0.1'))}%"
        if self.unit == RATIO:
            return str(Decimal(self.value).quantize(Decimal("0.001")))
        if self.unit == R_MULTIPLE:
            return f"{Decimal(self.value).quantize(Decimal('0.01'))}R"
        if self.unit == SECONDS:
            return _duration(int(self.value))
        return f"{self.value:,}" if isinstance(self.value, int) else str(self.value)

    def to_payload(self) -> dict[str, Any]:
        """What the model is shown. Deliberately includes the caveats."""
        payload: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "display": self.display,
            "unit": self.unit,
        }
        if self.sample_size is not None:
            payload["sample_size"] = self.sample_size
        if self.interval is not None:
            payload["confidence_interval"] = [
                str(self.interval[0]),
                str(self.interval[1]),
            ]
        if self.reliability is not None:
            payload["reliability"] = self.reliability
        if self.undefined_reason is not None:
            payload["undefined_reason"] = self.undefined_reason
        return payload


@dataclass
class EvidenceBundle:
    """Everything the coach may cite, and nothing else.

    Built by :mod:`app.ai.builder` from the analytics report, the compliance run and
    the pattern scan. Persisted verbatim in ``ai_analyses.input_metrics`` so any claim
    can be checked against the exact evidence that produced it.
    """

    values: dict[str, EvidenceValue] = field(default_factory=dict)
    #: Free-text context that is *not* a statistic — the trader's own strategy names,
    #: instrument symbols, rule labels. Citable, but never numeric.
    context: dict[str, str] = field(default_factory=dict)
    #: Statistics deliberately withheld and why. Shown to the model so it says "not
    #: enough data to tell" rather than reaching for the nearest available number.
    gaps: list[str] = field(default_factory=list)

    def add(self, value: EvidenceValue) -> None:
        self.values[value.key] = value

    def put(
        self,
        key: str,
        value: Decimal | int | str | None,
        *,
        unit: str,
        label: str,
        sample_size: int | None = None,
        interval: tuple[Decimal, Decimal] | None = None,
        reliability: str | None = None,
        undefined_reason: str | None = None,
    ) -> None:
        self.add(
            EvidenceValue(
                key=key,
                value=value,
                unit=unit,
                label=label,
                sample_size=sample_size,
                interval=interval,
                reliability=reliability,
                undefined_reason=undefined_reason,
            )
        )

    @property
    def citable_keys(self) -> set[str]:
        return set(self.values) | set(self.context)

    @property
    def defined_keys(self) -> set[str]:
        """Keys with an actual value.

        A model may *mention* an undefined statistic — "your profit factor is not
        defined because you have no losing trades yet" is a useful sentence — but it
        may not build a recommendation on one.
        """
        return {key for key, value in self.values.items() if value.is_defined} | set(
            self.context
        )

    def display_for(self, key: str) -> str | None:
        if key in self.values:
            return self.values[key].display
        return self.context.get(key)

    def to_payload(self) -> dict[str, Any]:
        return {
            "metrics": [value.to_payload() for value in self.values.values()],
            "context": dict(self.context),
            "gaps": list(self.gaps),
        }

    def is_empty(self) -> bool:
        return not self.values and not self.context


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    if seconds < 3600:
        return f"{seconds // 60} minutes"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02d}m" if minutes else f"{hours} hours"
