# ADR 0004 — Statistics report their uncertainty, and unproven segments are never findings

- **Status:** Accepted
- **Date:** 2026-07-31
- **Milestone:** 3

## Context

The analytics engine is where this product either earns trust or destroys it. Every
trading journal on the market computes expectancy, win rate and a per-segment
breakdown. The difference between a useful one and a harmful one is not which metrics
it computes — it is whether it distinguishes signal from noise before presenting a
number as a fact.

Three specific failure modes, all of which are the *default* behaviour if nobody
designs against them:

**1. Point estimates without uncertainty.** A trader with 60 trades and a $47
expectancy is shown "$47". The 95% interval might run from −$12 to +$104. The point
estimate and the interval support completely different decisions, and only one of them
is displayed.

**2. Segment scans that manufacture edges.** Slicing 500 trades by hour of day yields 24
buckets of ~20 trades. Every bucket shows a different expectancy because 20 trades
cannot estimate one. The best-looking hour will look excellent. Presenting it as "your
best hour" is reporting the loudest coincidence in a random sample — and the trader then
concentrates real money into it. At 5% significance across the full cube (hours ×
weekdays × setups × conditions, easily 7,000 comparisons) roughly 350 findings appear
from pure noise.

**3. Undefined statistics reported as numbers.** Profit factor with no losing trades is
undefined. Reporting it as infinity, or as zero, or omitting the row, all mislead. R
expectancy over trades with no recorded stop is undefined; treating missing R as 0.0
drags the average toward zero and describes a strategy nobody traded.

## Decision

**Every statistic reports its sample size. Every estimate that can carry uncertainty
does. Nothing is a finding until it survives a significance test adjusted for the size
of the scan that produced it.**

Concretely:

- `Estimate` carries `value`, `sample_size`, `interval` and `reliability`. Undefined
  results carry an `undefined_reason` in plain language — a sentence the AI layer can
  quote, rather than a null it might paper over.
- Confidence intervals are **non-parametric bootstrap** intervals, resampled from the
  actual trades. Normal-theory intervals are wrong in the direction that matters on
  distributions with an occasional 8R winner.
- `Reliability` gates presentation at 5 trades (nothing computed) and 30 (provisional
  vs reliable).
- Segments are tested by **permutation** against the rest of the sample, and the whole
  family is **Benjamini–Hochberg FDR-adjusted**. `ComparisonResult.is_significant`
  returns `None` — refusing to answer — when a result has not been adjusted.
- `Segment.is_actionable` requires all three: a reliable sample, an interval excluding
  zero, and adjusted significance. `DimensionAnalysis.best` ranks only actionable
  segments, so the "best hour" of a noise scan is `None`, not the luckiest bucket.
- Effect size (Cohen's *d*) is reported next to every p-value, because with enough
  trades a trivial difference becomes significant.
- All resampling is **seeded**. The same trades produce the same interval on every run;
  an interval that shifts between page loads makes every number beside it look arbitrary.

Two supporting choices:

**Exact decimal arithmetic throughout,** including `sqrt` via Python's `decimal` module.
Converting to `float` for a square root would reintroduce imprecision inside the code
that decides whether a trader has an edge. Resampling sidesteps the performance cost by
working in scaled integers at 10⁻⁸ — the storage scale, so nothing is lost.

**Ratios declare their basis.** Sharpe is computed on the *daily* series, never per
trade (a per-trade "Sharpe" rises when you trade less, independent of performance), and
carries `basis="return"` or `basis="pnl"` depending on whether an equity base was
supplied. Only the former is comparable with any published figure.

## Consequences

**Good.** The product can say "this sample cannot distinguish your strategy from
breakeven" — which is often the true answer and which no competitor says. The
segmentation cube becomes safe to expose: a scan over noise surfaces nothing, verified
by `test_scanning_pure_noise_produces_no_significant_findings` and
`test_segments_from_noise_are_not_actionable`. And the AI layer inherits all of it for
free: the payload it receives already contains intervals, sample sizes and reliability
flags, so an evidence-grounded claim is the easy path.

**Costs.** The product will frequently tell users it does not know. A journal that
confidently names a best setup after 40 trades feels more useful than one that says the
sample is too thin — right up until the user sizes into a phantom edge. This is a
deliberate trade of perceived usefulness for actual usefulness.

**Runtime.** A full report with segmentation and permutation testing takes seconds, not
milliseconds. Mitigated by a `light` configuration for interactive rendering — which
disables significance testing and therefore reports *every* segment as non-actionable,
so a fast render is less informative but never misleading. Milestone 13 moves the heavy
path to a scheduled job writing `performance_metrics`.

**FDR is not a guarantee of zero false positives.** Benjamini–Hochberg bounds the
expected *proportion* of false discoveries across many scans; an individual scan can
still surface one. This is documented in the tests rather than papered over — the
alternative, Bonferroni, is so conservative on thousands of comparisons that it would
reject genuine edges alongside the noise.

## Alternatives rejected

- **Parametric confidence intervals** (t-distribution). Cheaper and would remove the
  need for seeded resampling, but assumes a symmetry that trade P&L does not have.
- **Bonferroni correction.** Simpler and stricter, but at cube scale its threshold
  approaches zero and nothing real would ever surface.
- **Hiding thin segments entirely.** Rejected because a trader seeing 24 hours with 6
  missing would assume those hours are untraded rather than under-sampled. They are
  shown, flagged, and excluded from ranking.
- **NumPy/SciPy.** Would be faster and bring `scipy.stats` for free, but converts
  everything to float64 at the boundary. Revisit if a hot path demands it; the module
  signatures are already shaped for a drop-in replacement.
