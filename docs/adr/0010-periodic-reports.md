# ADR 0010 — A report is where careful analytics get rounded back into a story

**Status:** Accepted
**Date:** 2026-07-31
**Extends:** [ADR 0004](./0004-analytics-honesty.md), [ADR 0006](./0006-pattern-detection.md), [ADR 0008](./0008-counterfactual-simulation.md)

## Context

Milestone 12 adds periodic reports: daily through annual, composed from the analytics
engine, the pattern scan and the compliance engine.

Reports are read differently from every other surface. A dashboard is a thing a trader
interrogates; a report is a thing a trader is *told*. It arrives on a schedule, it is
titled with a period, and its figures are read as settled fact — which means every
qualification the engines attach can survive all the way to the reporting layer and then
be quietly dropped in the last step, by an implementation doing nothing more suspicious
than formatting a number.

Two things make this milestone the riskiest of the backend set. It is the first place the
*calendar* decides what gets grouped together, which is an arbitrariness nothing upstream
has to deal with. And it is the first place where the natural implementation is to
**aggregate** — sum the leaks, sum the months, subtract last period from this one — and
every one of those aggregations is wrong in a way that inflates the number a trader acts
on.

## Decision 1 — leak cost is attributed per trade, never summed per detector

ADR 0006 recorded that detectors overlap: "an injected late-session leak fires
overtrading, trading-while-down and after-a-losing-streak together, because a trader down
on the day late in the session is in all three states at once." Milestone 8 handled that
by ranking rather than summing.

A report cannot duck it the same way, because the headline figure a periodic report
exists to produce *is* the sum: what did my mistakes cost me this month?

Each detector estimates the full cost of the trades it names. Summing the estimates bills
the same money once per detector. Measured on real scans the naive sum overstates by
**1.6× to 1.9×**, and on one sample it produced a figure that is not merely inflated but
impossible: the detectors' estimates added to **$68,952** of leaks when the trader's total
losses across every losing trade in the period were **$57,417**. A report headlining that
is quoting more money than was lost.

So cost is attributed to trades, not to detectors. The union of affected trades is the set
any leak touched; a trade claimed by several leaks keeps the largest single claim rather
than their sum; and both the deduplicated total and the naive sum are returned so the gap
is visible. A future change that starts summing has an obviously wrong number to explain.

Averaging the claims on a shared trade was rejected: it lets a weak third detector dilute
a strong finding. Weighting by outcome was rejected too — it attributes the most cost to
the worst trades regardless of whether the behaviour caused them, which is assuming the
conclusion.

## Decision 2 — a period-over-period change is not a change until it beats its own null

Every journal ships this feature and almost all ship it wrong: compute a metric for both
periods, subtract, render an arrow. On a trader taking twenty trades a month that arrow is
noise with a direction. The win rate moves four or five points between any two months of
an unchanged process, and a green arrow appears about half the time.

The damage is specific and compounding. A trader shown "win rate ↑ 4 points" concludes
that last month's change worked and keeps doing it. A trader shown "expectancy ↓ $30"
changes a process that was fine. A *monthly* report delivers that twelve times a year.

Changes are therefore permutation-tested and the whole comparison set is corrected as one
Benjamini–Hochberg family, exactly as in ADR 0004. Two consequences, both of which make
the feature look worse and are correct anyway:

* **Almost nothing will be established on a monthly report.** Two months of twenty trades
  cannot distinguish a four-point shift from chance. The honest rendering — "48% against
  44%; with 21 and 19 trades, chance produces a gap that size most of the time" — tells
  the trader something true.
* **The compared set is deliberately short.** Every metric added is another chance to
  clear the threshold and raises the bar for all the others.

`direction` distinguishes `flat` from `unchanged`, and the distinction is load-bearing:
`flat` says the values matched, `unchanged` says the measurement could not tell them
apart. A UI collapsing the two shows a settled fact where there is an open question.

Only metrics that are a **mean of per-trade values** are compared, because that is what
makes the permutation test valid. Profit factor, max drawdown and Sharpe are not, so they
are absent rather than tested wrongly.

## Decision 3 — long reports are computed, not aggregated

An annual report is built from the year's trades. Never from twelve monthly reports.

Aggregating sub-reports is the intuitive implementation and it is wrong twice over. A leak
present all year is established once per month, so summing the monthly costs bills it
twelve times — decision 1's error reintroduced one level up. And significance does not
aggregate at all: twelve inconclusive months do not combine into an annual finding, and
one conclusive month does not make the year one.

Because the natural reading of an annual figure is that it is the sum of the monthly ones,
quarterly and annual reports carry an explicit note in the payload saying it is not.

## Decision 4 — sections refuse individually, and a thin period still shows its numbers

A report is not all-or-nothing. A month with enough trades for performance statistics but
too few for a pattern scan shows the statistics and states why the patterns are missing.
Dropping the whole report would withhold numbers the trader owns; rendering an empty
pattern list would imply the scan found nothing.

`draws_conclusions` is separate from having data. A daily report on four trades shows the
four trades honestly and concludes nothing — a "finding" from four trades is the loudest
of four coin flips.

A period with **no** trades is reported as flat rather than skipped. A gap in a monthly
series is information, and silently omitting April would make the series lie about its own
continuity.

## Decision 5 — periods are complete, immutable, and idempotently scheduled

An incomplete period is never reported on. A month report generated on the 9th describes
nine days while carrying the month's label, and every figure in it — including the
comparison against a full previous month — reads as a claim about the whole.

Regenerating a period appends a row rather than updating one. A report is a record of what
was believed on a date, and "the March report said the overtrading was costing $3,000"
must stay answerable after the analytics engine changes. Reports carry `report_version`
so a reader can see where the break is.

The scheduler keys on `(report_type, period_start)`, matching the table's unique
constraint, so re-running it produces nothing new. That matters because it will be re-run
after every failure. It generates oldest-first, which is not cosmetic: a report preserves
what was believed at a point in time, and producing December before November inverts the
one artefact that exists to preserve that ordering.

Reports are only generated for periods the trading history spans. A trader who started in
March gets no January report, and one who stopped in April does not accrue an empty report
every month forever.

## A bug this milestone found upstream

Building the comparison required the FDR helper, and reading it closely revealed that
`control_false_discovery_rate` returned its results **sorted by p-value** while two
callers — the pattern scan (ADR 0006) and the what-if sweep (ADR 0008) — zipped them back
against their own findings positionally.

The effect was not a cosmetic mix-up. A finding with p = 0.90 received the adjusted
p-value earned by an unrelated finding with p = 0.001 and was published as significant:
a manufactured discovery, produced by the exact mechanism built to prevent manufactured
discoveries. `segmentation.py` was unaffected because it re-associates results by label.

No existing test caught it, and the reason is worth recording. On an all-noise sample
every q-value is high however they are shuffled, so the null batteries passed. On a sample
with one real effect the *count* of significant findings is still one, so the positive
controls passed too. Only a test asserting that each finding's q-value belongs to its own
p-value can see it, and those now exist at the helper and at both call sites.

The contract is now positional: the returned list matches the input order, and sorting is
an internal detail.

**Fixing it changed a milestone 10 claim.** With the ordering corrected, an M10 null-battery
seed began establishing a scenario on pure noise — the bug had been masking it. The engine
was not at fault: measured over 25 noise sweeps, 2 established something, about 8%, which
is what a 5% FDR bound looks like at that sample size. The test was wrong to demand zero.
Benjamini–Hochberg bounds the expected *proportion* of false discoveries; it never
promises none. That test now asserts the property actually worth asserting — that the
correction is what suppresses the raw significance, demonstrated on a sample where three
scenarios clear raw p < 0.05 and none survives adjustment.

## Consequences

* Generating a report runs a pattern scan and a permutation battery, so it costs seconds
  and is an explicit `POST`. Milestone 13 moves it behind a worker on a real schedule;
  until then `/reports/run-schedule` *is* the schedule, invoked rather than triggered.
* A scheduler run is capped at 25 reports. A trader importing three years of history has
  hundreds of due periods, and building them all in one request would time out with no
  record of how far it got. The run reports what it deferred.
* The AI layer may cite a report's stored figures under the ADR 0002 placeholder contract,
  and must respect `draws_conclusions` and `is_established`. A metric that did not survive
  correction may be described as examined and unchanged — never as an improvement.
* `metrics_snapshot` stores the evidence rather than a reference to live data. Re-running
  analytics next month would otherwise change last month's report, and a record whose
  contents move is not a record.
