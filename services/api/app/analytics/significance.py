"""Hypothesis testing and multiple-comparison control.

This module exists to stop the product from manufacturing edges out of noise, which is
the default behaviour of every trading journal that reports segment breakdowns.

Consider what the segmentation cube does: 24 hours × 5 weekdays × 10 setups × 6 market
conditions is over seven thousand comparisons. At a 5% significance threshold, roughly
350 of them will look significant on pure noise. A journal that surfaces "your Tuesday
09:00 breakout edge" from that scan has not found anything — it has reported the
loudest coincidence in a large random sample. The trader then risks real money on it.

Two mechanisms prevent that:

**Permutation testing rather than a t-test.** The null hypothesis is that segment
membership is unrelated to outcome. That is tested directly by shuffling the labels and
seeing how often chance produces a gap as large as the observed one. No normality
assumption, which matters because trade P&L is neither normal nor equal-variance
between segments.

**Benjamini–Hochberg false discovery rate control.** When many hypotheses are tested at
once, the per-test p-value stops meaning what it says. BH rescales the threshold so that
the expected *proportion* of false findings among those reported is bounded. It is
preferred to Bonferroni here because Bonferroni is so conservative on thousands of
comparisons that it would reject genuine edges alongside the noise.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.analytics.montecarlo import DEFAULT_SEED
from app.analytics.statistics import PRECISION, mean, stdev
from app.analytics.types import MIN_SAMPLE

#: Conventional threshold. Applied to the FDR-adjusted p-value, never the raw one.
DEFAULT_ALPHA = Decimal("0.05")

DEFAULT_PERMUTATIONS = 5_000


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Whether two samples differ by more than chance would produce.

    Attributes:
        p_value: Fraction of label shufflings producing a difference at least as
            extreme as the observed one. Two-sided.
        effect_size: Cohen's d — the difference in means in pooled standard deviations.
            Reported because a statistically significant difference can still be too
            small to act on, and with enough trades everything becomes significant.
        adjusted_p_value: Set by :func:`control_false_discovery_rate` when this result
            is one of many. Until then it is ``None``, and ``is_significant`` refuses to
            answer.
    """

    label_a: str
    label_b: str
    mean_a: Decimal | None
    mean_b: Decimal | None
    difference: Decimal | None
    sample_a: int
    sample_b: int
    p_value: Decimal | None
    effect_size: Decimal | None
    permutations: int
    adjusted_p_value: Decimal | None = None
    undefined_reason: str | None = None

    @property
    def is_significant(self) -> bool | None:
        """Significance after multiple-comparison adjustment.

        Returns ``None`` when the result came from a scan but has not been adjusted —
        deliberately refusing to answer rather than reporting the unadjusted verdict,
        which is the one that manufactures findings.
        """
        if self.p_value is None:
            return None
        if self.adjusted_p_value is None:
            return None
        return self.adjusted_p_value < DEFAULT_ALPHA

    @property
    def effect_magnitude(self) -> str | None:
        """Cohen's conventional bands, for describing the size in words."""
        if self.effect_size is None:
            return None
        magnitude = abs(self.effect_size)
        if magnitude < Decimal("0.2"):
            return "negligible"
        if magnitude < Decimal("0.5"):
            return "small"
        if magnitude < Decimal("0.8"):
            return "medium"
        return "large"


def compare_samples(
    values_a: Sequence[Decimal],
    values_b: Sequence[Decimal],
    *,
    label_a: str = "a",
    label_b: str = "b",
    permutations: int = DEFAULT_PERMUTATIONS,
    seed: int = DEFAULT_SEED,
) -> ComparisonResult:
    """Two-sided permutation test on the difference of means.

    The procedure: pool both samples, repeatedly reshuffle into groups of the original
    sizes, and count how often the reshuffled difference is at least as large as the
    observed one. That count *is* the p-value, with no distributional assumption.
    """
    if len(values_a) < MIN_SAMPLE or len(values_b) < MIN_SAMPLE:
        return ComparisonResult(
            label_a=label_a,
            label_b=label_b,
            mean_a=mean(values_a),
            mean_b=mean(values_b),
            difference=None,
            sample_a=len(values_a),
            sample_b=len(values_b),
            p_value=None,
            effect_size=None,
            permutations=0,
            undefined_reason=(
                f"both groups need at least {MIN_SAMPLE} trades; "
                f"got {len(values_a)} and {len(values_b)}"
            ),
        )

    mean_a = mean(values_a)
    mean_b = mean(values_b)
    if mean_a is None or mean_b is None:  # pragma: no cover — guarded above
        return ComparisonResult(
            label_a, label_b, mean_a, mean_b, None, len(values_a), len(values_b), None, None, 0
        )

    observed = mean_a - mean_b
    pooled = [*values_a, *values_b]
    size_a = len(values_a)
    rng = random.Random(seed)

    at_least_as_extreme = 0
    for _ in range(permutations):
        shuffled = pooled[:]
        rng.shuffle(shuffled)
        left = shuffled[:size_a]
        right = shuffled[size_a:]
        left_mean = mean(left)
        right_mean = mean(right)
        if left_mean is None or right_mean is None:  # pragma: no cover
            continue
        if abs(left_mean - right_mean) >= abs(observed):
            at_least_as_extreme += 1

    with localcontext() as ctx:
        ctx.prec = PRECISION
        # The +1 correction keeps the p-value from ever being exactly zero: with a
        # finite number of shuffles the evidence can never rule chance out entirely,
        # and reporting p = 0 would overstate what was actually measured.
        p_value = Decimal(at_least_as_extreme + 1) / Decimal(permutations + 1)

    return ComparisonResult(
        label_a=label_a,
        label_b=label_b,
        mean_a=mean_a,
        mean_b=mean_b,
        difference=observed,
        sample_a=len(values_a),
        sample_b=len(values_b),
        p_value=p_value,
        effect_size=cohens_d(values_a, values_b),
        permutations=permutations,
    )


def cohens_d(values_a: Sequence[Decimal], values_b: Sequence[Decimal]) -> Decimal | None:
    """Standardised difference between two means.

    The answer to "is this difference big enough to care about?", which significance
    alone never gives: with a thousand trades per group, a difference of two dollars a
    trade is significant and irrelevant.
    """
    if len(values_a) < 2 or len(values_b) < 2:
        return None

    mean_a, mean_b = mean(values_a), mean(values_b)
    sd_a, sd_b = stdev(values_a), stdev(values_b)
    if mean_a is None or mean_b is None or sd_a is None or sd_b is None:
        return None

    with localcontext() as ctx:
        ctx.prec = PRECISION
        n_a, n_b = Decimal(len(values_a)), Decimal(len(values_b))
        pooled_variance = ((n_a - 1) * sd_a**2 + (n_b - 1) * sd_b**2) / (n_a + n_b - 2)
        if pooled_variance <= 0:
            return None
        return (mean_a - mean_b) / pooled_variance.sqrt()


def control_false_discovery_rate(
    results: Sequence[ComparisonResult], alpha: Decimal = DEFAULT_ALPHA
) -> list[ComparisonResult]:
    """Benjamini–Hochberg adjustment across a family of comparisons.

    Without this, scanning the segmentation cube produces a stream of confident
    findings that are almost entirely noise: seven thousand comparisons at a 5%
    threshold yield roughly 350 false positives even when nothing is true.

    BH sorts the p-values, compares the *i*-th smallest against ``i/m × alpha``, and
    enforces monotonicity so a result is never reported as stronger than a
    less-significant one. The output is adjusted p-values (q-values), which are what
    ``is_significant`` then reads.

    Comparisons that could not be tested pass through untouched — they are not
    hypotheses, so they neither consume nor contribute to the error budget.

    **The returned list is in the same order as the input**, which is load-bearing rather
    than a convenience. BH works by ranking p-values, so the natural implementation
    returns them sorted; every caller then has to remember to re-associate results with
    the findings they belong to. Two did not, and the consequence is not a cosmetic
    mix-up — a finding with p = 0.90 receives the adjusted p-value earned by an unrelated
    finding with p = 0.001 and is published as significant. That is precisely the
    manufactured discovery this function exists to prevent, produced by the mechanism
    meant to prevent it, and invisible in any aggregate check. Sorting is therefore an
    internal detail and the contract is positional.
    """
    indexed_testable = [
        (index, result) for index, result in enumerate(results) if result.p_value is not None
    ]

    if not indexed_testable:
        return list(results)

    ordered = sorted(indexed_testable, key=lambda item: item[1].p_value)  # type: ignore[arg-type,return-value]
    count = Decimal(len(ordered))

    adjusted: list[Decimal] = []
    with localcontext() as ctx:
        ctx.prec = PRECISION
        for rank, (_, result) in enumerate(ordered, start=1):
            assert result.p_value is not None
            adjusted.append(min(Decimal(1), result.p_value * count / Decimal(rank)))

        # Enforce monotonicity from the largest p-value downwards, so the adjusted
        # sequence never decreases as the raw p-value increases.
        for index in range(len(adjusted) - 2, -1, -1):
            adjusted[index] = min(adjusted[index], adjusted[index + 1])

    corrected = list(results)
    for (position, result), value in zip(ordered, adjusted, strict=True):
        corrected[position] = ComparisonResult(
            label_a=result.label_a,
            label_b=result.label_b,
            mean_a=result.mean_a,
            mean_b=result.mean_b,
            difference=result.difference,
            sample_a=result.sample_a,
            sample_b=result.sample_b,
            p_value=result.p_value,
            effect_size=result.effect_size,
            permutations=result.permutations,
            adjusted_p_value=value,
            undefined_reason=result.undefined_reason,
        )
    return corrected


@dataclass(frozen=True, slots=True)
class EdgeTest:
    """Whether a sample's expectancy is distinguishable from zero.

    The first question to ask of any strategy or segment, and the one a point estimate
    cannot answer.
    """

    sample_size: int
    expectancy: Decimal | None
    p_value: Decimal | None
    interval_low: Decimal | None
    interval_high: Decimal | None
    undefined_reason: str | None = None

    @property
    def has_demonstrable_edge(self) -> bool:
        """True only when the interval sits entirely above zero.

        Deliberately strict. "Not demonstrable" is not the same as "absent" — it means
        this sample cannot tell the difference — and the AI layer is required to phrase
        it that way rather than as a verdict on the strategy.
        """
        return self.interval_low is not None and self.interval_low > 0


def test_for_edge(
    values: Sequence[Decimal],
    *,
    iterations: int = 10_000,
    seed: int = DEFAULT_SEED,
) -> EdgeTest:
    """Bootstrap test of whether mean outcome is reliably above zero."""
    from app.analytics.montecarlo import bootstrap_mean

    if len(values) < MIN_SAMPLE:
        return EdgeTest(
            sample_size=len(values),
            expectancy=mean(values),
            p_value=None,
            interval_low=None,
            interval_high=None,
            undefined_reason=f"need at least {MIN_SAMPLE} trades to test for an edge",
        )

    result = bootstrap_mean(values, iterations=iterations, seed=seed)
    if result is None:  # pragma: no cover — guarded above
        return EdgeTest(len(values), mean(values), None, None, None, "insufficient sample")

    return EdgeTest(
        sample_size=len(values),
        expectancy=result.point_estimate,
        p_value=result.p_value_positive,
        interval_low=result.interval.low,
        interval_high=result.interval.high,
    )

