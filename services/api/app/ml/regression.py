"""Ridge logistic and ridge linear regression, fit by Newton's method.

Two heads, one shared design:

* **Win probability** — logistic regression, fit by iteratively reweighted least squares.
* **Expected R** — linear regression on the R multiple, over the subset of trades that
  have a recorded stop.

**Why regularisation is not optional.** A trader's history is a few hundred rows against
nine columns, drawn from a process that is barely stationary. Unpenalised logistic
regression on data like that has two failure modes and hits both: it chases noise, and
when a fold happens to be separable — every long trade in the training window won — it
drives a coefficient toward infinity and reports certainty. The ridge penalty makes the
Hessian positive definite, which removes the second failure outright, and shrinks
coefficients toward zero, which is the correct prior for "this feature probably does not
matter". The penalty strength is chosen inside each training fold, never on test data.

**Why Newton and not gradient descent.** Newton's method converges here in under ten
iterations with no learning rate to tune, and a learning rate is exactly the kind of
hyperparameter that gets set once on one trader's data and silently fails on another's.
The cost is forming and solving a small system per iteration, which at nine features is
nothing.

**The intercept is never penalised.** Shrinking it toward zero would pull the predicted
probability toward 50% rather than toward the base rate, which is not the prior anyone
intends. It is carried as column zero and excluded from the penalty.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from app.ml.linalg import FIT_PRECISION, SingularMatrixError, dot, sigmoid, solve

#: Ridge strengths tried when selecting a penalty. Spans four orders of magnitude, which
#: is enough for the selection to be meaningful without turning fitting into a grid
#: search — every extra candidate is another full fit inside every fold.
PENALTY_GRID: tuple[Decimal, ...] = (
    Decimal("0.1"),
    Decimal(1),
    Decimal(10),
    Decimal(100),
)

#: Newton stops when no coefficient moves by more than this. Reached in five or six
#: iterations on well-behaved data; the iteration cap is a guard, not the usual exit.
CONVERGENCE_TOLERANCE = Decimal("0.000001")

MAX_ITERATIONS = 40

#: Probabilities are clamped away from 0 and 1. A model asserting certainty on a trading
#: outcome is wrong about the world regardless of what the data showed, and an exact
#: 0 makes the log-loss infinite, which destroys the calibration report for one row.
PROBABILITY_FLOOR = Decimal("0.001")
PROBABILITY_CEILING = Decimal("0.999")


@dataclass(frozen=True, slots=True)
class FittedModel:
    """Coefficients and the provenance needed to reproduce a prediction.

    Attributes:
        intercept: The unpenalised constant term.
        coefficients: Aligned with the standardiser's retained feature names.
        penalty: The ridge strength actually used.
        precision: Decimal working precision at fit time. Stored because it is part of
            what makes a served prediction match the backtest that justified it.
        converged: Whether Newton reached the tolerance before the iteration cap. A model
            that did not converge is reported as such and is never deployable.
    """

    intercept: Decimal
    coefficients: tuple[Decimal, ...]
    feature_names: tuple[str, ...]
    penalty: Decimal
    iterations: int
    converged: bool
    training_rows: int
    precision: int = FIT_PRECISION

    def score(self, vector: Sequence[Decimal]) -> Decimal:
        """The linear predictor. Probability for the logistic head, R for the linear."""
        return self.intercept + dot(self.coefficients, vector)

    def probability(self, vector: Sequence[Decimal]) -> Decimal:
        probability = sigmoid(self.score(vector))
        if probability < PROBABILITY_FLOOR:
            return PROBABILITY_FLOOR
        if probability > PROBABILITY_CEILING:
            return PROBABILITY_CEILING
        return probability

    def to_payload(self) -> dict[str, Any]:
        return {
            "intercept": str(self.intercept),
            "coefficients": {
                name: str(value)
                for name, value in zip(self.feature_names, self.coefficients, strict=True)
            },
            "penalty": str(self.penalty),
            "iterations": self.iterations,
            "converged": self.converged,
            "training_rows": self.training_rows,
            "precision": self.precision,
        }


def fit_logistic(
    rows: Sequence[Sequence[Decimal]],
    labels: Sequence[Decimal],
    feature_names: Sequence[str],
    *,
    penalty: Decimal = Decimal(1),
) -> FittedModel:
    """Ridge logistic regression by iteratively reweighted least squares.

    Each step solves ``(X'WX + λI) δ = X'(y - p) - λβ`` and adds ``δ`` to the
    coefficients. ``W`` is the diagonal of ``p(1 - p)``; it collapses toward zero as
    predictions saturate, which is precisely when the unpenalised system becomes singular
    and the penalty earns its place.
    """
    width = len(feature_names)
    if width == 0 or not rows:
        return FittedModel(Decimal(0), (), tuple(feature_names), penalty, 0, False, 0)

    with localcontext() as ctx:
        ctx.prec = FIT_PRECISION
        # Column zero is the intercept; it carries a 1 on every row and is excluded from
        # the penalty by leaving penalties[0] at zero.
        size = width + 1
        penalties = [Decimal(0)] + [penalty] * width
        beta = [Decimal(0)] * size
        converged = False
        iterations = 0

        for step_number in range(1, MAX_ITERATIONS + 1):
            iterations = step_number
            gradient = [-p * b for p, b in zip(penalties, beta, strict=True)]
            hessian = [[Decimal(0)] * size for _ in range(size)]
            for index in range(size):
                hessian[index][index] = penalties[index]

            for row, label in zip(rows, labels, strict=True):
                design = [Decimal(1), *row]
                probability = sigmoid(dot(beta, design))
                weight = probability * (Decimal(1) - probability)
                residual = label - probability
                for i in range(size):
                    gradient[i] += design[i] * residual
                    if design[i] == 0:
                        continue
                    weighted = weight * design[i]
                    for j in range(i, size):
                        hessian[i][j] += weighted * design[j]

            # Only the upper triangle was accumulated; the matrix is symmetric.
            for i in range(size):
                for j in range(i):
                    hessian[i][j] = hessian[j][i]

            try:
                step = solve(hessian, gradient)
            except SingularMatrixError:
                # Reachable only with a zero penalty on a separable fold. The partial fit
                # is returned marked unconverged rather than raising: the caller's
                # deployability gate already refuses it, and a returned model can be
                # inspected while an exception cannot.
                break

            beta = [b + d for b, d in zip(beta, step, strict=True)]
            if all(abs(value) < CONVERGENCE_TOLERANCE for value in step):
                converged = True
                break

    return FittedModel(
        intercept=beta[0],
        coefficients=tuple(beta[1:]),
        feature_names=tuple(feature_names),
        penalty=penalty,
        iterations=iterations,
        converged=converged,
        training_rows=len(rows),
    )


def fit_linear(
    rows: Sequence[Sequence[Decimal]],
    labels: Sequence[Decimal],
    feature_names: Sequence[str],
    *,
    penalty: Decimal = Decimal(1),
) -> FittedModel:
    """Ridge linear regression — one closed-form solve, no iteration.

    Used for expected R, where the target is continuous. Reported as ``converged`` when
    the system solved, because a closed form either has an answer or does not.
    """
    width = len(feature_names)
    if width == 0 or not rows:
        return FittedModel(Decimal(0), (), tuple(feature_names), penalty, 0, False, 0)

    with localcontext() as ctx:
        ctx.prec = FIT_PRECISION
        size = width + 1
        penalties = [Decimal(0)] + [penalty] * width
        normal = [[Decimal(0)] * size for _ in range(size)]
        for index in range(size):
            normal[index][index] = penalties[index]
        moment = [Decimal(0)] * size

        for row, label in zip(rows, labels, strict=True):
            design = [Decimal(1), *row]
            for i in range(size):
                if design[i] == 0:
                    continue
                moment[i] += design[i] * label
                for j in range(i, size):
                    normal[i][j] += design[i] * design[j]

        for i in range(size):
            for j in range(i):
                normal[i][j] = normal[j][i]

        try:
            beta = solve(normal, moment)
            solved = True
        except SingularMatrixError:
            beta = [Decimal(0)] * size
            solved = False

    return FittedModel(
        intercept=beta[0],
        coefficients=tuple(beta[1:]),
        feature_names=tuple(feature_names),
        penalty=penalty,
        iterations=1,
        converged=solved,
        training_rows=len(rows),
    )
