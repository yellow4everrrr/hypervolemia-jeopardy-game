# 0021 — The path comes first

**Status:** accepted
**Date:** 2026-08-01
**Closes:** [0020 — features that bracket the outcome](0020-features-that-bracket-the-outcome.md)

## Context

The demo seed drew each number a trade records independently:

```python
entry = Decimal("5180.00") + Decimal(str(round(rng.uniform(-40, 40), 2)))
exit_price = entry + (net / (Decimal("12.50") * quantity) * Decimal("0.25"))
mae_r, mfe_r = _excursions(rng, realized_r)
```

Every one of those is individually plausible. Jointly they describe a market that does not
exist, and three separate defects came out of that gap.

**Entries did not belong to a price series.** Drawn per trade against a fixed centre, prices
inside a single session spanned 33 points on average and as much as 79. Nothing connected
the third trade of a day to the second. When bars were finally generated to join them, the
chart had to cover that ground, and it did so in visible jumps — because the bars were
being asked to explain prices that no price series had produced.

**Excursions had to be invented, and inventing them was wrong twice.** With no path, `mae_r`
and `mfe_r` could only be drawn from a distribution chosen to be consistent with the
outcome. The first choice was too generous — losers ran up to 1.8R in your favour, and the
what-if simulator reported a 1R target would have earned an extra $248,000. The correction
was too mean — winners' MFE and losers' MFE came out **disjoint**, MFE alone classified the
result perfectly, and the pattern engine published the outcome back as findings worth
-$762,759 and +$687,586 a year. ADR 0020 removed the pair from the feature space, which was
the right fix for the clustering. It did not fix the data.

**Bars generated afterwards could not contain their own trades.** A random walk near the
entry price produced a chart that did not pass through the price the trade was filled at:
an entry of 5175.96 against a bar spanning 5163.78–5165.13. `compute_excursions` refuses
that outright — "entry price must lie within the observed price range" — and before that the
replay draws the entry marker floating outside every candle. Anchoring the generator to the
recorded prices (a Brownian bridge through entry, exit and both extremes) fixed it, but it
was still reverse-engineering a path from conclusions the path was supposed to have
produced.

Three defects, one root: **numbers that describe what the price did, produced without a
price.**

## Decision

Generate the price path first, and read everything else off it.

`app/scripts/market_path.py` builds one session of minute candles as a random walk with
drift, and `place_trade` puts a trade on it:

* **Entry** is the price on the path at the entry minute.
* **Exit** is the stop or the target, whichever the path reaches first, or the close at the
  time stop if it reaches neither.
* **MAE and MFE are measured** from the highs and lows the path printed between those two
  moments.

`_excursions` is deleted. The bracketing identity now holds because the path enforces it,
not because a function asserts it on its way out — which matters, because both defective
versions satisfied that assertion.

**The planted leak is still steered, and still has to be earned.** `place_trade` searches
forward within a bounded window for an entry whose stop-or-target race ends the way the
session's design wants, and returns `None` when none does. The trade is real; *which* real
trade gets taken is biased. That is a fair description of a trader with a bad habit — the
market is not rigged, the entries are — and it keeps the leak a property of trade sequence
rather than of any price column. A trade that cannot be placed is skipped, never forced:
forcing one means a price that is not on the path, which is the entire thing this module
exists to prevent.

## Consequences

**The demo reconciles with itself.** Across 1,398 seeded trades, recomputing MAE and MFE
directly from the bars reproduces every recorded value exactly, and every entry and exit
price lies inside the minute bar covering it. Intra-session entry spread fell from 33.5
points mean / 78.9 max to 9.7 / 42.3. `backfill_demo_bars` is off the seed path, kept only
for histories seeded before this change.

**Two failures surfaced that the rewrite caused, and both were silent.** Neither raised an
error, broke a chart, or failed a test of the leak's design. Both were found by scanning
the seeded history rather than by reading the code, and both cost the demo its only planted
finding:

1. *The session ran out of room.* Trades are placed sequentially, so a session has a finite
   budget of them, and the trades it drops are always the last of the day — precisely the
   ones the leak is made of.
2. *The session was not one timeline.* The seed advanced a cursor per instrument, so ES and
   NQ ran independent clocks and a trade placed eighth could open before one placed third.
   The detector recovers a trade's position by sorting on `opened_at`. The leak was planted
   at one index and read at another.

The second is the more instructive. It is not a bug in any function; every function was
correct. It is two components disagreeing about what "the third trade of the session"
means, in a system where only one of them writes that number down.

**Statistical power is now a property of the seed, and is tested as one.** The leak being
*present* was never sufficient — it has to survive a permutation test corrected across seven
tests. Widening the profit target range from 1.4–2.0R to 1.2–2.6R does not touch the leak
and does not look like it touches anything, but the extra variance is enough to lose it:
measured across seven histories, the leak cleared p<0.01 on seven of seven at the narrow
range and five of seven at the wide one, and the shipped seed was one of the two failures.
`test_the_planted_leak_is_strong_enough_to_be_found` asserts the t-statistic; because a
Welch t is not the permutation test the detector runs, the sequencing property above gets
its own test rather than being inferred from it.

**"Widen the stop" became unanswerable, and that is correct.** A stopped-out trade's worst
price *is* its stop, so no trade's MAE reaches past 1R, and the simulator reports no
established difference for that scenario. The old data could answer it only because MAE was
invented past the stop. What a stopped-out trade would have done next is genuinely not in a
trader's own records either, and the honest report is the one that declines.

**The tests bind to the seed rather than to a copy of it.** The session loop is extracted as
`plan_session`, a pure function the tests call directly. A test that re-implemented the loop
would have kept passing through the cursor defect above, since the defect *is* a change to
how the loop advances.
