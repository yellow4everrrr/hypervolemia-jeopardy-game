# 0016 — Running the application is its own test stage

**Status:** accepted
**Date:** 2026-07-31

## Context

Thirteen milestones shipped with a passing type checker, a passing linter, and a passing
test suite on both sides of the stack. The first time the frontend was pointed at a real
API with a real database, six defects appeared within minutes — none of which any existing
check could have caught, because each lived precisely in the gap *between* two checked
things.

| What broke | Why nothing caught it |
|---|---|
| Patterns page crashed on load | `usePatterns` declared `{behaviours: [...]}`; the endpoint returns `{items, note}`. TypeScript checked the declaration against itself. |
| Blotter cells silently blank | `quantity`, `r_multiple`, `instrument_symbol` do not exist on the response. Reading an absent property in JavaScript is `undefined`, not an error. |
| Dashboard reported "0 trades" over 1,408 | The page was a hardcoded placeholder that no test asserted was temporary. |
| Replay chart empty on every trade | The window picks 2m; ingest writes 1m; `load_series` matched exactly and returned nothing. `bar_count: 0` is a valid response shape. |
| Reports printed `178.88686131386861313868613` | A mean of per-trade `Decimal`s carries every digit of its division. Correct arithmetic, unreadable output, and the string was *stored* and fed to the AI layer. |
| Leak card contradicted itself | Deduplicated total and naive sum differed at the 25th decimal, so a string comparison called them different and printed "the same money billed more than once" beside two identical figures. |

The pattern is consistent. Every one of these is a **seam** defect: a contract between two
components that no single component's tests can observe. Unit tests verify each side
against its own assumptions. Type checking verifies each side against its own
declarations. Neither compares the two, and a hand-transcribed interface is a copy that
drifts.

The severity ordering is also worth noting, because it is the reverse of the obvious one.
The crash was the *least* harmful: it was visible, immediate, and got fixed in minutes.
The blank blotter cells, the empty chart and the placeholder dashboard all rendered as
finished pages. Nobody files a bug against a page that looks complete.

## Decision

**Running the application against real data is a required stage, not a demo.** Three
things follow.

1. **Cross-language contracts are checked in Python**, the only place that can see both
   sides. `tests/api/test_wire_contracts.py` parses the exported interfaces from
   `apps/web/src/types/evidence.ts` and asserts every field the frontend declares exists in
   the shape the API sends. It runs one way — the API may return more than a screen
   consumes — and skips rather than fails in a backend-only checkout. The projections it
   checks against are named functions (`serialise_pattern`, `_serialise`) rather than dict
   literals, specifically so a test can reach their key sets.

2. **An empty result may not mean two different things.** `SqlAlchemyBarRepository.load_at`
   distinguishes "no data for this instrument" from "data exists at a resolution you did
   not ask for" and aggregates for the second case. Any query whose empty answer is
   ambiguous is a latent version of the blank chart.

3. **Exactness and legibility are separate concerns, resolved at the boundary.** The engine
   keeps full `Decimal` precision everywhere; the payload carries `display_current`
   alongside `current`; the UI trims trailing zeros *lexically* (`trimDecimal`) rather than
   through `Number`, because `Number("0.10000000000000000001")` is `0.1` and rounding for
   display must never become rounding of the value. Where a figure is compared for the
   purpose of describing it to a reader, it is compared **as rendered** — never tell someone
   two numbers differ when the screen shows them the same number.

## Consequences

- New endpoints consumed by the frontend should be projected through a named function and
  added to the wire-contract test. The cost is one test line; the alternative is a blank
  cell nobody reports.
- **The route smoke test now exists** (`apps/web/tests/smoke.spec.ts`, run by the `smoke`
  job in `web-ci.yml`). It stands up Postgres, migrates, seeds a demo history, starts the
  API and a production frontend build, and loads every route asserting: no page error, no
  crash boundary, no `undefined`/`NaN`/`[object Object]`, no figure with nine or more
  decimal places, **and** a route-specific string that proves the page rendered content
  rather than an empty container. That last clause is the one that matters — the crash was
  the easiest of the six defects to catch, and the blank blotter, empty chart and
  placeholder dashboard all passed every check that only asks whether an exception was
  thrown.

  It earned its place immediately. On its first run against a fresh database it caught a
  replay chart reporting zero bars, and the frontend job now also triggers on
  `services/api/**` changes, because renaming a response field is a green backend change
  that blanks a column nobody is watching.
- Frontend CI exists at all, which it did not for thirteen milestones: `tsc`, `eslint` and
  `vitest` ran only when someone remembered.
- Demo seed data must exercise the thresholds the engine actually uses. The first seeded
  history put five trades in every session against an `OVERTRADING_THRESHOLD` of six, so
  the one detector capable of finding the planted leak never had a sample — the scan came
  back clean on data built to be dirty. Seeds are fixtures, and a fixture that cannot
  trigger the code under demonstration is not demonstrating it.
