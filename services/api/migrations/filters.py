"""What autogenerate should ignore, as pure functions.

Separate from ``env.py`` because that module reads ``alembic.context`` at import time and
so cannot be imported outside an alembic run — which would leave the one piece of logic
here that can silently disable a CI gate untestable. See ``tests/unit/test_migration_env``.
"""

from __future__ import annotations

#: Timescale's own bookkeeping. Chunk storage is an implementation detail of a hypertable
#: and there are thousands of tables in it, so comparing them against the models produces
#: a diff proportional to how much data is loaded.
TIMESCALE_SCHEMAS = {"_timescaledb_internal", "_timescaledb_catalog"}

#: Hypertables and the column each is partitioned on, from ``create_hypertable`` in
#: migration 0001.
HYPERTABLE_TIME_COLUMNS = {
    "market_bars": "ts",
    "equity_curve_points": "recorded_at",
}

#: ``create_hypertable`` builds a descending index on the partitioning column as part of
#: converting the table, naming it ``{table}_{column}_idx``.
TIMESCALE_INDEXES = {
    f"{table}_{column}_idx" for table, column in HYPERTABLE_TIME_COLUMNS.items()
}


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Whether autogenerate should compare this object against the models.

    Two kinds are excluded, and the second one had CI red on every commit.

    The hypertable time indexes exist in the database and cannot exist in the models:
    SQLAlchemy did not create them, and declaring them would make the migration try to
    build them twice. Autogenerate sees a reflected index with no counterpart in the
    metadata and proposes to drop it, which the drift check in `api-ci.yml` reads as
    "the models have drifted" — on a schema that is exactly correct. Acting on that
    proposal for real would take the primary access path off the largest table here.

    Filtered by exact name against the tables migration 0001 actually converts, rather
    than by a ``_idx`` suffix pattern. A pattern would also hide a genuinely removed
    index on any other table, and a drift check that hides drift is worse than one that
    fails constantly — the failing one is at least noticed.

    ``reflected`` matters: only the database-side object is Timescale's. An index of the
    same name declared in the models is a real conflict, and hiding it would mask it.
    """
    if type_ == "table" and getattr(obj, "schema", None) in TIMESCALE_SCHEMAS:
        return False
    return not (type_ == "index" and reflected and name in TIMESCALE_INDEXES)
