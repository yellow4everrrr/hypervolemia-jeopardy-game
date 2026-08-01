"""API schemas for broker connections and sync."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.common.enums import BrokerKind, ConnectionStatus, SyncStatus


class ConnectBrokerRequest(BaseModel):
    """Request to link a broker login.

    Credentials are accepted here and handed straight to the secret store; they are
    never written to the database and never returned by any endpoint.
    """

    broker: Literal[BrokerKind.TRADOVATE] = BrokerKind.TRADOVATE
    label: Annotated[str, Field(min_length=1, max_length=80)]
    environment: Literal["demo", "live"] = "demo"
    username: Annotated[str, Field(min_length=1)]
    password: Annotated[str, Field(min_length=1)]
    #: API key pair from the Tradovate Trader application's API Access section.
    cid: Annotated[str, Field(min_length=1)]
    secret: Annotated[str, Field(min_length=1)]
    device_id: str | None = None


class BrokerConnectionSummary(BaseModel):
    """A linked broker login. Deliberately contains no credential material."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    broker: BrokerKind
    label: str
    environment: str
    status: ConnectionStatus
    external_user_id: str | None
    last_sync_at: datetime | None
    last_error: str | None
    created_at: datetime


class BrokerConnectionList(BaseModel):
    items: list[BrokerConnectionSummary]


class SyncRequest(BaseModel):
    #: Restrict the sync to specific broker account ids. Omit to sync all of them.
    broker_account_ids: list[str] | None = None


class SyncRunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    broker_connection_id: UUID
    status: SyncStatus
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    executions_ingested: int
    executions_skipped: int
    trades_written: int
    error: str | None


class SyncResponse(BaseModel):
    """Outcome of one sync pass.

    ``deferred`` is surfaced rather than hidden: a partial sync that silently reported
    success would let a missing trade go unnoticed, which is the failure this pipeline
    is built to prevent.
    """

    connection_id: UUID
    status: SyncStatus
    executions_fetched: int
    executions_ingested: int
    executions_skipped: int
    trades_written: int
    deferred: int
    unknown_broker_accounts: list[str]
    cursor_before: int
    cursor_after: int


class SyncRunList(BaseModel):
    items: list[SyncRunSummary]
