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

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.analytics.features import FEATURES, Feature
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
