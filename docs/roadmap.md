# Ledgerline — Product & Engineering Roadmap

Ledgerline is an institutional-grade AI trading journal: it connects directly to a
broker (Tradovate first), reconstructs every trade from raw fills, computes
quantitative performance statistics in Python, and uses an LLM **only to interpret
those computed numbers** — never to invent them.

This document is the contract between milestones. Each milestone is independently
reviewable, ships tests and docs, and leaves `main` deployable.

---

## Milestone map

| # | Milestone | Why it exists | Status |
|---|-----------|---------------|--------|
| 1 | **Foundation, domain model & schema** | Nothing downstream is trustworthy if trade reconstruction or the schema is wrong. Establishes clean architecture, the FIFO execution→trade engine, the normalized Postgres/TimescaleDB schema, and the test/CI/dev harness. | ✅ Complete |
| 2 | **Tradovate integration & sync pipeline** | Zero manual journaling is the core promise. OAuth + REST backfill + WebSocket live fills, idempotent ingestion, sync cursors, reconciliation against broker P/L. | ⏳ Next |
| 3 | **Analytics engine** | Every AI claim must trace to a deterministic Python number. Expectancy, profit factor, Sharpe/Sortino, SQN, Kelly, risk of ruin, drawdown, MAE/MFE, edge ratio, Monte Carlo, bootstrap confidence intervals, and the full segmentation cube. | Planned |
| 4 | **Market data & replay engine** | Replay and MAE/MFE need bar data. TimescaleDB hypertables, bar ingestion, replay window computation, marker generation, S3 snapshot pipeline. | Planned |
| 5 | **Frontend foundation** | Next.js + Clerk + design system, dashboard, trade blotter, trade detail. Bloomberg density with Linear polish. | Planned |
| 6 | **Trade replay UI** | Lightweight Charts playback: play/pause/seek/speed, entry/exit/stop/target markers, risk box, P&L animation, indicators, drawings. | Planned |
| 7 | **Strategy builder & compliance engine** | Turns subjective "did I follow my plan?" into a scored, rule-by-rule verdict on every imported trade. | Planned |
| 8 | **Pattern & setup detection** | Unsupervised clustering + hypothesis testing to surface hidden edges and leaks; automatic setup classification. | Planned |
| 9 | **AI coach layer** | Claude as head quant researcher, constrained by a strict evidence contract: it may only cite metrics returned by the analytics engine. | Planned |
| 10 | **What-if simulator** | Counterfactual re-simulation (different stop/target/RR/ATR trail/filters) with recomputed expectancy and significance. | Planned |
| 11 | **ML layer** | Success probability, expected R, optimal stop/target models with proper walk-forward validation and calibration. | Planned |
| 12 | **Reports & scheduling** | Daily → annual reports with leak quantification and expected annual improvement. | Planned |
| 13 | **Hardening & scale** | Partitioning, continuous aggregates, observability, rate limits, RLS, deployment. | Planned |

---

## Milestone 1 — delivered scope

**Why this first.** Trade reconstruction is the single point of failure for the whole
product: if fills are merged into trades incorrectly, every statistic, every AI
insight, and every replay is wrong in a way that is very hard to detect later. So
milestone 1 builds the reconstruction engine as a *pure, exhaustively tested domain
function* with no database or network involved, then wraps it in the persistence and
service scaffolding.

Delivered:

- Monorepo layout with clean-architecture backend (`domain` → `application` →
  `infrastructure` → `interfaces`, dependencies pointing inward only).
- Pure domain layer: instruments, executions, FIFO position/trade reconstruction
  (including position flips and pro-rata commission allocation), R-multiple maths,
  and exchange-aware session resolution.
- Full normalized SQLAlchemy 2.0 schema (32 tables, 22 enum types) with an Alembic
  migration that creates TimescaleDB hypertables when the extension is available and
  falls back to ordinary tables when it is not.
- Repository/unit-of-work ports with SQLAlchemy adapters, and the
  `IngestExecutions` use case (idempotent fill ingestion → trade rebuild).
- FastAPI app: request-id and timing middleware, structured JSON logging, typed
  error envelope, Clerk JWT verification, health/readiness probes, versioned v1 API.
- Dev harness: Docker Compose (TimescaleDB + Redis), Makefile, ruff + mypy (strict),
  111 passing tests, and a CI job that applies the migration history to a real
  TimescaleDB, checks the models have not drifted from it, and rolls back.

**Explicitly not in milestone 1:** any broker network call, any statistic beyond what
a single trade defines, any UI. Those are milestones 2, 3 and 5 — sequenced that way
so each can be reviewed against a stable foundation.

---

## Standing architectural rules

1. **Money is `Decimal`, never `float`.** Prices, P&L, commissions and fees use
   `NUMERIC(20, 8)` in Postgres and `Decimal` in Python, end to end.
2. **Time is `timestamptz`, stored UTC.** Session dates are derived through the
   instrument's exchange timezone, never through the server's locale.
3. **The domain layer imports nothing from infrastructure.** No SQLAlchemy, no
   FastAPI, no `httpx` below `app/domain/`. This is enforced by a test.
4. **Every tenant-owned row carries `user_id`,** and repositories require it — data
   isolation is not left to the caller remembering a filter.
5. **Ingestion is idempotent.** Broker identifiers are natural keys; re-running a
   sync must converge, not duplicate.
6. **Statistics are computed in Python and passed to the model as data.** The AI
   layer receives a metrics payload and may only reference values present in it.
7. **Design for 10⁵–10⁶ trades per user.** Every access path used by the UI is
   indexed; heavy aggregates are materialized, not computed per request.
