"""Execution repository — append-only fill storage."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.domain.trading.execution import Execution as DomainExecution
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Execution as ExecutionRow


class SqlAlchemyExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def existing_broker_ids(
        self, account_id: UUID, broker_execution_ids: Sequence[str]
    ) -> set[str]:
        """Which of these fills do we already have?

        Called before every write. Chunked because a backfill can hand us tens of
        thousands of ids at once and Postgres' parameter limit is finite.
        """
        if not broker_execution_ids:
            return set()

        found: set[str] = set()
        chunk_size = 5_000
        for start in range(0, len(broker_execution_ids), chunk_size):
            chunk = list(broker_execution_ids[start : start + chunk_size])
            stmt = select(ExecutionRow.broker_execution_id).where(
                ExecutionRow.account_id == account_id,
                ExecutionRow.broker_execution_id.in_(chunk),
            )
            found.update((await self._session.execute(stmt)).scalars().all())
        return found

    async def add_many(
        self, user_id: UUID, account_id: UUID, executions: Sequence[DomainExecution]
    ) -> int:
        """Insert fills, ignoring any that raced in between the check and the write.

        ``ON CONFLICT DO NOTHING`` on the ``(account_id, broker_execution_id)`` unique
        key makes this safe under concurrent syncs: the live WebSocket feed and a
        scheduled backfill can deliver the same fill simultaneously without either
        failing or duplicating it.
        """
        if not executions:
            return 0

        instrument_ids = await self._resolve_instrument_ids(
            {execution.instrument_symbol for execution in executions}
        )

        rows = [
            {
                "id": uuid7(),
                "user_id": user_id,
                "account_id": account_id,
                "instrument_id": instrument_ids[execution.instrument_symbol],
                "broker_execution_id": execution.external_id,
                "side": execution.side,
                "quantity": execution.quantity,
                "price": execution.price,
                "executed_at": execution.executed_at,
                "sequence": execution.sequence,
                "commission": execution.commission,
                "fees": execution.fees,
                "raw": execution.metadata,
            }
            for execution in executions
        ]

        stmt = (
            insert(ExecutionRow)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_executions_account_broker_execution")
            .returning(ExecutionRow.id)
        )
        return len((await self._session.execute(stmt)).scalars().all())

    async def ids_by_broker_id(
        self, account_id: UUID, broker_execution_ids: Sequence[str]
    ) -> dict[str, UUID]:
        """Map broker fill ids to our primary keys, for trade-leg linking."""
        if not broker_execution_ids:
            return {}
        stmt = select(ExecutionRow.broker_execution_id, ExecutionRow.id).where(
            ExecutionRow.account_id == account_id,
            ExecutionRow.broker_execution_id.in_(list(broker_execution_ids)),
        )
        return dict((await self._session.execute(stmt)).tuples().all())

    async def list_for_rebuild(
        self, account_id: UUID, since: datetime | None = None
    ) -> Sequence[DomainExecution]:
        """Load fills as domain objects, ordered for deterministic reconstruction."""
        stmt = (
            select(ExecutionRow, Instrument.symbol)
            .join(Instrument, Instrument.id == ExecutionRow.instrument_id)
            .where(ExecutionRow.account_id == account_id)
            .order_by(
                ExecutionRow.executed_at,
                ExecutionRow.sequence,
                ExecutionRow.broker_execution_id,
            )
        )
        if since is not None:
            stmt = stmt.where(ExecutionRow.executed_at >= since)

        return [
            DomainExecution(
                external_id=row.broker_execution_id,
                account_key=str(row.account_id),
                instrument_symbol=symbol,
                side=row.side,
                quantity=row.quantity,
                price=row.price,
                executed_at=row.executed_at,
                commission=row.commission,
                fees=row.fees,
                sequence=row.sequence,
            )
            for row, symbol in (await self._session.execute(stmt)).all()
        ]

    async def _resolve_instrument_ids(self, symbols: set[str]) -> dict[str, UUID]:
        stmt = select(Instrument.symbol, Instrument.id).where(
            Instrument.symbol.in_(sorted(symbols))
        )
        resolved = dict((await self._session.execute(stmt)).tuples().all())
        missing = symbols - set(resolved)
        if missing:
            raise KeyError(f"unknown instruments: {sorted(missing)}")
        return resolved
