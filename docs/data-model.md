# Data model

33 tables across seven concerns. Defined in `services/api/app/infrastructure/db/models/`,
created by migrations `0001_initial_schema` and `0002_broker_sync`.

## Conventions

| Concern | Decision | Why |
|---|---|---|
| Primary keys | UUIDv7 | Time-ordered (index appends rather than scatters), client-generatable, non-enumerable |
| Money & prices | `NUMERIC(20, 8)` → `Decimal` | Floats lose reconciliation against broker statements |
| Timestamps | `timestamptz`, stored UTC | Session dates derive from the exchange timezone, not the server's |
| Tenancy | `user_id` on every owned row | Single-predicate isolation; prerequisite for RLS |
| Enums | Native Postgres types | Self-documenting, rejects typos at write time; adding a variant is an explicit migration |
| Deletion | Soft (`deleted_at`) for journals, notes, strategies, setups | Trades reference them; hard deletion would orphan rows or rewrite history |
| Naming | Explicit constraint naming convention | Stable, revertible Alembic migrations |

## Identity

| Table | Purpose |
|---|---|
| `users` | Local user, joined to Clerk by `clerk_user_id`. Keeps the identity provider swappable and joins on indexed UUIDs. |
| `user_settings` | Preferences, saved dashboard layouts, default risk fraction. Split out so hot identity reads stay narrow. |

## Broker connectivity

| Table | Purpose |
|---|---|
| `broker_connections` | One per broker login. Holds `credential_ref` — a **pointer into a secret manager**, never a token — plus per-endpoint `sync_cursor` JSONB. |
| `accounts` | Trading accounts. Prop-firm fields (`max_daily_loss`, `max_drawdown`, `profit_target`) feed the compliance engine. |
| `account_balance_snapshots` | Broker-reported equity over time. Deliberately independent of our reconstructed P&L: the disagreement between the two is how a reconstruction bug is caught before the trader finds it. |
| `sync_runs` | Audit trail per sync attempt, including what was deferred and why. Without it, "why is yesterday missing?" is unanswerable. |
| `broker_instrument_map` | Translates a broker's own instrument identifier to ours. Keyed by `(broker, environment, external_id)` because Tradovate's demo and live environments assign different contract ids to the same contract. |

## Instruments

`instruments` is **global, not user-scoped** — ES is ES for everyone, and per-tenant
copies would let two users' P&L disagree about what a point is worth. Carries
`tick_size`, `tick_value`, `exchange_timezone` and `session_rollover`; `root_symbol`
lets statistics group across quarterly rolls instead of fragmenting into unusable
samples.

## The write path

| Table | Purpose |
|---|---|
| `orders` | Broker orders. Retained because *intent* is evidence: stop and target orders give the journal a trade's planned risk with no manual input, and the working-stop history turns "you move your stops" from an accusation into a measurement. |
| `executions` | Fills. Immutable, append-only, unique on `(account_id, broker_execution_id)`. The system's unit of truth. |
| `trades` | Round turns. 58 columns in four groups — see below. |
| `trade_executions` | Trade↔fill association with per-leg quantity, price and **pro-rata cost allocation**. Not a plain FK on `executions`, because one fill can belong to two trades when a position flips. |
| `positions` | Live position per `(account, instrument)`, including `open_lots` JSONB — the resumable FIFO state that keeps incremental sync O(new fills). |
| `trading_sessions` | Daily rollup per `(account, session_date)`. ~250 rows/year versus tens of thousands of trades; the calendar heatmap reads this. |

### The four column groups on `trades`

1. **Reconstructed** — direction, status, prices, quantities, realized points, gross,
   commission, fees, net. Derived from fills; **never hand-edited**. There is no API to
   change them: a hand-edited trade is a statistic that no longer describes anything
   that happened.
2. **Planned** — initial/final stop, target, planned risk and reward, R multiples.
   Inferred from working orders where possible.
3. **Context** — strategy, setup, market condition, confidence, emotion, notes.
   Auto-classified and correctable by the trader. `setup_source` records whether a
   label came from the classifier or the human, so the classifier can be evaluated.
4. **Scored** — execution quality, discipline, compliance. Computed by the rule engine,
   never by the language model.

Session attribution (`session_date`, `session_segment`, `entry_hour`, `entry_weekday`)
is denormalised onto the row at write time so that hourly and weekday breakdowns read a
`smallint` instead of re-deriving timezones across a million rows on every dashboard
load.

`reconstruction_version` lets a corrected engine identify exactly which trades need
rebuilding, rather than reprocessing everything or guessing.

## Strategy and compliance

| Table | Purpose |
|---|---|
| `strategies` | **Versioned.** Changing rules creates a new version rather than editing the old one, so historical compliance scores stay reproducible against the rules that were actually in force. |
| `strategy_rules` | Machine-evaluable rules. `expression` holds a declarative predicate **AST, not code** — inspectable, diffable between versions, and never executed. |
| `rule_evaluations` | Per-trade, per-rule verdict. `evaluable` distinguishes "rule was broken" from "rule could not be checked" (no stop recorded, no bar data) — conflating them would manufacture violations. |
| `setups` | Named patterns, system-provided or unlimited custom. |
| `tags` / `trade_tags` | Free-form labelling, `source` marks auto vs manual. |
| `market_conditions` | Regime. The most common confounder in trading statistics — a setup that "stopped working" has usually just met a different regime — so it is stored explicitly and controlled for rather than averaged across. |

## Journal and media

| Table | Purpose |
|---|---|
| `daily_journals` | Plan, review, mood, sleep, focus. The small amount of context that cannot be imported. |
| `notes` | Polymorphic by `(entity_type, entity_id)`; notes will hang off trades, sessions, strategies and reports. |
| `screenshots` | Automatic captures at six moments per trade (before entry, entry, exit, after exit, higher timeframe, execution timeframe). S3 key plus enough metadata to render a gallery without fetching anything. |

## Analytics

| Table | Purpose |
|---|---|
| `performance_metrics` | Metric sets keyed by `(scope, scope_key, period, engine_version)` with a JSONB payload. Chosen over wide columns because the set of interesting breakdowns grows continuously, and a schema where a new breakdown means a migration is one that stops being extended. Frequently sorted values (`net_pnl`, `expectancy`, `win_rate`, `profit_factor`) are promoted to real columns. |
| `equity_curve_points` | Cumulative P&L, R and drawdown after each trade. Hypertable; composite PK `(user_id, account_id, recorded_at)` doubles as the index the equity chart scans. |
| `risk_metrics` | Kelly, risk of ruin, VaR, drawdown, Monte Carlo output — with the `assumptions` that produced them, because a forward-looking estimate without its assumptions is not interpretable. |
| `reports` | Daily → annual and compliance reports, discriminated by `report_type`. One table rather than `weekly_reports` + `compliance_reports`: identical columns, indexes and lifecycle, and splitting them would multiply the query surface without buying a constraint the enum does not already give. |

## AI

| Table | Purpose |
|---|---|
| `ai_analyses` | `input_metrics` (the exact deterministic payload the model saw), `output`, `model`, `prompt_version`, `evidence_validated`, tokens and cost. The evidence contract is a schema, not a request. |
| `ai_recommendations` | Individual recommendations with evidence, Python-computed `expected_improvement`, and a `status` lifecycle — so the platform can answer whether acting on advice actually changed the numbers. |
| `detected_patterns` | Behavioural and statistical findings, each carrying `effect_size`, `p_value`, confidence bounds and `sample_size`. Below threshold, `is_significant = false` and it is shown as an observation, never a finding. This is the difference between finding edges and manufacturing them from noise. |

## Market data and replay

| Table | Purpose |
|---|---|
| `market_bars` | OHLCV. The only genuinely large table: one instrument-minute-year is ~350k rows. Hypertable chunked weekly, compressed after 30 days, PK `(instrument_id, timeframe, ts)` — exactly the prefix replay queries use. Global, not per-tenant. `source` is recorded because a trader replaying a trade must see the feed they traded on. |
| `replay_metadata` | Window, markers, indicators and drawings per trade. Stored rather than derived at render time so a trade reviewed today and again in a year shows the same chart. |

## TimescaleDB is optional

The initial migration checks `pg_available_extensions` and only creates hypertables and
compression policies when TimescaleDB is present. On stock Postgres the same tables
exist with the same constraints and the same query semantics — only the partitioning
differs. Local development and CI therefore need no special image, while production
gets chunked storage.

## Enum types

22 native Postgres enums, created explicitly and once at the top of the initial
migration; every column declares `create_type=False` against them. Several are shared
across tables (`order_side` by orders and executions, `timeframe` by bars, screenshots
and replays), and implicit creation would attempt each type more than once.

Adding a variant requires an `ALTER TYPE ... ADD VALUE` migration. That friction is
intentional: these values appear in analytics group-bys and in the persisted contract.
