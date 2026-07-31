"""Tests for per-trade risk arithmetic.

The recurring theme: quantities that are *undefined* must return ``None``, never a
placeholder. A zero R-multiple for a trade with no recorded stop would drag every
average toward zero and make the R distribution describe a strategy nobody traded.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import DomainError
from app.domain.common.enums import Direction
from app.domain.trading.risk import (
    compute_excursions,
    planned_reward_amount,
    planned_risk_amount,
    r_multiple,
    reward_risk_ratio,
    risk_per_contract,
)
from tests.conftest import ES


def test_risk_per_contract_long() -> None:
    assert risk_per_contract(Direction.LONG, Decimal("5000"), Decimal("4996")) == Decimal("4")


def test_risk_per_contract_short() -> None:
    assert risk_per_contract(Direction.SHORT, Decimal("5000"), Decimal("5004")) == Decimal("4")


def test_stop_on_the_wrong_side_is_not_risk() -> None:
    """A long stop above entry is a data error, not negative risk."""
    assert risk_per_contract(Direction.LONG, Decimal("5000"), Decimal("5004")) is None
    assert risk_per_contract(Direction.SHORT, Decimal("5000"), Decimal("4996")) is None


def test_stop_at_entry_is_not_risk() -> None:
    assert risk_per_contract(Direction.LONG, Decimal("5000"), Decimal("5000")) is None


def test_planned_risk_amount_converts_through_point_value() -> None:
    risk = planned_risk_amount(ES, Direction.LONG, Decimal("5000"), Decimal("4996"), Decimal(2))
    assert risk == Decimal("400.00000000")  # 4 pts * $50 * 2 contracts


def test_planned_reward_amount() -> None:
    reward = planned_reward_amount(ES, Direction.LONG, Decimal("5000"), Decimal("5008"), Decimal(2))
    assert reward == Decimal("800.00000000")


def test_target_behind_entry_is_not_reward() -> None:
    reward = planned_reward_amount(
        ES, Direction.LONG, Decimal("5000"), Decimal("4990"), Decimal(1)
    )
    assert reward is None


def test_reward_risk_ratio() -> None:
    assert reward_risk_ratio(Decimal("400"), Decimal("800")) == Decimal("2")
    assert reward_risk_ratio(None, Decimal("800")) is None
    assert reward_risk_ratio(Decimal("0"), Decimal("800")) is None


def test_r_multiple_uses_net_pnl() -> None:
    assert r_multiple(Decimal("800"), Decimal("400")) == Decimal("2")
    assert r_multiple(Decimal("-400"), Decimal("400")) == Decimal("-1")


def test_r_multiple_undefined_without_risk() -> None:
    assert r_multiple(Decimal("800"), None) is None
    assert r_multiple(Decimal("800"), Decimal("0")) is None


def test_excursions_for_a_long() -> None:
    result = compute_excursions(
        ES,
        Direction.LONG,
        entry_price=Decimal("5000"),
        quantity=Decimal(1),
        highest_price=Decimal("5010"),
        lowest_price=Decimal("4997"),
        risk_amount=Decimal("200"),
    )

    assert result.mae_points == Decimal("3")
    assert result.mfe_points == Decimal("10")
    assert result.mae_amount == Decimal("150.00000000")
    assert result.mfe_amount == Decimal("500.00000000")
    assert result.mae_r == Decimal("0.75")
    assert result.mfe_r == Decimal("2.5")
    assert result.edge_ratio is not None
    assert result.edge_ratio > 1


def test_excursions_for_a_short_invert() -> None:
    result = compute_excursions(
        ES,
        Direction.SHORT,
        entry_price=Decimal("5000"),
        quantity=Decimal(1),
        highest_price=Decimal("5006"),
        lowest_price=Decimal("4990"),
    )

    assert result.mae_price == Decimal("5006")
    assert result.mfe_price == Decimal("4990")
    assert result.mae_points == Decimal("6")
    assert result.mfe_points == Decimal("10")


def test_excursions_reject_a_window_that_excludes_entry() -> None:
    """If the entry price is outside the observed range, the bars are the wrong bars."""
    with pytest.raises(DomainError, match="within the observed price range"):
        compute_excursions(
            ES,
            Direction.LONG,
            entry_price=Decimal("5000"),
            quantity=Decimal(1),
            highest_price=Decimal("4999"),
            lowest_price=Decimal("4990"),
        )


def test_excursions_reject_inverted_extremes() -> None:
    with pytest.raises(DomainError, match="not be below"):
        compute_excursions(
            ES,
            Direction.LONG,
            entry_price=Decimal("5000"),
            quantity=Decimal(1),
            highest_price=Decimal("4990"),
            lowest_price=Decimal("5010"),
        )


def test_edge_ratio_undefined_when_no_adverse_excursion() -> None:
    """A trade that never traded against entry has no MAE to divide by."""
    result = compute_excursions(
        ES,
        Direction.LONG,
        entry_price=Decimal("5000"),
        quantity=Decimal(1),
        highest_price=Decimal("5010"),
        lowest_price=Decimal("5000"),
    )
    assert result.mae_points == Decimal("0")
    assert result.edge_ratio is None
