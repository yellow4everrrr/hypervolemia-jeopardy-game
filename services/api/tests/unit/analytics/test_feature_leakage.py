"""No clustering feature may read the outcome.

The pattern engine forms clusters from a feature vector and then runs a permutation test
asking whether those clusters differ in P&L. That question is only meaningful if the
clusters were built without knowing the P&L. If any feature is a function of the result,
the test becomes a tautology with a p-value attached: it partitions winners from losers
and then reports, with impressive confidence, that winners won.

``EXCLUDED_FROM_CLUSTERING`` states the rule as a list of column names. That is not
enforcement — it is a comment in tuple form, and it was broken by a feature that
mentioned none of the listed names. ``capture_efficiency`` extracted
``r_multiple / mfe_r`` under a ``mfe_r > 0`` guard, which makes its sign identical to the
sign of ``r_multiple``. It shipped, produced two "established" clusters at p = 0.002 with
six-figure annual impacts, and neither the name list nor any test noticed.

This file replaces the name list with a behavioural probe: **vary a trade's outcome,
hold everything else fixed, and require every feature to return exactly what it returned
before.** A feature that moves has read the result, whatever it is called and however
many operations separate the two.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.analytics.clustering import choose_clustering, trades_in_cluster
from app.analytics.features import FEATURES, Feature, build_matrix
from app.analytics.types import TradeRecord
from app.core.ids import uuid7
from app.domain.common.enums import Direction

#: Outcomes spanning a decisive win, a decisive loss, a scratch, and the boundary cases
#: that a naive guard tends to miss. If a feature is outcome-blind it returns the same
#: value for every one of these; if it is not, one of them will move it.
OUTCOMES: tuple[tuple[Decimal, Decimal | None], ...] = (
    (Decimal("1250.00"), Decimal("2.5")),
    (Decimal("-500.00"), Decimal("-1.0")),
    (Decimal("0.00"), Decimal("0.0")),
    (Decimal("-2400.00"), Decimal("-4.8")),
    (Decimal("75.00"), Decimal("0.15")),
    (Decimal("900.00"), None),
)


def base_trade() -> TradeRecord:
    """A trade with every non-outcome field populated.

    Fully populated on purpose: a feature that reads the outcome only when some optional
    field is present would slip past a sparse fixture by returning ``None`` throughout.
    """
    return TradeRecord(
        trade_id=uuid7(),
        account_id=uuid7(),
        opened_at=datetime(2026, 5, 12, 14, 32, tzinfo=UTC),
        closed_at=datetime(2026, 5, 12, 15, 4, tzinfo=UTC),
        direction=Direction.LONG,
        net_pnl=Decimal("1250.00"),
        gross_pnl=Decimal("1262.30"),
        commission=Decimal("7.50"),
        fees=Decimal("4.80"),
        quantity=Decimal("3"),
        r_multiple=Decimal("2.5"),
        duration_seconds=1_920,
        entry_hour=14,
        entry_weekday=2,
        instrument_symbol="ESU6",
        instrument_root="ES",
        mae_r=Decimal("-0.62"),
        mfe_r=Decimal("3.10"),
    )


@pytest.mark.parametrize("feature", FEATURES, ids=lambda f: f.name)
def test_no_feature_changes_when_only_the_outcome_changes(feature: Feature) -> None:
    """The core invariant. Everything else in this file supports this one assertion.

    Note what is *not* varied: ``mae_r`` and ``mfe_r`` stay fixed. Excursions are not the
    outcome — a trade can run 3R in favour and still close at a loss — so a feature may
    legitimately depend on them. What it may not depend on is where the trade actually
    closed.
    """
    trade = base_trade()
    baseline = feature.extract(trade)

    for net_pnl, r_multiple in OUTCOMES:
        varied = replace(
            trade,
            net_pnl=net_pnl,
            gross_pnl=net_pnl + Decimal("12.30"),
            r_multiple=r_multiple,
        )
        assert feature.extract(varied) == baseline, (
            f"feature {feature.name!r} changed from {baseline} to "
            f"{feature.extract(varied)} when only the outcome moved to "
            f"net_pnl={net_pnl}, r_multiple={r_multiple}. It is reading the result the "
            f"cluster test is supposed to be asking about, which makes that test "
            f"circular — see EXCLUDED_FROM_CLUSTERING in app/analytics/features.py."
        )


def test_the_probe_catches_the_feature_that_motivated_it() -> None:
    """Guards the guard.

    A probe that passes because it varies nothing, or because every feature returns
    ``None`` on the fixture, is worse than no probe — it certifies the property it
    stopped checking. So the removed feature is reconstructed here and the probe is
    required to reject it.
    """

    def capture_efficiency(trade: TradeRecord) -> Decimal | None:
        if trade.mfe_r is None or trade.r_multiple is None or trade.mfe_r <= 0:
            return None
        return trade.r_multiple / trade.mfe_r

    leaking = Feature(
        name="capture_efficiency",
        description="realized R as a fraction of the best R available",
        extract=capture_efficiency,
        higher_is="captured most of the available move",
        lower_is="gave back most of the available move",
    )

    with pytest.raises(AssertionError, match="reading the result"):
        test_no_feature_changes_when_only_the_outcome_changes(leaking)


def test_the_fixture_actually_exercises_every_feature() -> None:
    """A feature returning ``None`` on the fixture is untested by the probe above.

    ``None == None`` holds for every outcome, so a feature that never extracts anything
    passes trivially. This fails instead, forcing the fixture to grow a field rather than
    letting the coverage quietly lapse.
    """
    trade = base_trade()
    unexercised = [
        feature.name for feature in FEATURES if feature.extract(trade) is None
    ]

    assert not unexercised, (
        f"{unexercised} extract None from the fixture, so the leakage probe never "
        f"evaluates them. Populate the fields they read in base_trade()."
    )


def test_the_sign_of_a_leaking_feature_is_the_win_loss_label() -> None:
    """Why the removed feature was not a borderline call.

    ``r_multiple / mfe_r`` under a ``mfe_r > 0`` guard has the sign of ``r_multiple``
    identically — not usually, not on average. Clustering on it separates winners from
    losers by construction, before a single distance is computed.
    """
    for r_multiple, mfe_r in (
        (Decimal("2.1"), Decimal("2.6")),
        (Decimal("-0.8"), Decimal("0.4")),
        (Decimal("-1.0"), Decimal("0.1")),
        (Decimal("0.5"), Decimal("3.0")),
    ):
        efficiency = r_multiple / mfe_r
        assert (efficiency > 0) == (r_multiple > 0)


class TestTheFeatureSetAsAWhole:
    """The per-feature probe above is necessary and not sufficient.

    It varies one trade's outcome and requires each feature to hold still. ``mae_r`` and
    ``mfe_r`` pass it honestly — they are properties of the price path, and changing where
    a trade was exited does not change where it travelled. Yet clustering on them
    reconstructed the win/loss label at 92% accuracy on a realistic 1,403-trade history,
    because ``mae_r <= r_multiple <= mfe_r`` holds by definition: neither feature *is* the
    outcome, but together they bracket it, and two bounds locate a value.

    No test of one feature at a time can see that. This probe asks the question the engine
    actually depends on — **can the clusters be used to tell winners from losers?** — of
    the whole feature space at once.
    """

    #: Sized from a measured sweep, not taste, and the sweep is the only thing that can
    #: justify it — a single run cannot tell a reliable probe from a lucky one.
    #:
    #: Detection of the leaking feature set, across data seeds:
    #:
    #:   200 trades -> 2 of 6      250 trades -> 4 of 6      300 trades -> 12 of 12
    #:
    #: A probe that misses the defect two-thirds of the time certifies the property it
    #: stopped checking, which is worse than not having it. At 300 the leaking set scored
    #: 88-92% while the clean set never exceeded 52.3%, so the 70% threshold sits in a
    #: wide gap rather than between two touching distributions.
    #:
    #: Reducing this is not a free saving. It was 600, which cost 40 seconds and made
    #: this file the most expensive in the suite; 300 costs 9 and detects just as well.
    #: Below 300 the saving comes out of the probe's power, which is the one thing it
    #: has.
    TRADES = 300

    #: Ten null references at alpha 0.10 rather than the default nineteen at 0.05. Fewer
    #: references make the structure test *laxer* — clusters are admitted more readily —
    #: which is the conservative direction for both assertions here: the clean set is
    #: handed structure more easily and still fails to encode the outcome, and the
    #: leaking set is still detected 8 times out of 8. Halves the cost.
    REFERENCES = 10
    ALPHA = Decimal("0.10")

    def history(self, rng: random.Random, count: int = TRADES) -> list[TradeRecord]:
        """Trades whose outcome is unrelated to when, how big, or how long.

        Built so the honest answer is "these clusters say nothing about P&L": hour, size,
        duration and direction are drawn independently of the result. Excursions follow
        the identities every real trade obeys. Any classifier accuracy much above chance
        therefore comes from the feature space, not from the data.
        """
        trades = []
        for _ in range(count):
            win = rng.random() < 0.5
            r = Decimal(str(round(rng.uniform(0.4, 2.5) if win else -rng.uniform(0.5, 1.2), 2)))
            if win:
                mfe = r + Decimal(str(round(rng.uniform(0.05, 0.60), 2)))
                mae = Decimal(str(round(-rng.uniform(0.10, 0.95), 2)))
            else:
                mfe = Decimal(str(round(rng.uniform(0.0, 0.9), 2)))
                mae = r - Decimal(str(round(rng.uniform(0.0, 0.20), 2)))
            trades.append(
                TradeRecord(
                    trade_id=uuid7(),
                    account_id=uuid7(),
                    opened_at=datetime(2026, 3, 2, tzinfo=UTC),
                    closed_at=datetime(2026, 3, 2, 1, tzinfo=UTC),
                    direction=Direction.LONG if rng.random() < 0.5 else Direction.SHORT,
                    net_pnl=r * Decimal(500),
                    gross_pnl=r * Decimal(500) + Decimal("2.49"),
                    r_multiple=r,
                    duration_seconds=rng.randint(60, 3600),
                    entry_hour=rng.randint(9, 15),
                    entry_weekday=rng.randint(1, 5),
                    quantity=Decimal(rng.randint(1, 3)),
                    mae_r=mae,
                    mfe_r=mfe,
                )
            )
        return trades

    def separation(
        self, trades: list[TradeRecord], features: tuple[Feature, ...]
    ) -> float:
        """Best accuracy achievable by reading a cluster as a win/loss prediction."""
        matrix = build_matrix(trades, features=features)
        clustering = choose_clustering(
            matrix.rows,
            seed=20260731,
            references=self.REFERENCES,
            alpha=self.ALPHA,
            # Two groups is where this leak expresses itself — winners against losers —
            # and searching further k costs time without making the probe sharper.
            max_k=2,
        )
        if clustering is None:
            return 0.5  # No structure found says nothing about the outcome.

        best = 0.5
        for index in range(clustering.k):
            members = trades_in_cluster(clustering, matrix.trades, index)
            others = [
                trade
                for position, trade in enumerate(matrix.trades)
                if clustering.labels[position] != index
            ]
            if not members or not others:
                continue
            as_winners = sum(1 for t in members if t.net_pnl > 0) + sum(
                1 for t in others if t.net_pnl <= 0
            )
            as_losers = sum(1 for t in members if t.net_pnl <= 0) + sum(
                1 for t in others if t.net_pnl > 0
            )
            best = max(best, max(as_winners, as_losers) / len(trades))
        return best

    def test_clusters_do_not_reconstruct_the_win_loss_label(self) -> None:
        """**The test this class exists for.**

        Outcome is independent of every honest feature in this history, so a cluster
        should be no better at predicting win from loss than a coin. With ``mae_r`` and
        ``mfe_r`` in the feature space this reached 0.92; without them it sits at chance.

        The threshold is loose on purpose. It is not measuring clustering quality — it is
        drawing a line between "these groups happen to differ a little" and "these groups
        *are* the outcome", and only the second is a defect.
        """
        accuracy = self.separation(self.history(random.Random(20260731)), FEATURES)

        assert accuracy < 0.70, (
            f"clusters predict win/loss at {accuracy:.1%} on a history where outcome is "
            "independent of every feature — the feature space is encoding the result"
        )

    def test_the_probe_still_catches_the_excursion_pair(self) -> None:
        """Guards the guard: the test above passes trivially if the probe is dead.

        A clustering that finds nothing scores 0.5 and clears the 70% threshold with room
        to spare, so a fixture that has lost its power stays green while proving nothing.
        The removed pair is reconstructed here and the probe is required to reject it.

        **What this catches and what it does not.** Measured by shrinking `TRADES`: it
        fails at 150 and below, where the probe cannot see a 92%-accurate leak at all. It
        does *not* fail at 200, because this particular seed is one of the minority where
        200 still detects — across six seeds, 200 detected only twice. So this is a check
        against the probe being dead, not a proof that it is reliable; the sizing note on
        `TRADES` is where the reliability evidence lives, and that came from a sweep no
        single-seed test can reproduce cheaply.
        """
        with_excursions = (
            *FEATURES,
            Feature(
                name="mae_r",
                description="maximum adverse excursion in R",
                extract=lambda trade: trade.mae_r,
                higher_is="went less far against you",
                lower_is="went deep underwater before resolving",
            ),
            Feature(
                name="mfe_r",
                description="maximum favourable excursion in R",
                extract=lambda trade: trade.mfe_r,
                higher_is="offered a large move in your favour",
                lower_is="never moved far in your favour",
            ),
        )

        accuracy = self.separation(self.history(random.Random(20260731)), with_excursions)

        assert accuracy >= 0.70, (
            f"the excursion pair only reached {accuracy:.1%} — this fixture can no "
            "longer detect a leak it is supposed to catch, so the test beside it proves "
            "nothing"
        )

    def test_the_excursion_pair_is_what_would_break_it(self) -> None:
        """Names the specific combination, so a future re-add fails loudly and legibly.

        Without this, someone re-adding `mfe_r` sees only that a generic separation test
        went red, with no indication of which dimension did it or why a feature that
        passes the per-feature probe could be the cause.
        """
        assert {"mae_r", "mfe_r"}.isdisjoint({feature.name for feature in FEATURES}), (
            "mae_r and mfe_r bracket the outcome (mae_r <= r <= mfe_r), so clustering on "
            "them partitions the sample into winners and losers. See EXCLUDED_FROM_CLUSTERING."
        )
