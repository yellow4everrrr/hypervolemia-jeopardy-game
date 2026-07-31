"""Calibration metrics, including the two that are easiest to read backwards."""

from __future__ import annotations

import random
from decimal import Decimal

from app.ml.calibration import (
    assess,
    brier_score,
    expected_calibration_error,
    log_loss,
    reliability_curve,
)


def _honest_sample(size: int, probability: str, seed: int) -> tuple[list[Decimal], list[bool]]:
    """Predictions that are true by construction: outcomes drawn at the stated rate."""
    rng = random.Random(seed)
    value = Decimal(probability)
    predictions = [value] * size
    outcomes = [Decimal(str(rng.random())) < value for _ in range(size)]
    return predictions, outcomes


class TestBrierScore:
    def test_perfect_prediction_scores_zero(self) -> None:
        assert brier_score([Decimal(1), Decimal(0)], [True, False]) == Decimal(0)

    def test_confidently_wrong_scores_one(self) -> None:
        assert brier_score([Decimal(0), Decimal(1)], [True, False]) == Decimal(1)

    def test_it_is_a_proper_scoring_rule(self) -> None:
        """Honesty must beat exaggeration — the property the whole gate depends on.

        With a true rate of 60%, claiming 60% must score better than claiming 90%. If
        this failed, a model could improve its reported score by becoming more confident,
        and every calibration guarantee downstream would be worthless.
        """
        _, outcomes = _honest_sample(600, "0.6", seed=7)
        honest = brier_score([Decimal("0.6")] * 600, outcomes)
        overconfident = brier_score([Decimal("0.9")] * 600, outcomes)

        assert honest is not None and overconfident is not None
        assert honest < overconfident


class TestSkillScore:
    def test_predicting_the_base_rate_scores_no_skill(self) -> None:
        """The baseline compared against itself must score exactly zero.

        This is the anchor for every deployment decision: a model that has learned
        nothing scores 0, not some small positive number that looks like progress.
        """
        _, outcomes = _honest_sample(300, "0.45", seed=3)
        rate = Decimal(sum(1 for outcome in outcomes if outcome)) / Decimal(len(outcomes))
        report = assess([rate] * len(outcomes), outcomes, draws=50)

        assert report.skill == Decimal(0)
        assert not report.beats_baseline

    def test_a_model_that_knows_the_answer_scores_near_one(self) -> None:
        outcomes = [index % 3 == 0 for index in range(300)]
        predictions = [Decimal("0.999") if outcome else Decimal("0.001") for outcome in outcomes]
        report = assess(predictions, outcomes, draws=50)

        assert report.skill is not None and report.skill > Decimal("0.99")

    def test_a_model_worse_than_the_base_rate_scores_negative(self) -> None:
        _, outcomes = _honest_sample(300, "0.45", seed=5)
        backwards = [Decimal("0.9") if outcome else Decimal("0.1") for outcome in outcomes]
        inverted = [Decimal(1) - value for value in backwards]
        report = assess(inverted, outcomes, draws=50)

        assert report.skill is not None and report.skill < 0


class TestReliabilityCurve:
    def test_empty_bins_are_kept_rather_than_dropped(self) -> None:
        """A gap in the curve is information: the model never predicted in that range."""
        curve = reliability_curve([Decimal("0.05")] * 10, [True] * 10, bins=10)

        assert len(curve) == 10
        assert curve[0].count == 10
        assert curve[5].count == 0
        assert curve[5].observed is None

    def test_a_prediction_of_exactly_one_lands_in_the_last_bin(self) -> None:
        """Off-by-one at the boundary would silently create an eleventh bin."""
        curve = reliability_curve([Decimal(1)], [True], bins=10)
        assert curve[-1].count == 1

    def test_the_error_is_weighted_by_bin_count(self) -> None:
        """A three-prediction bin must not outvote a three-hundred-prediction one.

        The unweighted mean over bins is a common choice and a much worse one: it lets
        the sparsest, noisiest corner of the curve decide whether a model is calibrated.
        """
        curve = reliability_curve(
            [Decimal("0.15")] * 300 + [Decimal("0.85")] * 3,
            [False] * 255 + [True] * 45 + [False] * 3,
            bins=10,
        )
        weighted = expected_calibration_error(curve)

        gaps = [item.gap for item in curve if item.gap is not None]
        unweighted = sum((abs(gap) for gap in gaps), Decimal(0)) / Decimal(len(gaps))

        assert weighted is not None
        # The large, well-calibrated bin dominates; the tiny badly-wrong bin does not.
        assert weighted < unweighted


class TestCalibrationTest:
    def test_a_truthful_model_is_not_rejected(self) -> None:
        predictions, outcomes = _honest_sample(400, "0.55", seed=11)
        report = assess(predictions, outcomes, draws=300)

        assert report.is_calibrated
        assert report.calibration_p_value is not None
        assert report.calibration_p_value > Decimal("0.05")

    def test_a_systematically_overconfident_model_is_rejected(self) -> None:
        """Claims 85%, wins 50%. The p-value must be *small* — the reverse of the usual
        reading, and the single easiest thing to misinterpret in the package."""
        outcomes = [index % 2 == 0 for index in range(400)]
        report = assess([Decimal("0.85")] * 400, outcomes, draws=300)

        assert not report.is_calibrated
        assert report.calibration_p_value is not None
        assert report.calibration_p_value < Decimal("0.05")

    def test_the_p_value_is_never_exactly_zero(self) -> None:
        """The observed statistic is included in its own reference distribution.

        Reporting p = 0 from a finite number of draws claims more resolution than the
        simulation has.
        """
        outcomes = [True] * 200
        report = assess([Decimal("0.01")] * 200, outcomes, draws=100)

        assert report.calibration_p_value is not None
        assert report.calibration_p_value > Decimal(0)

    def test_it_is_deterministic_for_a_fixed_seed(self) -> None:
        predictions, outcomes = _honest_sample(200, "0.5", seed=13)
        first = assess(predictions, outcomes, draws=100, seed=99)
        second = assess(predictions, outcomes, draws=100, seed=99)

        assert first.calibration_p_value == second.calibration_p_value


class TestLogLoss:
    def test_it_punishes_confident_errors_far_harder_than_brier(self) -> None:
        """The reason it is reported alongside the Brier score rather than instead of it."""
        outcomes = [True] * 100
        mild = [Decimal("0.4")] * 100
        severe = [Decimal("0.01")] * 100

        brier_ratio = brier_score(severe, outcomes) / brier_score(mild, outcomes)  # type: ignore[operator]
        loss_ratio = log_loss(severe, outcomes) / log_loss(mild, outcomes)  # type: ignore[operator]

        assert loss_ratio > brier_ratio

    def test_an_empty_sample_is_undefined_not_zero(self) -> None:
        assert log_loss([], []) is None
        assert brier_score([], []) is None
