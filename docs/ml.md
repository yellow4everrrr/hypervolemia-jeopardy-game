# The ML layer

Win probability and expected R for a trade the trader has not taken yet — and, far more
often, a refusal to give one.

Design reasoning lives in [ADR 0009](./adr/0009-predictive-models.md).

---

## What it does

```
POST /api/v1/predictions/train
GET  /api/v1/predictions/next
```

Two heads, trained on the trader's own history, gated independently:

| Head | Target | Baseline it must beat |
|---|---|---|
| `win_probability` | did the trade win | always predicting the trader's base rate |
| `expected_r` | the R multiple | always predicting the trader's average R |

Neither is served unless it beats its baseline by more than luck explains **and** — for
the probability head — its stated probabilities match observed frequencies.

---

## The one thing this surface gets wrong by default

Everything else in the product describes what happened. This predicts. A trader shown
"68% chance of a winner" will **size on it**, and a model whose 68% comes up 45% of the
time is worse than no model: it will encourage bigger positions on exactly the trades it
is most wrong about.

So the default answer is no, and three separate gates have to open before a number
appears.

---

## Gate 1 — the model cannot see the outcome

`TradeRecord` carries `net_pnl`, `r_multiple`, `mfe_r`, `mae_r` and `duration_seconds`
right beside the entry fields. Every one is known only after the trade closed.

The clustering feature set in [pattern detection](./patterns.md) uses several of them on
purpose — clustering is *descriptive*, so "how much of the move did this capture" is a
legitimate description. Reusing that feature set to predict the result would be
predicting the outcome from the outcome.

The defence is not a naming convention. `EntrySnapshot` **has no outcome fields**, so a
feature extractor written against it cannot reach one — not by accident, not by a
refactor, not by whoever adds the ninth feature in two years.

A test demonstrates what the barrier is worth: smuggle the trade's own P&L in as a
feature, on data with **no** learnable signal, and the model's error drops from 0.25 to
under 0.02. From the outside it does not look suspicious. It looks excellent.

The second, subtler leak is sequence context. Every snapshot sees its session as it was
*before* its own trade — the first trade of a day sees zero prior trades and a flat P&L,
never the day's eventual total.

## Gate 2 — validation is walk-forward, split on sessions

Random k-fold on time series trains on next March to predict last January. The resulting
score is not optimistic, it is meaningless.

Folds are strictly forward, and they **never split inside a session**. Trades sharing a
day share a market, a news cycle, and a trader in one mood running one plan. Splitting
mid-session lets the model learn the day, which forward ordering alone does not prevent.

The ridge penalty is selected by a nested forward split *inside the training window*.
Choosing it on the test fold — even just by keeping whichever penalty scored best
afterwards — turns an out-of-sample number into an in-sample one wearing the wrong label.

## Gate 3 — skill must be established, not merely positive

This one was found by probing, and it is the most instructive.

Ten walk-forward runs on histories whose outcomes were **independent of every feature**
produced skill scores scattered around zero — and one landed at **+0.014**. Under a bare
`skill > 0` gate that model was deployable. A trader would have been shown per-trade win
probabilities derived from pure noise, with nothing marking them as such. One in ten is
a design defect, not an accident.

Skill now carries a **session-block bootstrap interval**, and deployment requires the
interval to exclude zero. Sessions are the resampling unit for the same reason folds
break on them: resampling individual trades treats four correlated trades as four
independent pieces of evidence and yields an interval roughly half the width it should be
— which would have hidden this exact false positive.

After the change: **0 false deployments in 40 runs**, and the positive control still
deploys.

The interval holds the fitted models fixed, so it measures uncertainty in *evaluating*
the models rather than in which models different data would have produced. It is
therefore somewhat optimistic. That caveat rides in the payload.

---

## Calibration

A proper probability must mean what it says. Three metrics, and the ordering is
deliberate.

**Accuracy is not reported anywhere.** It is the metric everyone asks for and it is a
trap: a trader who wins 42% of the time gets 58% accuracy from a model that predicts
"loss" every time. Worse, accuracy *improves* when a model pushes probabilities toward 0
and 1 — the opposite of calibration. ROC-AUC is absent for a related reason: it sees only
the ordering, so a model can rank trades perfectly while its stated probabilities are
twenty points wrong. Someone sizing on "72%" is not using the ordering.

**Brier score** is proper — it is minimised by telling the truth, so confidence cannot
be used to game it.

**Brier skill score** decides deployment, because a raw Brier score means nothing alone.

Calibration itself is tested against a **parametric bootstrap null**: if the model were
perfectly calibrated, outcomes would be Bernoulli draws at the predicted probabilities,
so simulating from the model's own predictions gives the exact reference distribution.
The observed error becomes a percentile rather than a number compared to a threshold
someone chose — which matters, because calibration error shrinks with sample size and a
fixed cutoff would call every small sample well calibrated.

> **The easiest thing to misread in the package:** a *low* `calibration_p_value` is bad
> news. It means the miscalibration is larger than sampling noise explains.

---

## Refusals

A refusal is a first-class result, with a reason, and it is stored.

```json
{
  "is_deployable": false,
  "refusal": "this model beat simply assuming your average win rate on every trade on your history (skill score 0.095), but resampling your sessions puts the true figure anywhere from -0.058 to 0.207 — the range includes zero, so the apparent edge cannot be distinguished from luck yet"
}
```

"No skill" and "not enough evidence of skill" are different findings leading to different
actions — the first says the features are wrong, the second says there is not enough
history yet. The wording distinguishes them.

The database enforces it: a `CHECK` constraint makes a row that is both deployable and
refused impossible, so a backfill or a manual fix cannot produce a model that serves
nothing and explains nothing.

---

## Why no NumPy or scikit-learn

The models solve systems of at most a dozen unknowns. A 12×12 Gaussian elimination is
thirty lines; the dependency is not worth it for that.

Arithmetic is exact `Decimal`, and the usual justification — money must not drift — does
not apply to a standardised feature matrix. The real reason is different: **a stored
model must reproduce its predictions.** A probability served in October has to match the
backtest that justified deploying it in July. Floating-point results depend on the
platform's `libm`, on compiler flags, and on summation order; `Decimal` results depend
only on the recorded precision. A model whose output moves in the third decimal after a
container upgrade has a calibration report that is a historical curiosity.

The cost is real — fitting is seconds, not milliseconds — so training is an explicit
`POST` and milestone 13 moves it to a worker.

---

## What is deliberately absent

**Categorical features.** Strategy, setup, instrument and market condition are all
knowable at entry and all tempting. One-hot over a dozen setups adds a dozen parameters
to a 400-row problem; target encoding is leakage unless recomputed inside every training
fold, which is exactly the subtlety that survives review and corrupts results silently.
[Segmentation](./analytics.md) already answers "how do my setups compare" with a proper
significance test.

**Optimal stop and target models.** Choosing them needs a grid search selected on
training folds and measured out of sample; done naively it is precisely the curve-fitting
the [what-if simulator](./what-if.md) refuses to do. Deferred rather than shipped badly.

---

## What it does not claim

The model describes trades resembling past ones **in the features it can see**. It is not
a forecast of the market, and it cannot account for anything about a particular setup
that the history does not record. That caveat is in the payload, not in this document.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/predictions/train` | Fit, validate walk-forward, gate, and store both heads. |
| `GET` | `/predictions/next` | Win probability and expected R in the current session context. |
| `GET` | `/predictions/models/{head}` | The newest model for a head, refusals included. |
| `GET` | `/predictions/features` | What the models may look at — and what they structurally cannot. |
