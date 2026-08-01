# ADR 0003 — The sync cursor never advances past a fill that was not ingested

- **Status:** Accepted
- **Date:** 2026-07-31
- **Milestone:** 2

## Context

Tradovate's API shapes the sync in ways worth stating plainly, because each one drove a
decision:

1. **There is no date- or cursor-filtered fill endpoint.** `fill/list` returns every
   fill the authenticated user has. Incremental sync must pull the list and filter
   client-side on `id`, which is monotonic per user.
2. **A fill carries no `accountId`.** It references an `orderId`, and only the order
   knows the account. Attribution requires a second lookup.
3. **A fill carries no costs.** Commissions and fees live on a separate `FillFee`
   entity, and that record can lag the fill.
4. **A fill carries no contract specification.** It references a `contractId`; the
   chain to a point value runs contract → maturity → product.

So a fill arrives referencing three entities we must fetch before it can be journalled,
and any of those lookups can come back empty — a race with the broker's own writes, a
newly listed contract, an account the user has not linked here, a fee record that has
not settled.

The question is what to do with a fill we cannot yet interpret. The obvious options:

- **Skip it and move on.** The cursor advances, the sync reports success, and the trade
  is gone. Permanently: nothing ever looks below the cursor again. The trader's win
  rate, expectancy and P&L are all quietly wrong, and there is no artefact anywhere
  indicating that something is missing.
- **Ingest it with assumed values.** Guess the account, assume a point value, cost it at
  zero. The trade exists but is wrong — and because ingestion is idempotent, it will
  never be re-costed or re-attributed. A wrong number that looks right is worse than a
  missing one.
- **Fail the whole sync.** Correct but brittle: one unparsable record from a broker
  blocks every other trade that day.

## Decision

**A fill that cannot be fully interpreted is *deferred*, and the cursor stops below the
lowest deferred fill.**

Concretely:

- The mapper returns executions *and* deferrals, each with a reason. It never
  substitutes a default for a missing account, point value or cost.
- `SyncBrokerAccount._next_cursor` advances to the highest observed fill id, then clamps
  to `min(deferred ids) - 1`. Fills for unlinked accounts clamp it the same way.
- The cursor never moves backwards, so a stale deferral cannot rewind into history
  already synced.
- Everything else in the batch is ingested normally. One bad record costs a retry, not
  a day.
- Every run records what it fetched, wrote, skipped and deferred in `sync_runs`.

Two supporting rules follow from the same principle:

**Fee records get a five-minute grace period.** A fill younger than that with no fee
record is deferred — costing it at zero would overstate P&L permanently, because
idempotent ingestion never revisits it. A fill older than that is accepted as genuinely
free, because simulated accounts really do have no fees and deferring forever would
wedge the cursor and stop the journal dead.

**The WebSocket is an optimisation, never a source of truth.** Streams drop silently,
and Tradovate disconnects the oldest connection whenever a customer exceeds their
simultaneous-connection allowance — which happens every time the trader opens the
Trader app. Every reconnect therefore triggers a REST catch-up before the stream is
trusted again, and the scheduled REST sync runs regardless. The stream cuts latency
from minutes to milliseconds; it never carries sole responsibility for a fill.

## Consequences

**Good.** A missing trade is the one failure mode this system cannot tolerate, and it is
now structurally prevented rather than guarded against. Every deferral is visible in the
sync run and self-heals on the next pass, so transient broker races need no operator
intervention. The rule is directly testable, and it is —
`test_cursor_stops_below_a_deferred_fill` and
`test_unlinked_broker_account_holds_the_cursor_back` are the tests that would fail if
anyone "optimised" this away.

**Costs.** A persistently unmappable fill blocks the cursor indefinitely, stalling that
connection's sync. This is deliberate — stalling is loud, skipping is silent — but it
needs an operational alert, which milestone 13 adds. The mitigation available today is
that `sync_runs.detail` names the blocking fill and the reason.

**Throughput.** Pulling the full fill list every sync is O(account history) rather than
O(new fills). At a few thousand fills that is a sub-second response; at a hundred
thousand it will need attention. The cursor already limits the *work* to new fills — only
the fetch is unbounded — so the fix when it is needed is a narrower fetch, not a
redesign.

## Alternatives rejected

- **Trusting Tradovate's `fillPair` entity** for round-turn grouping. It exists, and
  milestone 2 could have used it instead of our own FIFO reconstruction. Rejected for
  the reason given in ADR 0001: broker groupings differ between brokers and cannot be
  verified. `fillPair` will instead be used as a *reconciliation check* against our
  reconstruction.
- **A time-based cursor.** Fill timestamps are not unique and the API does not filter on
  them, so a timestamp cursor would either re-read or skip at every boundary.
- **Storing deferred fills in a dead-letter table.** More machinery than holding a
  cursor, and it introduces a second source of truth about what has been ingested.
