# ADR 0008 — A counterfactual is a claim about a sample, not about a rule

**Status:** Accepted
**Date:** 2026-07-31
**Extends:** [ADR 0004](./0004-analytics-honesty.md), [ADR 0006](./0006-pattern-detection.md)
**Serves:** [ADR 0002](./0002-ai-evidence-contract.md) — mechanism 4

## Context

Milestone 10 adds the what-if simulator: re-price a trader's actual history under a
different rule ("2R target", "stop trading after two losses") and report the difference.

It is the single most persuasive surface in the product and the easiest to be wrong on.
"A 2R target would have made you $8,400 last year" is a sentence a trader will act on
immediately, and every part of it is a modelling choice: which trades could be
re-priced, which order the extremes occurred in, how many rules were tried before this
one, and whether the difference would survive a different year.

It is also the only path by which `ai_recommendations.expected_improvement` is ever
populated. ADR 0002 lists that column as the fourth mechanism keeping the coach honest —
the figure exists precisely so the model cannot write it. That makes the correctness of
this engine a load-bearing part of the AI contract, not a feature beside it.

## Decision 1 — the null is a bootstrap of per-trade deltas

Choosing the null took three attempts. The first two each produced confident, plausible,
wrong findings, and the reasoning generalises beyond this milestone.

**A two-sample permutation test** treats baseline and simulated results as independent
draws to be pooled and reshuffled. They are not independent: they are the same trades
measured twice, and under most scenarios the great majority are *identical* on both
sides. Pooling fills the reference distribution with duplicates the design never
contained and understates how surprising the observed difference is.

**A sign-flip paired test** fixes the pairing but assumes the sign of each difference is
arbitrary under the null. It is not. A 2R target applied to a fixed history
deterministically produces the deltas it produces; there is no randomness whose sign
could have come out the other way. The null "this rule changes nothing" is trivially
false the instant the rule touches one trade, so the test returns *significant* for
every scenario that does anything at all — which is no information.

**A bootstrap of the per-trade deltas** asks the question the trader is actually asking:
*would this improvement survive a different sample of trades?* Resample the deltas with
replacement, recompute the mean, and read the interval. An improvement carried by two
lucky trades collapses; one spread across a history does not.

The interval is the primary evidence and the p-value is secondary. That ordering is what
caught the engine reporting a **$1 total difference across 240 trades** as significant:
one trade repriced, consistent sign, a tiny p-value, and an interval of `(0.000, 0.007)`
that plainly contains zero for any purpose a human has.

## Decision 2 — inapplicable is not unchanged

A trade with no recorded stop cannot be re-simulated against a different stop. The
tempting handling is to pass it through unchanged; it is also the most damaging one.
Unchanged trades enter both sides of the comparison identically and pull every measured
effect toward zero, so the more incomplete the data, the safer every counterfactual
looks. Missing data would make the simulator *more* confident that nothing is harmful.

Such trades are marked `inapplicable` and excluded from the comparison entirely, and
`coverage` reports what fraction of the sample the scenario could be applied to.
`is_actionable` requires coverage ≥ 50%: a result computed on 30% of trades describes a
subset, and saying so is the whole point.

Filter scenarios (`max_trades_per_session`, `only_hours`, …) need no excursion data and
therefore apply to the entire sample. They are correspondingly more trustworthy, because
they make no claim about a price path — only about which trades were taken.

## Decision 3 — the sweep is one family, and it is short

Every scenario in a sweep goes into a single Benjamini–Hochberg correction, exactly as
in ADR 0006. Nine scenarios is nine chances at p < 0.05, and the winner will look
specific and mechanistic.

`SweepReport.best` returns the largest **established** improvement, not the largest
improvement. Returning the raw maximum is precisely how a simulator becomes a
curve-fitting tool: the best-performing variant in any sweep is, more often than not,
the one that got luckiest.

`MAX_SCENARIOS = 12` is a statistical limit rather than a performance one, and the
`/simulator/scenarios` response says so in the payload. Testing fifty variants against
one history makes it nearly impossible for any of them to establish anything — a caller
who does it anyway has built a machine for finding nothing.

## Decision 4 — the stop resolves before the target

Executions record maximum favourable and maximum adverse excursion, but not which came
first. A trade that reached both −1R and +2R may have done either first, and nothing in
the data distinguishes the two.

The engine always resolves the stop first. This is the pessimistic reading and it is the
only defensible one: the optimistic reading would let every widened-target scenario
harvest gains from trades that were in fact stopped out on the way.

More generally, an excursion bounds what was **available**, never what was
*achievable*. Re-simulating an exit against a high the trade actually reached assumes
the trader would have recognised it as the high. Every counterfactual built on
excursions inherits that assumption, and the caveat rides in the response's
`interpretation` field rather than in documentation nobody opens.

## Decision 5 — a non-result is written with its reason

`expected_improvement` is filled on every quantify call, including the failures, with
`is_established: false` and a plain-language `reason`. An empty column would be
indistinguishable from "not simulated yet", and "expected improvement: unavailable"
tells a trader nothing they can act on.

> "only 34% of trades have the recorded stop and excursion data this scenario needs, so
> the result would describe a subset rather than your history"

That sentence tells them what to fix. It is generated by `_why_not()` from the
simulation result — it is not written by the model, which cannot reach this column at
all.

## Testing decision — the null battery is a driftless random walk

Two generators were written and discarded before one held, and both are documented in
the test file because each produced a convincing lie:

* Drawing `r_multiple` and `mfe_r` independently permits a −2R loser that recorded a
  +2.5R favourable excursion. That is physically impossible — the trade would have been
  a winner — and a target scenario duly repriced 73% of trades to wins.
* Clamping excursions to contain the outcome fixes the impossibility but makes MAE a
  *function* of the result: every loser now has a deep adverse excursion and every
  winner a shallow one. Tightening the stop is then guaranteed to help, on any sample,
  forever.

The surviving generator simulates each trade as a price path; MFE and MAE are its running
extremes and the R multiple is where it finished. All three are then jointly consistent
by construction, and on a driftless walk the optional stopping theorem guarantees that
**no** stop or target placement changes the expectation. Anything the sweep reports there
is a false positive, by proof rather than by convention.

A positive control sits beside it — a random walk plus one genuinely exploitable
regularity — because an engine hard-wired to report nothing would pass every null test in
the file.

## Consequences

* A sweep runs a bootstrap per scenario and is an explicit `POST`, never a dashboard
  render. Milestone 13 moves it to a worker.
* Coverage and `trades_evaluated` are part of every payload and every stored result. A
  future reader must be able to tell how much of the history a number describes.
* The simulator re-prices outcomes; it does **not** re-simulate the trader. Someone who
  had used a 2R target would have behaved differently in ways the data cannot show — held
  winners with more conviction, or abandoned the rule after three losses. That limit is
  stated in the payload and cannot be engineered away.
* Adding a scenario to `default_scenarios()` raises the bar for all the others. As in ADR
  0006 this is deliberate, and it is the reason the standard sweep is nine
  well-motivated rules rather than a grid search.
