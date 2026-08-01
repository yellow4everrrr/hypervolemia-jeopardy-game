"""The small amount of linear algebra the models need, in exact decimal arithmetic.

Two questions this file has to answer up front.

**Why not NumPy?** Because it is not here, and adding it would be the only reason it is
here. The models in this package solve systems of at most a dozen unknowns. A 12x12
Gaussian elimination is thirty lines and runs in microseconds; the dependency, the wheel,
and the version matrix are not worth it for that.

**Why ``Decimal`` and not ``float``?** The rest of the engine is exact, and the usual
justification — money must not drift — does not apply to a standardised feature matrix.
The real reason here is different and worth stating, because it is the one that survives
scrutiny: **a stored model must reproduce its predictions.** Coefficients are persisted,
and a prediction served in October has to match the backtest that justified deploying it
in July. Floating-point results depend on the platform's ``libm``, on compiler flags, and
on summation order; ``Decimal`` results depend on the precision setting alone, which is
recorded alongside the coefficients. A model whose probability output moves in the third
decimal place after a container upgrade is a model whose calibration report is a
historical curiosity.

The cost is speed, and it is real: fitting is seconds rather than milliseconds. Training
is therefore an explicit ``POST``, never a page render, and milestone 13 moves it to a
worker — the same treatment the pattern scan and the what-if sweep already get.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext

Matrix = list[list[Decimal]]
Vector = list[Decimal]

#: Working precision for fitting. Well above what the results are reported to, so that
#: accumulated error in the normal equations stays far below the last reported digit.
#: Recorded on every fitted model: it is part of what makes a prediction reproducible.
FIT_PRECISION = 40


class SingularMatrixError(ValueError):
    """Raised when a system has no unique solution.

    With ridge regularisation this should not happen — the penalty makes the matrix
    positive definite. It is raised rather than worked around because reaching it means
    an assumption elsewhere is wrong, and silently returning a pseudo-solution would hide
    that behind a plausible-looking model.
    """


def solve(matrix: Sequence[Sequence[Decimal]], rhs: Sequence[Decimal]) -> Vector:
    """Solve ``A x = b`` by Gaussian elimination with partial pivoting.

    Partial pivoting is not optional. Without it, a zero — or merely small — leading
    entry destroys the accuracy of everything below it, and the feature matrices here
    routinely contain a binary column whose first rows are all zero.
    """
    size = len(rhs)
    if any(len(row) != size for row in matrix) or len(matrix) != size:
        raise ValueError("solve requires a square system matching the right-hand side")

    with localcontext() as ctx:
        ctx.prec = FIT_PRECISION
        work: Matrix = [[Decimal(value) for value in row] for row in matrix]
        vector: Vector = [Decimal(value) for value in rhs]

        for column in range(size):
            pivot_row = max(range(column, size), key=lambda index: abs(work[index][column]))
            if work[pivot_row][column] == 0:
                raise SingularMatrixError(f"no pivot available in column {column}")
            if pivot_row != column:
                work[column], work[pivot_row] = work[pivot_row], work[column]
                vector[column], vector[pivot_row] = vector[pivot_row], vector[column]

            pivot = work[column][column]
            for row in range(column + 1, size):
                factor = work[row][column] / pivot
                if factor == 0:
                    continue
                for inner in range(column, size):
                    work[row][inner] -= factor * work[column][inner]
                vector[row] -= factor * vector[column]

        solution: Vector = [Decimal(0)] * size
        for row in reversed(range(size)):
            accumulated = vector[row]
            for column in range(row + 1, size):
                accumulated -= work[row][column] * solution[column]
            solution[row] = accumulated / work[row][row]

    return solution


def dot(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    return sum(
        (a * b for a, b in zip(left, right, strict=True)),
        Decimal(0),
    )


#: Beyond this the logistic curve is flat to more decimal places than anything is
#: reported to, and ``exp`` starts costing precision for no gain. Clamping also keeps a
#: separable training fold — where the optimiser would otherwise drive a coefficient to
#: infinity — from producing coefficients nobody can interpret.
LOGIT_LIMIT = Decimal(30)


def sigmoid(value: Decimal) -> Decimal:
    """Logistic function, clamped and evaluated in the numerically stable direction.

    For positive inputs ``1 / (1 + exp(-x))`` is stable; for negative ones the mirrored
    form avoids exponentiating a large positive number. Both branches are exact in
    decimal arithmetic, so the choice is about cost rather than correctness.
    """
    if value > LOGIT_LIMIT:
        value = LOGIT_LIMIT
    elif value < -LOGIT_LIMIT:
        value = -LOGIT_LIMIT

    if value >= 0:
        return Decimal(1) / (Decimal(1) + (-value).exp())
    exponential = value.exp()
    return exponential / (Decimal(1) + exponential)
