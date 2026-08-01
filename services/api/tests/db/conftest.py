"""Everything in this package needs a database, and pytest should know that without being told.

CI selects these tests with `pytest -m db` and runs everything else in a job that has no
PostgreSQL. That split only works if every test here carries the marker, and relying on
each new file to remember it does not hold: four of thirteen files had drifted, covering
22 tests. Those tests were **deselected** in the database job and **errored** in the unit
job, on `KeyError: 'LEDGERLINE_TEST_DATABASE_URL'` at fixture setup. Bar aggregation,
encrypted secrets, strategy loading and the screenshot sweep — all of them regression
tests for defects that had already reached the product once — were running in neither
place.

A missing marker is invisible in exactly the way that matters: `pytest -m db` reports a
green run over the tests it did collect and says nothing about the ones it skipped past.
Applying it by directory removes the chance to forget.

The skip is here for the same reason. A contributor running the whole suite without a
database should see these skipped with a reason, not a wall of `KeyError`s that look like
broken tests rather than an absent service.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the tests in this directory, and only those.

    The hook is invoked once per session with **every** collected item, not just the ones
    under the conftest that defines it — so the path check is load-bearing. Without it
    this marks the entire suite `db`, `pytest -m db` selects all 1001 tests, and the
    database job starts running the unit tests too.
    """
    needs_database = pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    )
    for item in items:
        if HERE in Path(item.path).parents:
            item.add_marker(pytest.mark.db)
            item.add_marker(needs_database)
