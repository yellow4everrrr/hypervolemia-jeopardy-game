"""Batching multi-row INSERTs under Postgres' bind-parameter ceiling.

A parameterised statement may carry at most **32,767** bind parameters, because the wire
protocol encodes the count as a signed 16-bit integer. A multi-row ``INSERT ... VALUES``
spends one parameter per column per row, so the row limit is not a property of the table
or of Postgres' capacity — it is ``32767 // len(columns)``, and it is reached far sooner
than anyone expects:

===========================  =======  ==============
table                        columns  rows per batch
===========================  =======  ==============
``rule_evaluations``               8           4,095
``executions``                    12           2,730
``market_bars``                    9           3,640
===========================  =======  ==============

Four repositories built row lists and inserted them in one statement. One batched; three
did not, and the failure they shared is unusually nasty:

* It does not degrade. Exceeding the limit raises ``InterfaceError`` from asyncpg before
  the statement is sent, so nothing is written and the caller gets a 500 — not a slow
  request, not a partial result.
* It is invisible at test scale. Fixtures insert tens of rows; the ceiling needs
  thousands. Every unit and integration test passed against code that could not survive
  a real user's first month.
* The thresholds sit inside ordinary usage rather than out at "hundreds of thousands of
  trades". Compliance evaluation broke at **512 trades** against the eight starter rules,
  and a broker sync importing 2,731 fills would break on the first import.

So the batch size is *derived from the column count* here rather than written as a
constant at each call site. A constant is a guess that stays correct only until someone
adds a column, and the person adding the column has no reason to suspect an INSERT
somewhere has a row budget.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

#: Postgres' hard limit on bind parameters in a single statement.
MAX_BIND_PARAMETERS = 32_767

#: Fraction of the ceiling actually used. The headroom absorbs a column being added later
#: without the batch size silently becoming illegal, and costs one extra round trip per
#: several thousand rows.
SAFETY_FACTOR = 0.8


def rows_per_batch(columns: int) -> int:
    """How many rows of ``columns`` fields fit in one statement, with headroom."""
    if columns <= 0:
        raise ValueError("a row must have at least one column")
    budget = int(MAX_BIND_PARAMETERS * SAFETY_FACTOR) // columns
    # A row wider than the budget cannot be batched away, but one row per statement is
    # always legal: 32,767 columns in a single table is not reachable in Postgres.
    return max(budget, 1)


def batched(rows: Sequence[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    """Split rows into chunks that fit inside one INSERT.

    The column count comes from the first row, which is what SQLAlchemy will use to build
    the statement. Rows with differing key sets are a bug in the caller — SQLAlchemy would
    produce a ragged VALUES clause — so the widest row is used and the mismatch is left to
    fail loudly rather than being silently normalised here.
    """
    if not rows:
        return

    columns = max(len(row) for row in rows)
    size = rows_per_batch(columns)
    for start in range(0, len(rows), size):
        yield list(rows[start : start + size])
