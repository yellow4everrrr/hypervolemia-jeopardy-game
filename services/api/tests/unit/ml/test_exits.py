"""Exit selection, and the fill bias that decides what may be searched.

The null here is exact rather than conventional: on a driftless random walk the optional
stopping theorem proves that no stop or target placement changes the expectation. Anything
established on this data is a false positive by proof, not by convention.

The first two tests are the important ones. They do not exercise the selection procedure at
all — they measure the *re-pricing*, and they are what forced the design.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.analytics.types import Direction, TradeRecord
from app.analytics.whatif import Scenario, _delta, _reprice
from app.core.ids import uuid7
from app.ml.exits import (
    TARGET_CANDIDATES,
    ExitRule,
    _score,
    _stability,
    candidates,
    recommend,
    select,
)

BASE = datetime(2026, 2, 2, 14, 30, tzinfo=UTC)
ACCOUNT = uuid7()


def history(
    count: int, seed: int, *, revert: float = 0.0, per_session: int = 6
) -> list[TradeRecord]:
    """A driftless random walk, with excursions that are its running extremes.

    ``revert`` pulls price back toward its start after it runs up, which is the one shape
    where taking profit at a level genuinely beats holding — used for the positive control,
    because an engine that established nothing under all circumstances would pass every
    null test here while being useless.
    """
    rng = random.Random(seed)
    trades: list[TradeRecord] = []
    session = date(2026, 2, 2)

    for index in range(count):
        if index % per_session == 0:
            session += timedelta(days=1)
        position = high = low = 0.0
        for _ in range(240):
            position += rng.gauss(-revert * position, 0.1)
            high, low = max(high, position), min(low, position)

        opened = datetime.combine(session, BASE.timetz()) + timedelta(
            minutes=13 * (index % per_session)
        )
        trades.append(
            TradeRecord(
                trade_id=uuid7(),
                account_id=ACCOUNT,
                opened_at=opened,
                closed_at=opened + timedelta(minutes=5),
                direction=Direction.LONG,
                net_pnl=Decimal(str(round(position * 100, 2))),
                r_multiple=Decimal(str(round(position, 3))),
                mae_r=Decimal(str(round(low, 3))),
                mfe_r=Decimal(str(round(high, 3))),
                quantity=Decimal(1),
                session_date=session,
                entry_hour=opened.hour,
                entry_weekday=session.isoweekday(),
                duration_seconds=300,
            )
        )
    return trades


class TestTheFillBias:
    """Why stops are not in the grid. Measured on the re-pricing, with no selection."""

    def test_repricing_a_stop_is_optimistic_on_data_where_it_cannot_help(self) -> None:
        """One fixed stop, no grid, no choosing — and it still reports a large gain.

        On a driftless walk optional stopping proves the expected difference is zero. The
        gap is the perfect-fill assumption: an excursion says price *reached* a level, not
        that a fill was available there, and conditioning on "the extreme passed the stop"
        selects the paths that overshot it.
        """
        trades = history(1200, 99)
        # Built directly, because ExitRule deliberately cannot express a stop.
        stop_only = Scenario(label="0.5R stop", stop_r=Decimal("0.5"), target_r=None)

        priced = [_reprice(trade, stop_only) for trade in trades]
        applicable = [item for item in priced if item.outcome != "inapplicable"]
        mean_delta = sum((_delta(item) for item in applicable), Decimal(0)) / Decimal(
            len(applicable)
        )

        # Materially positive where the truth is exactly zero.
        assert mean_delta > Decimal(1)

    def test_repricing_a_target_is_biased_the_other_way(self) -> None:
        """The asymmetry that makes targets usable.

        A stop is a market order and fills at the trigger *or worse*, so assuming the
        trigger is optimistic. A target is a limit order and fills at the limit *or
        better*, so assuming the limit is conservative — the same artefact, opposite sign.
        """
        trades = history(1200, 99)
        priced = [
            _reprice(trade, ExitRule(target_r=Decimal(1)).to_scenario()) for trade in trades
        ]
        applicable = [item for item in priced if item.outcome != "inapplicable"]
        mean_delta = sum((_delta(item) for item in applicable), Decimal(0)) / Decimal(
            len(applicable)
        )

        # Biased against itself, so a surviving improvement survives despite it.
        assert mean_delta < 0

    def test_the_grid_contains_no_stops(self) -> None:
        """The consequence, asserted so it cannot be quietly reverted.

        "Just add stops to the grid" is the obvious future change, and this is where
        whoever tries finds out why not.
        """
        assert all(rule.to_scenario().stop_r is None for rule in candidates())
        assert len(candidates()) == len(TARGET_CANDIDATES)


class TestTheNullBattery:
    @pytest.mark.parametrize("seed", [1, 5, 10])
    def test_nothing_is_established_on_a_driftless_walk(self, seed: int) -> None:
        """Optional stopping proves no exit rule can help here."""
        result = recommend(history(600, seed), draws=300)

        assert not result.is_established, (
            f"recommended {result.rule} on noise "
            f"(improvement {result.improvement}, stability {result.stability})"
        )
        assert result.rule is None
        assert result.refusal

    def test_the_procedure_still_ran_rather_than_refusing_for_want_of_data(self) -> None:
        """Guards the battery above: a refusal for thin data would pass it vacuously."""
        result = recommend(history(600, 3), draws=300)

        assert len(result.folds) >= 5
        assert result.scored_trades >= 100
        assert result.improvement is not None


class TestThePositiveControl:
    def test_a_genuine_target_edge_is_found(self) -> None:
        """Mean-reverting paths: price runs up and comes back, so a target captures what
        holding gives away. If this is refused the engine is inert."""
        result = recommend(history(600, 21, revert=0.02), draws=300)

        assert result.is_established, f"refused a real edge: {result.refusal}"
        assert result.rule is not None
        assert result.improvement is not None and result.improvement > 0

    def test_the_selection_is_stable_when_the_edge_is_real(self) -> None:
        result = recommend(history(600, 21, revert=0.02), draws=300)

        assert result.stability == Decimal(1)

    def test_the_interval_excludes_zero_when_established(self) -> None:
        result = recommend(history(600, 22, revert=0.02), draws=300)

        assert result.interval is not None
        assert result.interval.low > 0


class TestRefusals:
    def test_a_short_history_is_refused_before_anything_is_selected(self) -> None:
        result = recommend(history(30, 7), draws=200)

        assert not result.is_established
        assert "sessions" in (result.refusal or "")
        assert result.folds == ()

    def test_an_unstable_selection_is_refused_and_names_the_rules(self) -> None:
        """A procedure that picks a different target every window is fitting noise, and a
        trader could not have followed it — there was never one rule to follow."""
        from app.ml.exits import FoldResult

        folds = [
            FoldResult(index, ExitRule(target_r=Decimal(index)), 20, 30, ())
            for index in range(1, 5)
        ]

        assert _stability(folds) == Decimal("0.25")

    def test_stability_is_one_when_every_fold_agrees(self) -> None:
        from app.ml.exits import FoldResult

        folds = [
            FoldResult(index, ExitRule(target_r=Decimal(2)), 20, 30, ())
            for index in range(1, 5)
        ]

        assert _stability(folds) == Decimal(1)

    def test_the_refusal_explains_rather_than_stating_unavailable(self) -> None:
        result = recommend(history(600, 1), draws=300)
        refusal = result.refusal or ""

        assert len(refusal) > 40
        assert "per trade" in refusal or "sessions" in refusal or "windows" in refusal


class TestScoring:
    def test_a_rule_that_cannot_be_applied_scores_nothing(self) -> None:
        """Coverage matters as much here as in the sweep: a rule applicable to a fifth of
        the window was selected on a fifth of the evidence."""
        import dataclasses

        stripped = [
            dataclasses.replace(trade, mfe_r=None, mae_r=None)
            for trade in history(120, 4)
        ]

        assert _score(stripped, ExitRule(target_r=Decimal(2))) is None

    def test_selection_returns_none_when_no_candidate_applies(self) -> None:
        import dataclasses

        stripped = [
            dataclasses.replace(trade, mfe_r=None, mae_r=None)
            for trade in history(120, 4)
        ]

        assert select(stripped) is None

    def test_selection_is_deterministic(self) -> None:
        trades = history(300, 8)

        assert select(trades) == select(trades)


class TestPayload:
    def test_it_states_that_the_improvement_is_out_of_sample(self) -> None:
        payload = recommend(history(600, 1), draws=200).to_payload()

        assert "out-of-sample performance of the selection" in payload["interpretation"]

    def test_a_refused_recommendation_carries_no_rule(self) -> None:
        payload = recommend(history(600, 1), draws=200).to_payload()

        assert payload["is_established"] is False
        assert payload["rule"] is None
        assert payload["refusal"]
