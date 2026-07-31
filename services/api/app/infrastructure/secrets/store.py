"""Secret storage.

Broker credentials give full control of a brokerage account. They are therefore never
written to the database: ``broker_connections.credential_ref`` holds an opaque handle,
and the secret itself lives in a dedicated store. A database dump — a backup, a
replica, an accidental export — must not be enough to trade on someone's behalf.

Two implementations ship here. ``EnvironmentSecretStore`` reads from the process
environment and suits a single-tenant deployment where the operator is the trader.
``InMemorySecretStore`` is for tests. A managed store (AWS Secrets Manager, Railway
secrets) implements the same protocol without anything above this module changing.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from app.core.errors import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Handles look like ``tradovate/<connection id>``. The prefix keeps different brokers'
#: secrets separable in stores that support hierarchical policies.
SECRET_REF_SEPARATOR = "/"


class SecretStore(Protocol):
    async def get(self, reference: str) -> dict[str, Any]: ...

    async def put(self, reference: str, secret: dict[str, Any]) -> None: ...

    async def delete(self, reference: str) -> None: ...


class InMemorySecretStore:
    """Non-persistent store for tests and local experiments."""

    def __init__(self, initial: dict[str, dict[str, Any]] | None = None) -> None:
        self._secrets: dict[str, dict[str, Any]] = dict(initial or {})

    async def get(self, reference: str) -> dict[str, Any]:
        if reference not in self._secrets:
            raise NotFoundError(f"no secret stored for {reference!r}")
        return dict(self._secrets[reference])

    async def put(self, reference: str, secret: dict[str, Any]) -> None:
        self._secrets[reference] = dict(secret)

    async def delete(self, reference: str) -> None:
        self._secrets.pop(reference, None)


class EnvironmentSecretStore:
    """Reads secrets from environment variables.

    A reference ``tradovate/abc123`` maps to ``LEDGERLINE_SECRET_TRADOVATE_ABC123``,
    holding a JSON object. Writes are refused rather than silently discarded: an
    application that appears to store a credential but does not is worse than one that
    plainly cannot.
    """

    def __init__(self, prefix: str = "LEDGERLINE_SECRET_") -> None:
        self._prefix = prefix

    def _key(self, reference: str) -> str:
        normalised = reference.replace(SECRET_REF_SEPARATOR, "_").replace("-", "_").upper()
        return f"{self._prefix}{normalised}"

    async def get(self, reference: str) -> dict[str, Any]:
        raw = os.environ.get(self._key(reference))
        if raw is None:
            raise NotFoundError(
                f"no secret configured for {reference!r}",
                details={"expected_env_var": self._key(reference)},
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NotFoundError(f"secret for {reference!r} is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise NotFoundError(f"secret for {reference!r} must be a JSON object")
        return parsed

    async def put(self, reference: str, secret: dict[str, Any]) -> None:
        raise NotImplementedError(
            "the environment secret store is read-only; set "
            f"{self._key(reference)} in the deployment environment"
        )

    async def delete(self, reference: str) -> None:
        raise NotImplementedError("the environment secret store is read-only")


def credential_reference(broker: str, connection_id: object) -> str:
    return f"{broker}{SECRET_REF_SEPARATOR}{connection_id}"
