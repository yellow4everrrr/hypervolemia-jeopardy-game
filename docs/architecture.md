# Architecture

## The one-paragraph version

Fills arrive from a broker. They are stored immutably and never edited. A pure,
deterministic engine reconstructs them into round-turn trades. A Python analytics
engine computes statistics from those trades. An LLM reads those statistics and writes
prose about them — and is structurally prevented from inventing numbers of its own.
Everything else is presentation.

## Layering

```
                    ┌──────────────────────────────────────┐
                    │  interfaces/   FastAPI routers,      │  ← knows HTTP
                    │                schemas, auth, deps   │
                    └──────────────────┬───────────────────┘
                                       │ depends on
                    ┌──────────────────▼───────────────────┐
                    │  application/  use cases, ports      │  ← knows workflows
                    └──────────────────┬───────────────────┘
                                       │ depends on
                    ┌──────────────────▼───────────────────┐
                    │  domain/       trading rules,        │  ← knows only trading
                    │                pure, no I/O          │
                    └──────────────────▲───────────────────┘
                                       │ implements ports
                    ┌──────────────────┴───────────────────┐
                    │  infrastructure/  SQLAlchemy, Redis,  │  ← knows storage
                    │                   brokers, S3         │
                    └──────────────────────────────────────┘
```

Dependencies point inward. `infrastructure` implements protocols declared in
`application/ports.py`; the application layer never imports a concrete adapter. This is
enforced by `tests/unit/test_architecture.py`, which fails the build if `domain/` ever
imports SQLAlchemy, FastAPI, httpx or anything else it has no business knowing.

The practical payoff: the reconstruction engine — the code where a bug is most
expensive and hardest to spot — is tested with no database, no fixtures and no async,
in about a millisecond per case. That is why it has 30 tests instead of 3.

### What lives where

| Path | Contains | May import |
|---|---|---|
| `app/core/` | Settings, logging, UUIDv7, Decimal helpers, error taxonomy | stdlib, pydantic, structlog |
| `app/domain/` | Instruments, executions, trades, FIFO matching, sessions, risk | stdlib + `app.core` only |
| `app/application/` | Use cases and the ports they depend on | `app.domain`, `app.core` |
| `app/infrastructure/` | SQLAlchemy models, repositories, unit of work, adapters | anything |
| `app/interfaces/` | FastAPI app, routers, schemas, auth, dependencies | anything |

## The write path

```
Broker (Tradovate REST backfill + WebSocket live fills)
   │
   ▼
BrokerAdapter                        milestone 2 — converts payloads to domain objects
   │  Execution[]
   ▼
IngestExecutions (use case)
   │  1. filter fills already stored, by broker id  ──► idempotent re-sync
   │  2. persist new fills                          ──► immutable from here on
   │  3. load resumable FIFO position state         ──► O(new fills), not O(history)
   │  4. reconstruct_trades()                       ──► pure function, no I/O
   │  5. upsert trades + legs, save position state
   │  6. commit — steps 2-5 are one transaction
   ▼
analytics engine ──► performance_metrics, equity_curve_points, risk_metrics
   │
   ▼
AI layer ──► ai_analyses (prose + cited metric keys, validated against the payload)
```

Two properties make this survive a broker that misbehaves:

**Idempotency.** Broker identifiers are natural keys with unique constraints
(`uq_executions_account_broker_execution`, `uq_trades_account_origin_execution`).
Re-running a sync converges rather than duplicating. Combined with the transaction
boundary, a crash mid-run leaves the pre-run state and the next run repeats it —
at-least-once delivery over idempotent writes.

**Determinism.** `reconstruct_trades()` takes fills and instrument specs and returns
trades. No clock, no configuration, no network. Fills are sorted by
`(executed_at, sequence, external_id)`, so the same set always produces the same
trades regardless of the order the broker's pages arrived in. This is tested directly
(`test_reconstruction_is_order_independent`).

## Why the round turn is the unit

A *trade* is the interval between a position leaving flat and returning to flat, on one
instrument in one account. Scale-ins and scale-outs are the same trade; a position flip
ends one and starts another.

The alternatives are worse. Per-fill statistics reward slicing an order into pieces and
punish adding to a winner. Per-day statistics hide which decision produced the result.
The round turn is the smallest unit that corresponds to a *decision*, which is what the
journal exists to evaluate.

Matching is FIFO — CME/NFA convention for futures, and the only convention under which
"how long did I hold this?" has a defined answer when a position was scaled into.

## Data model highlights

Full table-by-table detail is in [`data-model.md`](./data-model.md). The decisions that
shape everything else:

- **UUIDv7 primary keys.** Time-ordered, so inserts append to the index tail instead of
  scattering; client-generatable, so ingestion builds object graphs before a flush.
- **`NUMERIC(20, 8)` for all money and prices.** `float` cannot be used anywhere near
  P&L; `app/core/money.py` refuses to accept one.
- **`timestamptz`, stored UTC.** Session dates are derived through the *exchange's*
  timezone, never the server's. A 23:30 UTC Sunday fill belongs to Monday's session.
- **`user_id` on every tenant-owned row.** One predicate isolates a tenant, composite
  indexes serve the UI without joining upward, and row-level security becomes available
  without a migration.
- **Executions link to trades through an association table.** A fill spanning a
  position flip contributes to two trades, with quantity and costs split pro rata.
- **Metrics stored as JSONB keyed by `(scope, scope_key, period)`.** New breakdowns are
  a new row, not a migration.
- **TimescaleDB hypertables for `market_bars` and `equity_curve_points`.** Optional:
  the migration detects the extension and falls back to ordinary tables, so CI and
  local dev run on stock Postgres with identical semantics.

## The AI contract

The rule is not "please don't make up numbers". It is enforced by structure:

1. The analytics engine computes a metrics payload in Python.
2. That exact payload is stored in `ai_analyses.input_metrics`.
3. The model receives the payload and returns prose plus the metric keys it cited.
4. Before the row is written, cited keys are validated against the payload. A claim
   referencing a statistic that is not present is rejected, not published.
5. `evidence_validated`, `model` and `prompt_version` are recorded on every row.

`safe_divide()` returning `None` instead of infinity matters here: profit factor with
no losing trades is *undefined*, and the model must be handed "not defined for this
sample" rather than a number it will confidently interpret.

## Scale posture

Target: 10⁵–10⁶ trades per user, a decade of history, sub-100ms dashboard queries.

- Every UI access path has a covering index (`ix_trades_user_opened_at`,
  `ix_trades_user_session_date`, and siblings).
- The blotter paginates by fetching `limit + 1` rows; no `COUNT(*)` over a filtered set.
- Daily rollups live in `trading_sessions` (~250 rows/year) rather than being
  aggregated from trades on every calendar render.
- Bar data is chunked weekly and compressed after 30 days.
- Position state is persisted, so an incremental sync is O(new fills) rather than
  O(entire history).

## Deferred deliberately

Marked here so they are decisions rather than oversights:

- **Row-level security** — `user_id` is present everywhere and repositories enforce
  scoping; RLS policies land in milestone 13 alongside a connection-level tenant role.
- **Read replicas / CQRS** — premature until the analytics engine's query shape is
  known (milestone 3).
- **Event sourcing** — executions are already an immutable append-only log with
  derived read models. Formal event sourcing would add ceremony without new guarantees.
- **A queue (Celery/Arq)** — needed for scheduled syncs and report generation, chosen
  in milestone 2 when there is a real workload to size it against.
