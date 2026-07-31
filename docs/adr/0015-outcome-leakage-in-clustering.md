# 0015 — A clustering feature that was the outcome in disguise

**Status:** accepted
**Date:** 2026-07-31
**Supersedes part of:** [0006 — pattern detection](0006-pattern-detection.md)

## Context

The pattern engine clusters trades on a feature vector and then runs a permutation test
asking whether the resulting groups differ in P&L. That question is only meaningful if
the clusters were built without knowing the P&L. `app/analytics/features.py` states the
rule in its module docstring and encodes it as a tuple:

```python
EXCLUDED_FROM_CLUSTERING = ("net_pnl", "r_multiple", "gross_pnl")
```

The rule was violated by a feature that names none of those columns:

```python
def _efficiency(trade):
    if trade.mfe_r is None or trade.r_multiple is None or trade.mfe_r <= 0:
        return None
    return trade.r_multiple / trade.mfe_r
```

The `mfe_r > 0` guard forces the denominator positive, so
`sign(capture_efficiency) == sign(r_multiple)` **identically** — not usually, not on
average. On a real 350-trade history the two agreed 350 times out of 350. The feature was
the win/loss label, rescaled.

## What it produced

Clustering split the sample almost exactly into winners and losers and labelled the two
groups from the feature that separated them: *"gave back most of the available move"* and
*"captured most of the available move"*. The permutation test then confirmed, correctly
and uselessly, that the two groups had different P&L. Both cleared FDR correction at
p = 0.002 and reached the Patterns page as established findings carrying estimated annual
impacts of **-$817,766** and **+$545,178**.

Nothing about this looked wrong from outside. The p-values were real, the correction was
applied to the whole family, the confidence intervals were bootstrapped, and the labels
read like insight. It was the loudest, most confident, most expensive-looking output the
system had ever produced, and it was a tautology.

Removing the feature drops the same run from two significant clusters to **no clusters at
all**, which is the honest answer for that history.

The irony is instructive: `behaviours.py` already documented this exact hazard for the
same quantity. `CaptureSummary` computes how much of the available move a trader takes and
refuses to make it a hypothesis test — "splitting trades by how much of their excursion
they captured and then comparing what they captured is circular". The principle was
understood in one module and violated in another, because the violation was arithmetic
rather than a name.

## Decision

1. **`capture_efficiency` is removed from `FEATURES`.** Not fixed, not reweighted —
   removed. A feature whose sign is the label cannot be repaired by scaling.

2. **The exclusion rule is enforced behaviourally, not by name.** A name list cannot catch
   arithmetic; a division reintroduces the outcome without mentioning it.
   `tests/unit/analytics/test_feature_leakage.py` varies each trade's outcome fields with
   every other field held fixed and fails any feature whose extracted value moves. It also
   reconstructs the removed feature and asserts the probe rejects it, so the guard cannot
   silently stop guarding.

3. **Excursions remain legitimate features.** `mae_r` and `mfe_r` stay. A trade can run
   3R in favour and still close at a loss — an excursion is not the outcome, and the probe
   deliberately holds them fixed while varying P&L.

## Consequences

- The cluster detector will find less, and on many histories nothing. That is the point;
  the previous behaviour was finding something on every history by construction.
- Any feature added later must pass the leakage probe, including features derived from
  columns that are individually innocent. Ratios are the dangerous shape.
- Stored patterns from before this change are stale. `replace_patterns` deletes the
  previous scan before inserting, so the next scan clears them without a migration.

## Why the code review did not catch it

This is the part worth carrying forward. The feature had a clear name, an accurate
docstring, an explicit `None` guard, and sat beneath a comment block correctly explaining
why outcome must not be a feature. Every local signal said it was careful work. The defect
was only visible by *executing* it — computing `sign(feature) == sign(outcome)` across a
real sample and getting 350/350, then re-running the scan without it and watching two
six-figure findings evaporate.

Invariant probing found this, as it found the FDR ordering bug and the inert row-level
security. Reading the code found none of them.
