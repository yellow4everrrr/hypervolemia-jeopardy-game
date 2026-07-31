# Ledgerline

An institutional-grade AI trading journal. Connect a broker, and every trade is
imported, reconstructed, analysed and coached on — with no manual journaling.

The core discipline: **Python computes every statistic; the AI layer only interprets
them.** A model is never asked to produce a number, and a claim citing a statistic it
was not given is rejected before it reaches the trader.

> This repository also contains two unrelated standalone HTML apps at the root
> (`hypervolemia_jeopardy.html`, `prop_expenses.html`) that predate the platform.

## Status

Milestones 1–4, 7 and 8 of 13 are complete. The backend milestones (7–12) are being
built before the frontend ones (5–6) — see [`docs/roadmap.md`](docs/roadmap.md) for what
each delivers and why the order changed.

| | |
|---|---|
| **Delivered** | Clean-architecture backend · FIFO trade reconstruction · 33-table Postgres/TimescaleDB schema · idempotent ingestion · Tradovate REST + WebSocket sync with cursor safety · institutional analytics with bootstrap intervals and FDR-controlled segmentation · bar ingestion, MAE/MFE and replay windows · rule-based compliance scoring · behavioural and cluster pattern detection · FastAPI with Clerk auth · 566 tests · CI |
| **Next** | Milestone 9 — the AI coach layer, constrained by the evidence contract in ADR 0002 |

## Quick start

```bash
make install     # create .venv and install the API with dev extras
make check       # lint (ruff) + type-check (mypy --strict) + test (pytest)
```

With Docker:

```bash
make up          # TimescaleDB + Redis + API, migrations applied on boot
make seed        # load ES/NQ/CL/GC contract specifications
make logs
```

Without Docker, against a local Postgres:

```bash
cp services/api/.env.example services/api/.env   # then edit
make migrate && make seed && make dev
```

The API serves OpenAPI docs at http://localhost:8000/docs outside production.

## Layout

```
services/api/            FastAPI backend
  app/core/              settings, logging, UUIDv7, Decimal helpers, errors
  app/domain/            trading rules — pure, no I/O, no framework imports
  app/analytics/         statistics — pure, exact decimal, seeded resampling
  app/application/       use cases and the ports they depend on
  app/infrastructure/    SQLAlchemy models, repositories, brokers, secrets
  app/interfaces/http/   routers, schemas, auth, middleware
  migrations/            Alembic
  tests/                 unit (no I/O), api (no database), db (real Postgres)
infra/                   docker-compose for the local stack
docs/                    architecture, data model, ADRs, roadmap
```

Dependencies point inward and a test enforces it: `app/domain/` may not import
SQLAlchemy, FastAPI, httpx or anything else outside the standard library and
`app.core`. This is why the reconstruction engine — the highest-risk code in the
product — is tested in milliseconds with no fixtures.

## Non-negotiables

These are enforced by tests, types or the schema, not by convention:

1. **No floats near money.** All P&L, prices and fees are `Decimal` /
   `NUMERIC(20, 8)`. `app/core/money.py` raises `TypeError` on a `float`.
2. **All timestamps are `timestamptz`, stored UTC.** Session dates derive from the
   *exchange's* timezone. A 23:30 UTC Sunday fill belongs to Monday's session.
3. **Fills are immutable.** Trades are derived and rebuildable; a corrected fill
   arrives as a new fill. There is no endpoint to edit a trade's P&L.
4. **Ingestion is idempotent.** Broker identifiers are unique keys; re-syncing a day
   converges instead of duplicating.
5. **Undefined statistics return `None`,** never zero or infinity. Profit factor with
   no losers is undefined, and the AI layer must be told so.
6. **Every tenant-owned row carries `user_id`,** and repositories require it.
7. **The sync cursor never advances past a fill that was not ingested.** A fill we
   cannot interpret is deferred with a reason and retried, never skipped.
8. **Broker credentials never touch the database.** Only a secret-store pointer does.
9. **No statistic is reported without its sample size,** and none is called a finding
   until it survives significance testing adjusted for the size of the scan.

## Testing

```bash
make test                       # unit + API tests, no infrastructure needed
make test-cov                   # with coverage
LEDGERLINE_TEST_DATABASE_URL=postgresql+asyncpg://... pytest -m db
```

Database-backed tests skip unless `LEDGERLINE_TEST_DATABASE_URL` points at a migrated
database; each runs in a transaction that is rolled back. CI additionally applies the
full migration history to a fresh TimescaleDB, asserts the models have not drifted from
the migrations, and rolls back to base.

## Documentation

- [Roadmap](docs/roadmap.md) — the 13 milestones and what each is for
- [Architecture](docs/architecture.md) — layering, write path, scale posture
- [Data model](docs/data-model.md) — all 33 tables and the decisions behind them
- [ADR 0001](docs/adr/0001-trade-reconstruction.md) — why round turns, why FIFO, why pure
- [ADR 0002](docs/adr/0002-ai-evidence-contract.md) — how the AI is prevented from inventing statistics
- [ADR 0003](docs/adr/0003-broker-sync.md) — why the sync cursor never skips a fill
- [ADR 0004](docs/adr/0004-analytics-honesty.md) — why unproven segments are never findings
- [ADR 0005](docs/adr/0005-compliance-scoring.md) — why "unevaluable" is not a violation
- [ADR 0006](docs/adr/0006-pattern-detection.md) — why a pattern must beat its own null
- [Analytics](docs/analytics.md) — every statistic, its definition, and what it refuses to compute
- [Compliance](docs/compliance.md) — the rule language, the scoring model, and the API
- [Patterns](docs/patterns.md) — behavioural detectors, clustering, and what stops both inventing findings
- [Tradovate integration](docs/tradovate-integration.md) — API specifics, auth, framing, known gaps

## Tech stack

**Backend** FastAPI · SQLAlchemy 2.0 (async) · PostgreSQL + TimescaleDB · Redis ·
Alembic · structlog
**Auth** Clerk (local JWKS verification, no per-request network call)
**Frontend** *(milestone 5)* Next.js · TypeScript · Tailwind · shadcn/ui ·
TanStack Query · Zustand · TradingView Lightweight Charts
**Tooling** ruff · mypy (strict) · pytest · Docker · GitHub Actions
