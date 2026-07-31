"""Whether a predicted probability means what it says.

This is the most important module in the package, and the reason is worth being blunt
about: **a trader will size on these numbers.** "68% chance of a winner" is not a
description, it is an instruction, and a model whose 68% comes up 45% of the time will
lose money faster than no model at all — the trader will take bigger positions on
precisely the trades the model is most wrong about.

Three metrics, and the ordering between them is deliberate.

**Accuracy is not reported anywhere.** It is the metric everyone asks for and it is a
trap. A trader who wins 42% of the time gets 58% accuracy from a model that predicts
"loss" every single time, and that model knows nothing. Worse, accuracy rewards exactly
the wrong behaviour: it improves when a model pushes its probabilities toward 0 and 1,
which is the opposite of calibration.

**Brier score** is the mean squared error of the probability against the outcome. It is
proper — it is minimised by telling the truth — so a model cannot improve it by
exaggerating confidence.

**Brier skill score** is the metric that decides deployment, because a raw Brier score is
uninterpretable on its own. It compares the model against the only baseline that matters:
always predicting the trader's own base rate. A skill score at or below zero means the
model has learned nothing a single number could not have told you, and the honest
response is to say so and refuse to serve it.

Calibration itself is tested against a **parametric bootstrap null**. If the model is
perfectly calibrated, outcomes are Bernoulli draws at the predicted probabilities, so
simulating outcomes from the model's own predictions and recomputing the calibration
error gives the exact reference distribution for a well-behaved model. The observed error
is then a percentile in that distribution rather than a number compared against a
threshold somebody chose. This matters because expected calibration error shrinks with
sample size — a fixed cutoff would call every small sample well calibrated.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.analytics.montecarlo import DEFAULT_SEED
from app.analytics.statistics import divide

#: Reliability bins. Ten is conventional; on a few hundred out-of-sample predictions it
#: already leaves some bins nearly empty, which is why bin counts are reported alongside
#: the rates and why the summary metric is bin-count weighted.
DEFAULT_BINS = 10

#: Draws for the calibration null. Enough to resolve a p-value to two decimals, which is
#: all the resolution the decision needs.
CALIBRATION_DRAWS = 1_000

#: Below this the calibration report is computed but marked unreliable: with 40 out-of-
#: sample predictions spread over ten bins, the reliability curve is noise with a shape.
MIN_CALIBRATION_SAMPLE = 50


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One bucket of the reliability curve.

    ``predicted`` is what the model claimed on average; ``observed`` is what happened.
    They should match. The gap between them, weighted by ``count``, is the calibration
    error.
    """

    low: Decimal
    high: Decimal
    count: int
    predicted: Decimal | None
    observed: Decimal | None

    @property
    def gap(self) -> Decimal | None:
        if self.predicted is None or self.observed is None:
            return None
        return self.observed - self.predicted

    def to_payload(self) -> dict[str, Any]:
        return {
            "range": [str(self.low), str(self.high)],
            "count": self.count,
            "predicted": str(self.predicted) if self.predicted is not None else None,
            "observed": str(self.observed) if self.observed is not None else None,
            "gap": str(self.gap) if self.gap is not None else None,
        }


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """How trustworthy a set of predicted probabilities is.

    Attributes:
        brier: Mean squared error of probability against outcome. Lower is better.
        baseline_brier: The same for a model that always predicts the base rate.
        skill: ``1 - brier / baseline_brier``. Positive means the model beats always
            guessing the base rate; zero or below means it does not, and the model is
            not deployable however good it looks elsewhere.
        expected_calibration_error: Bin-count weighted mean absolute gap between claimed
            and observed rates.
        calibration_p_value: Fraction of perfectly calibrated simulations producing a
            calibration error at least as large as the observed one. A **small** value is
            bad news here — it means the miscalibration is larger than sampling noise
            explains. This is the reverse of the usual reading and is the single easiest
            thing to misinterpret in the package.
    """

    sample_size: int
    base_rate: Decimal | None
    brier: Decimal | None
    baseline_brier: Decimal | None
    skill: Decimal | None
    log_loss: Decimal | None
    expected_calibration_error: Decimal | None
    calibration_p_value: Decimal | None
    bins: tuple[ReliabilityBin, ...]

    @property
    def is_reliable(self) -> bool:
        return self.sample_size >= MIN_CALIBRATION_SAMPLE

    @property
    def beats_baseline(self) -> bool:
        return self.skill is not None and self.skill > 0

    @property
    def is_calibrated(self) -> bool:
        """Whether miscalibration is within what sampling noise explains.

        Absence of evidence, and it says so: a thin sample cannot reject anything, which
        is why deployment requires ``is_reliable`` as well.
        """
        return (
            self.calibration_p_value is not None
            and self.calibration_p_value >= Decimal("0.05")
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "base_rate": str(self.base_rate) if self.base_rate is not None else None,
            "brier": str(self.brier) if self.brier is not None else None,
            "baseline_brier": (
                str(self.baseline_brier) if self.baseline_brier is not None else None
            ),
            "skill": str(self.skill) if self.skill is not None else None,
            "log_loss": str(self.log_loss) if self.log_loss is not None else None,
            "expected_calibration_error": (
                str(self.expected_calibration_error)
                if self.expected_calibration_error is not None
                else None
            ),
            "calibration_p_value": (
                str(self.calibration_p_value)
                if self.calibration_p_value is not None
                else None
            ),
            "beats_baseline": self.beats_baseline,
            "is_calibrated": self.is_calibrated,
            "is_reliable": self.is_reliable,
            "bins": [item.to_payload() for item in self.bins],
            "interpretation": (
                "A low calibration p-value means the model's stated probabilities do "
                "not match observed frequencies by more than sampling noise explains. "
                "Skill compares the model against always predicting the base rate; at "
                "or below zero it has learned nothing."
            ),
        }


def brier_score(predictions: Sequence[Decimal], outcomes: Sequence[bool]) -> Decimal | None:
    if not predictions:
        return None
    total = sum(
        ((probability - (Decimal(1) if outcome else Decimal(0))) ** 2
         for probability, outcome in zip(predictions, outcomes, strict=True)),
        Decimal(0),
    )
    return total / Decimal(len(predictions))


def log_loss(predictions: Sequence[Decimal], outcomes: Sequence[bool]) -> Decimal | None:
    """Mean negative log likelihood.

    Punishes confident errors far more harshly than the Brier score does, which is the
    behaviour wanted from a secondary metric: a model that says 99% and is wrong should
    look much worse than one that says 60% and is wrong. Requires probabilities strictly
    inside (0, 1), which :mod:`app.ml.regression` guarantees by clamping.
    """
    if not predictions:
        return None
    total = Decimal(0)
    for probability, outcome in zip(predictions, outcomes, strict=True):
        if probability <= 0 or probability >= 1:
            return None
        total -= probability.ln() if outcome else (Decimal(1) - probability).ln()
    return total / Decimal(len(predictions))


def reliability_curve(
    predictions: Sequence[Decimal], outcomes: Sequence[bool], *, bins: int = DEFAULT_BINS
) -> tuple[ReliabilityBin, ...]:
    """Bucket predictions and compare claimed rates against observed ones.

    Empty bins are kept in the output with a ``None`` rate rather than dropped. A gap in
    the curve is information — it says the model never made predictions in that range —
    and a curve that silently omits it looks smooth and complete.
    """
    edges = [Decimal(index) / Decimal(bins) for index in range(bins + 1)]
    buckets: list[list[tuple[Decimal, bool]]] = [[] for _ in range(bins)]

    for probability, outcome in zip(predictions, outcomes, strict=True):
        index = int(probability * bins)
        if index >= bins:
            index = bins - 1
        elif index < 0:
            index = 0
        buckets[index].append((probability, outcome))

    curve: list[ReliabilityBin] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            curve.append(ReliabilityBin(edges[index], edges[index + 1], 0, None, None))
            continue
        count = Decimal(len(bucket))
        predicted = sum((item[0] for item in bucket), Decimal(0)) / count
        observed = Decimal(sum(1 for item in bucket if item[1])) / count
        curve.append(
            ReliabilityBin(edges[index], edges[index + 1], len(bucket), predicted, observed)
        )
    return tuple(curve)


def expected_calibration_error(bins: Sequence[ReliabilityBin]) -> Decimal | None:
    """Bin-count weighted mean absolute gap between claimed and observed rates.

    Weighting by count is what keeps a bin holding three predictions from dominating the
    summary. An unweighted mean over bins is a widely used and much worse choice: it lets
    the sparsest, noisiest corner of the curve decide whether a model is calibrated.
    """
    total = sum(item.count for item in bins)
    if total == 0:
        return None
    weighted = Decimal(0)
    for item in bins:
        gap = item.gap
        if gap is None:
            continue
        weighted += Decimal(item.count) * abs(gap)
    return weighted / Decimal(total)


def assess(
    predictions: Sequence[Decimal],
    outcomes: Sequence[bool],
    *,
    bins: int = DEFAULT_BINS,
    draws: int = CALIBRATION_DRAWS,
    seed: int = DEFAULT_SEED,
) -> CalibrationReport:
    """Full calibration assessment of out-of-sample predictions.

    The baseline is the base rate *of these same outcomes*. Using a global or historical
    win rate instead would let a model score positive skill purely because the test
    window happened to be a good stretch, which is a property of the market and not of
    the model.
    """
    size = len(predictions)
    if size == 0:
        return CalibrationReport(0, None, None, None, None, None, None, None, ())

    wins = sum(1 for outcome in outcomes if outcome)
    base_rate = Decimal(wins) / Decimal(size)

    model_brier = brier_score(predictions, outcomes)
    baseline = brier_score([base_rate] * size, outcomes)
    skill: Decimal | None = None
    if model_brier is not None and baseline is not None:
        ratio = divide(model_brier, baseline)
        # A zero baseline Brier means the window was all wins or all losses, so the base
        # rate predicted perfectly and there is no room for skill to be defined.
        skill = Decimal(1) - ratio if ratio is not None else None

    curve = reliability_curve(predictions, outcomes, bins=bins)
    observed_error = expected_calibration_error(curve)

    p_value: Decimal | None = None
    if observed_error is not None and size > 0:
        rng = random.Random(seed)
        at_least_as_extreme = 0
        for _ in range(draws):
            simulated = [
                Decimal(str(rng.random())) < probability for probability in predictions
            ]
            simulated_curve = reliability_curve(predictions, simulated, bins=bins)
            simulated_error = expected_calibration_error(simulated_curve)
            if simulated_error is not None and simulated_error >= observed_error:
                at_least_as_extreme += 1
        # The observed statistic is included in its own reference distribution, so a
        # p-value can never be reported as exactly zero from a finite number of draws.
        p_value = Decimal(at_least_as_extreme + 1) / Decimal(draws + 1)

    return CalibrationReport(
        sample_size=size,
        base_rate=base_rate,
        brier=model_brier,
        baseline_brier=baseline,
        skill=skill,
        log_loss=log_loss(predictions, outcomes),
        expected_calibration_error=observed_error,
        calibration_p_value=p_value,
        bins=curve,
    )
