"""Proving the leakage barrier is load-bearing rather than decorative.

A guard nobody has watched fail is a guard nobody knows the value of. These tests
deliberately breach each defence and assert that the result is exactly the catastrophe
the defence was built to prevent — so that a future change which quietly removes one has
a failing test explaining what it cost.
"""

from __future__ import annotations

from decimal import Decimal

from app.ml.features import FEATURES, PredictiveFeature, Standardiser
from app.ml.report import train
from app.ml.snapshot import build_snapshots
from app.ml.validation import WalkForwardResult, final_model, walk_forward, win_label
from tests.unit.ml.conftest import noise_history


class TestWhatLeakageWouldLookLike:
    def test_a_post_hoc_feature_manufactures_skill_out_of_pure_noise(self) -> None:
        """The reason ``EntrySnapshot`` exists, demonstrated.

        A feature that encodes the outcome is smuggled in — here, the trade's own net
        P&L, exactly what would happen if someone reused the clustering feature set from
        ``app.analytics.features`` for prediction. The data has **no** learnable signal,
        yet the model reports near-perfect skill.

        This is what a leaked model looks like from the outside: not suspicious, just
        excellent. Nothing in the metrics distinguishes it from a genuine discovery,
        which is why the defence has to be structural.
        """
        history = noise_history(seed=1000)
        samples = build_snapshots(history)
        outcome_by_trade = {
            str(sample.snapshot.trade_id): sample.net_pnl for sample in samples
        }

        leaked = PredictiveFeature(
            name="leaked_outcome",
            description="the trade's own result — never legitimately available at entry",
            extract=lambda snapshot: outcome_by_trade[str(snapshot.trade_id)],
        )

        clean = walk_forward(samples, classify=True, label_of=win_label, step_sessions=10)
        dirty = walk_forward(
            samples,
            classify=True,
            label_of=win_label,
            features=(*FEATURES, leaked),
            step_sessions=10,
        )

        clean_error = _mean_squared_error(clean)
        dirty_error = _mean_squared_error(dirty)

        # On data with no signal an honest model cannot do much better than the base
        # rate, whose Brier score here is around 0.25.
        assert clean_error > Decimal("0.2")
        # The leaked model reads the answer.
        assert dirty_error < Decimal("0.02")

        # And it sails through the deployability gate, which is the frightening part.
        assert train(samples, step_sessions=10).win_probability.is_deployable is False

    def test_the_snapshot_makes_that_feature_impossible_to_write(self) -> None:
        """The same leak attempted the way it would actually happen.

        Not by looking up a side table, but by reaching for the field on the object —
        which is what a developer adding a feature would naturally try. There is no such
        attribute, so the mistake is a crash at the first test rather than a model.
        """
        snapshot = build_snapshots(noise_history(seed=1, sessions=2, per_session=2))[0].snapshot

        for forbidden in ("net_pnl", "r_multiple", "mfe_r", "mae_r", "closed_at"):
            assert not hasattr(snapshot, forbidden)


class TestStandardisationLeakage:
    """Scaling fit on the full history is a walk-forward evaluation in disguise."""

    def test_a_standardiser_reflects_only_the_rows_it_was_fit_on(self) -> None:
        """The centre must come from training rows, not from everything available.

        Fitting the scaler on the whole history before splitting is one of the most
        common ways an out-of-sample number becomes an in-sample one: the scaler has
        already seen the future, and every "unseen" row is standardised using statistics
        that included it.
        """
        samples = build_snapshots(noise_history(seed=3, sessions=40, per_session=4))
        early = [sample.snapshot for sample in samples[:80]]
        everything = [sample.snapshot for sample in samples]

        on_train = Standardiser.fit(early)
        on_all = Standardiser.fit(everything)

        assert on_train.names == on_all.names
        assert on_train.centres != on_all.centres

        # Named explicitly, because the assertion above would also pass if some
        # incidental column differed. Session P&L accumulates through the history, so
        # its mean over the first 80 trades cannot equal its mean over all of them —
        # unless the scaler was fit on data the training window never saw.
        index = on_train.names.index("session_pnl_before")
        assert on_train.centres[index] != on_all.centres[index]

    def test_the_fold_fitter_returns_the_scaler_it_used(self) -> None:
        """Model and scaler travel together so they cannot be mismatched.

        Returning a model without its scaler would let a caller standardise test rows
        with statistics from a different window — a leak with no visible symptom.
        """
        samples = build_snapshots(noise_history(seed=4, sessions=30, per_session=3))
        fitted = final_model(samples, classify=True, label_of=win_label)

        assert fitted is not None
        model, standardiser = fitted
        assert model.feature_names == standardiser.names


def _mean_squared_error(result: WalkForwardResult) -> Decimal:
    predictions = result.predictions
    assert predictions
    total = sum(
        ((item.predicted - item.actual) ** 2 for item in predictions), Decimal(0)
    )
    return total / Decimal(len(predictions))
