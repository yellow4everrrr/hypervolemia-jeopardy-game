"""FastAPI dependencies: settings, database session, current user."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, NotFoundError
from app.core.logging import bind_contextvars
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.session import get_sessionmaker
from app.infrastructure.db.tenancy import set_tenant
from app.infrastructure.secrets.store import (
    InMemorySecretStore,
    SecretStore,
)
from app.infrastructure.storage.objects import (
    ObjectStore,
    build_object_store,
)
from app.interfaces.http.auth import AuthenticatedPrincipal, ClerkTokenVerifier

SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache(maxsize=1)
def get_verifier() -> ClerkTokenVerifier:
    return ClerkTokenVerifier(get_settings())


@lru_cache(maxsize=1)
def get_secret_store() -> SecretStore:
    """Where broker credentials live.

    Encrypted in Postgres in deployed environments, in-memory locally so that linking a
    demo account during development needs no key and leaves nothing behind.

    It used to return ``EnvironmentSecretStore`` for staging and production, which is
    **read-only** — its ``put`` raises. Since ``POST /broker/connections`` writes the
    credential it just verified, linking a broker returned a 500 in every deployed
    environment. Nothing caught it because every test ran against the in-memory store.
    ``EnvironmentSecretStore`` remains for reading a credential injected by a platform's
    own secret manager, which is a real deployment shape, but it can no longer be the
    default for a flow that writes.
    """
    settings = get_settings()
    if settings.requires_secret_encryption:
        from app.infrastructure.db.session import get_sessionmaker
        from app.infrastructure.secrets.encrypted import (
            EncryptedDatabaseSecretStore,
            build_cipher,
        )

        return EncryptedDatabaseSecretStore(
            get_sessionmaker(), build_cipher(settings.secret_encryption_keys)
        )
    return InMemorySecretStore()


SecretStoreDep = Annotated[SecretStore, Depends(get_secret_store)]


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Where rendered chart images live, as a request dependency.

    The choice itself lives in `build_object_store`, in infrastructure, because the
    background worker needs the same one and a queue handler should not import the HTTP
    layer to find out where a file goes.

    Cached so the store is built once per process rather than per request, which is worth
    doing but is no longer load-bearing: the local store is a directory, so a fresh
    instance finds everything an earlier one wrote.

    That used to be a dict, and the dict caused a defect this docstring existed to warn
    about. Two processes meant two stores, so a capture performed by the background worker
    wrote bytes the API could not read; a restart meant a third, so the gallery listed six
    frames from Postgres and 404'd on every one. Both were mistaken for bugs in the
    capture code more than once, because from either side alone the system looks correct.
    See :class:`FilesystemObjectStore`.
    """
    return build_object_store(get_settings())


ObjectStoreDep = Annotated[ObjectStore, Depends(get_object_store)]


async def get_session() -> AsyncIterator[AsyncSession]:
    """Request-scoped session.

    Commit is explicit and belongs to the use case; this dependency only guarantees the
    session is closed and that an unhandled exception rolls back rather than leaking a
    dirty connection back into the pool.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_principal(
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    x_debug_user: Annotated[str | None, Header()] = None,
) -> AuthenticatedPrincipal:
    """Verify the caller's Clerk session token.

    The ``X-Debug-User`` header is honoured only when ``auth_dev_bypass`` is on, which
    :func:`app.core.config.get_settings` refuses to allow in production.
    """
    if settings.auth_dev_bypass and x_debug_user:
        return AuthenticatedPrincipal(clerk_user_id=x_debug_user)

    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("missing bearer token")

    principal = get_verifier().verify(authorization.split(" ", 1)[1].strip())
    request.state.principal = principal
    bind_contextvars(clerk_user_id=principal.clerk_user_id)
    return principal


PrincipalDep = Annotated[AuthenticatedPrincipal, Depends(get_principal)]


async def get_current_user(principal: PrincipalDep, session: SessionDep) -> User:
    """Resolve the local user row, and bind the transaction to that tenant.

    Binding here rather than in each router is deliberate: every authenticated endpoint
    already depends on this, so there is no path that reaches a repository with an
    unbound session. ``users`` itself carries no ``user_id`` and has no policy, which is
    why this lookup works before the binding exists.

    The binding is transaction-scoped, so it cannot survive back into the pool.
    """
    stmt = select(User).where(
        User.clerk_user_id == principal.clerk_user_id, User.deleted_at.is_(None)
    )
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise NotFoundError("no Ledgerline account is linked to this identity")

    await set_tenant(session, user.id)
    bind_contextvars(user_id=str(user.id))
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
