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
| 2 | **Tradovate integration & sync pipeline** | Zero manual journaling is the core promise. Token lifecycle, REST backfill, WebSocket live fills, idempotent ingestion, safe sync cursors, reconciliation against broker P/L. | ✅ Complete |
| 3 | **Analytics engine** | Every AI claim must trace to a deterministic Python number. Expectancy, profit factor, Sharpe/Sortino, SQN, Kelly, risk of ruin, drawdown, edge ratio, Monte Carlo, bootstrap confidence intervals, the full segmentation cube, and the significance control that stops it manufacturing edges. | ✅ Complete |
| 4 | **Market data & replay engine** | Replay and MAE/MFE need bar data. TimescaleDB hypertables, bar ingestion, replay window computation, marker generation, S3 snapshot pipeline. | ⏳ Next |
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

## Milestone 2 — delivered scope

**Why this second.** Milestone 1 built a reconstruction engine with nothing to feed it.
Until real fills arrive, every downstream milestone would be built and validated against
synthetic data — and synthetic data never contains the cases that break things: a fill
whose order has not settled, a contract nobody seeded, a stream that drops mid-session.

Delivered:

- Tradovate REST client: token lifecycle including the `p-ticket` time-penalty protocol,
  conservative token-bucket rate limiting, batched entity joins, and Decimal-safe JSON
  parsing so a price never passes through a float.
- Mapping layer that refuses to guess. A fill without its order, its contract
  specification, or (within a grace period) its fee record is deferred with a reason,
  never approximated.
- `SyncBrokerAccount`: cursor-safe orchestration that never advances past a fill it did
  not ingest — see [ADR 0003](./adr/0003-broker-sync.md).
- Real-time WebSocket connection: SockJS-derived frame codec, unconditional heartbeats,
  request/response correlation, reconnect with jittered backoff, and a mandatory REST
  catch-up on every reconnect.
- Secret-store abstraction. Broker credentials are never written to the database.
- `broker_instrument_map` and `broker_connections.external_user_id` (migration 0002).
- Reconciliation of our reconstructed P&L against the broker's own realized figure.
- HTTP endpoints to link a broker login, list connections, sync on demand, and read the
  sync audit trail.
- 126 new tests, including 6 database-backed integration tests that run the real
  pipeline end to end against a mocked Tradovate API.

**Explicitly not in milestone 2:** scheduled background syncing (needs the worker chosen
in milestone 13), market data ingestion (milestone 4), and any statistic beyond what a
single trade defines (milestone 3).

**Not yet verified against a live Tradovate account** — the integration is built from the
published specification and tested against a mocked API. See
[the integration notes](./tradovate-integration.md) for what that leaves open.

---

## Milestone 3 — delivered scope

**Why this third.** Milestones 1 and 2 produce trustworthy trades. This is where they
become answers. It also has to come before the AI layer: ADR 0002 says the model may only
cite computed statistics, and that requires the computed set to exist first as a
concrete, enumerable object.

Delivered:

- Pure analytics package in exact decimal arithmetic — no floats anywhere, including
  square roots, using Python's `decimal` module.
- Core metrics: expectancy (with interval), expectancy in R, win rate, profit factor,
  payoff ratio, SQN, edge ratio, cost ratio, hold time split by outcome, and full
  distribution shape including skew and kurtosis.
- Equity curve, drawdown periods with recovery, streak analysis, daily series.
- Sharpe and Sortino computed on the **daily** series with an explicit basis flag; MAR;
  Kelly with half-Kelly and warnings.
- Bootstrap confidence intervals, Monte Carlo simulation, simulation-based risk of ruin,
  probability of profit — all seeded and reproducible, resampled in scaled integers.
- Permutation testing with Benjamini–Hochberg FDR control, and a segmentation cube over
  ten dimensions where nothing is a finding until it survives the scan
  ([ADR 0004](./adr/0004-analytics-honesty.md)).
- `AnalyticsReport.to_payload()` / `metric_keys()` — the AI layer's evidence base and
  its citable allow-list.
- Persistence to `performance_metrics`, `equity_curve_points` and `risk_metrics`, plus
  four HTTP endpoints.
- 169 new tests, expected values computed by hand, including two that assert the engine
  finds *nothing* in pure noise.

**Explicitly not in milestone 3:** MAE/MFE and edge ratio return undefined until
milestone 4 supplies bar data; scheduled recomputation waits for the worker in
milestone 13.

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
