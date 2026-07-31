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
| 9 | **AI coach layer** | Claude as head quant researcher, constrained by a strict evidence contract: it may only cite metrics returned by the analytics engine. | ✅ Complete |
| 10 | **What-if simulator** | Counterfactual re-simulation (different stop/target/RR/ATR trail/filters) with recomputed expectancy and significance. | ✅ Complete |
| 11 | **ML layer** | Success probability and expected R with walk-forward validation, calibration and a refusal-to-serve gate. Optimal stop/target deferred — see below. | ✅ Complete |
| 12 | **Reports & scheduling** | Daily → annual reports with leak quantification and expected annual improvement. | ⏳ Next |
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

## Milestone 9 — delivered scope

**Why this now.** Every prior milestone produces numbers. This is where they become
advice — and where the product's single largest credibility risk lives. A fluent,
confident sentence containing a number nobody computed is indistinguishable, to a
reader, from a real one, and a trader who acts on it and loses money has been harmed by
the product.

Delivered:

- **The coach never writes a number.** It writes `{{metric.key}}` placeholders and
  Python substitutes the computed value ([ADR 0007](./adr/0007-coach-placeholders.md)).
  The figure a trader reads is the computed figure *by construction*, not because a
  checker caught a bad one afterwards.
- An `EvidenceBundle` carrying each statistic with its sample size, confidence interval,
  reliability, and — when undefined — the reason. Gaps are recorded explicitly, so the
  model reports an absence rather than guessing at it.
- A validator that rejects unknown keys, bare numerals that match no computed value,
  invented precision, recommendations built on undefined statistics, and uncited claims.
  Rejection is all-or-nothing; rejected analyses are stored so the rejection rate stays
  visible.
- Three claim tiers — `finding` / `observation` / `hypothesis` — with the prompt
  prescribing which language each permits, so an untested observation cannot be phrased
  as established.
- `GET /coach/evidence` publishes the bundle with no model call: the claim that the coach
  only interprets computed statistics is checkable by the trader, not just by us.
- Recommendation tracking (`accepted` / `dismissed` / `resolved`), because the only
  question that matters about coaching is whether acting on it changed the numbers.
- 36 new tests, written adversarially — every one plays a model that has invented
  something. `app/ai/` is barred from importing the Anthropic SDK by an architecture
  test, so all of them run with no API key.

**Explicitly not in milestone 9:** `ai_recommendations.expected_improvement` stays empty
until the what-if simulator fills it. The model never writes an expected-improvement
figure — that was ADR 0002's fourth mechanism and it remains intact.

---

## Milestone 10 — delivered scope

**Why this now.** It closes ADR 0002's fourth mechanism. The coach can say a rule is
costing the trader money; until this milestone nothing could compute what changing it
would be worth, and `ai_recommendations.expected_improvement` sat empty by design rather
than by omission.

Delivered:

- Counterfactual re-pricing over stop, target, session-cap, losing-streak and
  hour filters, walked in session order because the filters are sequence-dependent.
- A skipped trade advances neither the session counter nor the losing streak — the
  counterfactual trader never took it, and advancing them would simulate someone who
  took the trade and ignored the result.
- Trades the scenario cannot apply to are marked **inapplicable and excluded**, with
  `coverage` reporting the fraction actually simulated. Counting them as unchanged
  would dilute every effect toward zero and make each counterfactual look safe.
- The whole sweep FDR-corrected as one family; `best` returns the largest *established*
  improvement, never the largest improvement.
- `POST /simulator/quantify` fills `expected_improvement` from a real re-simulation,
  and writes an explicit non-result with its reason when it cannot.
- 32 new tests.

**Choosing the null took three attempts**, and the first two each produced confident,
wrong findings — recorded here because the reasoning generalises:

1. A **two-sample permutation test** treats baseline and simulated as independent draws.
   They are the same trades measured twice, so pooling them inflates the reference
   variance with duplicates the design never contained.
2. A **sign-flip paired test** assumes each difference's sign is arbitrary under the
   null. A 2R target deterministically produces its deltas, so that null is trivially
   false the moment the rule touches one trade — the test then returns "significant" for
   every scenario, which is no information.
3. A **bootstrap of the per-trade deltas** asks the question actually being posed: would
   this improvement survive a different sample of trades? It is what caught a scenario
   reporting a $1 difference over 240 trades as significant.

Two synthetic-data generators also had to be discarded before the null battery was
honest — one allowed a losing trade to record an impossible favourable excursion, the
other made adverse excursion a function of the outcome so that tightening the stop was
guaranteed to help. The battery now uses a driftless random walk, where the optional
stopping theorem says no exit rule can have an edge.

The reasoning is recorded in
[ADR 0008](./adr/0008-counterfactual-simulation.md), and the working guide is
[docs/what-if.md](./what-if.md).

**Explicitly not in milestone 10:** ATR-based trailing stops, which need bar-by-bar
paths rather than the excursion summary; and portfolio-level simulation across accounts.

---

## Milestone 11 — delivered scope

**Why this now.** Everything before it describes the past. This one makes a claim about a
trade that has not happened, and a trader will *size on that number* — which makes it the
surface where being wrong is most expensive, and the one where the default answer has to
be no.

Delivered:

- Two heads — win probability (ridge logistic) and expected R (ridge linear) — each
  trained, validated and gated independently, so a trader who records no stops still gets
  a win-rate model.
- **A structural leakage barrier.** `EntrySnapshot` carries only what was knowable at
  entry and has no outcome fields at all, so a feature extractor written against it
  cannot reach `net_pnl`, `r_multiple`, `mfe_r`, `mae_r` or `duration_seconds` — not by
  accident, not by refactor. Sequence context (trades so far today, session P&L, losing
  streak) is filled as of *before* each trade, which is the second and subtler leak.
- Walk-forward validation that splits on **session boundaries**, never inside a day, with
  the ridge penalty chosen by a nested forward split of the training window only.
- Calibration against a parametric bootstrap null, a Brier skill score against the
  trader's own base rate, and deliberately **no accuracy, F1 or ROC-AUC** — all of them
  are insensitive to calibration, which is the entire question.
- Refusals as first-class stored results with plain-language reasons, plus a `CHECK`
  constraint making a row that is both deployable and refused impossible.
- Pure-Decimal Newton fitting and a hand-written linear solver: no new dependency, and a
  stored model reproduces its predictions regardless of platform `libm`.
- 100 new tests, migration `0003_prediction_models`.

**The skill gate was wrong on the first attempt**, and it is recorded because the failure
generalises. The original rule was `skill > 0`: serve the model if it beat the baseline
out of sample. Probing it on ten histories whose outcomes were *independent of every
feature* produced skill scores scattered around zero — and one landed at **+0.014**, which
under that rule was deployable. A trader would have been shown per-trade win
probabilities computed from pure noise, with nothing marking them as such.

That is the same defect ADR 0006 and ADR 0008 each found in their own domain: a point
estimate compared against a threshold, with nothing said about how far it would move on a
different sample. Skill now carries a **session-block bootstrap interval** and deployment
requires it to exclude zero — sessions rather than trades, because resampling correlated
trades individually would have produced an interval half the width it should be and
hidden this exact false positive. After the change: **0 false deployments in 40 runs**,
with the positive control still deploying.

The reasoning is recorded in [ADR 0009](./adr/0009-predictive-models.md), and the working
guide is [docs/ml.md](./ml.md).

**Explicitly not in milestone 11:** categorical features (one-hot overfits at this sample
size and target encoding leaks unless recomputed per fold — segmentation already compares
setups with a proper test); and optimal stop/target models, which need a grid search
selected on training folds and measured out of sample. Shipping the naive version of the
latter would undo ADR 0008.

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
