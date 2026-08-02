# Deploying to Fly.io

Every command here is one **you** run. Nothing in this document asks you to paste a
credential into a chat window, and no step needs anyone but you to hold a secret — the
same rule the broker connection screen follows, for the same reason.

Read the **[What this deployment does not do](#what-this-deployment-does-not-do)** section
before pointing real money at it.

---

## What gets created

| Piece | What it is | Why |
|---|---|---|
| `ledgerline-api` | Fly app, two process groups | `app` serves HTTP; `worker` drains the job queue. One image, so a handler cannot drift from the code that enqueues it. |
| `ledgerline-web` | Fly app | Next.js, built standalone. |
| Postgres | Fly Postgres, or Timescale Cloud | See [the database note](#the-database-is-the-one-real-decision). |
| Redis | Upstash Redis | Cache and rate limiting. Everything in it is rebuildable. |
| Tigris bucket | Fly object storage | Chart screenshots. S3-compatible, which is why `s3_endpoint_url` is a setting. |

## Before you start

```bash
brew install flyctl        # or: curl -L https://fly.io/install.sh | sh
fly auth login
```

---

## 1. The database is the one real decision

`market_bars` is the only genuinely large table — one instrument at one minute for one
year is roughly 350,000 rows — and the schema declares it a TimescaleDB hypertable with
compression on older chunks.

**The migration does not require the extension.** It checks `pg_available_extensions`
first and creates ordinary tables when TimescaleDB is absent, so either option below
produces a working deployment. What you lose without it is chunk pruning and compression
on the largest table, which is a performance and storage question rather than a
correctness one.

I have **not** verified whether Fly's managed Postgres ships the `timescaledb` extension,
and you should not take my word for it either way — check before assuming:

```bash
fly postgres create --name ledgerline-db --region iad
fly postgres connect -a ledgerline-db
```
```sql
SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb';
```

One row means you get hypertables. No rows means ordinary tables, and if you want the
extension, use **Timescale Cloud** instead and skip `fly postgres create` — you will get a
connection string to use in step 3 exactly as you would Fly's.

Attach a Fly database (skip if you are using Timescale Cloud):

```bash
fly postgres attach ledgerline-db -a ledgerline-api
```

That sets `DATABASE_URL`. The application reads `LEDGERLINE_DATABASE_URL` and needs the
async driver, so set it explicitly in step 3 — `postgresql+asyncpg://…`, not
`postgres://…`. A sync URL fails at the first query, not at boot.

## 2. Create the apps and the bucket

```bash
cd services/api
fly launch --no-deploy --copy-config --name ledgerline-api --region iad

fly storage create --name ledgerline-media -a ledgerline-api
```

`fly storage create` sets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `BUCKET_NAME`
as secrets. Set `LEDGERLINE_S3_BUCKET` to the bucket it reports.

## 3. Secrets

Generate the encryption key **on your machine** and never let it leave:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```bash
fly secrets set -a ledgerline-api \
  LEDGERLINE_DATABASE_URL='postgresql+asyncpg://USER:PASSWORD@HOST:5432/DBNAME' \
  LEDGERLINE_REDIS_URL='rediss://…' \
  LEDGERLINE_SECRET_ENCRYPTION_KEYS='<the key you just generated>' \
  LEDGERLINE_S3_BUCKET='ledgerline-media' \
  LEDGERLINE_CLERK_JWKS_URL='https://<your-clerk-domain>/.well-known/jwks.json' \
  LEDGERLINE_CLERK_ISSUER='https://<your-clerk-domain>' \
  LEDGERLINE_CORS_ORIGINS='https://ledgerline-web.fly.dev'
```

Optional — the coach refuses to answer without it and says so on screen, which is a
working state, not a broken one:

```bash
fly secrets set -a ledgerline-api LEDGERLINE_ANTHROPIC_API_KEY='sk-ant-…'
```

**`LEDGERLINE_SECRET_ENCRYPTION_KEYS` is newest-first and comma-separated.** Rotation is a
prepend: add the new key at the front, re-write stored secrets, then drop the old one.
`MultiFernet` decrypts with any key in the list and encrypts with the first, so there is
no window in which a stored broker credential cannot be read.

**Losing this key loses every stored broker credential.** They are not recoverable from a
database backup — that is the point of encrypting them. Put it in a password manager
before you continue.

## 4. Deploy

```bash
cd services/api && fly deploy
```

Watch the release command. `alembic upgrade head` runs before any new machine takes
traffic, and a failure aborts the release rather than leaving half the fleet on a schema
it does not understand.

```bash
cd ../../apps/web
fly launch --no-deploy --copy-config --name ledgerline-web --region iad
```

Edit `NEXT_PUBLIC_API_BASE` in `apps/web/fly.toml` to your API's real hostname **before**
deploying. It is a build argument, not an environment variable: Next inlines every
`NEXT_PUBLIC_*` during `next build`, so it is a string literal inside the client bundle by
the time the container starts. Setting it as a secret does nothing at all.

```bash
fly deploy
```

## 5. Check it actually works

```bash
curl -fsS https://ledgerline-api.fly.dev/health
curl -fsS https://ledgerline-api.fly.dev/health/ready
```

`/health` is liveness and deliberately does not touch the database — a brief database
blip should not restart every machine. `/health/ready` does check dependencies.

An unauthenticated request must be refused. If this returns data, `auth_dev_bypass` is on
somewhere it should not be, and every trader's history is public:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://ledgerline-api.fly.dev/api/v1/trades
# expect 401
```

Confirm both process groups are up — a missing `worker` means jobs queue forever while
every screen reports success:

```bash
fly status -a ledgerline-api
fly logs -a ledgerline-api
```

In the logs you should see `app.secret_encryption_ready` and `app.object_storage_ready`.
Their absence means the process did not get that far.

---

## What this deployment does not do

Stated plainly, because a deployment that looks finished is the failure mode this whole
codebase is built around.

- **Nothing has ever run against a live broker.** Tradovate sync is tested entirely
  against fakes. Rate limits, reconnects, partial fills and cursor resumption after a
  dropped socket are all unexercised. This is the largest untested surface in the project.
- **No production environment has ever run this.** These files are written from the code,
  not from a deployment that has been observed working. Expect to find something.
- **There is no backup policy here.** Fly Postgres snapshots are not a backup strategy,
  and the Fernet key is not in the database.
- **There is no staging environment.** Consider deploying a second app with
  `LEDGERLINE_ENVIRONMENT=staging` and a separate database before pointing anything real
  at production.
- **RLS is enforced, and the check is fatal in production only.** `_verify_tenant_isolation`
  warns elsewhere. Postgres exempts superusers from row-level security unconditionally, so
  connecting the application as the owning role silently disables tenant isolation. Use a
  non-superuser application role.

## The failure this configuration was written to prevent

`S3ObjectStore` imports `aioboto3` lazily so the tests and the domain carry no AWS SDK.
For a while it was neither a declared dependency nor installed by the Dockerfile, which
meant a deployed image would boot, answer every endpoint, pass every health check — and
raise `RuntimeError` at the first screenshot capture. Locally the store is a directory, so
that code never runs and no amount of local testing would have found it.

`pyproject.toml` now declares an `s3` extra, the Dockerfile installs `.[s3]`, and
`_verify_object_storage` refuses to start a deployed process without it. If you see that
error at boot, the image was built without the extra.
