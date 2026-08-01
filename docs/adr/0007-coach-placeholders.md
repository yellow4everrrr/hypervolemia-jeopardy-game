# ADR 0007 — The coach writes placeholders, not numbers

**Status:** Accepted
**Date:** 2026-07-31
**Implements:** [ADR 0002](./0002-ai-evidence-contract.md)

## Context

ADR 0002 set the rule in milestone 1: statistics are computed in Python, and the model
may only interpret them. It listed four enforcement mechanisms, of which the third was
"the model returns cited metric keys, and every cited key is validated against the
payload before the row is written."

Building milestone 9 showed that mechanism is necessary but not sufficient.

A model can cite `core.win_rate` — a real key, present in the payload — and write "your
win rate is 34%" beside it when the computed value is 42%. The citation validates. The
sentence is wrong. Worse, it is wrong in the most dangerous possible way: specific,
confident, and attached to a genuine-looking source. Key validation checks that the
model *pointed at* real evidence; it does not check that what it *said* matches.

Any post-hoc check on generated prose has the same shape of gap. You are comparing
free text against a set of values and hoping the comparison is exhaustive.

## Decision

**The model does not write numbers. It writes placeholders, and Python substitutes the
values.**

The coach emits:

> Your expectancy is `{{core.expectancy}}` across `{{sample.trades}}` trades.

The renderer replaces each placeholder with the value the analytics engine computed. The
number the trader reads is the computed number **by construction** — not because the
model behaved, and not because a checker caught it afterwards. There is no code path in
which a model-authored digit reaches a trader as a statistic.

This also closes a subtler hole: **formatting**. A model asked to write "0.4237 as a
percentage" can produce 42%, 42.4%, or 4.24%. Formatting is where a value quietly
becomes a different value, and it now happens in `EvidenceValue.display`, in Python,
once.

### The remaining gap, and how it is closed

A model can still write a bare numeral instead of using a placeholder. Two checks:

1. **Unknown placeholders reject the analysis.** A key absent from the bundle fails
   validation rather than rendering as empty text — a sentence with a value silently
   missing reads as a completed fact.
2. **Bare numerals reject the analysis unless they match the evidence.** Every digit
   outside a placeholder is treated as a claim and compared against every value in the
   bundle — the raw value, its rendered display, and its percentage/ratio conversion,
   normalised for currency symbols and separators. Quoting correctly is allowed;
   inventing is not.

The tolerance for (2) is the precision the model actually wrote. "31.4" against a
computed 31.42 is quoting; "31.4231" against the same value is inventing digits the
statistic does not have.

Small integers (0–10, 100) are exempt. Rejecting them would make the coach unable to
write "there are two things to fix", and they cannot carry a fabricated statistic.

## Decision 2 — three tiers of claim, and language that matches

Every claim carries a `kind`:

| Kind | Meaning | Permitted language |
|---|---|---|
| `finding` | Survived a significance test with FDR control | "this is costing you", "the evidence shows" |
| `observation` | Computed and accurate, but untested or not significant | "in this sample", "so far", "worth watching" |
| `hypothesis` | The model's own reading — not computed at all | "my read is", "this looks like" |

`hypothesis` is the tier ADR 0002 explicitly carved out: qualitative reasoning is
legitimate model output, and it is the only tier permitted to stand without a citation.
It must be labelled, and its confidence is the model's own — never presented beside a
Python-computed interval as though they were the same kind of thing.

An **undefined** statistic may be *discussed* but never *recommended on*. "Your profit
factor is undefined because you have no losing trades yet" is useful; "size up, your
profit factor is undefined" is not. Recommendations are validated at the `finding`
standard, because a recommendation is a stronger act than an observation.

## Decision 3 — rejection is all-or-nothing

If any claim fails, the whole analysis is withheld. Publishing the claims that passed
would leave a narrative built around missing pieces, and the trader would have no way to
know a paragraph had been removed — the remaining argument would read as complete.

Rejected analyses are **stored** with `evidence_validated = false`. The rejection rate is
the signal that a prompt has drifted, and discarding rejections would hide it. A rising
rate is a reason to fix the prompt, never to relax the check.

## Decision 4 — gaps are evidence

The bundle carries an explicit `gaps` list: statistics that are undefined and why, scans
that found nothing, periods too short to annualise from. A model shown an absence
guesses at it; a model shown "this is absent because the sample is too small" reports
that instead.

`GET /coach/evidence` returns the bundle without calling a model. The claim that the
coach only interprets computed statistics is therefore checkable by the trader, not just
by us: that endpoint is the complete set of things it is able to say.

## Consequences

* **The coach cannot discuss a statistic nobody implemented.** Widening what it can say
  means adding a key to `app/ai/builder.py` — an engineering task with a test, not a
  prompt edit. This is the cost ADR 0002 accepted, made concrete.
* **The purity boundary is load-bearing.** `app/ai/` may not import the Anthropic SDK,
  enforced by an architecture test. The tests proving fabrication is impossible run in
  milliseconds with no API key, and a guarantee that is expensive to check stops being
  checked.
* **Every analysis stores the exact bundle it was shown** in `ai_analyses.input_metrics`,
  alongside `model` and `prompt_version`. A disputed sentence can be checked against the
  evidence that produced it rather than against whatever the engine returns today.
* **The system prompt is cached and the evidence follows it** in the user turn. That
  ordering is what makes the cache hit; a per-trader system prompt would never be reused.
* **A refusal is a result, not an outage.** Safety classifiers return HTTP 200 with
  `stop_reason: "refusal"` and an empty `content`; indexing it would surface a safety
  outcome as an IndexError.
* `ai_recommendations.expected_improvement` stays empty until the what-if simulator
  (milestone 10) fills it. The model never writes an expected-improvement figure —
  that was ADR 0002's fourth mechanism and it remains intact.
