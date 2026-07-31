"""Row-level security: making tenant isolation structural rather than conventional.

Standing rule 6 says *every tenant-owned row carries ``user_id``, and repositories require
it*. An audit across 26 user-scoped models and every query in the codebase found no place
that rule had been broken — the convention has held.

That is exactly the moment to stop relying on it. The rule is enforced by review, and
milestone 13 introduces the thing most likely to break it: a **worker**. Every path so far
takes its ``user_id`` from an authenticated principal, so a missing filter tends to be
obvious. A background handler has no principal, no request, and nobody looking at it — it
is where a future `select(Trade).where(Trade.id == ...)` will be written, and it will look
completely reasonable.

So the boundary moves into Postgres. Every tenant table gets a policy comparing its
``user_id`` against a session variable, and both entry points — the HTTP session
dependency and the worker — set that variable before doing anything. A query that forgets
its filter then returns nothing instead of another tenant's rows.

**Why a GUC and not a database role per tenant.** Roles do not pool: a connection pool
keyed by tenant either holds one pool per user, which does not scale past a few hundred,
or reconnects per request, which costs more than the query. ``set_config(..., is_local =>
true)`` is scoped to the transaction, travels with the pooled connection safely, and is
reset by the transaction ending — so a leaked setting cannot outlive its unit of work.

**The application user must not be the table owner.** Postgres exempts a table's owner
from its own policies unless ``FORCE ROW LEVEL SECURITY`` is set, and it always exempts
superusers. Migrations therefore force RLS on, and :func:`assert_rls_effective` exists so a
deployment can prove the policies actually bite rather than assuming they do.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)

#: The session variable the policies read. Namespaced because unqualified custom GUCs are
#: rejected by Postgres, and ``app.`` keeps it clearly ours.
TENANT_SETTING = "app.user_id"

#: Tables carrying ``user_id`` that RLS is applied to. Kept here rather than derived from
#: the models at runtime so that the migration and the test assert against the *same*
#: list — a table added to the schema and forgotten here fails the coverage test below.
TENANT_TABLES: tuple[str, ...] = (
    "accounts",
    "account_balance_snapshots",
    "ai_analyses",
    "ai_recommendations",
    "broker_connections",
    "daily_journals",
    "detected_patterns",
    "equity_curve_points",
    "executions",
    "jobs",
    "market_conditions",
    "notes",
    "orders",
    "performance_metrics",
    "positions",
    "prediction_models",
    "replay_metadata",
    "reports",
    "risk_metrics",
    "rule_evaluations",
    "screenshots",
    "setups",
    "strategies",
    "strategy_rules",
    "sync_runs",
    "tags",
    "trades",
    "trading_sessions",
)


async def set_tenant(session: AsyncSession, user_id: UUID) -> None:
    """Bind the current transaction to one tenant.

    ``is_local => true`` scopes the setting to the transaction rather than the session,
    which is what makes this safe on a pooled connection: the value cannot survive into
    whatever uses the connection next, however the transaction ends.
    """
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": TENANT_SETTING, "value": str(user_id)},
    )


async def clear_tenant(session: AsyncSession) -> None:
    """Drop the tenant binding, leaving policies to match nothing.

    Used by paths that must see no tenant data at all. Setting an empty string rather
    than resetting is deliberate — a policy comparing against a missing setting would
    error, while comparing against ``''`` simply matches no rows.
    """
    await session.execute(
        text("SELECT set_config(:name, '', true)"), {"name": TENANT_SETTING}
    )


async def current_tenant(session: AsyncSession) -> str | None:
    value = (
        await session.execute(
            text("SELECT current_setting(:name, true)"), {"name": TENANT_SETTING}
        )
    ).scalar_one_or_none()
    return value or None


async def assert_rls_effective(session: AsyncSession, *, table: str = "trades") -> bool:
    """Whether RLS is actually enforced for the connected role.

    Worth calling at startup, because every way this silently does nothing looks fine
    from the application: the migration ran but the app connects as the table owner, or
    as a superuser, or ``FORCE`` was never applied. In all three cases queries succeed
    and return everything, and the only symptom is the absence of an error.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity,
                       pg_catalog.pg_has_role(current_user, c.relowner, 'USAGE') AS is_owner,
                       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = :table AND n.nspname = current_schema()
                """
            ),
            {"table": table},
        )
    ).first()

    if row is None:
        logger.warning("tenancy.table_missing", table=table)
        return False

    enabled, forced, is_owner, is_super = row
    effective = bool(enabled) and (bool(forced) or not is_owner) and not is_super

    if not effective:
        logger.warning(
            "tenancy.rls_not_effective",
            table=table,
            enabled=bool(enabled),
            forced=bool(forced),
            connected_as_owner=bool(is_owner),
            connected_as_superuser=bool(is_super),
            detail=(
                "row-level security will not restrict this connection; queries missing "
                "a user_id filter would return every tenant's rows"
            ),
        )
    return effective
