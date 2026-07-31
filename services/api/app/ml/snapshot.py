"""What was knowable at the moment of entry — and nothing else.

This module exists to make target leakage *structurally impossible* rather than merely
discouraged.

A predictive model is trained on features and asked to guess an outcome. If any feature
encodes the outcome, the model appears excellent and is worthless: it has learned to read
the answer. In trading data this is not a hypothetical risk, it is the default one.
:class:`~app.analytics.types.TradeRecord` carries ``net_pnl``, ``r_multiple``, ``mfe_r``,
``mae_r``, ``closed_at`` and ``duration_seconds`` right beside the entry fields, and every
one of them is known only after the trade is over. The clustering feature set in
:mod:`app.analytics.features` uses several of them deliberately — clustering is
descriptive, so "how much of the move did this trade capture" is a legitimate
*description*. Reusing that feature set to *predict* the result would be predicting the
outcome from the outcome, and it would produce a model with a 96% hit rate that loses
money on the first live trade.

The defence is not a naming convention or a code review checklist. It is this dataclass.
:class:`EntrySnapshot` has no outcome fields, so a feature extractor written against it
cannot reach one — not by accident, not by a well-meaning refactor, and not by whoever
adds the ninth feature two years from now. ``build_snapshots`` is the only bridge from a
trade to a snapshot, it is the only place in the package that touches both, and it is
tested field-by-field against the record it came from.

Sequence context follows the same rule as the compliance engine: each snapshot sees the
session *as it was before its own trade was taken*. The first trade of a day sees zero
prior trades and a flat session P&L, never the day's eventual total. Getting this
backwards is the second-most-common leak in trading models and the harder one to spot,
because the resulting feature looks entirely innocent.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from app.domain.common.enums import Direction, SessionSegment

if TYPE_CHECKING:  # pragma: no cover - import cycle guard only
    from app.analytics.types import TradeRecord

#: Fields on ``TradeRecord`` that describe how a trade *finished*. Named here so the
#: leakage test can assert that none of them appears on ``EntrySnapshot``. The test reads
#: this tuple rather than a hand-written list, so adding an outcome field to the record
#: and then mirroring it onto the snapshot fails the build.
OUTCOME_FIELDS = (
    "net_pnl",
    "gross_pnl",
    "commission",
    "fees",
    "r_multiple",
    "mae_r",
    "mfe_r",
    "closed_at",
    "duration_seconds",
)


@dataclass(frozen=True, slots=True)
class EntrySnapshot:
    """Everything a model may look at, as of the instant the trade was opened.

    Deliberately missing every field in :data:`OUTCOME_FIELDS`. If a feature needs one of
    them, the feature is not predictive and does not belong in this package.

    Attributes:
        trades_before_today: How many trades the trader had already taken in this
            session. The trader knew this; the model may too.
        session_pnl_before: Realized session P&L *before* this trade. Signed, and
            ``Decimal(0)`` for the first trade of a session — which is a fact, not a
            missing value.
        consecutive_losses_before: Losing streak carried into this trade.
        minutes_since_last_trade: Gap since the previous entry in the same session, or
            ``None`` for the first trade — genuinely undefined, never zero. Zero would
            claim the trader re-entered instantly.
        prior_trades_total: Trades in the trader's whole history before this one. The
            walk-forward splitter uses it; features may too, as an experience proxy.
    """

    trade_id: UUID
    account_id: UUID
    opened_at: datetime
    direction: Direction
    quantity: Decimal

    session_date: date | None = None
    session_segment: SessionSegment | None = None
    entry_hour: int | None = None
    entry_weekday: int | None = None
    instrument_symbol: str | None = None
    instrument_root: str | None = None
    strategy: str | None = None
    setup: str | None = None
    market_condition: str | None = None

    # --- Sequence context, as of *before* this trade -------------------------------
    trades_before_today: int = 0
    session_pnl_before: Decimal = Decimal(0)
    consecutive_losses_before: int = 0
    consecutive_wins_before: int = 0
    minutes_since_last_trade: Decimal | None = None
    prior_trades_total: int = 0

    @property
    def is_first_of_session(self) -> bool:
        return self.trades_before_today == 0

    @property
    def is_down_on_session(self) -> bool:
        return self.session_pnl_before < 0


@dataclass(frozen=True, slots=True)
class LabelledSnapshot:
    """A snapshot paired with what actually happened to it.

    The two are kept in *separate* objects joined at the last moment, so that training
    code handles ``snapshot`` and evaluation code handles ``outcome``, and neither can
    reach across by writing ``x.net_pnl``. The pairing is a tuple in all but name; making
    it a class is what lets the type checker keep the halves apart.

    Attributes:
        r_multiple: ``None`` when the trade had no recorded stop. The expected-R model
            skips those rather than imputing, and reports how many it skipped.
    """

    snapshot: EntrySnapshot
    won: bool
    net_pnl: Decimal
    r_multiple: Decimal | None = None

    @property
    def has_r(self) -> bool:
        return self.r_multiple is not None


def build_snapshots(trades: list[TradeRecord]) -> list[LabelledSnapshot]:
    """The single bridge from trade records to snapshots.

    Walks the history in chronological order, accumulating per-session state, and hands
    each trade the state as it stood *before* that trade was taken.

    Scratches (exactly break-even after costs) count as losses for the ``won`` label and
    break a winning streak. That is a modelling choice worth stating: a scratch is not a
    win, and a probability model needs a binary label. It is recorded as its own outcome
    everywhere else in the engine, so nothing is lost — only this one label is coarse.
    """
    ordered = sorted(trades, key=lambda trade: (trade.opened_at, str(trade.trade_id)))

    per_session_count: dict[date | None, int] = defaultdict(int)
    per_session_pnl: dict[date | None, Decimal] = defaultdict(Decimal)
    last_entry: dict[date | None, datetime] = {}
    losing_streak = 0
    winning_streak = 0

    labelled: list[LabelledSnapshot] = []
    for index, trade in enumerate(ordered):
        session = trade.session_date
        previous = last_entry.get(session)
        gap: Decimal | None = None
        if previous is not None:
            gap = Decimal((trade.opened_at - previous).total_seconds()) / Decimal(60)

        snapshot = EntrySnapshot(
            trade_id=trade.trade_id,
            account_id=trade.account_id,
            opened_at=trade.opened_at,
            direction=trade.direction,
            quantity=trade.quantity,
            session_date=session,
            session_segment=trade.session_segment,
            entry_hour=trade.entry_hour,
            entry_weekday=trade.entry_weekday,
            instrument_symbol=trade.instrument_symbol,
            instrument_root=trade.instrument_root,
            strategy=trade.strategy,
            setup=trade.setup,
            market_condition=trade.market_condition,
            trades_before_today=per_session_count[session],
            session_pnl_before=per_session_pnl[session],
            consecutive_losses_before=losing_streak,
            consecutive_wins_before=winning_streak,
            minutes_since_last_trade=gap,
            prior_trades_total=index,
        )
        labelled.append(
            LabelledSnapshot(
                snapshot=snapshot,
                won=trade.net_pnl > 0,
                net_pnl=trade.net_pnl,
                r_multiple=trade.r_multiple,
            )
        )

        per_session_count[session] += 1
        per_session_pnl[session] += trade.net_pnl
        last_entry[session] = trade.opened_at
        if trade.net_pnl > 0:
            winning_streak += 1
            losing_streak = 0
        else:
            losing_streak += 1
            winning_streak = 0

    return labelled
