"""Broker connection and sync endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.domain.common.enums import ConnectionStatus, JobKind
from app.infrastructure.brokers.tradovate.factory import (
    build_client,
    build_sync,
    credentials_from_secret,
)
from app.infrastructure.db.models.broker import Account, BrokerConnection, SyncRun
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.secrets.store import credential_reference
from app.interfaces.http.deps import CurrentUserDep, SecretStoreDep, SessionDep
from app.interfaces.http.schemas.broker import (
    BrokerConnectionList,
    BrokerConnectionSummary,
    ConnectBrokerRequest,
    SyncRequest,
    SyncResponse,
    SyncRunList,
    SyncRunSummary,
)
from app.jobs.queue import JobQueue

router = APIRouter(prefix="/broker", tags=["broker"])
logger = get_logger(__name__)


@router.post(
    "/connections",
    response_model=BrokerConnectionSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Link a broker login",
)
async def connect_broker(
    payload: ConnectBrokerRequest,
    user: CurrentUserDep,
    session: SessionDep,
    secrets: SecretStoreDep,
) -> BrokerConnectionSummary:
    """Store credentials in the secret store and verify them against the broker.

    The connection row is written only after the credentials actually work. Storing an
    unverified connection would leave the user staring at a "connected" account that
    silently syncs nothing.
    """
    settings = get_settings()
    connection_id = uuid7()
    reference = credential_reference(payload.broker.value, connection_id)

    secret = {
        "username": payload.username,
        "password": payload.password,
        "cid": payload.cid,
        "secret": payload.secret,
        "device_id": payload.device_id,
    }
    credentials = credentials_from_secret(
        secret, app_id=settings.service_name, app_version=settings.version
    )

    # Verify before persisting anything. This raises AuthenticationError or
    # RateLimitedError, which the error handler turns into a 401 or 429.
    client = build_client(credentials, payload.environment)
    try:
        token = await client._auth.token()
        accounts = await client.list_accounts()
    finally:
        await client._http.aclose()

    await secrets.put(reference, secret)

    connection = BrokerConnection(
        id=connection_id,
        user_id=user.id,
        broker=payload.broker,
        label=payload.label,
        environment=payload.environment,
        status=ConnectionStatus.ACTIVE,
        credential_ref=reference,
        external_user_id=str(token.user_id),
    )
    session.add(connection)
    await session.flush()

    # Mirror the broker's accounts so fills have somewhere to land. Without this the
    # first sync would defer everything as "unknown broker account".
    for broker_account in accounts:
        session.add(
            Account(
                id=uuid7(),
                user_id=user.id,
                broker_connection_id=connection_id,
                broker_account_id=str(broker_account.id),
                name=broker_account.name,
                is_active=broker_account.active,
            )
        )
    await session.commit()

    logger.info(
        "broker.connected",
        connection_id=str(connection_id),
        broker=payload.broker.value,
        environment=payload.environment,
        accounts=len(accounts),
    )
    return BrokerConnectionSummary.model_validate(connection)


@router.get("/connections", response_model=BrokerConnectionList, summary="List broker logins")
async def list_connections(user: CurrentUserDep, session: SessionDep) -> BrokerConnectionList:
    rows = (
        await session.execute(
            select(BrokerConnection)
            .where(BrokerConnection.user_id == user.id)
            .order_by(BrokerConnection.created_at.desc())
        )
    ).scalars().all()
    return BrokerConnectionList(
        items=[BrokerConnectionSummary.model_validate(row) for row in rows]
    )


@router.post(
    "/connections/{connection_id}/sync",
    response_model=SyncResponse,
    summary="Sync a broker connection now",
)
async def sync_connection(
    connection_id: UUID,
    payload: SyncRequest,
    user: CurrentUserDep,
    session: SessionDep,
    secrets: SecretStoreDep,
) -> SyncResponse:
    """Run a sync immediately.

    Synchronous because a manual sync is a foreground action a user is waiting on, and
    a day's fills is a handful of batched requests. The scheduled path in milestone 13
    runs the same use case from a worker.
    """
    settings = get_settings()
    connection = await _load_connection(session, user.id, connection_id)

    if not connection.credential_ref:
        raise ValidationError("this connection has no stored credentials; re-link it")

    secret = await secrets.get(connection.credential_ref)
    credentials = credentials_from_secret(
        secret, app_id=settings.service_name, app_version=settings.version
    )
    client = build_client(credentials, connection.environment)

    try:
        use_case = build_sync(
            session, client, user_id=user.id, connection_id=connection_id
        )
        outcome = await use_case.execute(
            user_id=user.id,
            connection_id=connection_id,
            trigger="manual",
            account_filter=set(payload.broker_account_ids) if payload.broker_account_ids else None,
        )
    finally:
        await client._http.aclose()

    # This is what makes screenshot capture *automatic*. Queued rather than rendered here
    # because a first import can produce thousands of trades, and a sync that also drew
    # six charts each would take minutes and time out. Enqueued unconditionally: the
    # handler sweeps for trades that have none, so a run with nothing new is a cheap
    # query rather than a special case the caller has to detect.
    await JobQueue(session).enqueue(
        user_id=user.id,
        kind=JobKind.CAPTURE_SCREENSHOTS,
        payload={},
        account_id=None,
    )
    await SqlAlchemyUnitOfWork(session).commit()

    return SyncResponse(
        connection_id=outcome.connection_id,
        status=outcome.status,
        executions_fetched=outcome.executions_fetched,
        executions_ingested=outcome.executions_ingested,
        executions_skipped=outcome.executions_skipped,
        trades_written=outcome.trades_written,
        deferred=outcome.deferred,
        unknown_broker_accounts=sorted(outcome.unknown_accounts),
        cursor_before=outcome.cursor_before,
        cursor_after=outcome.cursor_after,
    )


@router.get(
    "/connections/{connection_id}/runs",
    response_model=SyncRunList,
    summary="Recent sync runs",
)
async def list_sync_runs(
    connection_id: UUID, user: CurrentUserDep, session: SessionDep, limit: int = 20
) -> SyncRunList:
    """The audit trail. This is what makes "why is yesterday missing?" answerable."""
    await _load_connection(session, user.id, connection_id)
    rows = (
        await session.execute(
            select(SyncRun)
            .where(SyncRun.broker_connection_id == connection_id, SyncRun.user_id == user.id)
            .order_by(SyncRun.started_at.desc())
            .limit(min(limit, 100))
        )
    ).scalars().all()
    return SyncRunList(items=[SyncRunSummary.model_validate(row) for row in rows])


async def _load_connection(
    session: AsyncSession, user_id: UUID, connection_id: UUID
) -> BrokerConnection:
    result = await session.execute(
        select(BrokerConnection).where(
            BrokerConnection.id == connection_id, BrokerConnection.user_id == user_id
        )
    )
    connection: BrokerConnection | None = result.scalar_one_or_none()
    if connection is None:
        raise NotFoundError("broker connection not found")
    return connection
