# ADR 0011 — The queue is a table, and tenant isolation belongs in the database

**Status:** Accepted
**Date:** 2026-07-31
**Extends:** [ADR 0003](./0003-broker-sync.md)

## Context

Milestone 13 hardens the platform for deployment. Two of its pieces are architectural
rather than operational, and they turned out to be the same decision viewed twice.

Three surfaces — the pattern scan, model training and report generation — cost seconds
each and have been synchronous `POST` endpoints since they were built, each milestone
deferring the worker to this one. That debt is now due.

Separately, standing rule 6 says *every tenant-owned row carries `user_id`, and
repositories require it*. An audit run at the start of this milestone — an AST pass over
every query in the codebase, cross-referenced against the 26 user-scoped models — found
**no place the rule had been broken**. Every HTTP endpoint passes `user.id`; every
repository constrains on it. The convention has held for twelve milestones.

That is exactly the moment it becomes worth replacing, because the worker is what will
break it. Every path so far derives `user_id` from an authenticated principal, so a
missing filter is conspicuous in review — the parameter is right there. A background
handler has no principal, no request and nobody watching, and a
`select(Trade).where(Trade.id == ...)` written there looks entirely reasonable.

## Decision 1 — the job queue is a Postgres table, not Redis

Redis is already a dependency and a list-based queue would be less code. It is the wrong
choice here for one reason: **a job's completion and the rows it writes must commit
together.**

With an external broker they cannot. The worker either acknowledges the job before its
Postgres transaction commits — and loses the work when that transaction rolls back — or
commits first and then fails to acknowledge, and the job runs again. This is the dual-write
problem and it has no application-layer solution; every mitigation is a smaller window.

Claiming with `SELECT ... FOR UPDATE SKIP LOCKED` inside the same transaction as the work
deletes the second system. The state transition and the report it generated are one commit.
There is nothing to reconcile because there is nothing to reconcile *with*.

`SKIP LOCKED` is what makes it a queue rather than a bottleneck: without it ten workers
block on the same oldest row and serialise; with it each takes the oldest row nobody holds.

The costs are real and accepted. Postgres is not a message broker — this polls, it holds a
row lock for the duration of a job, and it will not scale to a million jobs a minute. The
workload is a handful of jobs per trader per day at seconds each, and correctness of the
*result* dominates queue throughput by a wide margin.

Three supporting choices:

* **Leases, not just locks.** A worker killed mid-job holds its lock until the connection
  dies, which behind a proxy can be a long time. Every claim writes `leased_until`, and a
  job past it is claimable again however the previous worker died.
* **At-least-once, so handlers must be idempotent.** The lease recovery above guarantees
  it: the original worker may have completed the work before dying. Each handler documents
  how it behaves on a re-run.
* **Dead-lettering, with the error on the row.** A failure is retried with geometric
  backoff; after `max_attempts` the job stops. A queue that retries forever turns one
  poison job into an outage, and a dead job whose cause lives only in a log line is a job
  nobody will diagnose.

The synchronous endpoints are deliberately kept. The queue is an *optimisation*: a
deployment with no worker still functions, just slower.

## Decision 2 — row-level security, and the measurement that changed it

Every tenant table gets a policy comparing `user_id` against a transaction-scoped session
variable, set by the HTTP session dependency and by the worker.

The worker sets it **from the job row**. That is the point: whatever a handler does, and
whatever its author forgot, Postgres will only show it one tenant's rows.

A GUC rather than a role per tenant, because roles do not pool — one pool per user does
not scale past a few hundred, and reconnecting per request costs more than the query.
`set_config(..., is_local => true)` is scoped to the transaction, so a binding cannot
survive back into the pool and bind the next request to the previous request's tenant.

The policy compares against `current_setting('app.user_id', true)`, which is NULL when
unset. `user_id = NULL` is NULL rather than true, so an unbound connection sees **nothing**.
That is the correct failure direction: a path that forgets to bind returns empty rather
than everything.

### What measuring it revealed

The first implementation created the policies, enabled RLS, and set `FORCE ROW LEVEL
SECURITY`. Probing it against a real database:

```
connected as: postgres   superuser: True
UNBOUND connection sees 2 trades          <-- should be 0
bound to alice: sees 2 trades
   CROSS-TENANT ROWS VISIBLE: {bob's id}
```

**Alice read Bob's trades through a correctly-configured policy.** Postgres exempts
superusers unconditionally and table owners unless forced — and most deployments connect
as the role that ran the migrations. The policies were present, correct, and completely
inert.

This is the worst shape a security control can have, because every check short of an
actual cross-tenant read reports success. The migration is there. The policies exist.
`FORCE` is set. Nothing errors.

Two changes followed, and both are load-bearing:

1. **The migration creates `ledgerline_app`** — `NOLOGIN NOBYPASSRLS`, granted DML on the
   schema plus default privileges for future tables — and the application must connect as
   it. Re-measured under that role: unbound sees 0 rows, bound sees exactly its own tenant.
2. **`assert_rls_effective` runs at startup and is fatal in production.** It inspects
   `relrowsecurity`, `relforcerowsecurity`, ownership and superuser status, because every
   one of those can silently disable the whole mechanism. In production a false result
   refuses to start; elsewhere it warns, so local development against a superuser
   connection still works.

The tests carry the same lesson. `tests/db/test_tenancy_integration.py` issues
`SET LOCAL ROLE ledgerline_app` before every assertion, and there is a test asserting the
check returns **False** for the suite's own superuser connection — because a tenancy test
written against the default connection would have passed while proving nothing.

## Decision 3 — two rate-limit buckets, failing open

A single global limit is the usual implementation and the wrong shape here: costs differ by
three orders of magnitude between reading a trade and running a pattern scan. A limit
generous enough for the blotter lets one client saturate every worker; one tight enough to
protect the scans makes the blotter unusable.

So there are two buckets, and an expensive request consumes from **both** — the expensive
limit is a sub-limit, not an alternative, or a client could spend its whole standard
allowance on scans.

The token bucket is a Lua script because read-modify-write from the client is a race: two
requests both read five tokens remaining and both proceed.

Keyed on a hash of the bearer token rather than the IP: IP keying punishes everyone behind
one NAT and is trivially evaded by whoever it would catch. A *hash*, because the identity
becomes a Redis key and can reach a log line, and a live credential in either is how a
bearer token leaks.

**It fails open.** If Redis is unreachable the request is allowed and the failure logged. A
rate limiter protects against load; it is not authorisation, and turning a Redis outage
into an API outage trades a degradation for a failure. This is stated explicitly because
"fail closed" is the right default for most middleware and the wrong one for this.

Health and readiness probes are exempt. A limiter that can fail a readiness check will
eventually take a deployment down during an incident — exactly when the probes matter most.

## Consequences

* The application must connect as `ledgerline_app` or another non-owner, non-superuser
  role. Deploying with the migration role leaves tenant isolation inert; the startup check
  refuses to boot in production rather than letting that pass.
* Migrations, backfills and the schema-drift check need to see everything, so
  `ledgerline_admin` (`BYPASSRLS`) exists for them. It is auditable and it is explicitly
  not the role the application uses.
* Existing database tests still run as a superuser and are unaffected, which is why the
  tenancy suite drops its own role rather than relying on the connection.
* Handlers must stay idempotent. `train_models` deliberately is not fully — it appends to
  an append-only audit table — and the handler documents why suppressing that duplicate
  would be worse than tolerating it.
* Jobs are per-tenant but not per-tenant *fair*: one trader importing three years of
  history can occupy every worker for a while. Acceptable at this scale, and the fix when
  it is not is a weighted claim rather than a second queue.
* `LISTEN`/`NOTIFY` was rejected. It would cut the poll delay and adds a failure mode — a
  notification delivered while no worker listens is lost — so a correct implementation
  still needs the poll as a backstop. Since the poll must exist, it is the whole design.
