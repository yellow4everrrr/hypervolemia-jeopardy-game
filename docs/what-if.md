# The what-if simulator

Counterfactual re-simulation: what a different rule would have produced on the trader's
actual history — and, more importantly, when it refuses to say.

Design reasoning lives in [ADR 0008](./adr/0008-counterfactual-simulation.md).

---

## What it does

```
POST /api/v1/simulator/sweep
```

Each scenario re-prices every trade, then the whole sweep is corrected as one family:

| Scenario knob | Effect |
|---|---|
| `stop_r` | Exit at this R distance when the trade traded through it |
| `target_r` | Exit at this R multiple when the trade reached it |
| `max_trades_per_session` | Skip trades beyond this index in a session |
| `skip_after_consecutive_losses` | Skip trades taken with this many losses behind them |
| `only_hours` | Restrict to these entry hours |

The stop is evaluated **before** the target. Excursions record both extremes without
recording which came first, so the pessimistic reading is the only safe one.

---

## Three ways a counterfactual flatters itself

### 1. Silent inapplicability

A trade with no recorded stop cannot be re-simulated against a different stop. Counting
it as *unchanged* would add it to both sides of the comparison and pull every measured
effect toward zero — making every counterfactual look safe.

Such trades are marked `inapplicable` and excluded, and `coverage` reports what fraction
of the sample the scenario could actually be applied to. A simulation that re-priced 30%
of trades describes a subset, not a history, and `is_actionable` requires coverage ≥ 50%.

Filters (`max_trades_per_session`, `only_hours`, …) need no excursion data, so they apply
to the whole sample. They are correspondingly more trustworthy: they make no claim about
a price path.

### 2. Selection across the sweep

Nine scenarios is nine chances to clear p < 0.05, and the winner will look specific.
Every scenario is tested and the family is Benjamini–Hochberg corrected together — the
same discipline as the [segmentation cube](./analytics.md) and the
[pattern scan](./patterns.md).

`best` returns the largest **established** improvement, not the largest improvement. The
best-performing variant in any sweep is the one that got luckiest, and returning it
unqualified is how a simulator becomes a curve-fitting tool.

`MAX_SCENARIOS = 12` is a statistical limit, not a performance one. Testing fifty
variants makes it nearly impossible for any of them to establish anything.

### 3. Hindsight

Re-simulating an exit against the excursion a trade actually reached assumes the trader
would have known where the high was. Excursions bound what was **available**, never what
was achievable, and every counterfactual built on them inherits that.

The caveat rides in the payload's `interpretation` field, not in documentation.

---

## Choosing the null — three attempts

This was the hardest part of the milestone and each wrong answer produced confident,
plausible findings.

**A two-sample permutation test** treats baseline and simulated as independent draws.
They are not: they are the same trades measured twice, most identical on both sides.
Pooling them inflates the reference variance with duplicates the design never contained.

**A sign-flip paired test** assumes the sign of each difference is arbitrary under the
null. It is not — a 2R target deterministically produces the deltas it produces. That
null is trivially false the moment the rule touches one trade, so the test returns
"significant" for every scenario that changes anything, which is no information at all.

**A bootstrap of the per-trade deltas** asks the question the trader is actually asking:
*would this improvement survive a different sample of trades?* Resample with replacement,
recompute the mean difference, and check whether the interval excludes zero. An
improvement driven by two lucky trades collapses; one spread across the history does not.

The bootstrap interval is the primary evidence. It is what caught a scenario reporting a
$1 total difference over 240 trades as significant — one trade repriced, a consistent
sign, a tiny p-value, and an interval of `(0.000, 0.007)` that plainly includes zero.

A scenario is `is_actionable` only with: a reliable sample, coverage ≥ 50%, a delta
interval excluding zero, **and** significance after the family-wide correction.

---

## Testing it

The null battery uses a **driftless random walk**: each trade is a simulated price path,
MFE and MAE are its running extremes, and the R multiple is where it finished. All three
are then jointly consistent, and the optional stopping theorem guarantees no stop or
target placement changes the expectation. Anything the sweep reports there is a false
positive.

Two earlier generators had to be discarded, and both are documented in the test file
because each produced a convincing lie:

- Drawing `r_multiple` and `mfe_r` independently lets a −2R loser record a +2.5R
  favourable excursion. Physically impossible — it would have been a winner — and a
  target scenario then repriced nearly every trade to a win.
- Clamping excursions to contain the outcome fixes that but makes MAE a *function* of
  the result: every loser has a deep adverse excursion, every winner a shallow one.
  Tightening the stop is then guaranteed to help, on any sample, forever.

There is also a positive control — a random walk plus one genuinely exploitable
regularity — because an engine that reported nothing under all circumstances would pass
every null test in the file.

---

## Expected improvement

```
POST /api/v1/simulator/quantify
```

The only path by which `ai_recommendations.expected_improvement` is ever filled. This is
[ADR 0002](./adr/0002-ai-evidence-contract.md)'s fourth mechanism: the figure is
computed, never generated. The coach cannot write it.

A recommendation is turned into a scenario, simulated against the trader's real history,
and the result attached with its interval and coverage. When the improvement cannot be
established, that is recorded explicitly with a reason:

```json
{
  "is_established": false,
  "reason": "only 34% of trades have the recorded stop and excursion data this scenario needs, so the result would describe a subset rather than your history"
}
```

"Expected improvement: unavailable" tells a trader nothing. "Only 12 of your trades have
the recorded stop this needs" tells them what to fix.

---

## What it does not claim

A trader who had used a 2R target would have traded differently in ways the data cannot
show — held winners with more conviction, cut losers sooner, or abandoned the rule after
three losses. **The simulation re-prices the outcomes; it does not re-simulate the
trader.**

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/simulator/sweep` | Re-price history under each scenario, corrected as one family. |
| `POST` | `/simulator/quantify` | Compute one recommendation's expected improvement. |
| `GET` | `/simulator/scenarios` | The standard sweep, and why it is short. |
