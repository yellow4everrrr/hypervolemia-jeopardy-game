# ADR 0005 — Compliance scoring is three-valued, weighted, and capped

**Status:** Accepted
**Date:** 2026-07-31
**Supersedes:** none

## Context

Ledgerline scores every imported trade against the rules of the strategy it was taken
under, producing a compliance number the trader sees on the trade, the day, and the
month. That number will be looked at more often than any statistic in the system, and
it will be used to decide whether a losing month was a bad plan or bad execution. If it
is wrong in a way that flatters, it is worse than absent.

Three design questions had to be settled before any of it could be built.

## Decision 1 — rules are a declarative AST, not code

A rule is stored as a JSON predicate tree and evaluated by an interpreter in
`app/domain/rules/ast.py`. The obvious alternative — storing a Python or JavaScript
snippet and executing it — turns every user's strategy definition into remote code
execution against our own servers, and no sandbox makes that a good trade for the
expressiveness gained.

The AST buys three things beyond safety:

* **Inspectable.** `Node.describe()` renders a rule back into English, which is what
  the compliance report and the rule builder's preview show. A trader whose rule reads
  differently than they intended has caught their own bug before saving it.
* **Diffable.** Strategy versions can be compared field by field, so "what changed
  between v2 and v3?" has a real answer.
* **Bounded.** `MAX_DEPTH = 12`. A tree deeper than that is generated or hostile, and
  either way the recursive evaluator should not follow it.

Rules reference facts by name from an explicitly enumerated vocabulary
(`AVAILABLE_FIELDS`), not by reflection over an object. Reflection would let a rule
bind to an internal attribute that a refactor renames, at which point the rule silently
becomes unevaluable and the trader is never told.

## Decision 2 — evaluation is three-valued

Every rule evaluates to `PASS`, `FAIL`, or **`UNEVALUABLE`**.

The third value is the one that matters. A rule about stop distance cannot be checked
on a trade with no recorded stop. Both binary options are wrong:

* Scoring it as a pass inflates every score, and inflates them most for the traders
  with the worst data — precisely the ones who need the number to be honest.
* Scoring it as a failure invents indiscipline out of a gap in our own ingestion. The
  trader is being told they broke a rule when what actually happened is that we did not
  capture their stop order.

So `build_context()` **omits** unknown keys rather than setting them to `None`, and an
absent key makes the comparison unevaluable. Presence flags (`has_stop`, `has_target`)
are the deliberate exception: "was a stop recorded?" is answerable even when the answer
is no, unlike "how far away was it?".

Unevaluable results are excluded from the scoring denominator and reported separately
as **coverage**. A score of 100% over two of twelve rules is a different claim from
100% over all twelve, and the payload makes the difference visible rather than leaving
it to be inferred.

When nothing at all could be checked, the score is `None`, not zero. Zero would claim
total indiscipline; the honest answer is that the journal lacks the data to say.

## Decision 3 — severity is weighted and a critical breach caps the score

Rules carry a severity, and severity carries weight:

| Severity | Weight |
|---|---|
| advisory | 0.5 |
| minor | 1 |
| major | 3 |
| critical | 8 |

Beyond the weighting, a trade that breaches a *critical* rule cannot score above
`CRITICAL_VIOLATION_CEILING = 0.5`, however many other rules it satisfied.

An unweighted average would report 90% compliance for a trade that passed nine
advisories and blew through its hard risk limit. Trading discipline is not an average.
Entering half a point late and trading without a stop are not two units of the same
quantity, and a score that treats them as such trains the trader to optimise the
number instead of the behaviour.

The ceiling is a cap rather than a fixed penalty: a critical breach that also fails
most other rules keeps its lower score. The rule is "a critical violation means the
trade was not compliant", not "a critical violation means exactly half".

## Decision 4 — sequence-sensitive rules see the session as it was

Rules like "no more than six trades a session" and "wait ten minutes after two losses"
are about a trade's position in a sequence, not about the trade itself. Each trade is
therefore evaluated against the session state as it stood **immediately before** it was
taken — zero prior trades for the first of the day, the running P&L excluding its own
result, the loss streak it walked into.

This forces a constraint on the read path: **compliance runs load whole sessions**. A
`LIMIT` on trades would cut a day in half and tell the engine that the trader's fourth
trade was their first. The repository resolves a session-date floor with a grouped
query first and loads by date, so the window is bounded by trading days rather than by
rows.

## Decision 5 — strategies are versioned, never edited in place

Revising a strategy's rules creates a new version and deactivates the old one, linked
through `supersedes_id`. Editing rules in place would silently rewrite history:
yesterday's 92% would be recomputed against rules that did not exist yesterday, and the
compliance trend — the thing the number is actually for — would become meaningless.

## Consequences

* Compliance scores are comparable across trades only when coverage is comparable, so
  coverage is returned alongside every score and shown in the UI.
* The rule vocabulary is a published API contract (`GET /strategies/fields`). Adding a
  field to `TradeFacts` without adding it to `AVAILABLE_FIELDS` makes it unreachable;
  removing one breaks saved rules, so removal requires a migration of stored
  expressions.
* Rule impact figures are **descriptive**. Trades that broke a rule differ from trades
  that did not in ways beyond the rule itself — the same state of mind that widens a
  stop also picks worse entries. The API returns that caveat in the payload rather than
  in documentation, because a number that persuasive needs its limits attached to it.
  This is the same standard ADR 0004 sets for the analytics engine and ADR 0002 sets
  for the AI layer.
* The AI coach (milestone 9) may cite a compliance score, its coverage, and a specific
  rule verdict, because all three are computed here. It may not describe a trade as
  disciplined or reckless on any other basis.
