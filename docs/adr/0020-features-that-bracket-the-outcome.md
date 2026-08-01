# 0020 — Features that bracket the outcome

**Status:** accepted
**Date:** 2026-08-01
**Supersedes part of:** [0015 — outcome leakage in clustering](0015-outcome-leakage-in-clustering.md)

## Context

ADR 0015 removed `capture_efficiency` from the clustering feature space and replaced a
list of forbidden column names with a behavioural probe: vary a trade's outcome, hold
everything else fixed, and fail any feature whose value moves. That probe is sound and it
still passes.

It also missed the next instance, because the next instance does not move.

`mae_r` and `mfe_r` are properties of the price path, not functions of the exit. Change
where a trade was closed and neither value changes — they describe how far it travelled,
which is a fact about the market. Both pass the per-feature probe honestly.

What the probe cannot see is that **together they bracket the result**:

```
mae_r <= r_multiple <= mfe_r
```

true by definition, since a trade cannot be exited better than the best price it reached
or worse than the worst. Neither feature *is* the outcome. Two bounds, however, locate a
value, and standardised Euclidean distance over the pair separates the sign of the result
almost perfectly.

## Evidence

Measured on a 1,403-trade history:

| feature space | clusters | as a win/loss classifier |
|---|---|---|
| with `mae_r`, `mfe_r` | 2 groups, 97.0% and 11.9% winners | **92.2%** |
| without them | 3 groups, 45–54% winners | 51.2% (chance) |

The permutation test then confirmed, correctly and uselessly, that the two groups had
different P&L, and the Patterns page published them as established findings with annual
impacts of -$762,759 and +$687,586 — figures within 15% of the ones `capture_efficiency`
produced before it was removed.

The finding was unusable even setting the circularity aside. "Trades that never moved far
in your favour lost money" names no decision a trader could take: MFE is not knowable at
entry, so there is no rule that avoids that group.

## A second defect, found first and separately

The demo seed made this far worse and hid how much of it was real.

`_excursions` drew losers' MFE from `uniform(0, 0.45)` and winners' from
`r + uniform(0.05, 0.60)`. Since the smallest winner was around 0.6R the two ranges came
out **disjoint**: across 1,447 seeded trades, winners' MFE spanned 0.68–2.99 and losers'
0.00–0.45, with an empty gap between them and **not one loser in 714 ever reaching 1R**.
Its own docstring said such a trade "is a real event, but it is not the common one"; the
code made it impossible.

That is a defect on its own terms, independent of clustering. It also rigged the what-if
simulator in the mirror image of the bug that function was written to fix: with no losing
trade ever reaching 1R in your favour, a 1R profit target can never fill on a loser, so
every "take profit at N" scenario could only ever cost money.

Correcting it — an explicit mixture where ~13% of losers clear 0.6R and ~3% clear 1R —
moved the clustering's win/loss accuracy from 96.8% to 92.2%. **It did not fix the
leakage**, which is what established that the leakage was structural rather than an
artifact of the seed. Both had to be fixed, and neither would have been convincing
without the other: the seed fix alone leaves a 92% tautology in place, and the feature fix
alone leaves a demo history in which no losing trade ever ran a full unit of risk in your
favour.

## Decision

1. Remove `mae_r` and `mfe_r` from `FEATURES`. They remain available for *describing* a
   cluster and for the excursion statistics on the dashboard; the objection is to
   clustering on them, not to measuring them.
2. Add a **feature-set-level** probe alongside the per-feature one. It builds a history in
   which the outcome is independent of every honest feature, clusters it, and fails if
   cluster membership predicts win from loss better than 70%. A test of one feature at a
   time cannot catch a relationship that only exists between two.
3. Fix the seed's excursion distribution and pin its shape with tests, so a history in
   which no losing trade ever reaches 1R cannot come back.

## Consequences

- On the demo history the scan now returns one established finding — the planted
  overtrading leak, which is what the seed was built to contain — plus three clusters
  ("entered later in the day", "long", "short"), none of which is established. That is
  the honest result: the clusters became behavioural, and "did these groups perform
  differently?" became a real question again.
- The general rule, now stated twice by two different failures: **a feature does not have
  to be the outcome to leak it.** It is enough to be a function of it (`capture_efficiency`),
  or to bound it together with another feature (`mae_r`/`mfe_r`). The next instance will
  be a third thing, which is why the guard is a behavioural probe over the whole feature
  space rather than another list.
