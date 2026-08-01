"""Enforcement of the evidence contract.

ADR 0002 committed to a rule: the model interprets computed statistics and never
produces them. A system prompt saying so reduces the rate of invented numbers. It does
not make them impossible, and the failures it leaves are the confident, plausible ones
— "your win rate on Friday afternoons is 34%" reads identically whether it came from an
aggregation or from next-token prediction.

So the model does not write numbers at all.

**It writes placeholders, and Python substitutes the values.** The coach emits
``Your expectancy is {{core.expectancy}} over {{sample.trades}} trades``; the renderer
replaces each placeholder with the value computed by the analytics engine. The number
the trader reads is therefore the computed number *by construction* — not because the
model was well behaved, and not because a checker caught it afterwards.

That leaves one gap: a model that writes a bare numeral anyway. Two defences close it:

* **Unknown placeholders are rejected.** A key absent from the bundle fails the
  analysis rather than rendering as empty text or as the literal placeholder.
* **Bare numerals are rejected unless they match the evidence.** A digit outside a
  placeholder is treated as a claim and checked against every value in the bundle. A
  number that matches is allowed through (the model quoted correctly); one that does
  not fails the analysis. This is the check that catches the fabrication the
  placeholder scheme was designed to prevent.

The result is stored in ``ai_analyses.evidence_validated``. A rising rejection rate is
a signal about the prompt, never a reason to relax the check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ai.evidence import EvidenceBundle

#: ``{{ some.key }}`` — the only way a number reaches the trader.
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.\[\]-]+)\s*\}\}")

#: Any run of digits, with optional decimal part, sign, thousands separators, and a
#: trailing percent or R. Deliberately greedy: the point is to find every numeral.
NUMERAL = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?%?R?")

#: Numerals that are never statistics about the trader. Ordinals and small counts turn
#: up in ordinary prose ("the first thing to fix", "both of these"), and rejecting
#: them would make the coach unable to write English.
BENIGN_NUMERALS = frozenset(
    {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "100"}
)


@dataclass(frozen=True, slots=True)
class Claim:
    """One statement the coach makes, with the evidence it rests on."""

    statement: str
    cites: tuple[str, ...]
    confidence: str
    #: ``observation`` (descriptive), ``hypothesis`` (the model's own reading, not
    #: computed), or ``finding`` (survived a significance test in Python).
    kind: str = "observation"

    def rendered(self, bundle: EvidenceBundle) -> str:
        return render(self.statement, bundle)


@dataclass
class ValidationResult:
    """Whether an analysis may be shown to the trader."""

    is_valid: bool = True
    unknown_keys: list[str] = field(default_factory=list)
    undefined_keys: list[str] = field(default_factory=list)
    uncited_numbers: list[str] = field(default_factory=list)
    uncited_claims: list[str] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.unknown_keys:
            reasons.append(
                "cites statistics that were not computed: "
                + ", ".join(sorted(set(self.unknown_keys)))
            )
        if self.undefined_keys:
            reasons.append(
                "builds a recommendation on undefined statistics: "
                + ", ".join(sorted(set(self.undefined_keys)))
            )
        if self.uncited_numbers:
            reasons.append(
                "states numbers that appear in no computed statistic: "
                + ", ".join(sorted(set(self.uncited_numbers)))
            )
        if self.uncited_claims:
            reasons.append(
                f"{len(self.uncited_claims)} claim(s) cite no evidence at all"
            )
        return reasons

    def to_payload(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "unknown_keys": self.unknown_keys,
            "undefined_keys": self.undefined_keys,
            "uncited_numbers": self.uncited_numbers,
            "uncited_claims": self.uncited_claims,
            "reasons": self.reasons,
        }


def render(text: str, bundle: EvidenceBundle) -> str:
    """Substitute every placeholder with its computed value.

    An unknown key renders as ``[unavailable]`` rather than raising — by the time this
    runs, :func:`validate` has already decided whether the analysis is publishable, and
    a renderer that throws would turn a validation failure into a 500.
    """

    def substitute(match: re.Match[str]) -> str:
        return bundle.display_for(match.group(1)) or "[unavailable]"

    return PLACEHOLDER.sub(substitute, text)


def cited_keys(text: str) -> list[str]:
    return PLACEHOLDER.findall(text)


def bare_numerals(text: str) -> list[str]:
    """Numerals written outside a placeholder.

    Placeholders are stripped first so their *keys* — which can contain digits, as in
    ``segments.hour_of_day.14`` — are not mistaken for claims.
    """
    stripped = PLACEHOLDER.sub(" ", text)
    return [match.group(0) for match in NUMERAL.finditer(stripped)]


def validate(claims: list[Claim], bundle: EvidenceBundle) -> ValidationResult:
    """Check every claim against the evidence before any of it reaches the trader.

    Rejection is all-or-nothing. Publishing the claims that passed and dropping the
    ones that did not would leave a narrative built around missing pieces, and the
    trader would have no way to know a paragraph was removed.
    """
    result = ValidationResult()
    citable = bundle.citable_keys
    defined = bundle.defined_keys

    for claim in claims:
        text = claim.statement
        keys = set(cited_keys(text)) | set(claim.cites)

        result.unknown_keys.extend(sorted(keys - citable))

        # An undefined statistic may be *discussed* but not *recommended on*. The coach
        # saying "profit factor is undefined because you have no losers yet" is useful;
        # "increase size because your profit factor is undefined" is not.
        if claim.kind != "observation":
            result.undefined_keys.extend(sorted((keys & citable) - defined))

        if not keys and claim.kind != "hypothesis":
            result.uncited_claims.append(text)

        for numeral in bare_numerals(text):
            if not _is_supported(numeral, bundle):
                result.uncited_numbers.append(numeral)

    result.is_valid = not (
        result.unknown_keys
        or result.undefined_keys
        or result.uncited_numbers
        or result.uncited_claims
    )
    return result


def _is_supported(numeral: str, bundle: EvidenceBundle) -> bool:
    """Whether a bare numeral corresponds to something Python actually computed.

    Compared against both the raw value and its rendered display, because the model
    may legitimately transcribe either. Normalised for the decoration that carries no
    meaning — currency symbols, thousands separators, a trailing ``R`` or ``%`` — so
    that ``$1,234.50`` and ``1234.5`` are recognised as the same claim.
    """
    cleaned = numeral.strip()
    if cleaned in BENIGN_NUMERALS:
        return True

    candidate = _to_number(cleaned)
    if candidate is None:
        return True  # not actually numeric after normalisation

    for value in bundle.values.values():
        if _matches(candidate, value.value):
            return True
        if _matches(candidate, _to_number(value.display)):
            return True
        # A percentage in the prose against a ratio in the evidence, and vice versa.
        if _matches(candidate * 100, value.value) or _matches(
            candidate / 100, value.value
        ):
            return True
        if value.sample_size is not None and _matches(candidate, value.sample_size):
            return True
    return False


def _to_number(text: str | None) -> Decimal | None:
    if text is None:
        return None
    stripped = re.sub(r"[$,%R\s]", "", text)
    if not stripped or stripped in {"-", "."}:
        return None
    try:
        return Decimal(stripped)
    except InvalidOperation:
        return None


def _matches(candidate: Decimal, value: Any) -> bool:
    """Equality after rounding to the coarser of the two representations.

    The coach writing "0.34" for an underlying 0.3417 is quoting, not inventing. The
    tolerance is the precision the model actually wrote — quoting more digits than the
    statistic has is what would be caught.
    """
    if value is None or isinstance(value, str):
        return False
    try:
        actual = Decimal(str(value))
    except InvalidOperation:  # pragma: no cover — values are numeric by construction
        return False

    if candidate == actual:
        return True
    exponent = candidate.as_tuple().exponent
    # A non-integer exponent means NaN or Infinity, which no statistic produces and no
    # tolerance can be defined against.
    if not isinstance(exponent, int) or exponent > 0:
        return False
    quantum = Decimal(1).scaleb(exponent)
    return actual.quantize(quantum) == candidate.quantize(quantum)
