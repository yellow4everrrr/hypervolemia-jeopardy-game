"""Ingest broker fills and rebuild the trades they affect.

This is the write path that every broker integration funnels into. It is deliberately
broker-agnostic: milestone 2's Tradovate client converts its payloads into
:class:`~app.domain.trading.execution.Execution` objects and calls this. A CSV importer
or a second broker does the same, and neither touches reconstruction logic.

The sequence is:

1. **Filter** fills already stored, by broker id. Re-syncing a day is a no-op.
2. **Persist** the new fills. They are immutable from this point.
3. **Resume** FIFO state for each affected position, so only new fills are folded in
   rather than the account's entire history.
4. **Reconstruct** trades and upsert them by origin fill id.
5. **Save** the closing position state for the next run.

Steps 2–5 share one transaction. A crash mid-run therefore leaves the database in the
pre-run state and the next sync repeats it — at-least-once delivery over an idempotent
operation, which is the only combination that survives a broker that occasionally
replays its own messages.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.application.ports import (
    ExecutionRepository,
    InstrumentRepository,
    PositionStateRepository,
    TradeRepository,
    UnitOfWork,
)
from app.core.errors import DomainError
from app.core.logging import get_logger
from app.domain.trading.execution import Execution
from app.domain.trading.matching import reconstruct_trades

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestResult:
    executions_received: int
    executions_written: int
    executions_skipped: int
    trades_written: int
    open_positions: int

    @property
    def was_noop(self) -> bool:
        return self.executions_written == 0


class IngestExecutions:
    """Use case: fold a batch of fills into the journal."""

    def __init__(
        self,
        *,
        executions: ExecutionRepository,
        trades: TradeRepository,
        instruments: InstrumentRepository,
        positions: PositionStateRepository,
        uow: UnitOfWork,
    ) -> None:
        self._executions = executions
        self._trades = trades
        self._instruments = instruments
        self._positions = positions
        self._uow = uow

    async def execute(
        self, *, user_id: UUID, account_id: UUID, executions: Sequence[Execution]
    ) -> IngestResult:
        if not executions:
            return IngestResult(0, 0, 0, 0, 0)

        known = await self._executions.existing_broker_ids(
            account_id, [execution.external_id for execution in executions]
        )
        fresh = [execution for execution in executions if execution.external_id not in known]

        if not fresh:
            logger.info(
                "ingest.noop",
                account_id=str(account_id),
                received=len(executions),
                reason="all executions already known",
            )
            return IngestResult(len(executions), 0, len(executions), 0, 0)

        specs = await self._instruments.get_specs(
            sorted({execution.instrument_symbol for execution in fresh})
        )
        missing = {e.instrument_symbol for e in fresh} - set(specs)
        if missing:
            # Refusing is the correct behaviour: an assumed tick value silently
            # produces P&L that is wrong by a constant factor, which is far harder to
            # notice than a failed sync.
            raise DomainError(
                "cannot ingest fills for unknown instruments",
                details={"symbols": sorted(missing)},
            )

        written = await self._executions.add_many(user_id, account_id, fresh)

        opening_state = await self._positions.load(account_id)
        result = reconstruct_trades(fresh, specs, opening_state)
        trades_written = await self._trades.upsert_many(user_id, account_id, result.trades)
        await self._positions.save(account_id, dict(result.closing_state))

        await self._uow.commit()

        logger.info(
            "ingest.completed",
            account_id=str(account_id),
            received=len(executions),
            written=written,
            skipped=len(executions) - len(fresh),
            trades=trades_written,
            open_positions=len(result.closing_state),
        )
        return IngestResult(
            executions_received=len(executions),
            executions_written=written,
            executions_skipped=len(executions) - len(fresh),
            trades_written=trades_written,
            open_positions=len(result.closing_state),
        )
