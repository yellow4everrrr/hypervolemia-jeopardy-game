# Strategy builder & compliance engine

How Ledgerline turns "did I follow my plan?" into a number with an audit trail.

The design decisions behind this are recorded in
[ADR 0005](./adr/0005-compliance-scoring.md); this document is the working reference.

---

## The shape of a rule

A rule is a JSON predicate tree. The two node kinds are **comparisons** and
**combinators**.

```json
{ "field": "risk_pct_of_equity", "op": "lte", "value": "0.01" }
```

```json
{
  "any": [
    { "field": "consecutive_losses", "op": "lt", "value": 2 },
    { "field": "minutes_since_last_trade", "op": "gte", "value": 10 }
  ]
}
```

Combinators are `all`, `any` and `none`, and nest to a depth of 12.

Operators: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `between`, `in`, `not_in`, `is_set`,
`is_not_set`, `contains`.

Numeric thresholds are written as **strings** when they are money or ratios
(`"0.01"`, `"1.5"`). The whole system is `Decimal`; a JSON number would arrive as a
float and lose precision before it was ever compared.

Every rule carries a `label` used in reports. Without one the engine renders the
comparison itself — `risk pct of equity is at most 0.01` — which is readable but rarely
as good as what the trader would write.

---

## The field vocabulary

Rules reference facts by name. The full list is served by
`GET /api/v1/strategies/fields` and defined in `app/domain/rules/context.py`.

| Group | Fields |
|---|---|
| Trade shape | `direction`, `instrument`, `quantity`, `entry_price`, `exit_price` |
| Timing | `entry_time`, `entry_hour`, `weekday`, `session_segment`, `duration_seconds` |
| Risk | `has_stop`, `has_target`, `risk_amount`, `risk_pct_of_equity`, `planned_r_multiple`, `stop_moved`, `stop_widened` |
| Outcome | `net_pnl`, `r_multiple`, `mae_r`, `mfe_r` |
| Context | `setup`, `market_condition`, `confidence`, `emotion` |
| Session discipline | `trades_today`, `session_pnl_before`, `consecutive_losses`, `minutes_since_last_trade`, `is_after_daily_loss_limit` |

Two of these deserve a note:

**`stop_widened`** is true when the final stop was further from entry than the initial
one. Moving a stop *closer* is risk reduction and is not flagged; moving it further is
the behaviour that turns a planned loss into a large one.

**`risk_pct_of_equity`** divides the trade's planned risk by the account equity **as of
that trade**, read from the nearest balance snapshot at or before it (within seven
days). Using today's balance would grade a $500 risk taken on a $10,000 account against
the $80,000 the account holds two good years later, and every early trade would look
disciplined.

A rule naming a field outside this list is rejected when the strategy is saved. The
response includes the vocabulary, because a typo should come back with the answer.

---

## Three-valued evaluation

| Outcome | Meaning |
|---|---|
| `pass` | The rule was checked and satisfied. |
| `fail` | The rule was checked and broken. |
| `unevaluable` | The rule could not be checked — the trade lacks the data it needs. |

Unevaluable is not a failure. `build_context()` omits keys it has no value for, and an
absent key makes the comparison unevaluable rather than false.

Combinators propagate unevaluability **only when it changes the answer**. An `any`
group with one passing child is a pass even if a sibling could not be checked, because
the rule is satisfied regardless. An `all` group with one failing child is a fail for
the same reason.

---

## Scoring

```
score = Σ weight(passed rules) / Σ weight(checkable rules)
weight = rule.weight × severity_weight[rule.severity]
```

Severity weights are `advisory 0.5`, `minor 1`, `major 3`, `critical 8`.

Unevaluable rules appear in neither sum. They are reported as **coverage**:

```
coverage = checkable rules / total rules
```

Two guards on the result:

* **A critical violation caps the score at 0.5.** A hard risk limit breached is not
  offset by good entry timing.
* **A trade with nothing checkable scores `None`, not zero.** Zero would claim total
  indiscipline; `None` says the journal cannot tell.

### Worked example

A strategy with three rules — critical (weight 8), major (3), minor (1) — on a trade
that broke the critical rule, passed the major one, and had no data for the minor one:

* checkable weight: 8 + 3 = 11 (the minor rule is excluded)
* earned weight: 3
* raw score: 3/11 ≈ 0.273
* critical breach → capped at 0.5, which does not bind here
* coverage: 2/3 ≈ 0.667

The same trade with the critical rule *passed* and the major one broken scores 8/11 ≈
0.727. Same violation count, very different number — which is the point of weighting.

---

## Session context

Sequence-sensitive rules are evaluated against the session as it stood immediately
before the trade:

| Field | As of |
|---|---|
| `trades_today` | Trades already closed this session, excluding this one. The first trade of the day sees `0`. |
| `session_pnl_before` | Running session P&L, excluding this trade's own result. |
| `consecutive_losses` | Losing trades immediately preceding this one. A winner resets the streak; a scratch neither continues nor breaks it. |
| `minutes_since_last_trade` | Minutes between the previous trade's close and this entry. |

Because of this, a compliance run **loads whole sessions**. The `sessions` query
parameter bounds a run by trading days, not by trades — a half-loaded day would report
every trade in it against a fabricated position in the sequence.

---

## Starter rules

Eight templates are offered at onboarding, served by
`GET /api/v1/strategies/starter-rules`:

| Code | Severity | What it enforces |
|---|---|---|
| `always_use_a_stop` | critical | Every trade has a stop recorded. |
| `never_widen_a_stop` | critical | Stops are never moved further from entry. |
| `stop_after_daily_loss_limit` | critical | No trades once the daily loss limit is hit. |
| `risk_at_most_1_percent` | major | Risk at most 1% of equity per trade. |
| `max_trades_per_session` | major | At most six trades in a session. |
| `pause_after_two_losses` | major | Wait ten minutes after two consecutive losses. |
| `no_revenge_sizing` | major | Do not increase size while down on the session. |
| `minimum_reward_to_risk` | minor | Planned reward is at least 1.5× risk. |

Each targets a leak that shows up in most traders' histories, and each is checkable from
data the journal imports automatically. A template that needed manual input would score
as unevaluable from day one and teach the trader that the number does not mean anything.

---

## Versioning

Strategies are versioned. `POST /strategies/{id}/revisions` creates version *n+1*,
deactivates version *n*, and links them through `supersedes_id`. Compliance scores
computed under the old rules stay valid, because the rules that produced them still
exist.

---

## Rule impact

`GET /api/v1/compliance/rules/{rule_id}/impact` compares P&L on trades that broke a
rule against trades that followed it, and reports the difference and its total across
violations. Unevaluable trades are excluded from both groups — including them in either
would contaminate the comparison with trades the rule never applied to.

This is a **descriptive** comparison. Violations are not randomly assigned: the same
state of mind that widens a stop also picks worse entries, so the difference measures
what violation is associated with, not what it caused. The payload carries that caveat
in an `interpretation` field so it travels with the number.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/strategies/fields` | The rule vocabulary, with descriptions and severity weights. |
| `GET` | `/strategies/starter-rules` | Templates, each rendered in English. |
| `POST` | `/strategies/validate` | Parse an expression, return what it reads as and which fields it uses. |
| `GET` | `/strategies` | List strategies with their rules. |
| `POST` | `/strategies` | Create a strategy. Rules are validated at save time. |
| `GET` | `/strategies/{id}` | One strategy with its rules. |
| `POST` | `/strategies/{id}/revisions` | Supersede a strategy with a new version. |
| `POST` | `/compliance/evaluate` | Score trades and persist verdicts. Bounded by `sessions`. |
| `GET` | `/compliance/trades/{id}` | The stored rule-by-rule verdict for one trade. |
| `GET` | `/compliance/rules/{id}/impact` | What breaking one rule has been associated with. |

---

## Persistence

| Table | Holds |
|---|---|
| `strategies` | Versioned strategy definitions with a checklist. |
| `strategy_rules` | One row per rule; `expression` is the JSONB AST. |
| `rule_evaluations` | One row per (trade, rule): `passed`, `evaluable`, and the full detail payload. Upserted on the natural key, so recomputation converges. |
| `trades.compliance_score` | `NUMERIC(18, 8)`, or `NULL` when nothing was checkable. |

`rule_evaluations` stores `evaluable` as its own column rather than encoding
unevaluability as `passed = NULL`. Aggregates over the table are the main consumer, and
a nullable boolean invites `WHERE NOT passed` to silently miss the rows that matter
most.
