# ADR 0001 — Trades are reconstructed from fills, FIFO, by a pure function

- **Status:** Accepted
- **Date:** 2026-07-31
- **Milestone:** 1

## Context

The product's promise is zero manual journaling: connect a broker, and every trade
appears, correctly, with no typing. That makes the fill→trade grouping the single most
consequential piece of code in the system. If it is wrong, every statistic, every AI
insight and every replay is wrong in a way that looks entirely plausible.

Brokers hand us fills, not trades. Tradovate reports each partial fill separately, with
its own timestamp, price and commission. Turning those into something a trader
recognises requires three decisions.

### Decision 1 — what is a trade?

Options considered:

1. **One fill = one trade.** Trivially correct, useless. It rewards slicing an order
   into pieces (more "trades", better-looking win rate) and punishes adding to a winner.
2. **One day = one trade.** Hides which decision produced the result — the thing the
   journal exists to evaluate.
3. **Round turn: flat → position → flat.** Scale-ins and scale-outs belong to the same
   trade; a position flip ends one and starts another.

### Decision 2 — which matching convention?

FIFO, LIFO, or average-cost. Average cost destroys the notion of a holding period for
scaled positions. LIFO is legal in some jurisdictions but makes "how long did I hold
this?" answer with the *most recent* lot, which is not what a trader means.

### Decision 3 — where does it live?

Either in SQL (a window function over executions), in the ORM layer, or as a pure
function in the domain layer.

## Decision

**A trade is a round turn.** Reconstruction is **FIFO**. The engine is a **pure
function** — `reconstruct_trades(executions, specs, opening_state) → trades, state` —
in `app/domain/trading/matching.py`, with no database, clock, network or configuration.

Supporting choices:

- Fills sort by `(executed_at, sequence, external_id)`, so pagination order cannot
  change the output.
- A fill spanning a position flip splits across both trades, with commissions and fees
  allocated **pro rata by quantity**, so per-trade costs sum to exactly what the broker
  charged.
- The engine accepts and returns `PositionState` (open lots plus the in-flight trade),
  making incremental sync O(new fills) rather than O(entire history).
- A fill for an instrument with no known specification raises rather than assuming a
  point value.
- Duplicate external ids raise rather than being silently de-duplicated.

## Consequences

**Good.** The highest-risk code in the product is tested without any infrastructure —
30 cases covering scale-ins, scale-outs, flips, cost allocation, ordering, multi-account
and multi-instrument netting, run in about a millisecond each. Determinism means a
rebuild is always safe: reprocessing a day produces identical trades, which is what
makes the `(account_id, origin_execution_id)` upsert key work. The purity also means a
second broker requires no changes here at all.

**Costs.** Trade rows are derived state that must be kept in sync with fills — handled
by making rebuilds idempotent and versioned (`reconstruction_version`). Resumable state
is a second source of truth about open positions; the mitigation is
`test_incremental_matches_full_rebuild`, which asserts that fill-by-fill processing
equals batch processing, and a documented fallback of full rebuild if it ever diverges.

**Refusing to guess is a feature.** A missing instrument spec fails the sync loudly.
The alternative — assuming a tick value — produces P&L wrong by a constant factor,
which nobody notices until they reconcile against a broker statement months later.

## Alternatives rejected

- **SQL window functions.** Fast, but position flips and pro-rata cost allocation
  become nearly unreadable, and the logic could not be unit-tested without a database.
- **Trusting the broker's own trade grouping.** Tradovate does report position-level
  P&L, and milestone 2 will reconcile against it — but as a *check*, not a source.
  Broker groupings differ between brokers, and depending on them would make our
  statistics unportable and unverifiable.
