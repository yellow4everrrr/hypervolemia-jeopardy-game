"""Repositories supporting broker sync: instruments, accounts, cursors, sync runs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.core.logging import get_logger
from app.domain.common.enums import SyncStatus, TradeStatus
from app.domain.trading.instrument import InstrumentSpec
from app.infrastructure.db.models.broker import Account, BrokerConnection, SyncRun
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade

logger = get_logger(__name__)


class SqlAlchemyInstrumentUpserter:
    """Keeps instrument reference data current from what actually traded.

    A newly listed contract — a quarterly roll, a product the trader has not touched
    before — must never block a sync. The broker already told us the tick size and
    point value while mapping the fill, so the instrument row is created on the spot
    rather than waiting for someone to seed it.

    Specifications are *updated* on conflict as well as inserted: if a seeded value
    ever disagrees with the broker's, the broker wins, because the broker is what the
    trader's statement is computed from.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_specs(self, specs: dict[str, InstrumentSpec]) -> int:
        if not specs:
            return 0

        rows = [
            {
                "id": uuid7(),
                "symbol": spec.symbol,
                "root_symbol": _root_symbol(spec.symbol),
                "exchange": spec.exchange,
                "asset_class": spec.asset_class,
                "currency": spec.currency,
                "tick_size": spec.tick_size,
                "tick_value": spec.tick_value,
                "price_precision": spec.price_precision,
                "exchange_timezone": spec.exchange_timezone,
                "is_active": True,
            }
            for spec in specs.values()
        ]

        statement = insert(Instrument).values(rows)
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_instruments_symbol_exchange",
                set_={
                    column: getattr(statement.excluded, column)
                    for column in (
                        "tick_size",
                        "tick_value",
                        "price_precision",
                        "exchange_timezone",
                        "asset_class",
                        "currency",
                    )
                },
            )
        )
        return len(rows)


class SqlAlchemyAccountResolver:
    """Maps a broker account identifier to a local account.

    Returns ``None`` for an account we do not know rather than creating one. Auto-
    creating would silently start journalling an account the trader never linked —
    including, for a user with both demo and live logins, mixing simulated fills into
    real performance statistics.
    """

    def __init__(self, session: AsyncSession, connection_id: UUID) -> None:
        self._session = session
        self._connection_id = connection_id
        self._cache: dict[str, UUID | None] = {}

    async def resolve(self, broker_account_id: str) -> UUID | None:
        if broker_account_id in self._cache:
            return self._cache[broker_account_id]

        statement = select(Account.id).where(
            Account.broker_connection_id == self._connection_id,
            Account.broker_account_id == broker_account_id,
        )
        account_id = (await self._session.execute(statement)).scalar_one_or_none()
        if account_id is None:
            logger.info(
                "sync.unknown_broker_account",
                connection_id=str(self._connection_id),
                broker_account_id=broker_account_id,
            )
        self._cache[broker_account_id] = account_id
        return account_id


class SqlAlchemyCursorStore:
    """Persists the sync cursor on the connection's ``sync_cursor`` JSONB.

    A JSONB document rather than a column because different brokers cursor on different
    things — Tradovate on a monotonic fill id, another broker on a timestamp or an
    opaque page token — and none of them should require a migration.
    """

    CURSOR_KEY = "fills"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, connection_id: UUID) -> int:
        statement = select(BrokerConnection.sync_cursor).where(
            BrokerConnection.id == connection_id
        )
        cursor = (await self._session.execute(statement)).scalar_one_or_none() or {}
        value = cursor.get(self.CURSOR_KEY, {}).get("max_id", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            # A corrupt cursor must not silently skip history; start from the beginning
            # and let idempotent ingestion absorb the re-read.
            logger.warning("sync.cursor_invalid", connection_id=str(connection_id), value=value)
            return 0

    async def save(self, connection_id: UUID, cursor: int) -> None:
        statement = select(BrokerConnection.sync_cursor).where(
            BrokerConnection.id == connection_id
        )
        current = dict((await self._session.execute(statement)).scalar_one_or_none() or {})
        current[self.CURSOR_KEY] = {
            "max_id": int(cursor),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        await self._session.execute(
            update(BrokerConnection)
            .where(BrokerConnection.id == connection_id)
            .values(sync_cursor=current, last_sync_at=datetime.now(UTC))
        )


class SqlAlchemySyncRunRecorder:
    """Writes the audit trail for each sync attempt."""

    def __init__(self, session: AsyncSession, user_id: UUID) -> None:
        self._session = session
        self._user_id = user_id

    async def start(self, connection_id: UUID, trigger: str) -> UUID:
        run_id = uuid7()
        await self._session.execute(
            insert(SyncRun).values(
                id=run_id,
                user_id=self._user_id,
                broker_connection_id=connection_id,
                status=SyncStatus.RUNNING,
                trigger=trigger,
                started_at=datetime.now(UTC),
            )
        )
        # Committed immediately so a crashed run leaves a RUNNING row behind rather
        # than no evidence that a sync was ever attempted.
        await self._session.commit()
        return run_id

    async def finish(
        self,
        run_id: UUID,
        *,
        status: SyncStatus,
        executions_ingested: int,
        executions_skipped: int,
        trades_written: int,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await self._session.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(
                status=status,
                finished_at=datetime.now(UTC),
                executions_ingested=executions_ingested,
                executions_skipped=executions_skipped,
                trades_written=trades_written,
                error=error,
                detail=detail or {},
            )
        )
        await self._session.commit()


class SqlAlchemyReconciliationReader:
    """Reads our own realized P&L for comparison against the broker's."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def realized_pnl_for_day(self, account_id: UUID, session_date: object) -> Decimal:
        """Net realized P&L for closed trades in one session.

        Closed trades only — the broker's ``realizedPnL`` excludes open positions, and
        comparing a figure that includes them against one that does not would produce a
        divergence alert on every open trade.
        """
        statement = select(func.coalesce(func.sum(Trade.net_pnl), 0)).where(
            Trade.account_id == account_id,
            Trade.session_date == session_date,
            Trade.status == TradeStatus.CLOSED,
        )
        return Decimal((await self._session.execute(statement)).scalar_one())


def _root_symbol(symbol: str) -> str:
    """Strip a futures month/year suffix: ``ESZ5`` → ``ES``, ``MNQH26`` → ``MNQ``.

    Grouping by root is what keeps a quarterly roll from fragmenting a trader's
    history into four unusably small samples.
    """
    month_codes = "FGHJKMNQUVXZ"
    for index in range(len(symbol) - 1, 0, -1):
        if symbol[index] in month_codes and symbol[index + 1 :].isdigit():
            return symbol[:index]
    return symbol
