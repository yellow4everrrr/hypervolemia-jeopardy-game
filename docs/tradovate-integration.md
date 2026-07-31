# Tradovate integration

Reference for the milestone 2 sync pipeline. API details below were verified against
Tradovate's published specification (api.tradovate.com) rather than recalled.

## Requirements Tradovate imposes on the user

API access is gated by the broker, not by us:

- A **live** Tradovate account with more than **$1,000 in equity**
- An **API Access subscription**
- An **API Key** generated in the Trader application

A demo-only account cannot obtain API credentials, and the same key then works against
both the demo and live hosts. Without these three, no amount of correct code will
connect.

## Hosts

| Purpose | Demo | Live |
|---|---|---|
| REST | `https://demo.tradovateapi.com/v1` | `https://live.tradovateapi.com/v1` |
| WebSocket | `wss://demo.tradovateapi.com/v1/websocket` | `wss://live.tradovateapi.com/v1/websocket` |
| Market data | `wss://md.tradovateapi.com/v1/websocket` | same |
| Market replay | `wss://replay.tradovateapi.com/v1/websocket` | same |

Selected by `broker_connections.environment`; see `tradovate/config.py`.

## Authentication

`POST /auth/accesstokenrequest` with `{name, password, appId, appVersion, cid, sec}`
returns `{accessToken, mdAccessToken, expirationTime, userId, name, hasLive}`.

Three behaviours the code handles explicitly:

**HTTP 200 can mean failure.** A rejected login returns 200 with `errorText` set.
Success is decided by the presence of a token, never by the status code.

**Rate limiting is a timed penalty, not a 429.** Too many attempts return
`{"p-ticket", "p-time", "p-captcha"}`. The request was *not* handled; it may be retried
after `p-time` seconds with the ticket echoed in the body. Penalties up to 30 seconds
are waited out in-process; longer ones are surfaced so the scheduler backs off instead
of holding a worker. When `p-captcha` is true a third-party application cannot recover
at all — the user must sign in through the Trader application and wait roughly an hour.

**Tokens are renewed early.** `GET /auth/renewaccesstoken` is called ten minutes before
expiry, so a transient failure can be retried while the current token still works.
Renewal responses omit `mdAccessToken`, so the existing one is carried forward.

## The entity join

Tradovate exposes data with fine granularity and expects clients to assemble it. A fill
alone cannot be journalled:

```
Fill { id, orderId, contractId, timestamp, action, qty, price }
  │
  ├── Order      { id, accountId, contractId }        → which account?  (no accountId on the fill!)
  ├── FillFee    { id, commission, clearingFee,       → what did it cost?
  │                exchangeFee, nfaFee,
  │                brokerageFee, ipFee }
  └── Contract   { id, name, contractMaturityId }
        └── ContractMaturity { id, productId }
              └── Product    { valuePerPoint, tickSize }  → what is a point worth?
```

Naively that is five requests per fill. `TradovateClient` batches by unique id through
the `items` endpoints, turning a day's fills into a handful of requests.

`tick_value` is derived as `valuePerPoint × tickSize`. For ES: `50 × 0.25 = 12.50`,
matching the published contract specification — which is the check that the conversion
is the right way round.

## Incremental sync

There is **no date- or cursor-filtered fill endpoint**. `fill/list` returns everything,
and the cursor is the highest ingested fill id (monotonic per user), stored in
`broker_connections.sync_cursor`.

The cursor never advances past a fill that was not ingested. That rule and its
consequences are [ADR 0003](./adr/0003-broker-sync.md).

## Real-time stream

Frames are SockJS-derived: a type character plus an optional JSON array.

| Frame | Meaning |
|---|---|
| `o` | Session opened |
| `h` | Server heartbeat (~2.5s, **suppressed while streaming data**) |
| `a[...]` | Array of responses and/or events |
| `c` | Closed |

Client requests are four newline-separated fields:

```
<endpoint>\n<request id>\n<query>\n<json body>
```

Authorization is once per connection: `authorize\n1\n\n<accessToken>`, answered with
`a[{"s":200,"i":1}]`. Then `user/syncrequest` with `{"users":[<userId>]}` subscribes to
every change on the user's data, delivered as `props` events whose `entity` payload is
identical to the REST representation — which is what lets the stream and the backfill
share one mapping layer.

Two operational facts drive the connection manager:

**The client must heartbeat unconditionally.** The server sends no heartbeats while
actively streaming, so liveness cannot be inferred from inbound traffic. `[]` every 2
seconds regardless of what is arriving.

**Disconnection is routine.** Tradovate permits one simultaneous connection per customer
by default and disconnects the oldest when that is exceeded — so opening the Trader app
kicks our stream. Reconnect with backoff, and always run a REST catch-up before trusting
the stream again.

## Reconciliation

`POST /cashBalance/getcashbalancesnapshot` returns the broker's own `realizedPnL`.
`ReconcileAccount` compares it against our reconstructed net P&L for the session.

This is the system's smoke alarm. Our figure comes from FIFO reconstruction, pro-rata
fee allocation and decimal arithmetic; theirs is computed independently. A persistent
gap means one of us is wrong, and it is far more likely to be us. Only *closed* trades
are compared, because the broker's realized figure excludes open positions.

## Rate limits

Tradovate publishes no hard per-endpoint numbers and answers overuse with a time
penalty. The client applies a conservative token bucket (5 requests/second, burst 10),
because tripping the limit costs more than the requests it saves — the penalty blocks
the next request too.

`423` and `429` are both treated as rate limiting; `Retry-After` is surfaced when present.

## Credentials

Never stored in the database. `broker_connections.credential_ref` holds a pointer into a
secret store (`tradovate/<connection id>`); the secret itself lives in
`EnvironmentSecretStore` or a managed store. A database dump must not be enough to trade
on someone's account.

## Endpoints used

| Endpoint | Purpose |
|---|---|
| `auth/accesstokenrequest`, `auth/renewaccesstoken` | Token lifecycle |
| `account/list` | Mirror broker accounts on connect |
| `fill/list` | Backfill |
| `fillFee/items` | Commissions and fees |
| `order/items`, `order/list` | Account attribution |
| `orderVersion/ldeps` | Stop/target revision history (milestone 7 evidence) |
| `contract/items`, `contractMaturity/items`, `product/items` | Contract specifications |
| `cashBalance/getcashbalancesnapshot` | Reconciliation |
| `user/syncrequest` (WebSocket) | Real-time user data |

## Known gaps

- **The published OpenAPI schema is incomplete** for some runtime entities: the `Order`
  component omits `accountId`, which the live API plainly returns. Boundary models are
  therefore strict about fields we depend on and lenient about everything else, and
  `TradovateOrder` requires `accountId` regardless of what the spec says.
- **Exchange names are mapped from ids** by a small table in `source.py`. Tradovate
  exposes an exchange entity we do not otherwise need; an unknown id falls back to CME
  and the product id is preserved in the execution metadata for correction.
- **Currency is assumed USD.** Products carry a `currencyId` requiring another lookup;
  every Tradovate futures product a retail trader touches settles in USD. The id is
  preserved in the raw payload.
- **Not yet verified against a live account.** Every behaviour above is implemented from
  the published specification and tested against a mocked API. The first real connection
  may surface field-level surprises; they will be confined to `models.py` and
  `mapping.py`.
