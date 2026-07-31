# Ledgerline

An institutional-grade AI trading journal. Connect a broker, and every trade is
imported, reconstructed, analysed and coached on — with no manual journaling.

The core discipline: **Python computes every statistic; the AI layer only interprets
them.** A model is never asked to produce a number, and a claim citing a statistic it
was not given is rejected before it reaches the trader.

> This repository also contains two unrelated standalone HTML apps at the root
> (`hypervolemia_jeopardy.html`, `prop_expenses.html`) that predate the platform.

## Status

**All 13 milestones are complete.** See [`docs/roadmap.md`](docs/roadmap.md) for what each
delivers and why the backend was built before the frontend.

| | |
|---|---|
| **Delivered** | Clean-architecture backend · FIFO trade reconstruction · 33-table Postgres/TimescaleDB schema · idempotent ingestion · Tradovate REST + WebSocket sync with cursor safety · institutional analytics with bootstrap intervals and FDR-controlled segmentation · bar ingestion, MAE/MFE and replay windows · rule-based compliance scoring · behavioural and cluster pattern detection · an AI coach that cannot state an uncomputed number · counterfactual what-if simulation · walk-forward-validated predictive models that refuse to serve unproven skill · periodic reports with deduplicated leak costs · a Postgres-backed job queue and database-enforced tenant isolation · FastAPI with Clerk auth · a Next.js frontend whose components cannot render a number without its qualification · bar-by-bar trade replay that never interpolates · 932 tests · CI |
| **Design rule** | Every surface refuses to state what it cannot support. Most months establish no changes; most models are refused; a replay never draws a path nobody recorded. |

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
apps/web/                Next.js frontend
  src/components/        evidence components — take Estimate, never number
  src/lib/               typed API client, formatting, query hooks
  src/types/             the backend's payload shapes, transcribed
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
6. **Every tenant-owned row carries `user_id`,** and Postgres enforces it. Row-level
   security on all 28 tenant tables means a query missing its filter returns nothing
   rather than another tenant's rows — provided the app connects as a non-owner role,
   which the startup check refuses to boot without in production.
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
- [ADR 0007](docs/adr/0007-coach-placeholders.md) — why the coach writes placeholders, not numbers
- [ADR 0008](docs/adr/0008-counterfactual-simulation.md) — why a counterfactual is a claim about a sample
- [ADR 0009](docs/adr/0009-predictive-models.md) — why a model that cannot prove its skill is not served
- [ADR 0010](docs/adr/0010-periodic-reports.md) — why report figures are attributed, never summed
- [ADR 0011](docs/adr/0011-jobs-and-tenant-isolation.md) — why the queue is a table, and RLS needs a non-owner role
- [ADR 0012](docs/adr/0012-frontend-honesty.md) — why the UI is where the qualifications get dropped
- [ADR 0013](docs/adr/0013-replay-invents-a-path.md) — why the replay steps rather than animates
- [Analytics](docs/analytics.md) — every statistic, its definition, and what it refuses to compute
- [Compliance](docs/compliance.md) — the rule language, the scoring model, and the API
- [What-if](docs/what-if.md) — counterfactual re-simulation, and how the null was chosen
- [ML layer](docs/ml.md) — the leakage barrier, walk-forward validation, and why most models are refused
- [Reports](docs/reports.md) — periodic reports, deduplicated leak costs, and tested comparisons
- [Operations](docs/operations.md) — workers, tenant isolation, rate limits, and the role you must connect as
- [Patterns](docs/patterns.md) — behavioural detectors, clustering, and what stops both inventing findings
- [AI coach](docs/ai-coach.md) — the evidence bundle, the placeholder contract, and what gets rejected
- [Tradovate integration](docs/tradovate-integration.md) — API specifics, auth, framing, known gaps

## Tech stack

**Backend** FastAPI · SQLAlchemy 2.0 (async) · PostgreSQL + TimescaleDB · Redis ·
Alembic · structlog
**Auth** Clerk (local JWKS verification, no per-request network call)
**Frontend** Next.js · TypeScript · Tailwind · shadcn/ui ·
TanStack Query · Zustand · TradingView Lightweight Charts
**Tooling** ruff · mypy (strict) · pytest · Docker · GitHub Actions
