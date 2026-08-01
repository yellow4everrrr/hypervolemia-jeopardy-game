"""Unit of work.

One use case, one transaction. The session is owned by the caller (the request scope
or the worker task); this wrapper only exposes the transaction boundary so that use
cases can commit without importing SQLAlchemy.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class SqlAlchemyUnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def flush(self) -> None:
        await self._session.flush()
