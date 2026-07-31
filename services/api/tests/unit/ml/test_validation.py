"""Walk-forward mechanics: forward ordering, session blocks, and honest refusals."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from app.ml.snapshot import build_snapshots
from app.ml.validation import (
    MIN_TRAIN_SESSIONS,
    final_model,
    r_label,
    walk_forward,
    win_label,
)
from tests.unit.ml.conftest import build_history, noise_history


class TestForwardOrdering:
    def test_every_scored_trade_comes_after_the_training_window(self) -> None:
        """The property the whole evaluation rests on.

        Asserted directly rather than inferred from the fold arithmetic: for each fold,
        no scored trade may fall on a session at or before that fold's training cutoff.
        Reconstructed here from the fold index so that a change to the stepping logic
        which reintroduces overlap fails loudly.
        """
        samples = build_snapshots(noise_history(seed=21, sessions=45, per_session=3))
        result = walk_forward(samples, classify=True, label_of=win_label, step_sessions=5)

        sessions = sorted({s.snapshot.session_date for s in samples if s.snapshot.session_date})
        by_trade = {str(s.snapshot.trade_id): s.snapshot.session_date for s in samples}

        assert result.predictions
        for prediction in result.predictions:
            train_cutoff = sessions[MIN_TRAIN_SESSIONS + 5 * (prediction.fold - 1) - 1]
            session = by_trade[prediction.trade_id]
            assert session is not None
            assert session > train_cutoff

    def test_no_trade_is_scored_twice(self) -> None:
        """Overlapping test windows would double-count evidence and narrow every
        interval built from these predictions."""
        samples = build_snapshots(noise_history(seed=22, sessions=45, per_session=3))
        result = walk_forward(samples, classify=True, label_of=win_label, step_sessions=5)

        identifiers = [prediction.trade_id for prediction in result.predictions]
        assert len(identifiers) == len(set(identifiers))

    def test_a_session_never_straddles_a_fold_boundary(self) -> None:
        """Rule two of the module: trades sharing a day are not independent draws.

        Every trade in a given session must be scored in the same fold, or the model
        will have trained on part of a day and been tested on the rest of it.
        """
        samples = build_snapshots(noise_history(seed=23, sessions=45, per_session=4))
        result = walk_forward(samples, classify=True, label_of=win_label, step_sessions=5)

        by_trade = {str(s.snapshot.trade_id): s.snapshot.session_date for s in samples}
        folds_per_session: dict[object, set[int]] = {}
        for prediction in result.predictions:
            folds_per_session.setdefault(by_trade[prediction.trade_id], set()).add(prediction.fold)

        assert all(len(folds) == 1 for folds in folds_per_session.values())

    def test_predictions_carry_their_session_for_block_resampling(self) -> None:
        samples = build_snapshots(noise_history(seed=24, sessions=40, per_session=3))
        result = walk_forward(samples, classify=True, label_of=win_label, step_sessions=5)

        assert all(prediction.session is not None for prediction in result.predictions)


class TestRefusals:
    def test_too_few_sessions_refuses_with_a_reason(self) -> None:
        samples = build_snapshots(noise_history(seed=25, sessions=6, per_session=4))
        result = walk_forward(samples, classify=True, label_of=win_label)

        assert result.refusal is not None
        assert "sessions" in result.refusal
        assert result.predictions == ()
        assert not result.is_scorable

    def test_a_history_with_many_trades_but_few_days_is_still_refused(self) -> None:
        """Sessions, not trades, are the unit of evidence.

        Four hundred trades taken across five days is five observations of a trader's
        behaviour, not four hundred. A gate counting trades would happily validate on it.
        """
        samples = build_snapshots(build_history(sessions=5, per_session=80, seed=26))
        result = walk_forward(samples, classify=True, label_of=win_label)

        assert result.refusal is not None

    def test_rows_without_a_target_are_counted_not_imputed(self) -> None:
        """Half the history has no recorded stop, so half the R rows must be skipped."""
        history = build_history(sessions=40, per_session=4, seed=27)
        stripped = [
            trade if index % 2 else dataclasses.replace(trade, r_multiple=None)
            for index, trade in enumerate(history)
        ]
        result = walk_forward(
            build_snapshots(stripped), classify=False, label_of=r_label, step_sessions=5
        )

        assert result.skipped_rows > 0
        assert result.size > 0


class TestPenaltySelection:
    def test_the_penalty_is_chosen_inside_the_training_window(self) -> None:
        """Each fold records the penalty it selected, and they may legitimately differ.

        A single penalty across every fold would mean it was chosen once — most likely
        with the whole history in view, which is the leak this design exists to avoid.
        """
        samples = build_snapshots(noise_history(seed=28, sessions=60, per_session=4))
        result = walk_forward(samples, classify=True, label_of=win_label, step_sessions=5)

        assert result.chosen_penalties
        assert len(result.chosen_penalties) == result.folds
        assert all(penalty > 0 for penalty in result.chosen_penalties)


class TestFinalModel:
    def test_it_trains_on_more_rows_than_any_single_fold(self) -> None:
        """The served model uses the whole history; the reported scores never do."""
        samples = build_snapshots(noise_history(seed=29, sessions=40, per_session=3))
        fitted = final_model(samples, classify=True, label_of=win_label)

        assert fitted is not None
        model, _ = fitted
        assert model.training_rows == len(samples)

    def test_it_is_deterministic(self) -> None:
        """Two fits of the same history must produce identical coefficients.

        Reproducibility is the stated reason this package uses exact decimal arithmetic
        instead of floats; if it did not hold, that choice bought nothing.
        """
        samples = build_snapshots(noise_history(seed=30, sessions=30, per_session=3))
        first = final_model(samples, classify=True, label_of=win_label)
        second = final_model(samples, classify=True, label_of=win_label)

        assert first is not None and second is not None
        assert first[0].coefficients == second[0].coefficients
        assert first[0].intercept == second[0].intercept

    def test_probabilities_stay_strictly_inside_zero_and_one(self) -> None:
        """A model asserting certainty about a trade is wrong about the world."""
        samples = build_snapshots(noise_history(seed=31, sessions=30, per_session=3))
        fitted = final_model(samples, classify=True, label_of=win_label)
        assert fitted is not None
        model, standardiser = fitted

        for sample in samples:
            vector = standardiser.apply(sample.snapshot)
            if vector is None:
                continue
            probability = model.probability(vector)
            assert Decimal(0) < probability < Decimal(1)
