"""Broker sync orchestration.

The sequence, and why each step is where it is:

1. **Fetch fills.** Tradovate offers no server-side date or cursor filter, so we pull
   the list and keep those above the stored cursor. The cursor is a fill id, which is
   monotonic per user.
2. **Resolve orders.** A fill carries no account. Orders supply it, and fills whose
   order cannot be resolved are deferred rather than attributed to a guess.
3. **Resolve contract specifications.** Contract → maturity → product, ending at the
   point value that converts a price move into money.
4. **Resolve fees.** Commissions live on a separate entity, batched by fill id.
5. **Map and ingest.** Reuse of the milestone 1 ingestion path means idempotency, FIFO
   reconstruction and rebuild safety come for free.
6. **Advance the cursor — only past fills that were actually ingested.** A deferred
   fill must be retried next run, so the cursor stops at the lowest deferred id. Moving
   past it would silently lose a trade, which is the worst failure this system has.

Every run is recorded in ``sync_runs``: what came in, what was written, what was
deferred and why. "Why is yesterday missing?" has to be answerable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.domain.common.enums import SyncStatus
from app.domain.trading.execution import Execution
from app.domain.trading.instrument import InstrumentSpec

logger = get_logger(__name__)

#: Fills below this cursor were seen on a previous run. Zero means "everything".
DEFAULT_CURSOR = 0


class BrokerSyncSource(Protocol):
    """What a broker adapter must provide to be syncable.

    Deliberately narrow: fetch fills newer than a cursor, and say which local account
    each belongs to. Everything Tradovate-specific — the four-way entity join, the
    fee lookup, the contract-specification walk — is the adapter's problem, not this
    use case's.
    """

    async def fetch_executions(
        self, *, since_cursor: int, account_filter: set[str] | None = None
    ) -> FetchResult: ...


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What an adapter returns for one sync pass."""

    executions: tuple[Execution, ...]
    #: Contract specifications encountered, so instrument reference data self-heals.
    specs: dict[str, InstrumentSpec]
    #: Fills that could not be mapped, with reasons. These hold the cursor back.
    deferred: tuple[DeferredItem, ...] = ()
    #: Highest fill id observed, ingested or not.
    highest_id: int = 0

    @property
    def lowest_deferred_id(self) -> int | None:
        if not self.deferred:
            return None
        return min(item.external_id for item in self.deferred)


@dataclass(frozen=True, slots=True)
class DeferredItem:
    external_id: int
    reason: str


class InstrumentUpserter(Protocol):
    async def upsert_specs(self, specs: dict[str, InstrumentSpec]) -> int: ...


class AccountResolver(Protocol):
    """Maps a broker account identifier to a local account id."""

    async def resolve(self, broker_account_id: str) -> UUID | None: ...


class SyncRunRecorder(Protocol):
    async def start(self, connection_id: UUID, trigger: str) -> UUID: ...

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
    ) -> None: ...


class CursorStore(Protocol):
    async def load(self, connection_id: UUID) -> int: ...

    async def save(self, connection_id: UUID, cursor: int) -> None: ...


class ExecutionIngestor(Protocol):
    """The milestone 1 ingestion use case, as a port."""

    async def execute(
        self, *, user_id: UUID, account_id: UUID, executions: Sequence[Execution]
    ) -> Any: ...


@dataclass
class SyncOutcome:
    connection_id: UUID
    status: SyncStatus
    executions_fetched: int = 0
    executions_ingested: int = 0
    executions_skipped: int = 0
    trades_written: int = 0
    deferred: int = 0
    unknown_accounts: set[str] = field(default_factory=set)
    cursor_before: int = 0
    cursor_after: int = 0
    error: str | None = None

    @property
    def advanced(self) -> bool:
        return self.cursor_after > self.cursor_before


class SyncBrokerAccount:
    """Pull new fills from a broker and fold them into the journal."""

    def __init__(
        self,
        *,
        source: BrokerSyncSource,
        ingestor: ExecutionIngestor,
        instruments: InstrumentUpserter,
        accounts: AccountResolver,
        cursors: CursorStore,
        runs: SyncRunRecorder,
    ) -> None:
        self._source = source
        self._ingestor = ingestor
        self._instruments = instruments
        self._accounts = accounts
        self._cursors = cursors
        self._runs = runs

    async def execute(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        trigger: str = "scheduled",
        account_filter: set[str] | None = None,
    ) -> SyncOutcome:
        run_id = await self._runs.start(connection_id, trigger)
        cursor = await self._cursors.load(connection_id)
        outcome = SyncOutcome(
            connection_id=connection_id, status=SyncStatus.RUNNING, cursor_before=cursor
        )
        outcome.cursor_after = cursor

        try:
            result = await self._source.fetch_executions(
                since_cursor=cursor, account_filter=account_filter
            )
            outcome.executions_fetched = len(result.executions)
            outcome.deferred = len(result.deferred)

            if result.specs:
                # Instrument reference data updates itself from what actually traded,
                # so a newly listed contract never blocks a sync.
                await self._instruments.upsert_specs(result.specs)

            grouped = await self._group_by_account(result.executions, outcome)

            for account_id, executions in grouped.items():
                ingest = await self._ingestor.execute(
                    user_id=user_id, account_id=account_id, executions=executions
                )
                outcome.executions_ingested += getattr(ingest, "executions_written", 0)
                outcome.executions_skipped += getattr(ingest, "executions_skipped", 0)
                outcome.trades_written += getattr(ingest, "trades_written", 0)

            outcome.cursor_after = self._next_cursor(cursor, result, outcome)
            if outcome.cursor_after != cursor:
                await self._cursors.save(connection_id, outcome.cursor_after)

            outcome.status = (
                SyncStatus.PARTIAL
                if (result.deferred or outcome.unknown_accounts)
                else SyncStatus.SUCCEEDED
            )

        except Exception as exc:
            outcome.status = SyncStatus.FAILED
            outcome.error = str(exc)
            await self._runs.finish(
                run_id,
                status=SyncStatus.FAILED,
                executions_ingested=outcome.executions_ingested,
                executions_skipped=outcome.executions_skipped,
                trades_written=outcome.trades_written,
                error=str(exc),
            )
            logger.exception("sync.failed", connection_id=str(connection_id), error=str(exc))
            raise

        await self._runs.finish(
            run_id,
            status=outcome.status,
            executions_ingested=outcome.executions_ingested,
            executions_skipped=outcome.executions_skipped,
            trades_written=outcome.trades_written,
            detail={
                "fetched": outcome.executions_fetched,
                "deferred": outcome.deferred,
                "unknown_accounts": sorted(outcome.unknown_accounts),
                "cursor_before": outcome.cursor_before,
                "cursor_after": outcome.cursor_after,
            },
        )
        logger.info(
            "sync.completed",
            connection_id=str(connection_id),
            status=outcome.status,
            fetched=outcome.executions_fetched,
            ingested=outcome.executions_ingested,
            trades=outcome.trades_written,
            deferred=outcome.deferred,
            cursor=outcome.cursor_after,
        )
        return outcome

    async def _group_by_account(
        self, executions: Sequence[Execution], outcome: SyncOutcome
    ) -> dict[UUID, list[Execution]]:
        """Route fills to local accounts, collecting any broker account we don't know.

        An unmapped broker account is reported, not invented. It usually means the user
        opened a new account at the broker and has not linked it here yet.
        """
        grouped: dict[UUID, list[Execution]] = {}
        resolved: dict[str, UUID | None] = {}

        for execution in executions:
            key = execution.account_key
            if key not in resolved:
                resolved[key] = await self._accounts.resolve(key)
            account_id = resolved[key]
            if account_id is None:
                outcome.unknown_accounts.add(key)
                continue
            grouped.setdefault(account_id, []).append(execution)
        return grouped

    def _next_cursor(self, current: int, result: FetchResult, outcome: SyncOutcome) -> int:
        """Advance only as far as is provably safe.

        The cursor stops just below the lowest deferred fill, so anything we could not
        map is fetched again next run. Advancing past it would drop the fill for good —
        and a silently missing trade is the one failure mode this system must not have.
        """
        candidate = max(current, result.highest_id)

        blockers = [item.external_id for item in result.deferred]
        if outcome.unknown_accounts:
            # Fills for unknown accounts must also be retried once the account is
            # linked, so they hold the cursor exactly as deferred fills do.
            blockers.extend(
                execution.sequence
                for execution in result.executions
                if execution.account_key in outcome.unknown_accounts
            )

        if blockers:
            candidate = min(candidate, min(blockers) - 1)
        return max(current, candidate)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Comparison of our reconstructed P&L against the broker's own figure.

    This is the system's smoke alarm. Our trades are derived through reconstruction,
    fee allocation and decimal arithmetic; the broker's realized P&L is computed
    independently. A persistent gap means one of them is wrong, and it is far more
    likely to be us. Detecting that before the trader does is the whole point.
    """

    account_id: UUID
    as_of: datetime
    ledgerline_realized_pnl: Any
    broker_realized_pnl: Any
    difference: Any
    tolerance: Any
    matches: bool

    @property
    def summary(self) -> str:
        verdict = "matches" if self.matches else "DIVERGES"
        return (
            f"{verdict}: ledgerline={self.ledgerline_realized_pnl} "
            f"broker={self.broker_realized_pnl} diff={self.difference}"
        )


class ReconcileAccount:
    """Compare our realized P&L for a session against the broker's."""

    def __init__(self, *, trades: Any, broker: Any) -> None:
        self._trades = trades
        self._broker = broker

    async def execute(
        self,
        *,
        account_id: UUID,
        broker_account_id: int,
        tolerance: Any,
        now: datetime | None = None,
    ) -> ReconciliationResult:
        moment = now or datetime.now(UTC)
        ours = await self._trades.realized_pnl_for_day(account_id, moment.date())
        snapshot = await self._broker.get_cash_balance_snapshot(broker_account_id)
        theirs = snapshot.realized_pnl

        if theirs is None:
            raise ExternalServiceError(
                "broker did not report realized P&L; cannot reconcile",
                details={"account_id": str(account_id)},
            )

        difference = ours - theirs
        matches = abs(difference) <= tolerance
        if not matches:
            logger.warning(
                "reconciliation.diverged",
                account_id=str(account_id),
                ledgerline=str(ours),
                broker=str(theirs),
                difference=str(difference),
            )
        return ReconciliationResult(
            account_id=account_id,
            as_of=moment,
            ledgerline_realized_pnl=ours,
            broker_realized_pnl=theirs,
            difference=difference,
            tolerance=tolerance,
            matches=matches,
        )
