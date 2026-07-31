"""Tradovate implementation of :class:`BrokerSyncSource`.

This is where Tradovate's fine-grained entity model is joined back together. A fill on
its own is nearly useless: it names an order and a contract by id and says nothing
about which account it belongs to or what a point of the instrument is worth. Turning
a page of fills into journalable executions takes four batched lookups:

    fills → orders          (which account?)
          → contracts       (which product?)
          → maturities      → products   (what is a point worth?)
          → fill fees       (what did it cost?)

Doing that per fill would be five HTTP calls per trade. Batching by unique id turns a
day's fills into a handful of requests, which is what keeps the sync inside the rate
budget.
"""

from __future__ import annotations

from app.application.use_cases.sync_broker_account import DeferredItem, FetchResult
from app.core.logging import get_logger
from app.infrastructure.brokers.tradovate.client import TradovateClient
from app.infrastructure.brokers.tradovate.mapping import ReferenceData, map_fills
from app.infrastructure.brokers.tradovate.models import TradovateFill

logger = get_logger(__name__)

#: Tradovate exposes exchange names through an entity we do not otherwise need. These
#: cover every exchange its futures products list on; an unknown id falls back to CME,
#: and the product id is preserved in the execution metadata so it can be corrected.
EXCHANGE_NAMES = {
    1: "CME",
    2: "CBOT",
    3: "NYMEX",
    4: "COMEX",
    5: "ICE",
    6: "CFE",
}


class TradovateSyncSource:
    """Fetches and maps fills for one Tradovate connection."""

    def __init__(
        self,
        client: TradovateClient,
        *,
        exchange_names: dict[int, str] | None = None,
    ) -> None:
        self._client = client
        self._exchanges = exchange_names or dict(EXCHANGE_NAMES)

    async def fetch_executions(
        self, *, since_cursor: int, account_filter: set[str] | None = None
    ) -> FetchResult:
        fills = await self._client.list_fills()
        new_fills = [fill for fill in fills if fill.id > since_cursor]

        if not new_fills:
            highest = max((fill.id for fill in fills), default=since_cursor)
            return FetchResult(executions=(), specs={}, highest_id=max(since_cursor, highest))

        logger.info(
            "tradovate.fills_fetched",
            total=len(fills),
            new=len(new_fills),
            since_cursor=since_cursor,
        )

        reference = await self._load_reference_data(new_fills)
        broker_accounts = {int(value) for value in account_filter} if account_filter else None
        mapped = map_fills(new_fills, reference, account_filter=broker_accounts)

        return FetchResult(
            executions=mapped.executions,
            specs=mapped.specs,
            deferred=tuple(
                DeferredItem(external_id=item.fill_id, reason=item.reason)
                for item in mapped.deferred
            ),
            highest_id=max(fill.id for fill in new_fills),
        )

    async def _load_reference_data(self, fills: list[TradovateFill]) -> ReferenceData:
        """Resolve every entity the fills reference, in four batched round trips."""
        orders = await self._client.get_orders([fill.order_id for fill in fills])
        contracts = await self._client.get_contracts([fill.contract_id for fill in fills])

        maturity_ids = [
            contract.contract_maturity_id
            for contract in contracts.values()
            if contract.contract_maturity_id is not None
        ]
        maturities = await self._client.get_contract_maturities(maturity_ids)
        products = await self._client.get_products(
            [maturity.product_id for maturity in maturities.values()]
        )
        fees = await self._client.get_fill_fees([fill.id for fill in fills])

        missing_fees = [fill.id for fill in fills if fill.id not in fees]
        if missing_fees:
            # The mapper decides what to do with these: recent fills are deferred and
            # retried, older ones are accepted as genuinely free (see FEE_GRACE_PERIOD).
            logger.info("tradovate.fees_missing", count=len(missing_fees))

        return ReferenceData(
            orders=orders,
            contracts=contracts,
            maturities=maturities,
            products=products,
            fees=fees,
            exchanges=self._exchanges,
        )
