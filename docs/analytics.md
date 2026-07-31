# Analytics engine

Every statistic the product reports, how it is defined, and what it refuses to compute.

The whole package (`services/api/app/analytics/`) is pure: no database, no clock unless
injected, no configuration. The same trades and config always produce the same numbers —
which is what makes the AI layer's evidence reproducible and the tests exact.

## Reading a result

Every estimate carries four things:

```json
{
  "value": "33.4858",
  "sample_size": 300,
  "reliability": "reliable",
  "interval": {
    "low": "14.7125", "high": "52.3476",
    "confidence": "0.95", "method": "bootstrap_percentile",
    "excludes_zero": true
  }
}
```

`reliability` is `insufficient` below 5 trades, `provisional` below 30, `reliable`
above. When a statistic cannot be computed, `value` is `null` and `undefined_reason`
explains why in a sentence — not a null the AI layer might paper over.

Decimals are **strings** throughout the payload. A JSON number is a float in every
browser, and this engine spends its entire implementation avoiding floats.

## What is computed

### Core

| Metric | Definition | Notes |
|---|---|---|
| Expectancy | Mean **net** P&L per trade | Always with a bootstrap interval |
| Expectancy R | Mean R-multiple | Trades without a stop are **excluded**, not zeroed; sample size reflects that |
| Win rate | Winners ÷ **decided** trades | Scratches excluded from the denominator — a scratch is not a loss |
| Profit factor | Gross profit ÷ gross loss | `null` with no losses — undefined, not infinite |
| Payoff ratio | Average win ÷ average loss | Always shown beside win rate; 70% at 0.3 payoff is a losing strategy |
| SQN | `sqrt(min(N,100)) × mean(R) / stdev(R)` | Sample multiplier capped at 100 so SQN measures quality, not duration |
| Edge ratio | Mean MFE ÷ mean MAE, in R | Measures the *entry* alone; needs bar data (milestone 4) |
| Cost ratio | Total costs ÷ gross profit | The overtrading number |
| Hold time | Mean duration, split by winners/losers | Losers held longer than winners is the classic management leak |

Mean **and** median are reported for winners and losers: trading distributions are
skewed, and one outsized winner moves only one of them.

### Distribution shape

Standard deviation, skewness and excess kurtosis, with `has_fat_tails` when excess
kurtosis exceeds 1. Reported because every ratio below assumes a shape trading results
do not have — positive skew means trend following, negative means premium selling, and
Sharpe alone cannot tell them apart.

### Drawdown

Trade-indexed (one point per trade) rather than calendar-indexed, so depth is comparable
between someone taking two trades a day and someone taking twenty. Max, average, current,
time underwater, longest decline and longest recovery, plus every individual drawdown
period with its trough and recovery point. `max_daily_drawdown` is computed separately on
the daily series — that is the one prop firms enforce.

Percentage drawdown is `null` when the preceding peak was not positive. A percentage
from a peak of zero is meaningless.

**Sign convention.** Summary depths — `max_drawdown`, `average_drawdown`,
`current_drawdown` — are **positive magnitudes**. The per-point series
(`EquityPoint.drawdown`, and the `drawdown` column on `equity_curve_points`) is
**signed negative**, because it is a chart series drawn below the zero line. The two
are deliberately different and must not be reconciled into one: a summary reporting
`max_drawdown: 1200` beside `current_drawdown: -1200` describes a single situation with
opposite signs, and anything comparing them ("are we at the worst point ever?") gets the
answer backwards.

### Risk-adjusted

**Sharpe and Sortino are computed on the daily series, never per trade.** A per-trade
"Sharpe" has dispersion-per-*trade* in the denominator, so it rises when you trade less,
independent of performance. Both are annualised at 252 trading days and refuse to compute
below 20 daily observations — annualising a good week multiplies noise by √252 and calls
it a year.

Each carries `basis`:

- `return` — an equity base was supplied; comparable with published figures.
- `pnl` — dollar-denominated, internally consistent, **not** comparable outside.

Sortino uses downside deviation with a zero target, summed over the full sample count.
The gap between Sharpe and Sortino is informative: it is the amount Sharpe penalises a
strategy for its *winners*.

MAR (annualised return ÷ max drawdown) needs both an equity base and a real drawdown.

### Position sizing and ruin

Kelly is reported as `full`, `half`, and a list of `warnings`. Full Kelly is
growth-optimal only if the estimated edge is exactly right, which it never is —
estimation error makes it reliably over-aggressive. Warnings fire below 100 trades and
above a 25% fraction.

**Risk of ruin is simulated, not solved.** The textbook formula assumes fixed-size binary
outcomes; real distributions are continuous with fat tails, and it is exactly the tail
the formula assumes away that causes ruin. Resampling the trader's own results keeps the
real distribution, outliers included.

Monte Carlo resamples with replacement, which **destroys sequence** — it assumes trades
are independent draws. Losing streaks cluster through tilt and regime, so ruin estimates
are, if anything, optimistic. That is stated in the stored `assumptions` rather than
silently corrected.

### Segmentation

Ten dimensions: hour of day, weekday, month, session segment, direction, instrument,
strategy, setup, market condition, duration bucket. Adding one is a dictionary entry in
`SEGMENT_DIMENSIONS`, not a schema change.

Each segment is tested by permutation against **the rest of the sample** (24 tests for
hours, not the 276 that pairwise comparison would need), and the family is
Benjamini–Hochberg FDR-adjusted.

A segment is `is_actionable` only when **all three** hold:

1. `reliability == reliable` (≥ 30 trades)
2. Its expectancy interval excludes zero
3. Adjusted p-value < 0.05

`best` and `worst` rank actionable segments only. On a scan over noise, `best` is
`null` — not the luckiest bucket. See [ADR 0004](./adr/0004-analytics-honesty.md).

### Streak state

Expectancy conditioned on the preceding run of wins or losses. This is tilt, measured:
if expectancy after two consecutive losses sits materially below baseline, decision
quality is degrading with the streak — one of the few genuinely behavioural findings a
journal can produce, and invisible in every aggregate.

## Conventions that change the answer

| Choice | This engine | Why |
|---|---|---|
| Net vs gross | **Net**, everywhere | Gross flatters; the gap is where overtrading hides |
| Scratches | Own category | Folding them either way shifts the win rate |
| Missing R | Excluded | Zeroing drags the mean and describes a strategy nobody traded |
| Variance | Sample (n−1) | Population understates dispersion and flatters every ratio built on it |
| Percentiles | Type-7, interpolated | Matches NumPy and R |
| Undefined ratios | `null` + reason | Never infinity, never zero |
| Confidence intervals | Non-parametric bootstrap | Trade P&L is skewed and fat-tailed |
| Randomness | Seeded (`20260731`) | An interval that moves between page loads is not trustworthy |

## Performance

Exact decimal arithmetic is roughly an order of magnitude slower than float. Resampling
sidesteps this by converting once to integers at 10⁻⁸ — the storage scale, so nothing is
lost — and doing all iterations in exact integer arithmetic.

A full report over 300 trades (10,000 bootstrap iterations, 5,000 Monte Carlo paths,
10 dimensions with permutation testing) takes a few seconds. `AnalyticsConfig` trades
accuracy for speed; the `light` preset used by the interactive endpoint disables
segmentation and significance testing, which means every segment reports
`is_actionable = false` — less informative, never misleading.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/analytics/performance` | Full report. `starting_equity` unlocks Sharpe-as-return, MAR and risk of ruin |
| `GET /api/v1/analytics/segments/{dimension}` | One dimension with significance control |
| `GET /api/v1/analytics/dimensions` | Available dimensions |
| `GET /api/v1/analytics/equity-curve` | Curve points with drawdown at each step |

## Storage

- `performance_metrics` — one row per slice per period; the full payload in JSONB with
  `expectancy`, `win_rate`, `profit_factor` and `net_pnl` promoted to columns for
  sorting. Upserted, so recomputation converges.
- `equity_curve_points` — hypertable; replaced wholesale on rebuild, because a corrected
  trade can renumber the curve.
- `risk_metrics` — forward-looking estimates **with their assumptions**. A risk-of-ruin
  figure without its horizon, threshold and iteration count cannot be interpreted.

`engine_version` is stamped on every row. When a formula changes it is bumped, making
stale rows identifiable and recomputable rather than silently mixed with new ones.

## For the AI layer (milestone 9)

`AnalyticsReport.to_payload()` is the model's entire evidence base, and
`metric_keys()` enumerates every citable dotted key (`core.win_rate`,
`drawdown.max`, `risk.risk_of_ruin`, …). A claim referencing a key absent from that set
is rejected before the analysis is stored. See
[ADR 0002](./adr/0002-ai-evidence-contract.md).
