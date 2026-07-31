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
from app.interfaces.http.auth import AuthenticatedPrincipal, ClerkTokenVerifier

SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache(maxsize=1)
def get_verifier() -> ClerkTokenVerifier:
    return ClerkTokenVerifier(get_settings())


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
    """Resolve the local user row for the verified principal."""
    stmt = select(User).where(
        User.clerk_user_id == principal.clerk_user_id, User.deleted_at.is_(None)
    )
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise NotFoundError("no Ledgerline account is linked to this identity")
    bind_contextvars(user_id=str(user.id))
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
