"""UUIDv7 generation (RFC 9562).

Primary keys are UUIDv7 rather than UUIDv4 or bigserial:

* **Time-ordered** — the first 48 bits are a Unix millisecond timestamp, so inserts
  append to the right of the B-tree instead of scattering across it. At a million
  trades per user this is the difference between a hot index tail and constant
  random I/O.
* **Client-generatable** — the ingestion pipeline can assign IDs before the row
  reaches Postgres, which keeps bulk inserts single-round-trip and makes
  cross-aggregate references possible without a flush.
* **Non-enumerable** — unlike sequences, IDs leak neither row counts nor customer
  volume when they appear in URLs.
"""

from __future__ import annotations

import os
import time
from uuid import UUID

_RAND_A_BITS = 12
_RAND_A_MAX = (1 << _RAND_A_BITS) - 1

# Monotonic guard: within the same millisecond we increment the 12-bit `rand_a`
# counter so that IDs minted in a tight loop stay strictly ordered.
_last_timestamp_ms = -1
_counter = 0


def uuid7(timestamp_ms: int | None = None) -> UUID:
    """Return a UUIDv7.

    Args:
        timestamp_ms: Unix time in milliseconds. Defaults to the current time. Passing
            an explicit value makes tests deterministic.
    """
    global _last_timestamp_ms, _counter

    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    if not 0 <= timestamp_ms < (1 << 48):
        raise ValueError("timestamp_ms out of range for UUIDv7")

    if timestamp_ms == _last_timestamp_ms:
        _counter += 1
        if _counter > _RAND_A_MAX:
            # Counter exhausted inside one millisecond: borrow from the next.
            timestamp_ms += 1
            _counter = 0
    else:
        # Seed the counter randomly in the lower half so that concurrent processes
        # are unlikely to collide, while leaving headroom to increment.
        _counter = int.from_bytes(os.urandom(2), "big") & (_RAND_A_MAX >> 1)
    _last_timestamp_ms = timestamp_ms

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = timestamp_ms << 80
    value |= 0x7 << 76  # version 7
    value |= _counter << 64
    value |= 0b10 << 62  # RFC 9562 variant
    value |= rand_b
    return UUID(int=value)


def uuid7_timestamp_ms(value: UUID) -> int:
    """Extract the embedded millisecond timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"expected a UUIDv7, got version {value.version}")
    return value.int >> 80
