# ADR 0014 — An excursion says price was there, not that you could trade there

**Status:** Accepted
**Date:** 2026-07-31
**Extends:** [ADR 0008](./0008-counterfactual-simulation.md), [ADR 0009](./0009-predictive-models.md)
**Corrects:** a bias in the milestone 10 what-if simulator

## Context

Optimal stop and target models were in the original specification and were deferred twice,
out of milestone 10 and again out of milestone 11, with the same stated reason: the naive
implementation is exactly the curve-fitting ADR 0008 refuses. Sweep a dozen stops against a
dozen targets, re-price the history under all 144 combinations, report the best cell. On a
trader with no exit edge at all, the best of 144 draws looks excellent and sounds specific.

Measured here on pure noise, that naive search advertised improvements between **+$1,854
and +$5,089**.

## Decision 1 — the selection procedure is the hypothesis, not the grid

Correcting 144 comparisons does not fix this. It over-corrects: a genuine moderate edge
cannot clear the adjusted threshold either, so the honest sweep answers "nothing" to every
trader whether or not they have one.

The question is reframed instead. Not *which cell is best?* but: **if I pick the best cell
on data I have, does that choice beat what the trader already does on data I have not?**
That is one hypothesis, tested once, and it is the question a trader actually has — they
are not going to trade the grid, they are going to trade whatever the procedure picks.

Per fold: select on the training sessions, apply **that** rule to the test sessions, record
the per-trade difference against what really happened. The reported improvement is the
out-of-sample performance of the selection, never the in-sample performance of the winner.

Two further gates, because a positive out-of-sample mean is not sufficient:

* **Selection stability.** A procedure that picks 1R in one window and 3R in the next is
  fitting each window's noise, and its average coming out positive is luck. A
  recommendation nobody could have followed consistently is not a recommendation.
* **A session-block bootstrap interval**, as in ADR 0009 — a mean is a point estimate, and
  this codebase has now been wrong twice by trusting one.

## Decision 2 — stops are not searched, and this is the important part

The first implementation searched both stops and targets and recommended a rule on **one
noise sample in three**, on a driftless random walk where the optional stopping theorem
*proves* no exit placement can change the expectation.

The cause is not in the selection. It is in the re-pricing, and it therefore applies to the
shipped what-if simulator as well. Over 4,000 driftless-walk trades, one fixed 0.5R stop —
no grid, no choosing, nothing selected — reported **+8.28 per trade**:

```
of 2,786 trades that traded through -0.5R:
   mean actual final R  = -0.619
   stopped at           = -0.500
```

**An excursion says price reached a level. It does not say a fill was available there.**
Conditioning on "the recorded extreme passed the stop" selects the paths that overshot it,
and the overshoot is precisely the amount by which assuming a perfect fill flatters the
counterfactual. Observing the path more finely shrinks the gap but never closes it — 0.094
at 60 samples per trade, 0.030 at 3,840 — because barrier overshoot decays only with the
square root of the sampling interval.

This is the same family as ADR 0008's warning that "excursions bound what was **available**,
never what was achievable", made quantitative and found to be larger than the effects being
measured.

**The sign of the error depends on the order type, and that is what makes targets usable.**

| | order type | fills at | assuming the level is |
|---|---|---|---|
| Stop | market | trigger **or worse** | optimistic |
| Target | limit | limit **or better** | conservative |

The same measurement confirms the mirror image: a 1R target on the same driftless data
reports **−4.94 per trade**, biased against itself.

So a target improvement that survives this procedure survives *despite* a bias working
against it, while a stop improvement of comparable size is indistinguishable from the bias
working for it.

No correction is available from excursion data. The overshoot at the moment of crossing is
not recoverable from the pair (extreme, outcome) — a slippage constant chosen to cancel it
would be a number invented to produce the answer, and the value that happens to zero the
bias depends on the bar resolution rather than on anything about the market.

The trader's own stop is therefore left exactly where they put it, and every candidate
changes only where profit is taken. `ExitRule` cannot express a stop, and a test asserts
the grid contains none, because "just add stops to the grid" is the obvious future change.

After the restriction: **0 false recommendations in 10 noise runs**, with the positive
control — mean-reverting paths, where taking profit genuinely beats holding — established in
3 of 3 with unanimous selection stability.

## Decision 3 — selection uses a point estimate, not a hypothesis test

The first version scored candidates by calling `simulate()`, which runs a bootstrap and a
significance test. That computed a confidence interval for each of 25 candidates in every
fold — hundreds of intervals whose only purpose was to be sorted, none ever reported — and
made a single run take minutes rather than **1.1 seconds**.

The principle is worth stating because it is easy to get backwards: **ranking needs a point
estimate; only the final out-of-sample result is a hypothesis.** Testing inside the
selection loop tests the wrong thing at great expense.

Re-pricing still goes through the simulator's own `_reprice`, so a rule evaluated here and
the same rule run through `/simulator/sweep` cannot disagree.

## Consequences

* **The milestone 10 what-if simulator carries this bias on its stop scenarios**, and its
  target scenarios carry the opposite one. This is part of why "widen the stop to 1.5R"
  appeared among the false positives measured in milestone 12. The sweep is unchanged for
  now — it corrects a family and reports intervals, so it is not silently confident — but
  its stop scenarios should be read as an upper bound rather than an estimate, and closing
  this properly needs bar-level fill modelling that the current schema does not support.
* Most traders will be refused, and the refusals distinguish *why*: no improvement, an
  unstable selection, too few windows, or an interval spanning zero. Each points at a
  different next step.
* The recommendation is a **take-profit level only**. That is a narrower feature than the
  original specification asked for, and the narrowing is the finding rather than a
  shortcut.
* `MIN_TRAIN_SESSIONS = 25` with 10-session steps means roughly 75 trading sessions before
  any recommendation is possible. Establishing an exit rule genuinely needs that much
  history; producing one from less would be producing it from the selection's own noise.
