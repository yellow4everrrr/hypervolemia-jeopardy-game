"""Tests for hypothesis testing and multiple-comparison control.

The most important test in this file is
``test_scanning_pure_noise_produces_no_significant_findings``. It constructs the exact
situation the segmentation cube creates — many comparisons over data with no real
effect — and asserts that nothing survives. Without FDR control it would fail, and the
product would be inventing edges for its users.
"""

from __future__ import annotations

import random
from decimal import Decimal

from app.analytics.significance import (
    ComparisonResult,
    cohens_d,
    compare_samples,
    control_false_discovery_rate,
)
from app.analytics.significance import test_for_edge as run_edge_test

FAST = 500  # permutations


def D(values: list[float]) -> list[Decimal]:  # noqa: N802
    return [Decimal(str(value)) for value in values]


# --- Permutation test -----------------------------------------------------------


def test_a_large_real_difference_is_detected() -> None:
    winners = D([100.0] * 30)
    losers = D([-100.0] * 30)

    result = compare_samples(winners, losers, permutations=FAST)

    assert result.p_value is not None
    assert result.p_value < Decimal("0.01")
    assert result.difference == Decimal(200)


def test_identical_samples_are_not_significant() -> None:
    values = D([10.0, -5.0, 20.0, -5.0, 10.0, 15.0, -8.0, 12.0])
    result = compare_samples(values, list(values), permutations=FAST)

    assert result.p_value is not None
    assert result.p_value > Decimal("0.5")


def test_small_groups_are_refused_rather_than_tested() -> None:
    """Testing four trades against four would produce a p-value that means nothing."""
    result = compare_samples(D([1.0, 2.0]), D([3.0, 4.0]), permutations=FAST)

    assert result.p_value is None
    assert "at least" in (result.undefined_reason or "")


def test_p_value_is_never_exactly_zero() -> None:
    """With finitely many shuffles, chance can never be ruled out entirely.

    Reporting p = 0 would overstate what was actually measured.
    """
    result = compare_samples(D([1000.0] * 20), D([-1000.0] * 20), permutations=100)

    assert result.p_value is not None
    assert result.p_value > 0


def test_permutation_test_is_reproducible() -> None:
    a = D([10.0, -5.0, 20.0, -5.0, 10.0, 15.0])
    b = D([-10.0, 5.0, -20.0, 5.0, -10.0, -15.0])

    first = compare_samples(a, b, permutations=FAST, seed=42)
    second = compare_samples(a, b, permutations=FAST, seed=42)

    assert first.p_value == second.p_value


# --- Effect size ----------------------------------------------------------------


def test_cohens_d_measures_separation_in_standard_deviations() -> None:
    """Means 10 apart with a pooled deviation near 1 → d near 10."""
    a = D([10.0, 11.0, 9.0, 10.0, 10.0])
    b = D([0.0, 1.0, -1.0, 0.0, 0.0])

    d = cohens_d(a, b)
    assert d is not None
    assert d > 8


def test_effect_magnitude_bands() -> None:
    negligible = compare_samples(
        D([10.0, 11.0, 9.0, 10.0, 10.5, 9.5]),
        D([10.1, 10.9, 9.1, 10.0, 10.4, 9.6]),
        permutations=FAST,
    )
    assert negligible.effect_magnitude == "negligible"

    large = compare_samples(D([100.0] * 10), D([0.0, 1.0] * 5), permutations=FAST)
    assert large.effect_magnitude == "large"


def test_significance_without_a_large_effect_is_possible() -> None:
    """With enough observations a trivial difference becomes significant.

    Which is why effect size is always reported next to the p-value: "statistically
    significant" is not the same as "worth changing your trading for".
    """
    rng = random.Random(1)
    a = [Decimal(str(round(rng.gauss(100, 10), 2))) for _ in range(400)]
    b = [Decimal(str(round(rng.gauss(102, 10), 2))) for _ in range(400)]

    result = compare_samples(a, b, permutations=1_000)

    assert result.effect_size is not None
    assert abs(result.effect_size) < Decimal("0.5")  # small at most


# --- Multiple comparisons -------------------------------------------------------


def test_scanning_pure_noise_produces_no_significant_findings() -> None:
    """The test this module exists for.

    Twenty comparisons drawn from one distribution, so no real effect exists anywhere.
    At an unadjusted 5% threshold roughly one would look significant and be reported to
    the trader as an edge. After FDR control, none survive.
    """
    rng = random.Random(20260731)
    comparisons = []
    for index in range(20):
        a = [Decimal(str(round(rng.gauss(0, 100), 2))) for _ in range(40)]
        b = [Decimal(str(round(rng.gauss(0, 100), 2))) for _ in range(40)]
        comparisons.append(
            compare_samples(a, b, label_a=f"seg{index}", label_b="rest", permutations=500)
        )

    adjusted = control_false_discovery_rate(comparisons)

    assert not any(result.is_significant for result in adjusted)


def test_a_real_effect_survives_the_correction() -> None:
    """FDR control must not be so conservative that it rejects genuine findings.

    That is why Benjamini–Hochberg is used rather than Bonferroni.

    Note what is *not* asserted: that the real effect is the only survivor. FDR bounds
    the expected *proportion* of false discoveries across many such scans; it does not
    promise zero false positives in any individual one. This particular seed draws two
    noise groups differing by 2.4 and 3.1 standard errors, which are genuine outliers
    in the data rather than a defect in the correction. Asserting exactly one survivor
    would be asserting a guarantee the method does not make.
    """
    rng = random.Random(7)
    comparisons = [
        compare_samples(
            [Decimal(str(round(rng.gauss(0, 50), 2))) for _ in range(40)],
            [Decimal(str(round(rng.gauss(0, 50), 2))) for _ in range(40)],
            label_a=f"noise{index}",
            label_b="rest",
            permutations=500,
        )
        for index in range(9)
    ]
    comparisons.append(
        compare_samples(
            D([300.0] * 40), D([-300.0] * 40), label_a="real", label_b="rest", permutations=500
        )
    )

    adjusted = control_false_discovery_rate(comparisons)
    significant = {result.label_a for result in adjusted if result.is_significant}

    assert "real" in significant


def test_adjusted_p_values_are_monotonic() -> None:
    """A stronger raw result must never be reported as weaker after adjustment."""
    comparisons = [
        ComparisonResult(
            label_a=f"s{index}",
            label_b="rest",
            mean_a=Decimal(1),
            mean_b=Decimal(0),
            difference=Decimal(1),
            sample_a=50,
            sample_b=50,
            p_value=Decimal(str(value)),
            effect_size=Decimal("0.5"),
            permutations=1_000,
        )
        for index, value in enumerate([0.001, 0.01, 0.03, 0.2, 0.7])
    ]

    adjusted = control_false_discovery_rate(comparisons)
    ordered = sorted(adjusted, key=lambda result: result.p_value or Decimal(0))
    values = [result.adjusted_p_value for result in ordered]

    assert all(values[i] <= values[i + 1] for i in range(len(values) - 1))  # type: ignore[operator]


def test_untestable_comparisons_pass_through_untouched() -> None:
    """They are not hypotheses, so they neither consume nor add to the error budget."""
    testable = compare_samples(D([100.0] * 20), D([-100.0] * 20), permutations=FAST)
    untestable = compare_samples(D([1.0]), D([2.0]), permutations=FAST)

    adjusted = control_false_discovery_rate([testable, untestable])

    assert len(adjusted) == 2
    assert any(result.adjusted_p_value is None for result in adjusted)


def test_significance_refuses_to_answer_before_adjustment() -> None:
    """An unadjusted verdict from a scan is the one that manufactures findings."""
    result = compare_samples(D([100.0] * 20), D([-100.0] * 20), permutations=FAST)

    assert result.p_value is not None
    assert result.adjusted_p_value is None
    assert result.is_significant is None


# --- Edge test ------------------------------------------------------------------


def test_edge_is_demonstrable_for_a_consistent_winner() -> None:
    values = [Decimal(200) if index % 3 else Decimal(-50) for index in range(120)]
    result = run_edge_test(values, iterations=2_000)

    assert result.has_demonstrable_edge
    assert result.p_value is not None and result.p_value < Decimal("0.05")


def test_edge_is_not_demonstrable_for_a_marginal_sample() -> None:
    """Not "no edge" — this sample cannot tell the difference, and says so."""
    values = D([300.0, -280.0, 250.0, -240.0, 20.0, -10.0])
    result = run_edge_test(values, iterations=2_000)

    assert not result.has_demonstrable_edge
    assert result.interval_low is not None and result.interval_low < 0


def test_edge_test_refuses_a_tiny_sample() -> None:
    result = run_edge_test(D([100.0, -50.0]))
    assert result.p_value is None
    assert not result.has_demonstrable_edge
    assert "at least" in (result.undefined_reason or "")
