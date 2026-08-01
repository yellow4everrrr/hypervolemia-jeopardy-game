# ADR 0009 — A model that cannot prove its skill is not served

**Status:** Accepted
**Date:** 2026-07-31
**Extends:** [ADR 0004](./0004-analytics-honesty.md), [ADR 0006](./0006-pattern-detection.md), [ADR 0008](./0008-counterfactual-simulation.md)

## Context

Milestone 11 adds predictive models over a trader's own history: the probability that a
trade taken now will win, and its expected R.

Every previous surface describes the past. This one makes a claim about a trade that has
not happened, and that changes the stakes in a specific way: **a trader will size on the
number.** "68% chance of a winner" is not a description, it is an instruction. A model
whose 68% comes up 45% of the time is strictly worse than no model, because it will
encourage larger positions on precisely the trades it is most wrong about.

The data makes this harder than the usual supervised-learning problem, in three ways that
compound. The sample is small — a few hundred to a few thousand rows. It is
non-stationary in every direction at once: the market regime changes, and so does the
trader, who is learning. And the outcome fields sit in the same record as the entry
fields, so the single most destructive mistake in the field is one attribute access away.

## Decision 1 — leakage is prevented structurally, not by convention

`EntrySnapshot` carries only what was knowable when the trade was opened. It has no
`net_pnl`, no `r_multiple`, no `mfe_r`, no `mae_r`, no `closed_at`, no
`duration_seconds`. Every predictive feature extractor takes a snapshot, so a feature
**cannot** reach an outcome — not by accident, not through a well-meaning refactor, and
not from whoever adds the ninth feature in two years.

The alternative was a naming convention and a review checklist. That was rejected because
the failure is invisible: a leaked model does not look suspicious from the outside, it
looks *excellent*. A test demonstrates the point by smuggling the trade's own P&L in as a
feature on data with no learnable signal at all; the model's error falls from 0.25 to
under 0.02, and nothing in any metric distinguishes it from a genuine discovery.

The temptation is concrete rather than hypothetical. `app.analytics.features` already
defines a feature set including `mfe_r`, `mae_r` and `capture_efficiency`, and it is
correct there — clustering is descriptive, so "how much of the available move did this
trade capture" is a legitimate description of a completed trade. Reusing that set for
prediction would be predicting the outcome from the outcome. Two feature sets exist
because the two questions are different, and the type system now enforces which is which.

The second leak is subtler and gets its own machinery: sequence context. Each snapshot
sees its session as it was *before* its own trade — the first trade of a day sees zero
prior trades and a flat session P&L, never the day's eventual total. A running total that
included the current trade would carry the outcome directly, in a feature that looks
entirely innocent.

## Decision 2 — validation is walk-forward and splits on sessions

Random k-fold cross-validation on time series trains on next March to predict last
January. The resulting score is not optimistic, it is meaningless.

Folds are strictly forward and the window expands rather than slides — a sliding window
silently changes the training size between folds, which makes the folds incomparable.

Folds also **never split inside a session**, for the same reason ADR 0006 treats a
trading day as the unit of behaviour: trades sharing a day share a market, a news cycle,
and a trader in one mood running one plan on one particular morning. Splitting
mid-session lets the model learn the day, and forward ordering alone does not prevent it.

The ridge penalty is chosen by a nested forward split *inside the training window*.
Selecting it on the test fold — including the informal version, where the reported number
is whichever penalty scored best — turns an out-of-sample figure into an in-sample one
wearing the wrong label. It is the most common way a walk-forward evaluation lies and it
leaves no trace in the output.

## Decision 3 — skill must be established, not merely positive

Found by probing, and the most instructive decision in the milestone.

The first version of the gate was `skill > 0`: serve the model if it beats the baseline
out of sample. Ten runs on histories whose outcomes were **independent of every feature**
produced skill scores scattered around zero — and one landed at **+0.014**. Under that
gate the model was deployable. A trader would have been shown per-trade win probabilities
derived from pure noise, and nothing in the payload marked them as such.

One in ten is a design defect. It is also the same defect ADR 0006 and ADR 0008 each
found in their own domain: **a point estimate compared against a threshold, with nothing
said about how far that estimate would move on a different sample.**

Skill now carries a bootstrap interval and deployment requires the interval to exclude
zero. Afterwards: zero false deployments in forty runs, with the positive control still
deploying.

**Sessions are the resampling unit.** Resampling individual trades would contradict the
argument in decision 2 — four correlated trades are not four independent pieces of
evidence — and would produce an interval roughly half the width it should be, which would
have concealed exactly the false positive this decision exists to prevent.

The interval holds the fitted models fixed while resampling the evaluation set, so it
measures uncertainty in *evaluating* the models rather than in which models different
data would have produced. It is therefore somewhat optimistic. Doing better means
re-running the whole walk-forward inside every bootstrap draw, which is not affordable
here; the limitation is stated in the payload rather than left for a reader to deduce.

## Decision 4 — calibration decides, and accuracy is never reported

Accuracy appears nowhere in the package. It is the metric everyone asks for and it is a
trap twice over: a trader who wins 42% of the time gets 58% accuracy from a model that
predicts "loss" every single time, and accuracy *improves* when a model pushes its
probabilities toward 0 and 1 — the opposite of calibration.

ROC-AUC is absent for a related reason. It sees only the ordering, so a model can rank
trades perfectly while its stated probabilities are twenty points wrong. Someone sizing
on "72%" is not using the ordering.

What is reported is the Brier score (proper — it is minimised by telling the truth, so
confidence cannot game it), the Brier skill score against the trader's own base rate, and
a calibration test against a **parametric bootstrap null**. If a model were perfectly
calibrated, outcomes would be Bernoulli draws at its predicted probabilities, so
simulating outcomes from the model's own predictions gives the exact reference
distribution. The observed calibration error is then a percentile rather than a number
compared against a cutoff somebody chose — which matters because expected calibration
error shrinks with sample size, and a fixed cutoff would call every small sample well
calibrated.

A consequence worth flagging because it reads backwards: **a low `calibration_p_value` is
bad news.** It means the miscalibration exceeds what sampling noise explains.

## Decision 5 — a refusal is a stored result with a reason

Models that fail any gate are stored, with `is_deployable = false` and plain-language
`refusal`. They are not discarded and not shown with a warning label — a warning label on
a dashboard is read once, whereas a model that will not load is read every time.

The wording distinguishes the two ways the skill gate can fail, because they lead to
different actions: "no better than assuming your average win rate" says the features are
wrong, while "beat it on your history, but resampling puts the true figure between −0.058
and 0.207" says there is not enough history yet.

A `CHECK` constraint enforces that a row is deployable exactly when it has no refusal.
This lives in the database rather than in application code because application code is
not the only thing that writes rows: a backfill or a manual fix could otherwise produce a
model that serves nothing and explains nothing.

## Decision 6 — exact decimal arithmetic, and no new dependency

The models solve systems of at most a dozen unknowns. A 12×12 Gaussian elimination with
partial pivoting is thirty lines and runs in microseconds; NumPy and scikit-learn are not
worth the dependency for that, and the same reasoning already produced a hand-written
k-means in milestone 8.

Arithmetic is `Decimal`. The usual justification in this codebase — money must not drift
— does not apply to a standardised feature matrix, so a different one is needed: **a
stored model must reproduce its predictions.** Coefficients are persisted, and a
probability served in October has to match the backtest that justified deploying it in
July. Floating-point results depend on the platform's `libm`, on compiler flags and on
summation order; decimal results depend only on the precision setting, which is stored
alongside the coefficients. A model whose output moves in the third decimal place after a
container upgrade has a calibration report that is a historical curiosity.

Fitting uses Newton's method rather than gradient descent, because Newton converges in
under ten iterations with no learning rate — and a learning rate is exactly the kind of
hyperparameter that gets tuned once on one trader's data and quietly fails on another's.
The ridge penalty is not optional: it makes the Hessian positive definite, which removes
the separable-fold failure where a coefficient runs to infinity and the model reports
certainty.

## Consequences

* Training costs seconds — dozens of fits plus two bootstraps — so it is an explicit
  `POST /predictions/train`, never a page render. Milestone 13 moves it to a worker.
* `GET /predictions/next` currently retrains rather than loading stored coefficients,
  because the sequence features are derived from history and reconstructing that context
  separately would be a second code path that could disagree with the first. This is
  temporary and marked as such.
* Most traders will see a refusal, and that is the intended behaviour rather than a
  shortfall. A few hundred trades is genuinely not enough to establish that a
  nine-feature model beats a single number.
* **Categorical features are deliberately absent.** One-hot over a dozen setups adds a
  dozen parameters to a 400-row problem; target encoding is leakage unless recomputed
  inside every training fold, which is the kind of subtlety that survives review and
  corrupts results silently. Segmentation already compares setups with a proper
  significance test.
* **Optimal stop and target models are deferred.** Choosing them requires a grid search
  selected on training folds and measured out of sample; done naively it is exactly the
  curve-fitting ADR 0008 refuses. Shipping it badly would undo that ADR.
* The AI layer may cite a model's probability, skill interval and calibration status, and
  must respect `is_deployable`. A refused model may be described as trained and not
  established — never as a prediction the trader can use.
