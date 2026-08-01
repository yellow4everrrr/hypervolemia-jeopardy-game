"""Batching INSERTs under Postgres' bind-parameter ceiling.

The ceiling is 32,767 parameters per statement, and a multi-row ``INSERT ... VALUES``
spends one per column per row. Three of four repositories built a row list and inserted
it in a single statement, which fails outright — asyncpg raises before sending anything,
so there is no partial write, no slow path, just a 500.

What makes it worth a dedicated test file is that the thresholds sit inside ordinary
usage rather than out at "hundreds of thousands of trades": compliance broke at **512
trades** against eight starter rules, and a broker import of 2,731 fills would have broken
on the first sync. Every existing test inserted tens of rows and passed.
"""

from __future__ import annotations

import pytest

from app.infrastructure.db.bulk import (
    MAX_BIND_PARAMETERS,
    batched,
    rows_per_batch,
)


def rows(count: int, columns: int) -> list[dict[str, int]]:
    return [{f"c{index}": index for index in range(columns)} for _ in range(count)]


@pytest.mark.parametrize("columns", [1, 2, 8, 9, 12, 40, 200])
def test_a_batch_never_exceeds_the_parameter_ceiling(columns: int) -> None:
    """The property the whole module exists for, across realistic row widths."""
    assert rows_per_batch(columns) * columns <= MAX_BIND_PARAMETERS


def test_the_widest_row_in_the_codebase_still_batches_usefully() -> None:
    """`executions` has twelve columns and is the narrowest margin in the app.

    A batch size of one would be "correct" and useless — it would turn a 5,000-fill
    import into 5,000 round trips.
    """
    assert rows_per_batch(12) > 1_000


def test_chunks_cover_every_row_exactly_once() -> None:
    """Batching must not drop or duplicate. Both would be silent."""
    source = rows(9_500, 8)

    chunks = list(batched(source))

    assert sum(len(chunk) for chunk in chunks) == len(source)
    assert [row for chunk in chunks for row in chunk] == source


def test_a_row_count_that_used_to_fail_now_splits() -> None:
    """The exact shape that broke: 534 trades × 8 rules, 8 columns each.

    4,272 rows × 8 columns is 34,176 parameters — over the ceiling by enough that the
    statement was rejected outright.
    """
    source = rows(534 * 8, 8)
    assert len(source) * 8 > MAX_BIND_PARAMETERS  # the bug, stated as arithmetic

    chunks = list(batched(source))

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) * 8 <= MAX_BIND_PARAMETERS


def test_a_small_list_stays_in_one_statement() -> None:
    """Batching must not cost a round trip on the common case."""
    assert len(list(batched(rows(50, 8)))) == 1


def test_no_rows_produces_no_statements() -> None:
    assert list(batched([])) == []


def test_the_widest_row_decides_the_batch() -> None:
    """A ragged list is a caller bug, but it must not silently produce an illegal batch.

    Sizing from the first row would let a later, wider row push a chunk over the limit.
    """
    ragged = [{"a": 1}, {f"c{index}": index for index in range(100)}]

    for chunk in batched(ragged):
        assert len(chunk) * 100 <= MAX_BIND_PARAMETERS


def test_a_zero_column_row_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one column"):
        rows_per_batch(0)
