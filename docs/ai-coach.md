# The AI coach

How Ledgerline uses a language model without letting it invent a number.

The reasoning is in [ADR 0002](./adr/0002-ai-evidence-contract.md) (the rule) and
[ADR 0007](./adr/0007-coach-placeholders.md) (the mechanism). This is the working
reference.

---

## The mechanism in one line

**The model never writes a number. It writes a placeholder, and Python substitutes the
computed value.**

What the model produces:

```
Your expectancy is {{core.expectancy}} across {{sample.trades}} trades.
```

What the trader reads:

```
Your expectancy is $31.42 across 248 trades.
```

The substituted value comes from the analytics engine. There is no code path in which a
model-authored digit reaches a trader as a statistic — not because the model was well
behaved, but because it was never given the opportunity.

---

## The pipeline

```
analytics engine ─┐
compliance engine ─┼─→ EvidenceBundle ─→ model ─→ claims ─→ validate ─→ render ─→ trader
pattern scan ─────┘                                            │
                                                               └─→ reject → stored, not shown
```

1. **Compute.** Three engines run: performance metrics, compliance scoring, pattern
   detection. Nothing is persisted — a coaching request must not silently rewrite the
   trader's stored metrics.
2. **Bundle.** Results are folded into a flat, keyed evidence set. This is the model's
   entire factual world.
3. **Ask.** The bundle goes to the model with a structured output schema.
4. **Validate.** Every claim is checked against the bundle.
5. **Render.** Only then are placeholders replaced with values.

---

## The evidence bundle

Each entry is more than a number:

```json
{
  "key": "core.expectancy",
  "label": "expectancy per trade",
  "display": "$31.42",
  "unit": "currency",
  "sample_size": 248,
  "confidence_interval": ["4.10", "58.90"],
  "reliability": "reliable"
}
```

The qualifiers travel with the figure so the model cannot quote an expectancy without
the evidence of how much data stands behind it. A model shown a bare number will quote
it with a confidence the number does not have.

**Undefined values carry their reason:**

```json
{
  "key": "core.profit_factor",
  "display": "not defined for this sample",
  "undefined_reason": "undefined — no losing trades in this sample"
}
```

A model shown `null` guesses. A model shown *why* the value is absent says so.

**Gaps are evidence too.** The bundle carries an explicit `gaps` list — scans that found
nothing, periods too short to annualise from, trades excluded for lacking a strategy.

### Inspecting it

```
GET /api/v1/coach/evidence
```

Returns the bundle with no model call. This is deliberate: the claim that the coach only
interprets computed statistics is checkable **by the trader**, not just by us. Anything
absent from that response is something the coach cannot say.

---

## Three tiers of claim

| Kind | Means | Language |
|---|---|---|
| `finding` | Survived a significance test with FDR control | "this is costing you", "the evidence shows" |
| `observation` | Computed and accurate, but untested or not significant | "in this sample", "so far", "worth watching" |
| `hypothesis` | The model's own reading — not computed | "my read is", "this looks like" |

`hypothesis` is the tier ADR 0002 carved out: qualitative reasoning is legitimate output.
It is the only tier that may stand without a citation, it must be labelled, and its
confidence is the model's own — never presented beside a Python-computed interval as
though they were the same kind of thing.

An **undefined** statistic may be discussed but never recommended on. Recommendations are
validated at the `finding` standard: a recommendation is a stronger act than an
observation, not a weaker one.

---

## What gets rejected

| Attempt | Outcome |
|---|---|
| `"your Friday win rate is 34%"` when nothing computed 34% | ✗ uncited number |
| `{{core.sharpe_ratio}}` when that key does not exist | ✗ unknown key |
| `"you have taken 249 trades"` when the count is 248 | ✗ off-by-one is still fabrication |
| `"expectancy is 31.4231"` when the value is 31.42 | ✗ invented precision |
| Recommending on an undefined profit factor | ✗ undefined key in a `finding` |
| A claim citing nothing at all | ✗ uncited claim |
| `"you have taken 248 trades"` (correct) | ✓ quoting, not inventing |
| `"$31.42"` against a stored `31.42` | ✓ formatting is normalised |
| `"42.4%"` against a stored ratio `0.4237` | ✓ percent/ratio conversion checked |
| `"there are 2 things to fix"` | ✓ small integers are exempt |

**Rejection is all-or-nothing.** Publishing the claims that passed would leave a
narrative built around missing pieces, and the trader would have no way to know a
paragraph had been removed.

**Rejected analyses are stored** with `evidence_validated = false`. The rejection rate is
how you find out a prompt has drifted; discarding rejections would hide it. A rising rate
is a reason to fix the prompt, never to relax the check.

---

## The prompt

`app/ai/prompts.py`, versioned as `coach-v1` and recorded on every row so advice stays
attributable when the prompt changes.

Most of it is constraint rather than instruction: what may be claimed, how a claim must
be qualified, and which words are not allowed about a statistic that has not cleared a
significance test. The persona is a Head Quantitative Researcher reviewing one trader's
results — direct, quantified, ranked by cost rather than by statistical certainty,
because the trader is deciding what to change on Monday.

Correlation is addressed explicitly. Every comparison in the bundle is observational,
and the prompt says so: *"Revenge trading costs you money" claims a mechanism the data
cannot establish.*

---

## Model configuration

| Setting | Value | Why |
|---|---|---|
| Model | `claude-opus-5` | Configurable via `LEDGERLINE_AI_MODEL` |
| Thinking | adaptive | Interpreting a significance-controlled evidence set benefits from it |
| Effort | `high` | Not a latency-critical path — a report is requested, not polled |
| `max_tokens` | 16,000 | Caps thinking *and* response together on Opus 5 |
| Output | `json_schema` | The shape is enforced, not parsed hopefully out of prose |
| Caching | system prompt | Constant across requests; evidence follows in the user turn |

A **refusal** (`stop_reason: "refusal"`) returns HTTP 200 with empty content. It is
reported as a result, not raised — indexing `content[0]` would surface a safety outcome
as an outage.

Cost and token counts are recorded per analysis. The coach is the most expensive path in
the product.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/coach/analyse` | Compute evidence, ask the coach, validate, store. |
| `GET` | `/coach/evidence` | The bundle, with no model call. |
| `GET` | `/coach/analyses` | Previous analyses; rejected ones behind a flag. |
| `GET` | `/coach/recommendations` | Open recommendations. |
| `PATCH` | `/coach/recommendations/{id}` | Accept, dismiss, or resolve. |

`PATCH` exists because the only question that matters about coaching is whether acting on
it changed the trader's numbers, and that cannot be answered without knowing which
recommendations were adopted.

Rate-limited per user per day (`LEDGERLINE_AI_DAILY_ANALYSIS_LIMIT`, default 50). The cap
is checked **before** the model call, so it bites before the spend.

---

## Persistence

| Column | Holds |
|---|---|
| `ai_analyses.input_metrics` | The exact bundle the model was shown, verbatim |
| `ai_analyses.output` | Rendered claims, recommendations, validation result |
| `ai_analyses.evidence_validated` | Whether every claim traced to the evidence |
| `ai_analyses.model` / `prompt_version` | What produced it |
| `ai_analyses.cost_usd` / tokens / latency | What it cost |
| `ai_recommendations.expected_improvement` | **Filled by the what-if simulator, never by the model** |

Storing the bundle verbatim is what makes a claim auditable months later: a disputed
sentence can be checked against the evidence that produced it, rather than against
whatever the analytics engine returns today.

---

## Testing without an API key

`app/ai/` may not import the Anthropic SDK — enforced by an architecture test. The
evidence bundle, the contract and the validator are pure, so the tests that prove a
fabricated statistic cannot reach a trader run in milliseconds with no network.

That is not a convenience. A guarantee that is expensive to check stops being checked.

`tests/unit/ai/test_contract.py` is written adversarially: every test plays a model that
has invented something. A well-behaved model is easy to validate; the failures that
matter are the fluent, plausible ones.
