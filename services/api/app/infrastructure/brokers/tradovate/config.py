"""Tradovate hosts, endpoints and protocol constants.

Everything environment-specific lives here so that switching a connection between demo
and live is a data change on ``broker_connections.environment``, not a code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TradovateEnvironment(StrEnum):
    DEMO = "demo"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class TradovateHosts:
    rest: str
    websocket: str
    market_data_websocket: str


_HOSTS: dict[TradovateEnvironment, TradovateHosts] = {
    TradovateEnvironment.DEMO: TradovateHosts(
        rest="https://demo.tradovateapi.com/v1",
        websocket="wss://demo.tradovateapi.com/v1/websocket",
        market_data_websocket="wss://md.tradovateapi.com/v1/websocket",
    ),
    TradovateEnvironment.LIVE: TradovateHosts(
        rest="https://live.tradovateapi.com/v1",
        websocket="wss://live.tradovateapi.com/v1/websocket",
        market_data_websocket="wss://md.tradovateapi.com/v1/websocket",
    ),
}


def hosts_for(environment: TradovateEnvironment | str) -> TradovateHosts:
    return _HOSTS[TradovateEnvironment(environment)]


class Endpoint(StrEnum):
    """REST paths, relative to the versioned base URL.

    Tradovate's convention is ``<entity>/<operation>``: ``item`` for one by id,
    ``items`` for a batch by ids, ``list`` for everything, ``deps``/``ldeps`` for
    children of a master entity.
    """

    ACCESS_TOKEN = "auth/accesstokenrequest"
    RENEW_TOKEN = "auth/renewaccesstoken"

    ACCOUNT_LIST = "account/list"
    FILL_LIST = "fill/list"
    FILL_FEE_LIST = "fillFee/list"
    FILL_FEE_ITEMS = "fillFee/items"
    ORDER_LIST = "order/list"
    ORDER_ITEMS = "order/items"
    ORDER_VERSION_LDEPS = "orderVersion/ldeps"
    POSITION_LIST = "position/list"
    CONTRACT_ITEMS = "contract/items"
    CONTRACT_MATURITY_ITEMS = "contractMaturity/items"
    PRODUCT_ITEMS = "product/items"
    CASH_BALANCE_SNAPSHOT = "cashBalance/getcashbalancesnapshot"


#: Client heartbeat interval. The server drops connections that go quiet for longer
#: than ~2.5s, and it stops sending its own heartbeats while streaming data — so the
#: client cannot infer liveness from server traffic and must beat unconditionally.
HEARTBEAT_INTERVAL_SECONDS = 2.0

#: How long to wait for a reply to a WebSocket request before giving up on it.
REQUEST_TIMEOUT_SECONDS = 30.0

#: Tradovate allows one simultaneous connection per customer by default and
#: disconnects the oldest when that is exceeded. A dropped connection is therefore an
#: ordinary event — possibly the user opening the Trader app — not an error state.
RECONNECT_BASE_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 60.0

#: Renew the access token this long before it expires, so a renewal failure has room
#: to be retried while the current token is still valid.
TOKEN_RENEWAL_MARGIN_SECONDS = 600

#: Conservative client-side request budget. Tradovate publishes no hard numbers for
#: most endpoints and answers overuse with a time penalty (see `auth.py`), so the
#: cheaper move is to stay well under any plausible limit.
REQUESTS_PER_SECOND = 5.0
REQUEST_BURST = 10

#: Batch size for `items`/`ldeps` lookups. Tradovate passes ids in the query string,
#: so the practical ceiling is URL length rather than a documented limit.
BATCH_SIZE = 100
