"""Declarative base, column types and mixins.

Conventions established here apply to every table:

* **UUIDv7 primary keys**, generated in Python so ingestion can build object graphs
  before touching the database.
* **``NUMERIC(20, 8)`` for all money and prices.** Never ``float``, never ``money``.
* **``timestamptz`` everywhere**, stored UTC, defaulted by the database clock so that
  a wrong container timezone cannot corrupt audit trails.
* **Explicit constraint naming**, so Alembic emits stable, revertible migrations
  instead of relying on whatever Postgres happened to auto-name a constraint.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, MetaData, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Scale/precision for every monetary and price column. 12 integral digits covers any
#: realistic notional; 8 fractional digits covers crypto tick sizes and FX pips.
MONEY = Numeric(20, 8, asdecimal=True)

#: Contract counts. Fractional for crypto/FX; integral in practice for futures.
QUANTITY = Numeric(20, 8, asdecimal=True)

#: Unitless ratios (R-multiples, factors, scores).
RATIO = Numeric(18, 8, asdecimal=True)

TZDateTime = DateTime(timezone=True)

E = TypeVar("E", bound=StrEnum)


def pg_enum(enum_cls: type[E], name: str) -> Enum:
    """Native Postgres enum storing the member *values* (not their Python names).

    Native enums are used deliberately: they make the schema self-documenting and
    reject typos at write time. The cost is that adding a variant needs an
    ``ALTER TYPE`` migration — which is the right amount of friction for a value that
    is part of the persisted contract and appears in analytics group-bys.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        Decimal: MONEY,
        datetime: TZDateTime,
        dict[str, Any]: JSONB,
        UUID: PgUUID(as_uuid=True),
    }

    def __repr__(self) -> str:  # pragma: no cover — debugging affordance
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


class UUIDPrimaryKeyMixin:
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SoftDeleteMixin:
    """Journals, notes and strategies are soft-deleted.

    A trader who deletes a strategy still has trades attributed to it; hard deletion
    would either orphan those rows or silently rewrite history. Analytics queries
    filter on ``deleted_at IS NULL``.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True, index=True)


class UserScopedMixin:
    """Every tenant-owned row carries its owner.

    Denormalising ``user_id`` onto child tables (executions, trades, screenshots) is a
    deliberate trade: it costs one column and buys single-predicate tenant isolation
    plus composite indexes that serve the UI's queries without joining upward. It is
    also the precondition for row-level security, which milestone 13 enables.
    """

    @staticmethod
    def _user_fk() -> Any:
        return ForeignKey("users.id", ondelete="CASCADE")

    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def short_string(length: int = 64) -> String:
    return String(length)


#: Convenience for JSONB columns that default to an empty object rather than NULL, so
#: callers never have to distinguish "no metadata" from "not set".
def jsonb_default_dict() -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
