"""The null battery: does the ML layer claim skill where there is none?

This is the file that matters. Everything else in the package can be correct while the
product still ships per-trade win probabilities derived from noise, and a trader will
size on those.

The battery is deliberately expensive — each run is a full walk-forward with nested
penalty selection and a session-block bootstrap — and deliberately kept anyway. It has
already caught one real defect: gating deployment on ``skill > 0`` rather than on the
interval deployed a model on pure noise in one run out of ten.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.ml.report import train
from app.ml.snapshot import build_snapshots
from tests.unit.ml.conftest import edged_history, noise_history

#: Kept small because each seed is a full walk-forward run. Six is enough to catch a
#: gate that fires on a tenth of noise samples, which is the failure rate the interval
#: gate was introduced to fix.
NULL_SEEDS = (1000, 1001, 1005, 1012, 1017, 1019)


class TestTheNullBattery:
    """On data with no signal, nothing may be deployed."""

    @pytest.mark.parametrize("seed", NULL_SEEDS)
    def test_no_model_is_deployed_on_noise(self, seed: int) -> None:
        report = train(build_snapshots(noise_history(seed=seed)), step_sessions=10)

        assert not report.win_probability.is_deployable, (
            f"a win-probability model was deployed on noise "
            f"(skill {report.win_probability.skill and report.win_probability.skill.value})"
        )
        assert not report.expected_r.is_deployable, (
            f"an expected-R model was deployed on noise "
            f"(skill {report.expected_r.skill and report.expected_r.skill.value})"
        )

    @pytest.mark.parametrize("seed", (1005, 1012))
    def test_positive_point_estimates_on_noise_are_still_refused(self, seed: int) -> None:
        """The specific failure the interval gate exists to prevent.

        These two seeds produce a *positive* skill score on data with no signal — +0.014
        and +0.011. Under the original ``skill > 0`` gate both were deployable. The
        interval must catch them, and it must be the interval doing it: the assertion
        checks the point estimate really is positive, so the test cannot pass for the
        trivial reason that the noise happened to score badly.
        """
        report = train(build_snapshots(noise_history(seed=seed)), step_sessions=10)
        skill = report.win_probability.skill

        assert skill is not None and skill.value is not None
        assert skill.value > 0, "seed no longer produces the positive-noise case it was chosen for"
        assert skill.interval is not None
        assert skill.interval.low <= 0
        assert not skill.established
        assert not report.win_probability.is_deployable

    def test_the_refusal_explains_which_way_it_failed(self) -> None:
        """"No skill" and "not enough evidence of skill" lead to different actions."""
        report = train(build_snapshots(noise_history(seed=1005)), step_sessions=10)
        refusal = report.win_probability.refusal or ""
        assert "luck" in refusal
        assert "skill score" in refusal


class TestThePositiveControl:
    """An engine that reported nothing under all circumstances would pass every null."""

    def test_a_genuine_edge_is_found_and_served(self) -> None:
        report = train(build_snapshots(edged_history(seed=500)), step_sessions=10)
        model = report.win_probability

        assert model.is_deployable, f"a real edge was refused: {model.refusal}"
        assert model.skill is not None and model.skill.value is not None
        assert model.skill.value > Decimal("0.05")
        assert model.skill.established
        assert model.model is not None

    def test_the_served_model_learned_the_right_feature(self) -> None:
        """Direction carried the edge, so direction must carry the largest coefficient.

        A model with the right skill score for the wrong reason would pass every other
        test in this file.
        """
        report = train(build_snapshots(edged_history(seed=500)), step_sessions=10)
        model = report.win_probability.model
        assert model is not None

        weights = dict(zip(model.feature_names, model.coefficients, strict=True))
        largest = max(weights, key=lambda name: abs(weights[name]))
        assert largest == "direction"
        # Long trades won more often, so the coefficient must be positive.
        assert weights["direction"] > 0


class TestRefusalPaths:
    """Every refusal carries a reason a trader can act on."""

    def test_a_short_history_is_refused_before_anything_is_fit(self) -> None:
        report = train(build_snapshots(noise_history(seed=7, sessions=8)), step_sessions=5)

        assert not report.win_probability.is_deployable
        assert "sessions" in (report.win_probability.refusal or "")
        assert report.win_probability.model is None

    def test_no_recorded_stops_refuses_only_the_expected_r_head(self) -> None:
        """The two heads have different data requirements and fail independently.

        A trader who records no stops still has a win-rate history; refusing both heads
        because one lacks its target would discard evidence that exists.
        """
        from tests.unit.ml.conftest import build_history

        history = build_history(sessions=60, per_session=4, seed=11, with_r=False)
        report = train(build_snapshots(history), step_sessions=10)

        assert report.expected_r.model is None
        assert not report.expected_r.is_deployable
        # The classifier reached the skill gate rather than failing on missing data.
        assert report.win_probability.validation.size > 0

    def test_a_refused_model_serves_no_predictions(self) -> None:
        from app.ml.report import predict

        report = train(build_snapshots(noise_history(seed=1000)), step_sessions=10)
        samples = build_snapshots(noise_history(seed=1000))

        assert predict(report.win_probability, samples[0].snapshot) is None

    def test_the_payload_never_hides_a_refusal(self) -> None:
        report = train(build_snapshots(noise_history(seed=1000)), step_sessions=10)
        payload = report.to_payload()

        assert payload["win_probability"]["is_deployable"] is False
        assert payload["win_probability"]["refusal"]
        assert "model" not in payload["win_probability"]
