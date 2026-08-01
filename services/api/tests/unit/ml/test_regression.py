"""The fitting machinery: does it recover known answers, and does it stay finite?"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from app.ml.linalg import SingularMatrixError, sigmoid, solve
from app.ml.regression import fit_linear, fit_logistic


class TestSolve:
    def test_it_recovers_a_known_solution(self) -> None:
        matrix = [
            [Decimal(2), Decimal(1), Decimal(-1)],
            [Decimal(-3), Decimal(-1), Decimal(2)],
            [Decimal(-2), Decimal(1), Decimal(2)],
        ]
        rhs = [Decimal(8), Decimal(-11), Decimal(-3)]
        solution = solve(matrix, rhs)

        assert [value.quantize(Decimal("0.0001")) for value in solution] == [
            Decimal(2),
            Decimal(3),
            Decimal(-1),
        ]

    def test_it_survives_a_zero_leading_entry(self) -> None:
        """Partial pivoting is not optional: a binary feature column routinely starts
        with a run of zeros, and without pivoting the elimination divides by one."""
        matrix = [[Decimal(0), Decimal(1)], [Decimal(1), Decimal(0)]]
        solution = solve(matrix, [Decimal(3), Decimal(5)])

        assert solution == [Decimal(5), Decimal(3)]

    def test_a_singular_system_raises_rather_than_guessing(self) -> None:
        matrix = [[Decimal(1), Decimal(2)], [Decimal(2), Decimal(4)]]
        with pytest.raises(SingularMatrixError):
            solve(matrix, [Decimal(1), Decimal(2)])


class TestSigmoid:
    def test_it_is_symmetric_about_zero(self) -> None:
        assert sigmoid(Decimal(0)) == Decimal("0.5")
        for value in (Decimal("0.5"), Decimal(2), Decimal(7)):
            assert (sigmoid(value) + sigmoid(-value)).quantize(
                Decimal("0.000000001")
            ) == Decimal(1)

    def test_extreme_inputs_saturate_instead_of_overflowing(self) -> None:
        """Clamping keeps a separable training fold from producing a coefficient
        nobody can interpret, and keeps ``exp`` from being handed a huge argument."""
        assert Decimal(0) < sigmoid(Decimal(-10000)) < Decimal("0.000001")
        assert Decimal("0.999999") < sigmoid(Decimal(10000)) < Decimal(1)


class TestLogisticFit:
    def test_it_recovers_a_planted_relationship(self) -> None:
        """One informative feature and one pure-noise feature.

        The informative coefficient must be large and correctly signed, and the noise
        coefficient must be much smaller — a fit that spread weight evenly across both
        would be fitting noise as hard as signal.
        """
        rng = random.Random(5)
        rows: list[list[Decimal]] = []
        labels: list[Decimal] = []
        for _ in range(400):
            signal = Decimal(1) if rng.random() < 0.5 else Decimal(-1)
            noise = Decimal(str(round(rng.uniform(-1, 1), 4)))
            probability = 0.85 if signal > 0 else 0.15
            rows.append([signal, noise])
            labels.append(Decimal(1) if rng.random() < probability else Decimal(0))

        model = fit_logistic(rows, labels, ["signal", "noise"], penalty=Decimal("0.1"))

        assert model.converged
        weights = dict(zip(model.feature_names, model.coefficients, strict=True))
        assert weights["signal"] > Decimal(1)
        assert abs(weights["noise"]) < weights["signal"] / Decimal(3)

    def test_the_intercept_tracks_the_base_rate_with_no_features(self) -> None:
        """With a constant feature column the model has nothing but the intercept, which
        must then reproduce the observed rate — the baseline every skill score is
        measured against."""
        rows = [[Decimal(0)] for _ in range(200)]
        labels = [Decimal(1)] * 60 + [Decimal(0)] * 140
        model = fit_logistic(rows, labels, ["constant"], penalty=Decimal("0.0001"))

        assert sigmoid(model.intercept).quantize(Decimal("0.01")) == Decimal("0.30")

    def test_a_stronger_penalty_shrinks_coefficients(self) -> None:
        """The defining behaviour of ridge, asserted so that a change which silently
        stops applying the penalty is caught."""
        rng = random.Random(9)
        rows = [[Decimal(str(round(rng.uniform(-2, 2), 3)))] for _ in range(200)]
        labels = [Decimal(1) if row[0] > 0 else Decimal(0) for row in rows]

        light = fit_logistic(rows, labels, ["x"], penalty=Decimal("0.1"))
        heavy = fit_logistic(rows, labels, ["x"], penalty=Decimal(100))

        assert abs(heavy.coefficients[0]) < abs(light.coefficients[0])

    def test_perfectly_separable_data_does_not_explode(self) -> None:
        """Unpenalised logistic regression drives a coefficient to infinity here.

        A separable fold is not rare on a few hundred trades, and an infinite coefficient
        produces probabilities of exactly 0 and 1 — certainty about a trading outcome.
        """
        rows = [[Decimal(-1)]] * 100 + [[Decimal(1)]] * 100
        labels = [Decimal(0)] * 100 + [Decimal(1)] * 100
        model = fit_logistic(rows, labels, ["x"], penalty=Decimal(1))

        assert model.coefficients[0].is_finite()
        assert abs(model.coefficients[0]) < Decimal(100)
        assert Decimal(0) < model.probability([Decimal(1)]) < Decimal(1)

    def test_it_converges_well_inside_the_iteration_cap(self) -> None:
        """Newton's method should take a handful of steps; hitting the cap means
        something is oscillating and the model is reported as unconverged."""
        rng = random.Random(3)
        rows = [[Decimal(str(round(rng.uniform(-2, 2), 3)))] for _ in range(300)]
        labels = [Decimal(1) if rng.random() < 0.5 else Decimal(0) for _ in rows]

        model = fit_logistic(rows, labels, ["x"])
        assert model.converged
        assert model.iterations < 15


class TestLinearFit:
    def test_it_recovers_known_coefficients(self) -> None:
        """``y = 3 + 2a - 1b`` exactly, with a negligible penalty."""
        rng = random.Random(2)
        rows: list[list[Decimal]] = []
        labels: list[Decimal] = []
        for _ in range(200):
            a = Decimal(str(round(rng.uniform(-3, 3), 4)))
            b = Decimal(str(round(rng.uniform(-3, 3), 4)))
            rows.append([a, b])
            labels.append(Decimal(3) + Decimal(2) * a - b)

        model = fit_linear(rows, labels, ["a", "b"], penalty=Decimal("0.000001"))

        assert model.intercept.quantize(Decimal("0.001")) == Decimal(3)
        assert model.coefficients[0].quantize(Decimal("0.001")) == Decimal(2)
        assert model.coefficients[1].quantize(Decimal("0.001")) == Decimal(-1)

    def test_the_intercept_is_never_penalised(self) -> None:
        """Shrinking the intercept would pull predictions toward zero rather than toward
        the mean of the data, which is not the prior anyone intends."""
        rows = [[Decimal(0)] for _ in range(100)]
        labels = [Decimal(50)] * 100

        light = fit_linear(rows, labels, ["x"], penalty=Decimal("0.001"))
        heavy = fit_linear(rows, labels, ["x"], penalty=Decimal(1000))

        assert light.intercept.quantize(Decimal("0.01")) == Decimal(50)
        assert heavy.intercept.quantize(Decimal("0.01")) == Decimal(50)


class TestDegenerateInput:
    def test_no_rows_produces_an_unconverged_empty_model(self) -> None:
        model = fit_logistic([], [], ["x"])
        assert not model.converged
        assert model.training_rows == 0

    def test_no_features_produces_an_unconverged_empty_model(self) -> None:
        model = fit_linear([[]], [Decimal(1)], [])
        assert not model.converged
