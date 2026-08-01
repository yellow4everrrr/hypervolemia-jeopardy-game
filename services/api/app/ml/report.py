"""Training both heads, and deciding whether either may be served.

The deployability gate is the point of this module. Everything upstream produces numbers;
this decides whether a trader is allowed to see them, and the default answer is no.

A win-probability model is **deployable** only when all four hold:

1. Walk-forward validation actually ran — enough sessions, enough scored predictions.
2. It beats the base rate out of sample (positive Brier skill score).
3. Its calibration is not rejected against the parametric bootstrap null.
4. The served fit converged.

Any one of those failing produces a refusal with a reason in plain language, not a
degraded model with a warning label. A warning label on a dashboard is read once; a model
that will not load is read every time.

The expected-R head is held to the same standard with one substitution: R has no base
rate, so the baseline is predicting the training mean R, and skill is measured against
that. A model that cannot beat "assume every trade returns your average R" has found
nothing, whatever its correlation looks like.

**Why no accuracy, F1, ROC-AUC or confusion matrix anywhere.** Every one of them is
insensitive to calibration, and calibration is the entire question here. A model can have
an excellent AUC — ranking trades correctly from worst to best — while its stated
probabilities are wrong by twenty points, because AUC only sees the ordering. A trader
sizing on "72%" is not using the ordering.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.analytics.montecarlo import DEFAULT_SEED
from app.ml.calibration import CalibrationReport, assess
from app.ml.features import Standardiser
from app.ml.regression import FittedModel
from app.ml.skill import SkillEvidence, assess_skill
from app.ml.snapshot import EntrySnapshot, LabelledSnapshot
from app.ml.validation import (
    MIN_SCORED_PREDICTIONS,
    WalkForwardResult,
    final_model,
    r_label,
    walk_forward,
    win_label,
)

#: Version stamped onto every stored model. Bumped whenever the feature set, the fitting
#: procedure or the deployability rules change — a probability served by v1 and one served
#: by v2 are not comparable, and a stored calibration report belongs to exactly one of
#: them.
MODEL_VERSION = 1


@dataclass(frozen=True, slots=True)
class ModelReport:
    """One trained head, its out-of-sample evidence, and whether it may be served.

    Attributes:
        model: The fit over the full history, used for serving. Never the source of any
            reported performance number — it has seen everything.
        calibration: Computed from walk-forward predictions only.
        refusal: Plain language for why this model is not deployable, or ``None``.
    """

    head: str
    model: FittedModel | None
    standardiser: Standardiser | None
    validation: WalkForwardResult
    calibration: CalibrationReport | None
    skill: SkillEvidence | None
    refusal: str | None

    @property
    def is_deployable(self) -> bool:
        return self.refusal is None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "head": self.head,
            "version": MODEL_VERSION,
            "is_deployable": self.is_deployable,
            "folds": self.validation.folds,
            "out_of_sample_predictions": self.validation.size,
            "rows_skipped": self.validation.skipped_rows,
            "skill": self.skill.to_payload() if self.skill is not None else None,
        }
        if self.refusal:
            payload["refusal"] = self.refusal
        if self.calibration is not None:
            payload["calibration"] = self.calibration.to_payload()
        if self.model is not None and self.standardiser is not None:
            payload["model"] = self.model.to_payload()
            payload["features"] = self.standardiser.to_payload()
        return payload


@dataclass(frozen=True, slots=True)
class TrainingReport:
    """Both heads, trained over one trader's history."""

    win_probability: ModelReport
    expected_r: ModelReport
    trades: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "win_probability": self.win_probability.to_payload(),
            "expected_r": self.expected_r.to_payload(),
            "interpretation": (
                "Performance figures come from walk-forward validation on data the "
                "model had not seen. The served model is refit on the full history and "
                "is never the source of a reported score. A model that does not beat "
                "the trader's own base rate is refused rather than shown with a caveat."
            ),
        }


def train(
    samples: Sequence[LabelledSnapshot],
    *,
    seed: int = DEFAULT_SEED,
    step_sessions: int | None = None,
) -> TrainingReport:
    """Train, validate and gate both heads."""
    kwargs: dict[str, Any] = {}
    if step_sessions is not None:
        kwargs["step_sessions"] = step_sessions

    return TrainingReport(
        win_probability=_train_classifier(samples, seed=seed, **kwargs),
        expected_r=_train_regressor(samples, **kwargs),
        trades=len(samples),
    )


def _train_classifier(
    samples: Sequence[LabelledSnapshot], *, seed: int, **kwargs: Any
) -> ModelReport:
    validation = walk_forward(samples, classify=True, label_of=win_label, **kwargs)
    if validation.refusal is not None:
        return ModelReport("win_probability", None, None, validation, None, None,
                           validation.refusal)

    if validation.size < MIN_SCORED_PREDICTIONS:
        return ModelReport(
            "win_probability", None, None, validation, None, None,
            f"only {validation.size} trades could be scored out of sample and at least "
            f"{MIN_SCORED_PREDICTIONS} are needed before a probability means anything",
        )

    calibration = assess(
        [prediction.predicted for prediction in validation.predictions],
        [prediction.won for prediction in validation.predictions],
        seed=seed,
    )
    skill = assess_skill(validation.predictions, seed=seed)

    refusal = _classifier_refusal(calibration, skill)
    fitted = None if refusal else final_model(samples, classify=True, label_of=win_label)
    if refusal is None and fitted is None:
        refusal = "the model could not be fit over the full history"
    if refusal is None and fitted is not None and not fitted[0].converged:
        refusal = "the fit over the full history did not converge"
        fitted = None

    return ModelReport(
        head="win_probability",
        model=fitted[0] if fitted else None,
        standardiser=fitted[1] if fitted else None,
        validation=validation,
        calibration=calibration,
        skill=skill,
        refusal=refusal,
    )


def _classifier_refusal(
    calibration: CalibrationReport, skill: SkillEvidence
) -> str | None:
    """Plain language for why a probability model may not be served.

    Ordered so the trader hears the most fundamental problem first. Failing to beat the
    base rate is a more important fact than being miscalibrated, because a model with no
    skill has nothing worth calibrating.

    The skill test reads ``established`` — the interval — rather than the point estimate.
    Gating on ``skill > 0`` deployed a model on pure noise in one probe run out of ten,
    at a skill score of +0.014, and nothing in the payload marked it as luck.
    """
    if not skill.established:
        return _no_skill_reason(
            skill, "your results", "simply assuming your average win rate on every trade"
        )
    if not calibration.is_calibrated:
        error = calibration.expected_calibration_error
        gap = f" — its stated probabilities are off by {error:.1%} on average" if error else ""
        return (
            "the model's stated probabilities do not match how often those trades "
            f"actually won{gap}, by more than sampling noise explains"
        )
    if not calibration.is_reliable:
        return (
            f"only {calibration.sample_size} out-of-sample predictions are available, "
            "which is too few to establish that the probabilities are trustworthy"
        )
    return None


def _no_skill_reason(skill: SkillEvidence, target: str, baseline: str) -> str:
    """Why a model failed the skill gate, distinguishing the two ways it can fail.

    "No better than a coin flip" and "better on this sample, but not by enough to rule
    out luck" are different findings and lead to different actions — the first says the
    features are wrong, the second says there is not enough history yet. Collapsing them
    into one message would hide that.
    """
    if skill.value is None:
        return (
            "there was not enough out-of-sample data to test whether this model "
            f"predicts {target}"
        )

    if skill.value <= 0:
        return (
            f"this model does not predict {target} any better than {baseline} "
            f"(skill score {skill.value:.3f}), so it is not served"
        )

    if skill.interval is None:
        return (
            f"this model looks better than {baseline} on your history "
            f"(skill score {skill.value:.3f}), but there are too few trading sessions to "
            "tell that apart from chance"
        )

    return (
        f"this model beat {baseline} on your history (skill score {skill.value:.3f}), but "
        f"resampling your sessions puts the true figure anywhere from {skill.interval.low:.3f} "
        f"to {skill.interval.high:.3f} — the range includes zero, so the apparent edge "
        "cannot be distinguished from luck yet"
    )


def _train_regressor(samples: Sequence[LabelledSnapshot], **kwargs: Any) -> ModelReport:
    validation = walk_forward(samples, classify=False, label_of=r_label, **kwargs)
    if validation.refusal is not None:
        return ModelReport("expected_r", None, None, validation, None, None, validation.refusal)

    if validation.size < MIN_SCORED_PREDICTIONS:
        return ModelReport(
            "expected_r", None, None, validation, None, None,
            f"only {validation.size} trades have both a recorded stop and enough history "
            f"to be scored out of sample; at least {MIN_SCORED_PREDICTIONS} are needed",
        )

    skill = assess_skill(validation.predictions)
    if not skill.established:
        return ModelReport(
            "expected_r", None, None, validation, None, skill,
            _no_skill_reason(skill, "your R multiples", "assuming your average R on every trade"),
        )

    fitted = final_model(samples, classify=False, label_of=r_label)
    if fitted is None or not fitted[0].converged:
        return ModelReport(
            "expected_r", None, None, validation, None, skill,
            "the model could not be fit over the full history",
        )

    return ModelReport(
        head="expected_r",
        model=fitted[0],
        standardiser=fitted[1],
        validation=validation,
        calibration=None,
        skill=skill,
        refusal=None,
    )


def predict(report: ModelReport, snapshot: EntrySnapshot) -> Decimal | None:
    """Serve one prediction, or ``None`` if the model is not deployable.

    Refusing here as well as at the API boundary is deliberate. A gate checked in one
    place is a gate someone routes around; this one is checked wherever a number is
    produced.
    """
    if not report.is_deployable or report.model is None or report.standardiser is None:
        return None
    vector = report.standardiser.apply(snapshot)
    if vector is None:
        return None
    if report.head == "win_probability":
        return report.model.probability(vector)
    return report.model.score(vector)
