"""The evidence contract, tested by trying to break it.

Every test here plays the part of a model that has invented a statistic. If any of them
passes validation, the product's central promise — that the coach interprets computed
numbers and never produces them — is not enforced, only requested.

The tests are deliberately adversarial rather than illustrative: a well-behaved model
is easy to validate, and the failures that matter are the fluent, plausible ones.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.ai.contract import (
    Claim,
    bare_numerals,
    cited_keys,
    render,
    validate,
)
from app.ai.evidence import (
    COUNT,
    CURRENCY,
    PERCENT,
    R_MULTIPLE,
    RATIO,
    SECONDS,
    EvidenceBundle,
)


@pytest.fixture
def bundle() -> EvidenceBundle:
    """A small, realistic evidence set."""
    evidence = EvidenceBundle()
    evidence.put(
        "core.win_rate",
        Decimal("0.4237"),
        unit=PERCENT,
        label="win rate",
        sample_size=248,
    )
    evidence.put(
        "core.expectancy",
        Decimal("31.42"),
        unit=CURRENCY,
        label="expectancy per trade",
        sample_size=248,
        interval=(Decimal("4.10"), Decimal("58.90")),
    )
    evidence.put("sample.trades", 248, unit=COUNT, label="closed trades")
    evidence.put(
        "core.profit_factor",
        None,
        unit=RATIO,
        label="profit factor",
        undefined_reason="undefined — no losing trades in this sample",
    )
    evidence.context["compliance.worst_rule.label"] = "Every trade has a stop recorded"
    return evidence


def claim(statement: str, *, kind: str = "observation", cites: tuple[str, ...] = ()) -> Claim:
    return Claim(statement=statement, cites=cites, confidence="moderate", kind=kind)


# --- Fabrication attempts ---------------------------------------------------------


def test_a_number_nobody_computed_is_rejected(bundle: EvidenceBundle) -> None:
    """The canonical failure this whole design exists to prevent.

    Fluent, specific, and completely invented. It must not reach a trader.
    """
    result = validate(
        [claim("Your win rate on Friday afternoons is 34.2%.", cites=("core.win_rate",))],
        bundle,
    )

    assert not result.is_valid
    assert "34.2%" in result.uncited_numbers


def test_citing_a_statistic_that_was_never_computed_is_rejected(
    bundle: EvidenceBundle,
) -> None:
    """A plausible key is not a computed key."""
    result = validate(
        [claim("Your Sharpe ratio is {{core.sharpe_ratio}}.", kind="finding")], bundle
    )

    assert not result.is_valid
    assert "core.sharpe_ratio" in result.unknown_keys


def test_a_correct_number_written_plainly_is_allowed(bundle: EvidenceBundle) -> None:
    """The check targets invention, not transcription.

    A model that quotes the evidence correctly has done nothing wrong, and rejecting it
    would make the coach unable to write a sentence.
    """
    result = validate(
        [claim("You have taken 248 trades.", cites=("sample.trades",))], bundle
    )

    assert result.is_valid


def test_a_number_off_by_a_digit_is_rejected(bundle: EvidenceBundle) -> None:
    """The failure mode a human reviewer would never catch."""
    result = validate(
        [claim("You have taken 249 trades.", cites=("sample.trades",))], bundle
    )

    assert not result.is_valid
    assert "249" in result.uncited_numbers


def test_a_recommendation_built_on_an_undefined_statistic_is_rejected(
    bundle: EvidenceBundle,
) -> None:
    """Profit factor is undefined here; nothing may be recommended on it."""
    result = validate(
        [
            claim(
                "Size up — your profit factor of {{core.profit_factor}} is excellent.",
                kind="finding",
            )
        ],
        bundle,
    )

    assert not result.is_valid
    assert "core.profit_factor" in result.undefined_keys


def test_an_undefined_statistic_may_still_be_explained(bundle: EvidenceBundle) -> None:
    """"Your profit factor is undefined because…" is a useful sentence, not a claim."""
    result = validate(
        [
            claim(
                "Profit factor is {{core.profit_factor}} — you have no losing trades "
                "yet, so there is nothing to divide by.",
                kind="observation",
            )
        ],
        bundle,
    )

    assert result.is_valid


def test_a_claim_with_no_evidence_at_all_is_rejected(bundle: EvidenceBundle) -> None:
    result = validate([claim("You should work on your discipline.")], bundle)

    assert not result.is_valid
    assert result.uncited_claims


def test_a_hypothesis_may_stand_without_a_citation(bundle: EvidenceBundle) -> None:
    """Qualitative reading is legitimate output — ADR 0002 says so explicitly.

    It must be labelled as the model's own, which the `kind` field carries.
    """
    result = validate(
        [claim("This looks like chasing a missed move.", kind="hypothesis")], bundle
    )

    assert result.is_valid


def test_rejection_is_all_or_nothing(bundle: EvidenceBundle) -> None:
    """Publishing the surviving claims would leave a narrative with holes in it.

    The trader would have no way to know a paragraph had been removed, and the
    remaining argument would read as though it were complete.
    """
    result = validate(
        [
            claim("You have taken {{sample.trades}} trades.", cites=("sample.trades",)),
            claim("Your Sortino ratio is 2.4.", cites=("core.sortino",)),
        ],
        bundle,
    )

    assert not result.is_valid


# --- Rendering --------------------------------------------------------------------


def test_placeholders_render_to_computed_values(bundle: EvidenceBundle) -> None:
    """The mechanism: the model names a statistic, Python supplies the number."""
    text = render(
        "Expectancy is {{core.expectancy}} over {{sample.trades}} trades.", bundle
    )

    assert text == "Expectancy is $31.42 over 248 trades."


def test_the_model_cannot_change_a_value_by_formatting_it(bundle: EvidenceBundle) -> None:
    """Formatting is where a number quietly becomes a different number.

    A ratio rendered as a percentage, a rounded figure that loses a minus sign — the
    display string is computed from the value, never written by the model.
    """
    assert render("{{core.win_rate}}", bundle) == "42.4%"
    assert bundle.values["core.win_rate"].value == Decimal("0.4237")


def test_context_values_render_as_text(bundle: EvidenceBundle) -> None:
    rendered = render("The rule is {{compliance.worst_rule.label}}.", bundle)

    assert rendered == "The rule is Every trade has a stop recorded."


def test_an_undefined_value_renders_as_an_explicit_absence(
    bundle: EvidenceBundle,
) -> None:
    """Never an empty string — a sentence with a value silently missing reads as fact."""
    assert render("{{core.profit_factor}}", bundle) == "not defined for this sample"


def test_rendering_an_unknown_key_does_not_raise(bundle: EvidenceBundle) -> None:
    """Validation decides publishability; the renderer must not turn that into a 500."""
    assert render("{{nope.not_a_key}}", bundle) == "[unavailable]"


# --- The numeral scanner ----------------------------------------------------------


def test_digits_inside_placeholder_keys_are_not_claims() -> None:
    """`segments.hour_of_day.14` names an hour; it does not assert the number 14."""
    assert bare_numerals("At {{segments.hour_of_day.14.expectancy}} you lose.") == []


def test_ordinary_small_numbers_do_not_trip_the_scanner(bundle: EvidenceBundle) -> None:
    """The coach still has to be able to write English."""
    result = validate(
        [
            claim(
                "There are 2 things to fix, and the 1st is {{compliance.worst_rule.label}}.",
                cites=("compliance.worst_rule.label",),
            )
        ],
        bundle,
    )

    assert result.is_valid


def test_currency_formatting_does_not_hide_a_match(bundle: EvidenceBundle) -> None:
    """`$31.42` and `31.42` are the same claim about the same statistic."""
    result = validate(
        [claim("Expectancy is $31.42 a trade.", cites=("core.expectancy",))], bundle
    )

    assert result.is_valid


def test_a_percentage_matches_its_underlying_ratio(bundle: EvidenceBundle) -> None:
    """Win rate is stored as 0.4237 and displayed as 42.4%; both are legitimate."""
    result = validate(
        [claim("You win 42.4% of trades.", cites=("core.win_rate",))], bundle
    )

    assert result.is_valid


def test_quoting_more_precision_than_the_statistic_has_is_rejected(
    bundle: EvidenceBundle,
) -> None:
    """A model inventing digits past the computed value is inventing a value."""
    result = validate(
        [claim("Your expectancy is 31.4231 a trade.", cites=("core.expectancy",))],
        bundle,
    )

    assert not result.is_valid


def test_a_sample_size_may_be_quoted(bundle: EvidenceBundle) -> None:
    """`sample_size` travels with every value, so citing it is citing evidence."""
    result = validate(
        [claim("That is across 248 trades.", cites=("core.win_rate",))], bundle
    )

    assert result.is_valid


def test_cited_keys_are_extracted_from_the_text() -> None:
    assert cited_keys("a {{one.two}} b {{ three.four }}") == ["one.two", "three.four"]


# --- Display formatting -----------------------------------------------------------


def test_negative_currency_keeps_its_sign() -> None:
    """A lost dollar rendered without its minus sign is a different number entirely."""
    evidence = EvidenceBundle()
    evidence.put("x", Decimal("-1234.5"), unit=CURRENCY, label="loss")

    assert evidence.values["x"].display == "-$1,234.50"


def test_r_multiples_and_durations_are_formatted_in_their_own_units() -> None:
    evidence = EvidenceBundle()
    evidence.put("r", Decimal("1.5"), unit=R_MULTIPLE, label="r")
    evidence.put("d", 5400, unit=SECONDS, label="hold")

    assert evidence.values["r"].display == "1.50R"
    assert evidence.values["d"].display == "1h 30m"


def test_the_payload_carries_the_caveats_not_just_the_number(
    bundle: EvidenceBundle,
) -> None:
    """A model shown a bare figure will quote it with confidence the figure lacks."""
    payload = bundle.values["core.expectancy"].to_payload()

    assert payload["sample_size"] == 248
    assert payload["confidence_interval"] == ["4.10", "58.90"]
    # Optional qualifiers are omitted rather than sent as null: a model shown
    # "reliability: null" will read it as a value and reason about it.
    assert "reliability" not in payload
    assert "undefined_reason" not in payload


def test_an_undefined_value_carries_its_reason(bundle: EvidenceBundle) -> None:
    payload = bundle.values["core.profit_factor"].to_payload()

    assert "undefined_reason" in payload
    assert payload["display"] == "not defined for this sample"
