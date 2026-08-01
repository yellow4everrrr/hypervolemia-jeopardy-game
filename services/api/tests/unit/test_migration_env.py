"""What the autogenerate filter hides, and what it must not.

`api-ci.yml` runs a drift check: autogenerate a migration against a freshly migrated
database and fail if it proposes any schema change, on the theory that a clean diff means
the models and the migrations agree.

It had been failing on every commit. `create_hypertable` builds a descending index on the
partitioning column as part of converting a table, so `market_bars_ts_idx` and
`equity_curve_points_recorded_at_idx` exist in the database and cannot exist in the
models — SQLAlchemy did not create them, and declaring them would make the migration try
to build them twice. Autogenerate saw two reflected indexes with no counterpart in the
metadata and proposed to drop them, which the check read as drift on a schema that was
exactly correct.

The danger in fixing it is over-filtering. A drift check that hides real drift is worse
than one that fails constantly: the failing one is at least noticed. So the filter names
the two indexes migration 0001 actually creates, and these tests pin both halves —
those two are hidden, and everything else still gets through.
"""

from __future__ import annotations

import pytest

from migrations.filters import (
    HYPERTABLE_TIME_COLUMNS,
    TIMESCALE_INDEXES,
    include_object,
)


class Reflected:
    """Stands in for a reflected schema object; only `schema` is ever read."""

    def __init__(self, schema: str | None = None) -> None:
        self.schema = schema


@pytest.mark.parametrize("name", sorted(TIMESCALE_INDEXES))
def test_a_hypertable_time_index_is_hidden(name: str) -> None:
    """Timescale made it, the models cannot declare it, so it is not drift."""
    assert not include_object(Reflected(), name, "index", True, None)


def test_the_hidden_names_match_the_hypertables_migration_0001_creates() -> None:
    """The set is derived, so adding a hypertable cannot silently forget its index."""
    derived = {
        f"{table}_{column}_idx" for table, column in HYPERTABLE_TIME_COLUMNS.items()
    }
    assert derived == TIMESCALE_INDEXES
    assert HYPERTABLE_TIME_COLUMNS == {
        "market_bars": "ts",
        "equity_curve_points": "recorded_at",
    }


@pytest.mark.parametrize(
    "name",
    [
        "trades_net_pnl_idx",
        "market_bars_source_idx",  # right table, wrong column
        "equity_curve_points_account_id_idx",
        "ix_trades_user_id",
    ],
)
def test_any_other_index_is_still_compared(name: str) -> None:
    """**The half that matters.**

    A filter written as "ignore reflected indexes ending in `_idx`" would pass the test
    above and quietly switch off index drift detection for the whole schema. An index
    dropped by hand in production would then never be reported.
    """
    assert include_object(Reflected(), name, "index", True, None)


def test_an_index_declared_in_the_models_is_compared_even_by_that_name() -> None:
    """`reflected=False` means it came from the metadata, so it is ours to compare.

    Only the database-side object is Timescale's. If a model ever declares an index by
    one of these names, that is a real conflict and hiding it would mask it.
    """
    for name in TIMESCALE_INDEXES:
        assert include_object(Reflected(), name, "index", False, None)


def test_timescale_chunk_tables_are_hidden() -> None:
    """Chunk storage is an implementation detail, and there are thousands of them."""
    internal = Reflected("_timescaledb_internal")
    catalog = Reflected("_timescaledb_catalog")

    assert not include_object(internal, "_hyper_1_1_chunk", "table", True, None)
    assert not include_object(catalog, "hypertable", "table", True, None)


def test_ordinary_tables_and_columns_are_compared() -> None:
    assert include_object(Reflected(), "trades", "table", True, None)
    assert include_object(Reflected("public"), "market_bars", "table", True, None)
    assert include_object(Reflected(), "stray_column", "column", True, None)
