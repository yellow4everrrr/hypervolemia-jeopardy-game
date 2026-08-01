"""Composition root for the Tradovate integration.

Everything above this module depends on protocols; this is the one place that knows
which concrete pieces fit together. Keeping the wiring here means a test can assemble
the same pipeline with fakes by calling the same function with different arguments.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from app.application.use_cases.ingest_executions import IngestExecutions
from app.application.use_cases.sync_broker_account import SyncBrokerAccount
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.infrastructure.brokers.base import TokenBucketLimiter
from app.infrastructure.brokers.tradovate.auth import TradovateAuthenticator, TradovateCredentials
from app.infrastructure.brokers.tradovate.client import TradovateClient
from app.infrastructure.brokers.tradovate.config import (
    REQUEST_BURST,
    REQUESTS_PER_SECOND,
    TradovateEnvironment,
    hosts_for,
)
from app.infrastructure.brokers.tradovate.source import TradovateSyncSource
from app.infrastructure.brokers.tradovate.websocket import (
    TradovateWebSocket,
    WebSocketTransport,
)
from app.infrastructure.db.repositories.broker import (
    SqlAlchemyAccountResolver,
    SqlAlchemyCursorStore,
    SqlAlchemyInstrumentUpserter,
    SqlAlchemySyncRunRecorder,
)
from app.infrastructure.db.repositories.executions import SqlAlchemyExecutionRepository
from app.infrastructure.db.repositories.instruments import SqlAlchemyInstrumentRepository
from app.infrastructure.db.repositories.positions import SqlAlchemyPositionStateRepository
from app.infrastructure.db.repositories.trades import SqlAlchemyTradeRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork

logger = get_logger(__name__)

REQUIRED_CREDENTIAL_FIELDS = ("username", "password", "cid", "secret")


def credentials_from_secret(
    secret: dict[str, Any], *, app_id: str, app_version: str
) -> TradovateCredentials:
    """Build credentials from a secret-store document, failing clearly if incomplete."""
    missing = [field for field in REQUIRED_CREDENTIAL_FIELDS if not secret.get(field)]
    if missing:
        raise ValidationError(
            "Tradovate credentials are incomplete",
            details={"missing": missing, "required": list(REQUIRED_CREDENTIAL_FIELDS)},
        )
    return TradovateCredentials(
        username=str(secret["username"]),
        password=str(secret["password"]),
        app_id=str(secret.get("app_id") or app_id),
        app_version=str(secret.get("app_version") or app_version),
        cid=str(secret["cid"]),
        secret=str(secret["secret"]),
        device_id=str(secret["device_id"]) if secret.get("device_id") else None,
    )


def build_client(
    credentials: TradovateCredentials,
    environment: TradovateEnvironment | str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> TradovateClient:
    """Assemble an authenticated REST client for one connection."""
    resolved = TradovateEnvironment(environment)
    http = httpx.AsyncClient(
        base_url=hosts_for(resolved).rest,
        timeout=timeout,
        transport=transport,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    authenticator = TradovateAuthenticator(http, credentials)
    return TradovateClient(
        http,
        authenticator,
        environment=resolved,
        limiter=TokenBucketLimiter(REQUESTS_PER_SECOND, REQUEST_BURST),
    )


def build_sync(
    session: Any,
    client: TradovateClient,
    *,
    user_id: UUID,
    connection_id: UUID,
) -> SyncBrokerAccount:
    """Wire the full sync pipeline for one connection.

    The ingestion use case is the same object milestone 1 built and tested — the broker
    integration adds a source and a cursor in front of it and changes nothing behind it.
    That is the payoff of having kept reconstruction free of broker concepts.
    """
    ingestor = IngestExecutions(
        executions=SqlAlchemyExecutionRepository(session),
        trades=SqlAlchemyTradeRepository(session),
        instruments=SqlAlchemyInstrumentRepository(session),
        positions=SqlAlchemyPositionStateRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
    return SyncBrokerAccount(
        source=TradovateSyncSource(client),
        ingestor=ingestor,
        instruments=SqlAlchemyInstrumentUpserter(session),
        accounts=SqlAlchemyAccountResolver(session, connection_id),
        cursors=SqlAlchemyCursorStore(session),
        runs=SqlAlchemySyncRunRecorder(session, user_id),
    )


async def websockets_transport(url: str) -> WebSocketTransport:  # pragma: no cover — needs a socket
    """Production transport, backed by the ``websockets`` library.

    Imported lazily so that the frame codec, connection manager and everything tested
    against a fake transport carry no hard dependency on it.
    """
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "the 'websockets' package is required for live Tradovate streaming"
        ) from exc

    connection = await connect(url, ping_interval=None, max_queue=1024)

    class _Adapter:
        async def send(self, message: str) -> None:
            await connection.send(message)

        async def recv(self) -> str:
            received = await connection.recv()
            return received if isinstance(received, str) else received.decode()

        async def close(self) -> None:
            await connection.close()

    return _Adapter()


def build_stream(
    client: TradovateClient,
    access_token: str,
    *,
    on_event: Any = None,
    on_reconnect: Any = None,
    transport_factory: Any = None,
) -> TradovateWebSocket:
    """Build the real-time connection for a client.

    ``on_reconnect`` should run a REST catch-up. It is a parameter rather than a
    default so that no caller can accidentally build a stream without one — the
    reconnect gap is the failure mode most likely to lose a trade.
    """
    return TradovateWebSocket(
        client.hosts.websocket,
        access_token,
        transport_factory or websockets_transport,
        on_event=on_event,
        on_reconnect=on_reconnect,
    )
