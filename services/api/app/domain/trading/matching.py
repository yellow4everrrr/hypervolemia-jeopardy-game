"""Execution → trade reconstruction (FIFO).

This module is the most consequential code in the product. If fills are grouped into
trades incorrectly, every downstream number — expectancy, win rate, R distribution,
every AI claim — is confidently wrong, and the error is nearly undetectable by
inspection. It is therefore a pure function of its inputs: no database, no clock, no
network, no configuration. The same fills always produce the same trades.

**Accounting convention: FIFO.** Closing fills are matched against the oldest open lot
first. This matches CME/NFA reporting for futures and, more importantly, it is the
convention that makes scale-ins interpretable: the first contract on is the first
contract off, so "how long did I hold this trade" has a defined answer.

**Resumability.** Reconstruction accepts and returns :class:`PositionState`, so an
incremental sync can process only the new fills for a day rather than replaying a
user's entire history. State is small (open lots plus the in-flight trade) and is
persisted by the sync pipeline in milestone 2.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from app.core.errors import DomainError
from app.core.money import ZERO
from app.domain.common.enums import Direction, ExecutionRole, Side, TradeStatus
from app.domain.trading.execution import Execution
from app.domain.trading.instrument import InstrumentSpec
from app.domain.trading.trade import ReconstructedTrade, TradeLeg

#: A position is identified by (account, instrument). Fills never net across either.
PositionKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class OpenLot:
    """An unmatched parcel of an entry fill."""

    quantity: Decimal
    price: Decimal
    execution_external_id: str


@dataclass(frozen=True, slots=True)
class PositionState:
    """Everything needed to resume reconstruction mid-position.

    ``lots`` and ``open_trade`` are always consistent: the summed lot quantity equals
    the open trade's unclosed quantity, and both are empty/None when flat.
    """

    lots: tuple[OpenLot, ...] = ()
    open_trade: ReconstructedTrade | None = None

    @property
    def is_flat(self) -> bool:
        return not self.lots

    @property
    def signed_quantity(self) -> Decimal:
        if self.open_trade is None:
            return ZERO
        total = sum((lot.quantity for lot in self.lots), start=ZERO)
        return total * self.open_trade.direction.signed_multiplier


@dataclass(frozen=True, slots=True)
class ReconstructionResult:
    """Trades produced by a run, plus the state needed to continue it.

    ``trades`` contains both closed round turns and any still-open trade, so callers
    can persist a live position without waiting for it to close. ``closing_state`` maps
    each position key to its state after the last fill.
    """

    trades: tuple[ReconstructedTrade, ...]
    closing_state: Mapping[PositionKey, PositionState]

    @property
    def closed_trades(self) -> tuple[ReconstructedTrade, ...]:
        return tuple(t for t in self.trades if t.status is TradeStatus.CLOSED)

    @property
    def open_trades(self) -> tuple[ReconstructedTrade, ...]:
        return tuple(t for t in self.trades if t.status is TradeStatus.OPEN)


@dataclass
class _TradeBuilder:
    """Mutable accumulator for one round turn."""

    account_key: str
    instrument_symbol: str
    direction: Direction
    spec: InstrumentSpec
    opened_at: object
    origin_execution_id: str
    legs: list[TradeLeg] = field(default_factory=list)
    realized_points: Decimal = ZERO
    open_quantity: Decimal = ZERO
    max_position_size: Decimal = ZERO

    @classmethod
    def resume(cls, trade: ReconstructedTrade, spec: InstrumentSpec) -> _TradeBuilder:
        return cls(
            account_key=trade.account_key,
            instrument_symbol=trade.instrument_symbol,
            direction=trade.direction,
            spec=spec,
            opened_at=trade.opened_at,
            origin_execution_id=trade.origin_execution_id,
            legs=list(trade.legs),
            realized_points=trade.realized_points,
            open_quantity=trade.quantity_open,
            max_position_size=trade.max_position_size,
        )

    def add_entry(self, execution: Execution, quantity: Decimal) -> None:
        commission, fees = execution.cost_for_quantity(quantity)
        self.legs.append(
            TradeLeg(
                execution_external_id=execution.external_id,
                role=ExecutionRole.ENTRY,
                quantity=quantity,
                price=execution.price,
                executed_at=execution.executed_at,
                commission=commission,
                fees=fees,
                sequence=execution.sequence,
            )
        )
        self.open_quantity += quantity
        self.max_position_size = max(self.max_position_size, self.open_quantity)

    def add_exit(self, execution: Execution, quantity: Decimal, realized_points: Decimal) -> None:
        commission, fees = execution.cost_for_quantity(quantity)
        self.legs.append(
            TradeLeg(
                execution_external_id=execution.external_id,
                role=ExecutionRole.EXIT,
                quantity=quantity,
                price=execution.price,
                executed_at=execution.executed_at,
                commission=commission,
                fees=fees,
                sequence=execution.sequence,
                realized_points=realized_points,
            )
        )
        self.open_quantity -= quantity
        self.realized_points += realized_points

    def build(self, *, closed: bool) -> ReconstructedTrade:
        last_leg = max(self.legs, key=lambda leg: (leg.executed_at, leg.sequence))
        return ReconstructedTrade(
            account_key=self.account_key,
            instrument_symbol=self.instrument_symbol,
            direction=self.direction,
            status=TradeStatus.CLOSED if closed else TradeStatus.OPEN,
            opened_at=self.opened_at,  # type: ignore[arg-type]
            legs=tuple(self.legs),
            realized_points=self.realized_points,
            max_position_size=self.max_position_size,
            point_value=self.spec.point_value,
            tick_size=self.spec.tick_size,
            currency=self.spec.currency,
            closed_at=last_leg.executed_at if closed else None,
            origin_execution_id=self.origin_execution_id,
        )


class _PositionMachine:
    """FIFO state machine for a single (account, instrument) position."""

    def __init__(self, spec: InstrumentSpec, state: PositionState) -> None:
        self._spec = spec
        self._lots: deque[OpenLot] = deque(state.lots)
        self._builder: _TradeBuilder | None = (
            _TradeBuilder.resume(state.open_trade, spec) if state.open_trade is not None else None
        )
        self._completed: list[ReconstructedTrade] = []

    @property
    def completed(self) -> list[ReconstructedTrade]:
        return self._completed

    def apply(self, execution: Execution) -> None:
        """Fold one fill into the position, emitting trades as they close."""
        remaining = execution.quantity

        while remaining > 0:
            if self._builder is None:
                self._open_trade(execution)

            assert self._builder is not None
            if self._is_increasing(execution.side):
                self._lots.append(
                    OpenLot(
                        quantity=remaining,
                        price=execution.price,
                        execution_external_id=execution.external_id,
                    )
                )
                self._builder.add_entry(execution, remaining)
                remaining = ZERO
                continue

            remaining = self._reduce(execution, remaining)

    def finish(self) -> PositionState:
        """Emit any still-open trade and return the resumable state."""
        if self._builder is None:
            return PositionState()
        open_trade = self._builder.build(closed=False)
        self._completed.append(open_trade)
        return PositionState(lots=tuple(self._lots), open_trade=open_trade)

    # --- internals -------------------------------------------------------------

    def _open_trade(self, execution: Execution) -> None:
        if self._lots:  # pragma: no cover — guarded invariant
            raise DomainError("cannot open a trade while lots remain open")
        self._builder = _TradeBuilder(
            account_key=execution.account_key,
            instrument_symbol=execution.instrument_symbol,
            direction=Direction.LONG if execution.side is Side.BUY else Direction.SHORT,
            spec=self._spec,
            opened_at=execution.executed_at,
            origin_execution_id=execution.external_id,
        )

    def _is_increasing(self, side: Side) -> bool:
        assert self._builder is not None
        return (side is Side.BUY) == (self._builder.direction is Direction.LONG)

    def _reduce(self, execution: Execution, remaining: Decimal) -> Decimal:
        """Match ``remaining`` against open lots FIFO. Returns the unmatched remainder.

        A non-zero remainder means the fill exceeded the open position — the position
        flipped. The current trade is closed here and the caller's loop opens a new
        trade in the opposite direction with what is left.
        """
        assert self._builder is not None
        sign = self._builder.direction.signed_multiplier
        matched = ZERO
        realized_points = ZERO

        while remaining > 0 and self._lots:
            lot = self._lots[0]
            quantity = min(remaining, lot.quantity)
            realized_points += (execution.price - lot.price) * sign * quantity
            matched += quantity
            remaining -= quantity
            if quantity == lot.quantity:
                self._lots.popleft()
            else:
                self._lots[0] = replace(lot, quantity=lot.quantity - quantity)

        if matched > 0:
            self._builder.add_exit(execution, matched, realized_points)

        if not self._lots:
            self._completed.append(self._builder.build(closed=True))
            self._builder = None

        return remaining


def reconstruct_trades(
    executions: Iterable[Execution],
    specs: Mapping[str, InstrumentSpec],
    opening_state: Mapping[PositionKey, PositionState] | None = None,
) -> ReconstructionResult:
    """Group fills into round-turn trades.

    Args:
        executions: Fills in any order; they are sorted deterministically before use.
        specs: Instrument specification per symbol. A missing symbol is an error rather
            than a guess — an assumed point value would silently corrupt P&L.
        opening_state: Position state carried over from a previous run, keyed by
            ``(account_key, instrument_symbol)``. Omit for a full rebuild.

    Returns:
        The trades produced (closed and open) and the closing position state.

    Raises:
        DomainError: If a fill references an instrument absent from ``specs``, or if a
            fill is duplicated by external id.
    """
    ordered = sorted(executions, key=lambda execution: execution.sort_key)
    _reject_duplicates(ordered)

    machines: dict[PositionKey, _PositionMachine] = {}
    grouped: dict[PositionKey, list[Execution]] = defaultdict(list)
    for execution in ordered:
        grouped[(execution.account_key, execution.instrument_symbol)].append(execution)

    # Positions that are open but saw no new fills still have to be reported, so seed
    # the machine map from the incoming state as well as from the new fills.
    keys = set(grouped) | set(opening_state or {})

    trades: list[ReconstructedTrade] = []
    closing_state: dict[PositionKey, PositionState] = {}

    for key in sorted(keys):
        _, symbol = key
        spec = specs.get(symbol)
        if spec is None:
            raise DomainError(
                f"no instrument specification for {symbol!r}; refusing to guess point value",
                details={"symbol": symbol},
            )
        machine = _PositionMachine(spec, (opening_state or {}).get(key, PositionState()))
        machines[key] = machine
        for execution in grouped.get(key, ()):
            machine.apply(execution)
        state = machine.finish()
        if not state.is_flat:
            closing_state[key] = state
        trades.extend(machine.completed)

    trades.sort(key=lambda trade: (trade.opened_at, trade.origin_execution_id))
    return ReconstructionResult(trades=tuple(trades), closing_state=closing_state)


def _reject_duplicates(executions: Sequence[Execution]) -> None:
    """Fail loudly on repeated external ids.

    Silently de-duplicating would hide a broken sync cursor; double-counting would
    corrupt P&L. Ingestion filters already-known fills *before* reconstruction, so a
    duplicate reaching this point is a bug worth surfacing.
    """
    seen: set[str] = set()
    for execution in executions:
        if execution.external_id in seen:
            raise DomainError(
                f"duplicate execution external_id {execution.external_id!r}",
                details={"external_id": execution.external_id},
            )
        seen.add(execution.external_id)
