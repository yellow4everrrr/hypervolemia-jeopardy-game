"""Tests for FIFO trade reconstruction.

These are the most important tests in the repository. Each one pins down a case where
a plausible-looking implementation silently produces wrong P&L: scale-outs matched
LIFO instead of FIFO, commissions counted twice across a position flip, an execution
ordering that depends on dictionary iteration order, a flip that swallows the second
trade.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import DomainError
from app.domain.common.enums import Direction, ExecutionRole, Side, TradeStatus
from app.domain.trading.matching import PositionState, reconstruct_trades
from tests.conftest import ExecutionFactory


def test_simple_long_round_turn(fill: ExecutionFactory, specs: dict) -> None:
    """Buy 1 at 5000, sell 1 at 5004 → +4 points → +$200 on ES."""
    result = reconstruct_trades(
        [fill(Side.BUY, 1, "5000.00", minute=0), fill(Side.SELL, 1, "5004.00", minute=10)],
        specs,
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.direction is Direction.LONG
    assert trade.status is TradeStatus.CLOSED
    assert trade.avg_entry_price == Decimal("5000.00")
    assert trade.avg_exit_price == Decimal("5004.00")
    assert trade.points_per_contract == Decimal("4.00")
    assert trade.ticks_per_contract == Decimal("16")
    assert trade.gross_pnl == Decimal("200.00000000")
    assert trade.net_pnl == Decimal("200.00000000")
    assert trade.duration_seconds == 600
    assert result.closing_state == {}


def test_simple_short_round_turn(fill: ExecutionFactory, specs: dict) -> None:
    """A short profits when price falls — the sign must not be inherited from the fill."""
    result = reconstruct_trades(
        [fill(Side.SELL, 2, "5010.00"), fill(Side.BUY, 2, "5000.00", minute=5)],
        specs,
    )

    trade = result.trades[0]
    assert trade.direction is Direction.SHORT
    assert trade.points_per_contract == Decimal("10.00")
    assert trade.gross_pnl == Decimal("1000.00000000")  # 10 pts * $50 * 2 contracts


def test_losing_trade_has_negative_pnl(fill: ExecutionFactory, specs: dict) -> None:
    result = reconstruct_trades(
        [fill(Side.BUY, 1, "5000.00"), fill(Side.SELL, 1, "4997.75", minute=1)],
        specs,
    )
    trade = result.trades[0]
    assert trade.gross_pnl == Decimal("-112.50000000")  # -2.25 pts * $50
    assert not trade.is_winner


def test_scale_in_uses_weighted_average_entry(fill: ExecutionFactory, specs: dict) -> None:
    """Adding to a position must weight by size, not average the prices naively."""
    result = reconstruct_trades(
        [
            fill(Side.BUY, 1, "5000.00", minute=0),
            fill(Side.BUY, 3, "5004.00", minute=1),
            fill(Side.SELL, 4, "5010.00", minute=5),
        ],
        specs,
    )

    trade = result.trades[0]
    # (1*5000 + 3*5004) / 4 = 5003, not (5000+5004)/2 = 5002.
    assert trade.avg_entry_price == Decimal("5003.00")
    assert trade.quantity_opened == Decimal(4)
    assert trade.is_scaled_in
    assert trade.gross_pnl == Decimal("1400.00000000")  # 7 pts * $50 * 4


def test_scale_out_matches_fifo_not_lifo(fill: ExecutionFactory, specs: dict) -> None:
    """The first contract on is the first contract off.

    LIFO would match the 5002 lot against the first exit and produce a different
    per-leg split — same total here, but a different holding period and a different
    realized-points attribution per leg, which feeds MAE/MFE and duration statistics.
    """
    result = reconstruct_trades(
        [
            fill(Side.BUY, 1, "5000.00", minute=0),
            fill(Side.BUY, 1, "5002.00", minute=1),
            fill(Side.SELL, 1, "5006.00", minute=5),
            fill(Side.SELL, 1, "5008.00", minute=9),
        ],
        specs,
    )

    trade = result.trades[0]
    exits = trade.exit_legs
    assert len(exits) == 2
    # First exit closes the 5000 lot: +6 points. Second closes 5002: +6 points.
    assert exits[0].realized_points == Decimal("6.00")
    assert exits[1].realized_points == Decimal("6.00")
    assert trade.realized_points == Decimal("12.00")
    assert trade.is_scaled_out


def test_partial_exit_leaves_trade_open(fill: ExecutionFactory, specs: dict) -> None:
    result = reconstruct_trades(
        [fill(Side.BUY, 3, "5000.00"), fill(Side.SELL, 1, "5004.00", minute=2)],
        specs,
    )

    trade = result.trades[0]
    assert trade.status is TradeStatus.OPEN
    assert trade.closed_at is None
    assert trade.quantity_open == Decimal(2)
    # Realized P&L accrues while the position is still open.
    assert trade.gross_pnl == Decimal("200.00000000")
    assert len(result.closing_state) == 1


def test_position_flip_creates_two_trades(fill: ExecutionFactory, specs: dict) -> None:
    """Selling 5 while long 2 closes the long and opens a 3-lot short."""
    result = reconstruct_trades(
        [fill(Side.BUY, 2, "5000.00", minute=0), fill(Side.SELL, 5, "5004.00", minute=3)],
        specs,
    )

    assert len(result.trades) == 2
    closed, opened = result.trades

    assert closed.direction is Direction.LONG
    assert closed.status is TradeStatus.CLOSED
    assert closed.quantity_closed == Decimal(2)
    assert closed.gross_pnl == Decimal("400.00000000")  # 4 pts * $50 * 2

    assert opened.direction is Direction.SHORT
    assert opened.status is TradeStatus.OPEN
    assert opened.quantity_opened == Decimal(3)
    assert opened.avg_entry_price == Decimal("5004.00")


def test_flip_allocates_commission_pro_rata(fill: ExecutionFactory, specs: dict) -> None:
    """A fill spanning two trades must not have its costs counted twice.

    The 5-lot sell carries $10 of commission. Two contracts close the long and three
    open the short, so the split is $4/$6 — and the total across both trades is exactly
    what the broker charged.
    """
    result = reconstruct_trades(
        [
            fill(Side.BUY, 2, "5000.00", minute=0, commission="4.00"),
            fill(Side.SELL, 5, "5004.00", minute=3, commission="10.00"),
        ],
        specs,
    )

    closed, opened = result.trades
    assert closed.commission == Decimal("8.00000000")  # 4.00 entry + 4.00 of the exit
    assert opened.commission == Decimal("6.00000000")
    # $4 on the entry plus $10 on the exit, distributed without loss or duplication.
    assert closed.commission + opened.commission == Decimal("14.00000000")


def test_commissions_and_fees_reduce_net_pnl(fill: ExecutionFactory, specs: dict) -> None:
    result = reconstruct_trades(
        [
            fill(Side.BUY, 1, "5000.00", commission="1.29", fees="0.37"),
            fill(Side.SELL, 1, "5001.00", minute=1, commission="1.29", fees="0.37"),
        ],
        specs,
    )

    trade = result.trades[0]
    assert trade.gross_pnl == Decimal("50.00000000")
    assert trade.commission == Decimal("2.58000000")
    assert trade.fees == Decimal("0.74000000")
    assert trade.net_pnl == Decimal("46.68000000")


def test_costs_can_turn_a_gross_winner_into_a_net_loser(
    fill: ExecutionFactory, specs: dict
) -> None:
    """Scalping one tick on MES loses money after commissions — the journal must say so."""
    result = reconstruct_trades(
        [
            fill(Side.BUY, 1, "5000.00", symbol="MESZ5", commission="0.74", fees="0.37"),
            fill(Side.SELL, 1, "5000.25", minute=1, symbol="MESZ5", commission="0.74", fees="0.37"),
        ],
        specs,
    )

    trade = result.trades[0]
    assert trade.gross_pnl == Decimal("1.25000000")
    assert trade.net_pnl == Decimal("-0.97000000")
    assert not trade.is_winner


def test_separate_accounts_do_not_net(fill: ExecutionFactory, specs: dict) -> None:
    """A long in one account and a short in another are two positions, not a flat book."""
    result = reconstruct_trades(
        [
            fill(Side.BUY, 1, "5000.00", account="acct-1"),
            fill(Side.SELL, 1, "5000.00", account="acct-2", minute=1),
        ],
        specs,
    )

    assert len(result.trades) == 2
    assert {trade.direction for trade in result.trades} == {Direction.LONG, Direction.SHORT}
    assert all(trade.status is TradeStatus.OPEN for trade in result.trades)


def test_separate_instruments_do_not_net(fill: ExecutionFactory, specs: dict) -> None:
    result = reconstruct_trades(
        [
            fill(Side.BUY, 1, "5000.00", symbol="ESZ5"),
            fill(Side.SELL, 1, "5000.00", symbol="MESZ5", minute=1),
        ],
        specs,
    )
    assert len(result.trades) == 2


def test_reconstruction_is_order_independent(fill: ExecutionFactory, specs: dict) -> None:
    """Shuffled input must produce identical trades — brokers paginate out of order."""
    executions = [
        fill(Side.BUY, 1, "5000.00", minute=0),
        fill(Side.BUY, 1, "5002.00", minute=1),
        fill(Side.SELL, 2, "5006.00", minute=5),
    ]
    forward = reconstruct_trades(executions, specs)
    reversed_order = reconstruct_trades(list(reversed(executions)), specs)

    assert forward.trades == reversed_order.trades


def test_same_timestamp_ties_broken_by_sequence(specs: dict) -> None:
    """Two fills in the same millisecond resolve by the broker's sequence number."""
    factory = ExecutionFactory()
    first = factory(Side.BUY, 1, "5000.00", minute=0, sequence=1)
    second = factory(Side.SELL, 1, "5005.00", minute=0, sequence=2)

    result = reconstruct_trades([second, first], specs)

    assert len(result.trades) == 1
    assert result.trades[0].direction is Direction.LONG
    assert result.trades[0].status is TradeStatus.CLOSED


def test_unknown_instrument_is_refused(fill: ExecutionFactory) -> None:
    """Guessing a point value would corrupt P&L by a constant factor. Refuse instead."""
    with pytest.raises(DomainError, match="no instrument specification"):
        reconstruct_trades([fill(Side.BUY, 1, "5000.00")], {})


def test_duplicate_execution_ids_are_rejected(fill: ExecutionFactory, specs: dict) -> None:
    duplicate = fill(Side.BUY, 1, "5000.00", external_id="dup")
    with pytest.raises(DomainError, match="duplicate execution"):
        reconstruct_trades([duplicate, duplicate], specs)


def test_negative_quantity_is_rejected(fill: ExecutionFactory) -> None:
    with pytest.raises(DomainError, match="quantity must be positive"):
        fill(Side.BUY, -1, "5000.00")


def test_naive_timestamp_is_rejected(specs: dict) -> None:
    from datetime import datetime

    from app.domain.trading.execution import Execution

    with pytest.raises(DomainError, match="timezone-aware"):
        Execution(
            external_id="x",
            account_key="a",
            instrument_symbol="ESZ5",
            side=Side.BUY,
            quantity=Decimal(1),
            price=Decimal("5000"),
            executed_at=datetime(2026, 3, 5, 14, 0),  # noqa: DTZ001 — the point of the test
        )


# --- Resumability ---------------------------------------------------------------


def test_state_resumes_an_open_position(fill: ExecutionFactory, specs: dict) -> None:
    """An incremental sync must continue the open trade, not start a second one."""
    first_batch = reconstruct_trades([fill(Side.BUY, 2, "5000.00", minute=0)], specs)
    assert first_batch.trades[0].status is TradeStatus.OPEN

    second_batch = reconstruct_trades(
        [fill(Side.SELL, 2, "5006.00", minute=10)],
        specs,
        opening_state=first_batch.closing_state,
    )

    assert len(second_batch.trades) == 1
    trade = second_batch.trades[0]
    assert trade.status is TradeStatus.CLOSED
    assert trade.quantity_opened == Decimal(2)
    assert trade.gross_pnl == Decimal("600.00000000")  # 6 pts * $50 * 2
    assert second_batch.closing_state == {}


def test_incremental_matches_full_rebuild(fill: ExecutionFactory, specs: dict) -> None:
    """Processing fills one at a time must equal processing them all at once.

    This is the property that makes incremental sync safe. If it ever fails, the fix is
    a full rebuild, not a patch — hence the explicit test.
    """
    executions = [
        fill(Side.BUY, 1, "5000.00", minute=0),
        fill(Side.BUY, 2, "5002.00", minute=1),
        fill(Side.SELL, 1, "5008.00", minute=4),
        fill(Side.SELL, 4, "5010.00", minute=6),  # closes 2 remaining, opens short 2
        fill(Side.BUY, 2, "5007.00", minute=9),  # closes the short
    ]

    full = reconstruct_trades(executions, specs)

    state: dict = {}
    incremental = []
    for execution in executions:
        step = reconstruct_trades([execution], specs, opening_state=state)
        state = dict(step.closing_state)
        incremental.extend(step.closed_trades)

    assert [t.net_pnl for t in incremental] == [t.net_pnl for t in full.closed_trades]
    assert [t.direction for t in incremental] == [t.direction for t in full.closed_trades]
    assert [t.legs for t in incremental] == [t.legs for t in full.closed_trades]


def test_untouched_open_position_is_still_reported(fill: ExecutionFactory, specs: dict) -> None:
    """A position that saw no new fills must survive the run, not vanish from state."""
    opening = reconstruct_trades([fill(Side.BUY, 1, "5000.00")], specs)

    quiet_run = reconstruct_trades([], specs, opening_state=opening.closing_state)

    assert len(quiet_run.open_trades) == 1
    assert quiet_run.closing_state.keys() == opening.closing_state.keys()


def test_position_state_reports_signed_quantity(fill: ExecutionFactory, specs: dict) -> None:
    long_state = reconstruct_trades([fill(Side.BUY, 3, "5000.00")], specs).closing_state
    short_state = reconstruct_trades([fill(Side.SELL, 3, "5000.00")], specs).closing_state

    assert next(iter(long_state.values())).signed_quantity == Decimal(3)
    assert next(iter(short_state.values())).signed_quantity == Decimal(-3)
    assert PositionState().is_flat


# --- Leg bookkeeping ------------------------------------------------------------


def test_legs_record_roles_and_origin(fill: ExecutionFactory, specs: dict) -> None:
    result = reconstruct_trades(
        [
            fill(Side.BUY, 1, "5000.00", external_id="open-me"),
            fill(Side.SELL, 1, "5004.00", minute=1, external_id="close-me"),
        ],
        specs,
    )

    trade = result.trades[0]
    assert trade.origin_execution_id == "open-me"
    assert [leg.role for leg in trade.legs] == [ExecutionRole.ENTRY, ExecutionRole.EXIT]
    assert trade.entry_legs[0].execution_external_id == "open-me"
    assert trade.exit_legs[0].execution_external_id == "close-me"


def test_max_position_size_tracks_the_peak(fill: ExecutionFactory, specs: dict) -> None:
    """Risk analytics size against peak exposure, not the closing quantity."""
    result = reconstruct_trades(
        [
            fill(Side.BUY, 2, "5000.00", minute=0),
            fill(Side.BUY, 3, "5001.00", minute=1),
            fill(Side.SELL, 4, "5003.00", minute=2),
            fill(Side.SELL, 1, "5004.00", minute=3),
        ],
        specs,
    )

    assert result.trades[0].max_position_size == Decimal(5)


def test_signed_quantity_at_moment(fill: ExecutionFactory, specs: dict) -> None:
    executions = [
        fill(Side.BUY, 2, "5000.00", minute=0),
        fill(Side.SELL, 1, "5004.00", minute=5),
        fill(Side.SELL, 1, "5006.00", minute=9),
    ]
    trade = reconstruct_trades(executions, specs).trades[0]

    assert trade.signed_quantity_at(executions[0].executed_at) == Decimal(2)
    assert trade.signed_quantity_at(executions[1].executed_at) == Decimal(1)
    assert trade.signed_quantity_at(executions[2].executed_at) == Decimal(0)


def test_multi_contract_instrument_scaling(fill: ExecutionFactory, specs: dict) -> None:
    """CL: $10 a tick at 0.01 tick size means $1,000 a point. Off-by-a-factor bait."""
    result = reconstruct_trades(
        [
            fill(Side.BUY, 1, "70.00", symbol="CLZ5"),
            fill(Side.SELL, 1, "70.50", minute=5, symbol="CLZ5"),
        ],
        specs,
    )

    trade = result.trades[0]
    assert trade.points_per_contract == Decimal("0.50")
    assert trade.ticks_per_contract == Decimal("50")
    assert trade.gross_pnl == Decimal("500.00000000")
