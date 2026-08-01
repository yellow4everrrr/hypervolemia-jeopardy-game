# Pattern & setup detection

Two ways of finding things a trader has not noticed, and one set of rules that stops
either from inventing them.

The reasoning behind the design is in [ADR 0006](./adr/0006-pattern-detection.md); this
is the working reference.

---

## What a scan does

```
POST /api/v1/patterns/scan
```

1. Runs seven **behavioural detectors** — named patterns, each a two-group comparison.
2. Builds a **feature matrix** and looks for clusters of behaviourally similar trades.
3. Tests every cluster against the rest of the sample.
4. Pools **every** p-value into one Benjamini–Hochberg correction.
5. Stores every test performed, with its verdict.

Steps 4 and 5 are what separate this from a tool that finds patterns in noise.

---

## The behavioural detectors

Each splits the sample on a behaviour and compares outcomes. What distinguishes them
from the [segmentation cube](./analytics.md) is **sequence**: hour of day is a property
of a trade, but revenge trading is a property of a trade's position in a sequence, and
no amount of slicing a flat table will surface it.

| Kind | Split | Compares |
|---|---|---|
| `revenge_trading` | Opened within 10 minutes of closing a loser | net P&L |
| `overtrading` | Beyond the 6th trade of a session | net P&L |
| `trading_while_down` | Session P&L already negative | net P&L |
| `size_escalation` | Larger than the trader's median size **and** down on the session | net P&L |
| `after_losing_streak` | Two or more losses immediately preceding | net P&L |
| `holding_losers_too_long` | Losers vs winners | **holding time** |
| `first_trade_of_session` | The opening trade | net P&L |

Session state is always **as of before the trade** — the same rule the
[compliance engine](./compliance.md) uses. A trade is judged on the decision, not on how
it turned out.

Trades the detector cannot judge are **excluded, not counted as absent**. The first trade
of a session has no previous exit, so it is neither an example nor a counter-example of
revenge trading; putting it in the "did not do it" group would dilute the comparison
with trades where the behaviour was impossible.

### The rule for adding a detector

**The split may not be a function of the value being compared.**

A detector that groups by `r_multiple / mfe_r` and then compares `r_multiple` returns the
smallest p-value the permutation count allows on *every* sample, noise included. Worse,
because BH is a step-up procedure, that rigged p-value raises the acceptance threshold
for every honest test beside it — one circular detector manufactures findings out of its
neighbours.

`test_detectors_do_not_split_on_the_value_they_compare` runs every detector against
independent data and fails the build if any returns the permutation floor.

Note that `holding_losers_too_long` is deliberately inverted for this reason: it groups
by outcome (already known) and compares holding time (not the outcome).

### Detectors are not independent

A trader down on the day, late in the session, after two losses is in three detector
states at once. A single real leak commonly lights up several. The report ranks by
estimated cost and does not present them as separate discoveries.

---

## Clustering

### Features

Eight, deliberately few — every extra dimension dilutes Euclidean distance, and every
frequently-missing feature costs trades from the run.

`duration_seconds` · `entry_hour` · `entry_weekday` · `quantity` · `direction` ·
`mae_r` · `mfe_r` · `capture_efficiency`

**Outcome is never a feature.** Clustering on P&L and then testing whether the clusters
differ in P&L is circular — it always finds a "losing pattern", because it built one.

**Missing values are never imputed.** A feature present on fewer than 70% of trades is
dropped from the run; trades missing a retained feature are excluded and reported as
excluded. Filling a gap with a zero or a sample mean invents a trade that did not happen
and lets it pull a centroid.

**Everything is standardised.** Duration is in seconds and R multiple is in single
digits; without z-scoring, Euclidean distance is a measurement of duration with rounding
noise attached.

### Choosing k

k-means++ seeded initialisation, Lloyd's algorithm to convergence, k from 2 to 6 chosen
by silhouette. Fully deterministic — a trader who reloads and sees different patterns has
learned, correctly, that they mean nothing.

### The structure test

A silhouette threshold cannot decide whether clusters exist, because **k-means partitions
anything**. Three hundred uniformly random trades come back as six tidy groups at
silhouette ≈ 0.29, above any threshold low enough to be useful on real data, with
descriptions that read exactly as confidently as the real ones.

So the winning clustering is compared against a null reference built by **shuffling each
feature column independently**. That preserves every feature's marginal distribution and
destroys only the associations between features — the right null, because a "kind" of
trade is a recurring *combination*: short **and** large **and** early.

```
structure_p = (references clustering at least as tightly + 1) / (references + 1)
```

Accepted only at `structure_p ≤ 0.05`. With 20 references the floor is 1/21 ≈ 0.048, so
`references` below 20 makes the test unpassable — that raises `ValueError` rather than
silently looking like a trader with no patterns.

`None` — *these trades do not fall into kinds* — is the answer for most traders, and it
is a real result.

### Describing a cluster

Centroids live in standardised space, so a coordinate **is** a z-score against this
trader's own averages. Coordinates beyond 0.5σ are rendered as phrases, strongest three
first:

> Trades that are held briefly, entered early in the session, sized larger than usual

`to_real_units()` converts a centroid back to seconds and contracts for display.

---

## From cluster to setup

A discovered cluster is **proposed**, never applied:

```
POST /api/v1/patterns/setups   { "setup_name": "...", "trade_ids": [...] }
```

An automatic label is a claim about what the trader was thinking when they took the
trade, and the journal does not get to make that claim unilaterally. Each proposal
carries per-trade confidence, computed from the distance to the nearest *other* centroid:

```
confidence = d(other) / (d(own) + d(other))
```

A trade on the boundary scores near 0.5; one deep inside its cluster approaches 1.
Accepted trades are written with `setup_source = 'auto'` and their confidence, so a later
model evaluation can tell proposed labels from corrected ones.

---

## Reading a report

| Field | Means |
|---|---|
| `sample.tests_performed` | How many places the scan looked. The denominator. |
| `sample.findings` | How many survived the family-wide correction. |
| `behaviours[]`, `clusters[]` | **Everything** examined, each with its own verdict. |
| `is_actionable` | Survived. This is the only field that constitutes a claim. |
| `polarity` | `leak` or `edge`, from the observed difference and the unit. |
| `estimated_cost` | Difference × affected trades. Currency-denominated detectors only. |
| `capture` | Exit quality, as an **estimate** — not part of the test family. |

A report that hides how many places it looked is presenting a filtered view of its own
search as though it were the whole search. Two findings out of twelve tests is a very
different claim from two out of two.

### Association, not causation

Every difference here is descriptive. Trades taken after a loss differ from other trades
in ways beyond the timing — the state of mind that produces a fast re-entry also picks
worse entries. The scan measures what a behaviour is *associated with*, not what it
caused.

---

## Persistence

`detected_patterns` receives a row for **every test performed**, not only the survivors,
with `is_significant` set accordingly. A rescan replaces the previous set rather than
upserting, so the stored rows always equal the latest scan's verdict.

Storing only survivors would make the table a record of each scan's luckiest result, and
would make "this leak recurred for four months" unfalsifiable — the months it did not
recur would be missing.

`estimated_annual_impact` scales observed cost by the sample's span, and is `null` below
thirty days of history. Scaling three days to a year multiplies the error by 120.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/patterns/scan` | Run the scan; store every tested pattern. |
| `GET` | `/patterns` | Read back the last scan, findings and failures alike. |
| `POST` | `/patterns/setups` | Accept a proposed cluster as a named setup. |

A scan runs a permutation test per detector plus a null battery for the clustering, which
puts it in the seconds. It is an explicit request, never a dashboard render; milestone 13
moves it to a worker.
