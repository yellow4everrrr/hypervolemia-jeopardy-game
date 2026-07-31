# 0017 — Limits that tests never reach

**Status:** accepted
**Date:** 2026-07-31
**Related:** [0016 — running the application is a test stage](0016-running-it-is-a-test-stage.md)

## Context

Building the last two screens — the what-if simulator and strategy compliance — surfaced
three defects in code that had been "complete" for milestones. None was in the logic
anyone had reviewed. All three were **thresholds no test approached**.

### 1. A bind-parameter ceiling reached at 512 trades

Postgres accepts at most 32,767 bind parameters in one statement, because the wire
protocol encodes the count as a signed 16-bit integer. A multi-row `INSERT ... VALUES`
spends one per column per row, so the real limit is `32767 // columns`:

| table | columns | rows per statement |
|---|---|---|
| `rule_evaluations` | 8 | 4,095 |
| `executions` | 12 | 2,730 |
| `market_bars` | 9 | 3,640 |

Four repositories built row lists and inserted them in one statement. `bars.py` batched;
the other three did not. Compliance evaluation therefore died at **512 trades** against
the eight starter rules, and a broker import of 2,731 fills would have died on the first
sync — well inside a new user's first month, not out at the "hundreds of thousands of
trades" the architecture was designed for.

The failure mode is the worst available: asyncpg raises before sending anything, so there
is no partial write, no slow path, no degradation. Just a 500.

### 2. A lazy relationship in an async request

`GET /strategies` returned a 500 for **every user who had a strategy**. `StrategySummary`
declares a `rules` field with `from_attributes=True`, so
`StrategySummary.model_validate(strategy)` read `strategy.rules` — a lazy relationship,
which under asyncio raises `MissingGreenlet` rather than emitting a query.

The line immediately after it assigned the correct rules, which is what made the code look
safe. The relationship was never *used*. It was still *read*, and reading was the whole
problem.

### 3. A p-value of 0.0001 for no effect at all

The what-if sweep computed its two-sided bootstrap p-value as
`2 × min(below, 1 − below)`, where `below` is the share of resamples at or below zero.
`proportion_at_or_below_zero` includes the resamples that land *exactly* on zero, so
`1 − below` is not the upper tail — it excludes them.

For a scenario that repriced no trades, every delta is exactly zero, every resample is
exactly zero, `below` is 1, and `1 − below` is 0. The formula read that as "not one
resample fell on the other side of zero" — the signature of an overwhelming effect — and
returned the floor, 0.0001. **The scenarios with literally no effect were the most
significant results in the sweep.** Being part of an FDR family, their bogus p-values
moved the Benjamini–Hochberg threshold for every honest test beside them.

## Decision

1. **Batch size is derived, not chosen.** `app/infrastructure/db/bulk.py` computes rows
   per statement from the column count and a safety factor. A constant at each call site
   is a guess that survives only until someone adds a column — and the person adding the
   column has no reason to suspect an INSERT somewhere has a row budget.

2. **Wire projections name their fields explicitly.** `_to_summary` constructs
   `StrategySummary` field by field instead of validating an ORM object. Passing a mapped
   instance to a Pydantic model with `from_attributes=True` makes every declared field a
   potential lazy load, and which fields are safe depends on how the object was loaded —
   a property of the caller, not of the type.

3. **Both bootstrap tails are measured, inclusively.** `BootstrapResult` now carries
   `proportion_at_or_above_zero` alongside `..._below_zero`. They deliberately sum to more
   than one when the distribution has an atom at zero, and the degenerate case now yields
   p = 1: no evidence, which is the truth about a rule that changed nothing.

## Consequences

- New bulk inserts should go through `batched()`. It is one import and removes a class of
  failure that is invisible until production.
- `tests/db/test_strategies_integration.py` loads from a **cold session** — committed,
  then read back through a session that has never seen the row. Every previous test
  created and listed in the same session, where the relationship is already populated,
  which is precisely why none of them saw the bug.
- The sweep is still slow: 65 seconds for nine scenarios over 1,447 trades even with the
  reduced resampling that `?quick=true` now enables. The cost is the re-pricing, not the
  bootstrap, so fewer resamples cannot fix it. **It belongs on the job queue**, which
  milestone 13 already built — `JobKind` has no simulation member yet, so that is an enum
  value, a migration, a handler and a polling UI. Not done here, and not disguised: a
  minute-long synchronous POST is past most proxy timeouts and far past the point where a
  person believes the page is broken.

## The pattern across all three

Every one of these is a **quantity** defect rather than a logic defect. The code does the
right thing for small inputs and the wrong thing past a threshold — 4,095 rows, a session
boundary, an empty delta vector — and unit tests are written with small inputs because
small inputs are what make a test readable.

Nothing in a code review flags them either. A single-statement INSERT looks efficient; a
`model_validate` call looks idiomatic; `1 - below` looks like the complement it is named
after. Finding them took running the system against a year of data, which is the same
conclusion ADR 0016 reached by a different route.
