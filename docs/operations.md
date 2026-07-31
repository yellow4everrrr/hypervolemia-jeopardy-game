# Running Ledgerline

Background workers, tenant isolation, and rate limits.

Design reasoning lives in [ADR 0011](./adr/0011-jobs-and-tenant-isolation.md).

---

## The one thing you must get right

**The application must connect as a role that is neither a superuser nor the owner of the
tenant tables.**

Row-level security is enabled and `FORCE`d on all 28 tenant tables. Postgres exempts
superusers unconditionally and table owners unless forced — and most deployments connect as
the role that ran the migrations. In that configuration the policies exist, are correct,
and **do nothing**. Measured on a real database before this was fixed:

```
connected as: postgres   superuser: True
UNBOUND connection sees 2 trades          <-- should be 0
bound to alice: sees 2 trades
   CROSS-TENANT ROWS VISIBLE: {bob's id}
```

Alice read Bob's trades through a correctly-configured policy, with no error anywhere.

Migration `0004` creates the role you need:

```sql
-- created by the migration, no password
ledgerline_app    -- NOLOGIN NOBYPASSRLS, DML on every table
ledgerline_admin  -- BYPASSRLS, for migrations and backfills only
```

Grant it a login and point the app at it:

```sql
ALTER ROLE ledgerline_app LOGIN PASSWORD 'set-me';
```

```bash
LEDGERLINE_DATABASE_URL=postgresql+asyncpg://ledgerline_app:...@host/ledgerline
```

Run migrations as the owner or `ledgerline_admin`, never as `ledgerline_app`.

**The app refuses to start in production if this is wrong.** `assert_rls_effective` checks
`relrowsecurity`, `relforcerowsecurity`, ownership and superuser status at boot; a false
result is fatal in production and a warning elsewhere, so local development against a
superuser connection still works.

---

## How isolation works

Every tenant table has:

```sql
CREATE POLICY tenant_isolation ON trades
USING      (user_id::text = current_setting('app.user_id', true))
WITH CHECK (user_id::text = current_setting('app.user_id', true));
```

The setting is written with `set_config(..., is_local => true)` — **transaction-scoped**, so
a binding cannot survive back into the connection pool and bind the next request to the
previous request's tenant.

Two places set it, and there are only two:

| Path | Where the tenant comes from |
|---|---|
| HTTP | `get_current_user`, from the verified Clerk principal |
| Worker | the `jobs.user_id` column on the claimed row |

`current_setting(..., true)` returns NULL when unset, and `user_id = NULL` is NULL rather
than true — so **an unbound connection sees nothing**. That is the correct failure
direction: a path that forgets to bind returns empty rather than everything.

`WITH CHECK` matters as much as `USING`. Without it a bound session could *insert* rows
owned by someone else — read isolation with no write isolation, which is the more damaging
half.

---

## Workers

```bash
python -m app.jobs.worker
```

One job per transaction: claim, bind the tenant, run, record, commit.

```sql
SELECT ... WHERE claimable ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1
```

The queue is a **table, not Redis**, because a job's completion and the rows it writes must
commit together. With an external broker they cannot: the worker either acks before its
transaction commits and loses work on rollback, or commits first and runs the job twice.
Claiming inside the same transaction as the work removes the second system entirely.

Postgres is not a message broker and this does not pretend otherwise — it polls, it holds a
row lock per job, and it will not do a million jobs a minute. It does a handful per trader
per day, correctly.

### Splitting workloads

```bash
python -m app.jobs.worker   # everything
```

`Worker(kinds=(JobKind.TRAIN_MODELS,))` restricts a process to certain kinds, so slow work
can be separated from latency-sensitive work without a second queue.

### What to expect operationally

| Behaviour | Value |
|---|---|
| Lease | 900s, extendable via `heartbeat()` |
| Retry backoff | 30s → 2m → 8m → 32m → 2h, capped |
| Attempts before dead-letter | 5 |
| Idle poll | 2s |

A worker killed mid-job leaves its row in `running`; the lease expires and another worker
reclaims it. **This is why handlers must be idempotent** — the dead worker may have
finished the work first.

| Handler | On a re-run |
|---|---|
| `detect_patterns` | replaces the previous scan's verdicts — converges |
| `generate_reports` | skips periods that already have a report — continues |
| `train_models` | **appends a second row**, deliberately |

`train_models` is the exception and it is documented rather than fixed: `prediction_models`
is an append-only audit table so that "the model said 68% on the 4th" stays answerable.
Suppressing the duplicate would mean making an append-only table conditionally not
append-only.

### Monitoring

```
GET /api/v1/jobs/queue
```

Two numbers matter. `dead` is work that will never run without intervention. `pending`
climbing while `running` stays at zero means **no worker is consuming the queue** — a depth
metric alone cannot distinguish that from "busy".

Every dead job carries `last_error` on the row, not just in a log line.

---

## Rate limits

Two buckets, because costs here differ by three orders of magnitude:

| Bucket | Rate | Burst |
|---|---|---|
| standard | 120/min | 240 |
| expensive | 6/min | 10 |

Expensive routes (`/patterns/scan`, `/predictions/train`, `/simulator/sweep`,
`/reports/generate`, `/reports/run-schedule`, `/coach/*`) consume from **both** — the
expensive limit is a sub-limit, not an alternative, or a client could spend its whole
standard allowance on scans.

Keyed on a SHA-256 prefix of the bearer token, never the token itself: the identity becomes
a Redis key and can reach a log line.

**It fails open.** If Redis is unreachable requests are allowed and the failure logged. A
rate limiter protects against load; it is not authorisation, and turning a Redis outage
into an API outage trades a degradation for a failure. Health and readiness probes are
exempt entirely — a limiter that can fail a readiness check will take a deployment down
during an incident, which is exactly when the probes matter most.

Rejections use the standard error envelope:

```json
{"error": {"code": "rate_limited",
           "message": "...",
           "details": {"bucket": "expensive", "retry_after_seconds": 8}}}
```

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | Queue a scan, training run or report batch. |
| `GET` | `/jobs` | List jobs, filterable by state and kind. |
| `GET` | `/jobs/{id}` | One job, with its result or error. |
| `GET` | `/jobs/queue` | Depth by state. |
| `POST` | `/jobs/{id}/cancel` | Cancel a job that has not started. |

A running job is deliberately **not** cancellable. Setting a flag no handler checks would
report a cancellation that did not happen, and the caller would stop waiting for work still
writing rows.

The synchronous endpoints remain. The queue is an optimisation: a deployment with no worker
still functions, just slower.
