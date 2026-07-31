"""The leakage barrier, and the sequence context that is the second way to breach it."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.analytics.types import TradeRecord
from app.domain.common.enums import Direction
from app.ml.features import FEATURES
from app.ml.snapshot import OUTCOME_FIELDS, EntrySnapshot, build_snapshots

BASE = datetime(2025, 3, 3, 14, 30, tzinfo=UTC)
ACCOUNT = uuid4()


def trade(
    *,
    offset_days: int = 0,
    offset_minutes: int = 0,
    pnl: str = "100",
    r: str | None = "1.0",
    direction: Direction = Direction.LONG,
) -> TradeRecord:
    opened = BASE + timedelta(days=offset_days, minutes=offset_minutes)
    return TradeRecord(
        trade_id=uuid4(),
        account_id=ACCOUNT,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=4),
        direction=direction,
        net_pnl=Decimal(pnl),
        r_multiple=Decimal(r) if r is not None else None,
        quantity=Decimal(2),
        session_date=opened.date(),
        entry_hour=opened.hour,
        entry_weekday=opened.isoweekday(),
        duration_seconds=240,
        mae_r=Decimal("-0.4"),
        mfe_r=Decimal("1.8"),
    )


class TestTheLeakageBarrier:
    """``EntrySnapshot`` is the defence, so its shape is asserted rather than trusted."""

    @pytest.mark.parametrize("field_name", OUTCOME_FIELDS)
    def test_no_outcome_field_reaches_the_snapshot(self, field_name: str) -> None:
        """The whole design rests on this.

        Parametrised over ``OUTCOME_FIELDS`` rather than a hand-written list so that
        adding an outcome field to ``TradeRecord`` and then mirroring it onto the
        snapshot fails here instead of silently producing a model that reads the answer.
        """
        assert field_name not in {f.name for f in dataclasses.fields(EntrySnapshot)}

    def test_outcome_fields_actually_exist_on_the_trade_record(self) -> None:
        """Guards the guard.

        If a field is renamed on ``TradeRecord`` and ``OUTCOME_FIELDS`` is not updated,
        the test above starts asserting the absence of a field nobody has — it passes
        while checking nothing.
        """
        available = {f.name for f in dataclasses.fields(TradeRecord)}
        assert set(OUTCOME_FIELDS) <= available

    def test_no_predictive_feature_can_reach_an_outcome(self) -> None:
        """Every extractor takes a snapshot, so a feature cannot see a result.

        Asserted by construction: passing a snapshot to every extractor must succeed,
        which it only can if none of them reaches for a field the snapshot lacks.
        """
        snapshot = build_snapshots([trade()])[0].snapshot
        for feature in FEATURES:
            feature.extract(snapshot)  # must not raise


class TestSequenceContext:
    """Each snapshot sees the session as it was *before* its own trade."""

    def test_the_first_trade_of_a_session_sees_nothing(self) -> None:
        samples = build_snapshots([trade(pnl="500"), trade(offset_minutes=30, pnl="-200")])
        first = samples[0].snapshot
        assert first.trades_before_today == 0
        assert first.session_pnl_before == Decimal(0)
        assert first.minutes_since_last_trade is None

    def test_session_pnl_excludes_the_trade_itself(self) -> None:
        """The subtle leak: a trade must not see its own result in the running total.

        If it did, ``session_pnl_before`` would carry the outcome directly and the model
        would appear to predict beautifully while having learned nothing.
        """
        samples = build_snapshots(
            [trade(pnl="500"), trade(offset_minutes=30, pnl="-200"), trade(offset_minutes=60)]
        )
        assert samples[0].snapshot.session_pnl_before == Decimal(0)
        assert samples[1].snapshot.session_pnl_before == Decimal(500)
        assert samples[2].snapshot.session_pnl_before == Decimal(300)

    def test_counters_reset_between_sessions(self) -> None:
        samples = build_snapshots(
            [
                trade(pnl="100"),
                trade(offset_minutes=30, pnl="100"),
                trade(offset_days=1, pnl="100"),
            ]
        )
        assert samples[1].snapshot.trades_before_today == 1
        assert samples[2].snapshot.trades_before_today == 0
        assert samples[2].snapshot.session_pnl_before == Decimal(0)

    def test_losing_streak_carries_across_sessions(self) -> None:
        """Streaks are a property of the trader, not of the calendar.

        Unlike the session counters, a losing streak does not reset at midnight — a
        trader arriving after three losses yesterday is in that state today.
        """
        samples = build_snapshots(
            [
                trade(pnl="-100"),
                trade(offset_minutes=30, pnl="-100"),
                trade(offset_days=1, pnl="-100"),
            ]
        )
        assert samples[0].snapshot.consecutive_losses_before == 0
        assert samples[1].snapshot.consecutive_losses_before == 1
        assert samples[2].snapshot.consecutive_losses_before == 2

    def test_a_win_clears_the_losing_streak(self) -> None:
        samples = build_snapshots(
            [
                trade(pnl="-100"),
                trade(offset_minutes=30, pnl="-100"),
                trade(offset_minutes=60, pnl="400"),
                trade(offset_minutes=90, pnl="-100"),
            ]
        )
        assert samples[2].snapshot.consecutive_losses_before == 2
        assert samples[3].snapshot.consecutive_losses_before == 0
        assert samples[3].snapshot.consecutive_wins_before == 1

    def test_a_scratch_counts_as_a_loss_for_the_label(self) -> None:
        """A documented modelling choice, asserted so it cannot drift silently."""
        samples = build_snapshots([trade(pnl="0")])
        assert samples[0].won is False

    def test_gap_is_none_for_the_first_trade_not_zero(self) -> None:
        """Zero would claim the trader re-entered instantly."""
        samples = build_snapshots([trade(), trade(offset_minutes=45)])
        assert samples[0].snapshot.minutes_since_last_trade is None
        assert samples[1].snapshot.minutes_since_last_trade == Decimal(45)

    def test_input_order_does_not_matter(self) -> None:
        """Snapshots are built from chronological order, not from list order.

        A repository returning trades sorted by insertion, id, or anything else must
        produce identical sequence context.
        """
        trades = [trade(pnl="100"), trade(offset_minutes=30, pnl="-50"), trade(offset_minutes=60)]
        forward = build_snapshots(list(trades))
        reversed_input = build_snapshots(list(reversed(trades)))
        assert [s.snapshot.session_pnl_before for s in forward] == [
            s.snapshot.session_pnl_before for s in reversed_input
        ]

    def test_r_multiple_stays_none_rather_than_being_filled(self) -> None:
        samples = build_snapshots([trade(r=None)])
        assert samples[0].r_multiple is None
        assert samples[0].has_r is False
