# ADR 0006 — A pattern is not a finding until it beats its own null

**Status:** Accepted
**Date:** 2026-07-31
**Extends:** [ADR 0004](./0004-analytics-honesty.md)

## Context

Milestone 8 adds two ways of surfacing patterns a trader has not noticed: named
behavioural detectors (revenge trading, overtrading, escalating size while down) and
unsupervised clustering over trade features.

Both are unusually good at producing convincing nonsense. A clustering algorithm always
returns clusters. A detector suite always returns a most-extreme detector. Rendered as
"we analysed your trades and found…", either will be believed, and unlike a wrong
statistic a wrong *pattern* comes with a narrative attached — the trader can always
recall a time they did exactly that.

ADR 0004 established the rule for the segmentation cube: nothing is a finding until it
survives multiple-comparison correction. This ADR extends that to discovery, where three
additional failure modes appear that FDR alone does not address.

## Decision 1 — the whole scan is one family

Every p-value in a scan, behavioural and cluster alike, goes into a **single**
Benjamini–Hochberg correction.

Seven detectors plus five clusters is twelve chances to clear p < 0.05. On a trader with
no leaks the expected number of "findings" is around 0.6, and whichever one lands will
look specific. Correcting the two halves separately would leave two families and roughly
double the false discovery rate the trader was promised.

The consequence is deliberately unintuitive: **adding a detector makes existing findings
harder to establish.** That is the correct accounting, and it is exactly why it is
enforced in one place (`_adjust_as_one_family`) rather than left to whoever adds the
next detector.

## Decision 2 — a detector may not split on the value it compares

This one was found by probing rather than by reasoning, and it is the most instructive
result of the milestone.

The original `cutting_winners_early` grouped trades by captured fraction
(`r_multiple / mfe_r < 0.5`) and then compared their `r_multiple`. The split variable
contains the compared value, so the two groups differ by construction. On **pure
independent noise it returned p = 0.0033 — the smallest value 300 permutations can
produce — on 100% of samples.**

The damage was not confined to that detector. Benjamini–Hochberg is a *step-up*
procedure: it sorts p-values and raises the acceptance threshold based on rank. One
guaranteed-minimal p-value therefore lifts the bar for every other test in the family.
In the eight-test scan, `revenge_trading` at p ≈ 0.055 was reported as an established
finding on noise; with the circular detector removed, the identical data yielded nothing.
**A single circular detector was manufacturing findings out of its honest neighbours.**

So: the split may not be a function of the compared value. Questions of the form "did I
exit too early?" are answered as **estimates with intervals** (`CaptureSummary`), outside
the test family entirely. "Your winners reached 2.1R and you captured 0.9R" is a real
claim that carries its own uncertainty and does not pretend to be a discovery.

Where a question genuinely is comparative, it is posed so the split is independent of the
value. "Do I hold losers longer than winners?" groups by outcome — already known — and
compares **holding time**, which is not the outcome. `test_detectors_do_not_split_on_the_
value_they_compare` runs every detector against independent data and fails the build if
any returns the permutation floor.

## Decision 3 — clustering must beat a shuffled null, not a threshold

The first implementation accepted any clustering scoring above a fixed silhouette of
0.15. Probing showed 300 **uniformly random** trades clustering into six tidy groups at
silhouette 0.29 — comfortably above any threshold low enough to be useful on real data —
with descriptions ("sized larger than usual, held briefly, entered late") that read
exactly as confidently as the real ones.

A fixed threshold cannot work, because k-means partitions anything and the silhouette of
that partition depends on the shape of the data, not on whether kinds exist in it.

So the winning clustering is tested against a null reference: each feature column is
shuffled independently, preserving every feature's marginal distribution and destroying
only the associations *between* features. That is the right null, because a "kind" of
trade is a recurring combination — short *and* large *and* early. The p-value is the
fraction of references clustering at least as tightly, with add-one smoothing so twenty
references can never evidence "never".

Uniform noise now returns `None`, and `None` — *these trades do not fall into kinds* — is
the honest answer for most traders.

A configuration guard falls out of this: with *B* references the smallest attainable
p-value is 1/(B+1), so `references=6` at α = 0.05 can never pass and would silently make
the feature a constant `None`. That raises `ValueError` rather than looking like a trader
with no patterns.

## Decision 4 — outcome is never a clustering feature

Clusters are formed from what a trade *was* — duration, size, hour, direction,
excursions — never from what it made. Clustering on P&L and then testing whether clusters
differ in P&L would always find a "losing pattern", because it built one.

## Decision 5 — everything tested is stored, not only what survived

`detected_patterns` receives a row for every test performed, with `is_significant` set
accordingly, and the API returns the failures alongside the findings.

Storing only survivors would make the table a record of each scan's luckiest result. It
would also make recurrence unfalsifiable: "this leak has appeared four months running" is
only evidence if the months it did not appear were also recorded.

## Consequences

* A scan costs seconds — a permutation test per detector plus a null battery for the
  clustering — so it is an explicit `POST /patterns/scan`, never a dashboard render.
  Milestone 13 moves it to a worker.
* Detectors are **not** independent of each other. An injected late-session leak fires
  overtrading, trading-while-down and after-a-losing-streak together, because a trader
  down on the day late in the session is in all three states at once. The report ranks by
  estimated cost and does not present them as separate discoveries.
* `estimated_annual_impact` is an extrapolation of observed cost by sample span, and is
  `None` below thirty days. Scaling three days to a year multiplies the error by 120.
* Discovered clusters are **proposed** as setups, never applied. An automatic label is a
  claim about what the trader was thinking; it needs their assent, and it carries
  per-trade confidence so a borderline member does not inherit a textbook member's
  certainty.
* The AI layer (milestone 9) may cite a pattern's label, effect size, adjusted p-value
  and sample size, and must respect `is_significant`. A pattern with
  `is_significant = false` may be described as examined and not established — never as
  something the trader does.
