# ADR 0002 — The AI layer interprets computed statistics and never produces them

- **Status:** Accepted
- **Date:** 2026-07-31
- **Milestone:** 1 (schema), enforced in milestone 9

## Context

The product's differentiator is coaching that behaves like a head quantitative
researcher rather than a fortune cookie. That requires an LLM. It also creates the
failure mode most likely to destroy the product's credibility: a fluent, confident
sentence containing a number nobody computed.

"Your win rate on Friday afternoons is 34%" is indistinguishable, to a reader, from a
number derived from their actual trades — whether it came from an aggregation or from
next-token prediction. A trader who acts on a fabricated statistic and loses money has
been harmed by the product, and would be right never to trust it again.

Prompt instructions ("only use the provided data") reduce the rate of this. They do not
make it structurally impossible, and the failures they leave are exactly the confident,
plausible ones.

## Decision

Statistics are computed in Python. The model is given those statistics as data and is
permitted only to interpret them. Four mechanisms enforce this rather than request it:

1. **The metrics payload is the entire evidence base.** The analytics engine produces a
   structured payload; nothing else is passed as fact.
2. **The payload is persisted verbatim** in `ai_analyses.input_metrics`, alongside
   `model` and `prompt_version`.
3. **The model returns cited metric keys** with its prose. Before the row is written,
   every cited key is validated against the payload. A claim referencing a statistic
   not present is rejected, not published; the result is recorded in
   `evidence_validated`.
4. **Expected-improvement figures are computed, not generated.**
   `ai_recommendations.expected_improvement` is filled by the what-if simulator, with
   its own confidence interval.

Supporting rules already implemented in milestone 1:

- `safe_divide()` returns `None`, never infinity or zero, for undefined ratios. Profit
  factor with no losing trades is *undefined*; the model must receive "not defined for
  this sample" rather than a number it will interpret with confidence.
- `r_multiple()` returns `None` when a trade has no recorded stop, instead of a
  placeholder that would drag every average toward zero.
- `detected_patterns` stores `effect_size`, `p_value`, confidence bounds and
  `sample_size`. Below threshold, `is_significant = false` and the finding is surfaced
  as an observation.

## Consequences

**Good.** Every claim is traceable to a stored payload and reproducible. Prompt and
model changes are attributable rather than silently blended. The rule "the model may
only cite what it was given" is checkable in code and in review.

**Costs.** Every statistic the coach may discuss must exist in the analytics engine
first — the model cannot improvise a breakdown nobody implemented. This slows feature
work and is the correct trade. Validation adds a step that can reject output, which
needs monitoring: a rising rejection rate is a signal about the prompt, not a reason to
relax the check.

**Explicitly not covered.** This ADR constrains *statistics*. Qualitative reasoning —
"this looks like a chase entry after a missed move" — is legitimate model output, and
must be phrased as a hypothesis with a stated confidence, distinct from any
Python-computed confidence interval.
