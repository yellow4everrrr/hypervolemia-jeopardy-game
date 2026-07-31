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
| 4 | **Market data & replay engine** | Replay and MAE/MFE need bar data. TimescaleDB hypertables, bar ingestion, replay window computation, marker generation, S3 snapshot pipeline. | ✅ Complete |
| 5 | **Frontend foundation** | Next.js + Clerk + design system, dashboard, trade blotter, trade detail. Bloomberg density with Linear polish. | Planned (after 12) |
| 6 | **Trade replay UI** | Lightweight Charts playback: play/pause/seek/speed, entry/exit/stop/target markers, risk box, P&L animation, indicators, drawings. | Planned (after 12) |
| 7 | **Strategy builder & compliance engine** | Turns subjective "did I follow my plan?" into a scored, rule-by-rule verdict on every imported trade. | ✅ Complete |
| 8 | **Pattern & setup detection** | Unsupervised clustering + hypothesis testing to surface hidden edges and leaks; automatic setup classification. | ✅ Complete |
| 9 | **AI coach layer** | Claude as head quant researcher, constrained by a strict evidence contract: it may only cite metrics returned by the analytics engine. | ⏳ Next |
| 10 | **What-if simulator** | Counterfactual re-simulation (different stop/target/RR/ATR trail/filters) with recomputed expectancy and significance. | Planned |
| 11 | **ML layer** | Success probability, expected R, optimal stop/target models with proper walk-forward validation and calibration. | Planned |
| 12 | **Reports & scheduling** | Daily → annual reports with leak quantification and expected annual improvement. | Planned |
| 13 | **Hardening & scale** | Partitioning, continuous aggregates, observability, rate limits, RLS, deployment. | Planned |

### A note on ordering

The backend milestones (7–12) are being built before the frontend ones (5–6), which is
a deliberate departure from the numbering above.

The reason is that 7–12 compound on each other and on the analytics engine, while 5 and
6 consume an API. Building the UI against a half-finished API means building it twice:
the compliance panel, the pattern list, the coach transcript and the what-if controls
each change the shape of the trade detail view, and a frontend written before those
exist would be refactored on every backend milestone. The frontend milestones are
unchanged in scope — only in sequence — and the numbering is kept so existing
references stay valid.

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

## Milestone 4 — delivered scope

**Why this fourth.** Two of milestone 3's metrics — MAE/MFE and edge ratio — were
returning undefined because nothing supplied prices *between* entry and exit. Replay
needs the same data. Building both on one bar pipeline avoids two ingestion paths that
would inevitably disagree about what a candle is.

Delivered:

- `BarSeries` with exact-decimal OHLCV bars, `covering()` window selection that includes
  the bar a trade opened inside, and on-the-fly `resample` to coarser timeframes.
- Excursion computation from highs and lows rather than closes — a stop is hit by the
  low, not by the close — feeding `mae_r`, `mfe_r` and `edge_ratio` back onto trades.
- `sequence_ambiguous()`: when a single bar contains both the excursion extremes, the
  order of events inside it is unknowable, and the result is flagged rather than assumed.
- Replay window computation with automatic timeframe choice, entry/exit/scale markers,
  and a risk box that is `None` when no stop was recorded.
- TimescaleDB `market_bars` hypertable, bar repository, and an S3-compatible object
  store with content-addressed screenshot keys.

**Explicitly not in milestone 4:** the replay *UI* (milestone 6) and automatic screenshot
capture, which needs a headless renderer scheduled by the worker in milestone 13.

---

## Milestone 7 — delivered scope

**Why this now.** Every metric so far answers "how did I do?". None answers "did I do
what I said I would?" — and for most traders the gap between those two questions is
where the money goes. It also has to precede the AI layer: a coach that can point at a
specific broken rule and what it cost is giving evidence, and one that cannot is giving
opinion.

Delivered:

- A declarative predicate AST for rules ([ADR 0005](./adr/0005-compliance-scoring.md)).
  Rules are stored as JSON trees and interpreted; nothing user-supplied is ever executed.
- **Three-valued evaluation** — pass, fail, or *unevaluable*. A rule about stop placement
  cannot be checked on a trade with no recorded stop, and scoring that as a violation
  would manufacture indiscipline out of missing data.
- Severity-weighted scoring where unevaluable rules are excluded from the denominator
  and reported as coverage, plus a ceiling that stops one critical breach being averaged
  away by nine passed advisories.
- A 30-field rule vocabulary published over HTTP, so the rule builder renders the
  engine's own contract and a typo is rejected at save time rather than reading as
  unevaluable forever.
- Session-aware context: sequence-sensitive rules ("at most six trades a session",
  "wait ten minutes after two losses") are evaluated against the state of the day as it
  was *immediately before* each trade.
- Rule impact measurement — what breaking a rule has historically been associated with,
  labelled as association rather than causation in the payload itself.
- Versioned strategies: revising rules creates a new version, so past compliance scores
  remain reproducible against the rules that were actually in force.
- Eight starter rules, every one checkable from data the journal imports automatically.
- 61 new tests, including database-backed proof that recomputation converges and that an
  unevaluable rule is never stored as a failure.

**Explicitly not in milestone 7:** automatic setup classification (milestone 8) and any
natural-language commentary on a compliance report (milestone 9).

---

## Milestone 8 — delivered scope

**Why this now.** The analytics engine answers questions that are put to it. This
milestone asks the questions — it looks for leaks the trader has not thought to check
for. It must precede the AI layer for the same reason milestone 3 did: a coach that can
point at a specific quantified leak is giving evidence, and one that cannot is giving
opinion.

Delivered:

- Seven **behavioural detectors** covering sequence-dependent leaks the segmentation cube
  structurally cannot see: revenge trading, overtrading, trading while down, size
  escalation, losing streaks, holding asymmetry, and opening-trade performance.
- Deterministic **k-means** over a documented eight-feature trade space, in exact decimal
  arithmetic, with k-means++ seeding and k chosen by silhouette.
- A **null-reference structure test** for clustering: k-means partitions anything, so the
  winning clustering must beat column-shuffled references before it is reported at all
  ([ADR 0006](./adr/0006-pattern-detection.md)).
- **One FDR family across the entire scan** — behavioural and cluster tests corrected
  together, so adding a detector makes existing findings harder to establish.
- Cluster descriptions in the trader's own vocabulary, and cluster-to-setup proposals
  carrying per-trade confidence. Nothing is auto-applied.
- Persistence of **every test performed**, not only the survivors, so recurrence is
  falsifiable.
- 40 new tests, led by a null battery: five seeds of pure noise through the full scan,
  asserting nothing is found.

**Two defects this milestone found in itself,** both invisible in the output and both
producing confident, plausible findings — recorded here because they are the reason the
null battery exists:

1. A detector whose split was a function of the value it compared returned the minimum
   possible p-value on 100% of noise samples, and — through the step-up FDR procedure —
   dragged its honest neighbours over the significance line with it.
2. A fixed silhouette threshold accepted the six-cluster partition k-means imposes on
   uniformly random data.

**Explicitly not in milestone 8:** natural-language interpretation of a pattern
(milestone 9), and scheduled rescanning (milestone 13).

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
