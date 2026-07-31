"""Ports — the interfaces the application layer depends on.

These are ``Protocol`` classes, not abstract base classes, so infrastructure adapters
satisfy them structurally without importing anything from this module. Dependencies
point inward: use cases know *what* they need, never *how* it is provided. That is
what makes the ingestion pipeline testable against in-memory fakes and what will let
milestone 2 add a second broker without touching a use case.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.domain.trading.execution import Execution
from app.domain.trading.instrument import InstrumentSpec
from app.domain.trading.matching import PositionKey, PositionState
from app.domain.trading.trade import ReconstructedTrade


@runtime_checkable
class UnitOfWork(Protocol):
    """Transaction boundary. One use case, one transaction, one commit."""

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class InstrumentRepository(Protocol):
    async def get_spec(self, symbol: str, exchange: str | None = None) -> InstrumentSpec | None: ...

    async def get_specs(self, symbols: Sequence[str]) -> dict[str, InstrumentSpec]: ...

    async def resolve_id(self, symbol: str, exchange: str | None = None) -> UUID | None: ...


class ExecutionRepository(Protocol):
    async def existing_broker_ids(
        self, account_id: UUID, broker_execution_ids: Sequence[str]
    ) -> set[str]:
        """Return the subset already stored: the ingestion idempotency filter."""
        ...

    async def add_many(
        self, user_id: UUID, account_id: UUID, executions: Sequence[Execution]
    ) -> int:
        """Persist new fills. Returns the number written."""
        ...

    async def list_for_rebuild(
        self, account_id: UUID, since: datetime | None = None
    ) -> Sequence[Execution]: ...


class TradeRepository(Protocol):
    async def upsert_many(
        self, user_id: UUID, account_id: UUID, trades: Sequence[ReconstructedTrade]
    ) -> int:
        """Insert or update trades keyed by ``(account_id, origin_execution_id)``."""
        ...

    async def count(self, user_id: UUID) -> int: ...


class PositionStateRepository(Protocol):
    """Persists the reconstruction engine's resumable FIFO state."""

    async def load(self, account_id: UUID) -> dict[PositionKey, PositionState]: ...

    async def save(self, account_id: UUID, state: dict[PositionKey, PositionState]) -> None: ...


class Clock(Protocol):
    """Injected time.

    Nothing outside the interface layer calls ``datetime.now`` directly: a use case
    that reads the wall clock cannot be tested deterministically, and time-dependent
    trading logic is exactly the code that most needs deterministic tests.
    """

    def now(self) -> datetime: ...
