# Periodic reports

Daily through annual, composed from the engines that already computed the numbers — and
built so the qualifications survive the last step.

Design reasoning lives in [ADR 0010](./adr/0010-periodic-reports.md).

---

## What it does

```
POST /api/v1/reports/generate       # one period
POST /api/v1/reports/run-schedule   # every completed period with no report yet
```

| Section | Source | Refuses when |
|---|---|---|
| Performance | [analytics engine](./analytics.md) | never — the numbers are the trader's own |
| Against last period | permutation test, FDR-corrected | period too thin, or no prior period |
| Patterns | [pattern scan](./patterns.md) | period too thin |
| What your leaks cost | deduplicated attribution | nothing was established as a leak |

---

## Why a report is the riskiest surface in the product

A dashboard is something a trader interrogates. A report is something a trader is *told* —
it arrives on a schedule, it carries a period in its title, and its figures are read as
settled fact.

Which means every qualification the engines attach can travel all the way here and then be
dropped in the last step, by code doing nothing more suspicious than formatting a number.

It is also the first place the natural implementation is to **aggregate**: sum the leaks,
sum the months, subtract last period from this one. All three are wrong, and all three
inflate the number a trader acts on.

---

## 1. Leak costs are attributed per trade, not summed per detector

Detectors overlap. A trader who is down on the day, late in the session, after two losses
is caught by three of them at once — [ADR 0006](./adr/0006-pattern-detection.md) recorded
this and handled it by ranking rather than summing.

A report cannot duck it, because the headline figure it exists to produce *is* the sum.

Each detector estimates the full cost of the trades it names, so adding the estimates bills
the same money once per detector. Measured on real scans the naive sum overstates by
**1.6× to 1.9×**. On one sample it was not merely inflated but impossible:

| | |
|---|---|
| Naive sum of five detectors | **−$68,952** |
| Deduplicated total | **−$37,048** |
| Total lost across every losing trade | **−$57,417** |

The naive figure claims the leaks cost more money than the trader lost. That is what a
report would have headlined.

So the union of affected trades is the unit. A trade claimed by several leaks keeps the
largest single claim, never the sum. Both figures are returned:

```json
{
  "total_cost": "-37047.59",
  "naive_sum": "-68952.42",
  "overstatement_factor": "1.861",
  "trades_touched": 222
}
```

`exclusive_cost` — what a leak cost on trades no other leak touched — is what the list is
ranked by. It answers "what would fixing this alone be worth", which is the question a
trader is actually asking.

A leak whose every trade is also claimed by another is flagged `is_wholly_shared`. It may
be the better description of the same behaviour, but it is not extra work, and listing it
as a separate item to fix implies it is.

---

## 2. A change is not a change until it beats its own null

The standard implementation computes a metric for both periods, subtracts, renders an
arrow. On twenty trades a month that arrow is **noise with a direction** — the win rate
moves four or five points between any two months of an unchanged process, and a green
arrow appears about half the time.

A trader shown "win rate ↑ 4 points" concludes last month's change worked. A trader shown
"expectancy ↓ $30" changes a process that was fine. A monthly report does this twelve times
a year.

So changes are permutation-tested and corrected as one family. What a trader sees instead:

> average P&L per trade was 12.40 against 18.90 — with 21 and 19 trades, chance produces a
> gap that size often enough that this is not a change

**Almost nothing will be established on a monthly report, and that is the accurate result**
rather than a missing feature. Two samples of twenty trades cannot distinguish a real shift
from ordinary variation.

`direction` distinguishes `flat` from `unchanged`, and the difference matters: `flat` means
the values matched, `unchanged` means the measurement could not tell them apart.

Only metrics that are a **mean of per-trade values** are compared — that is what makes the
permutation test valid. Profit factor, max drawdown and Sharpe are not, so they are absent
rather than tested wrongly.

---

## 3. An annual report is not twelve monthly reports

It is computed from the year's trades.

Aggregating is wrong twice. A leak present all year is established once per month, so
summing bills it twelve times — section 1's error one level up. And significance does not
aggregate at all: twelve inconclusive months do not combine into a finding, and one
conclusive month does not make the year one.

Quarterly and annual reports carry that warning in the payload, because the natural reading
of an annual figure is that it is the sum of the monthly ones.

---

## Thin periods

Sections refuse individually. A month with enough trades for statistics but too few for a
pattern scan shows the statistics and says why the patterns are missing.

`draws_conclusions` is separate from having data:

| Report | Sessions needed before concluding |
|---|---|
| daily | 1 |
| weekly | 3 |
| monthly | 8 |
| quarterly | 20 |
| annual | 60 |

A daily report on four trades shows the four trades honestly and concludes nothing. A
period with **no** trades is reported as flat rather than skipped — a gap in a monthly
series is information.

---

## Scheduling

`/reports/run-schedule` is the schedule, invoked rather than triggered. Milestone 13 moves
it behind a worker.

- **Completed periods only.** A month report generated on the 9th describes nine days while
  carrying the month's label.
- **Idempotent**, keyed on `(report_type, period_start)` to match the table's unique
  constraint. It will be re-run after every failure.
- **Oldest first.** A report preserves what was believed at a point in time; producing
  December before November inverts that.
- **Only periods the history spans.** A trader who started in March gets no January report;
  one who stopped in April does not accrue an empty report forever.
- **Capped at 25 per run**, with the remainder reported as deferred. Three years of imported
  history is hundreds of due periods.

Regenerating appends a row rather than updating one. "The March report said the overtrading
was costing $3,000" has to stay answerable after the engine changes.

---

## A bug this milestone found upstream

Building the comparison meant reading the FDR helper closely, which revealed it returned
results **sorted by p-value** while the pattern scan and the what-if sweep zipped them back
positionally.

A finding with p = 0.90 received the q-value earned by an unrelated finding with p = 0.001,
and was published as significant — a manufactured discovery, produced by the mechanism
built to prevent manufactured discoveries.

No test caught it. On all-noise samples every q-value is high however they are shuffled, so
the null batteries passed; on a sample with one real effect the *count* of findings is still
one, so the positive controls passed. Only asserting that each finding's q-value belongs to
its own p-value can see it.

Fixing it changed a milestone 10 claim, which is recorded in
[ADR 0010](./adr/0010-periodic-reports.md): with correct ordering, one noise seed began
establishing a scenario. The engine was right — 2 of 25 noise sweeps establish something,
about 8%, which is what a 5% FDR bound looks like. The **test** was wrong to demand zero.
BH bounds the expected proportion of false discoveries; it never promises none.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/reports/generate` | Build one period's report. |
| `POST` | `/reports/run-schedule` | Build every completed period with no report yet. |
| `GET` | `/reports` | List generated reports. |
| `GET` | `/reports/latest/{type}` | The most recent report of a type. |
| `GET` | `/reports/definition` | What a report contains, and when it declines to conclude. |
