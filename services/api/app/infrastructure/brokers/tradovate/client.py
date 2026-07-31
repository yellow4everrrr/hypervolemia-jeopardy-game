"""Tradovate REST client.

Every call goes through :meth:`TradovateClient._get`/:meth:`_post`, which apply the
rate limiter, attach a fresh bearer token, retry once on a 401 with a new token, and
parse the body with ``Decimal`` numbers.

The batching helpers matter more than they look. Tradovate exposes entities with fine
granularity and expects clients to join them: a fill knows its order id, the order
knows the account, the contract knows its maturity, the maturity knows the product,
and only the product knows what a point is worth. A naive implementation issues five
requests per fill. These helpers turn that into a handful of batched ``items`` calls
per sync.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

import httpx

from app.core.errors import AuthenticationError, ExternalServiceError, RateLimitedError
from app.core.logging import get_logger
from app.infrastructure.brokers.base import TokenBucketLimiter, response_json
from app.infrastructure.brokers.tradovate.auth import TradovateAuthenticator
from app.infrastructure.brokers.tradovate.config import (
    BATCH_SIZE,
    REQUEST_BURST,
    REQUESTS_PER_SECOND,
    Endpoint,
    TradovateEnvironment,
    hosts_for,
)
from app.infrastructure.brokers.tradovate.models import (
    TradovateAccount,
    TradovateCashBalanceSnapshot,
    TradovateContract,
    TradovateContractMaturity,
    TradovateFill,
    TradovateFillFee,
    TradovateOrder,
    TradovateOrderVersion,
    TradovateProduct,
)

logger = get_logger(__name__)

ModelT = TypeVar("ModelT")


class TradovateClient:
    """Thin, typed wrapper over the Tradovate REST API."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        authenticator: TradovateAuthenticator,
        *,
        environment: TradovateEnvironment | str = TradovateEnvironment.DEMO,
        limiter: TokenBucketLimiter | None = None,
    ) -> None:
        self._http = http
        self._auth = authenticator
        self._environment = TradovateEnvironment(environment)
        self._limiter = limiter or TokenBucketLimiter(REQUESTS_PER_SECOND, REQUEST_BURST)

    @property
    def environment(self) -> TradovateEnvironment:
        return self._environment

    @property
    def hosts(self) -> Any:
        return hosts_for(self._environment)

    # --- Entities ---------------------------------------------------------------

    async def list_accounts(self) -> list[TradovateAccount]:
        payload = await self._get(Endpoint.ACCOUNT_LIST)
        return [TradovateAccount.model_validate(item) for item in _as_list(payload)]

    async def list_fills(self) -> list[TradovateFill]:
        """All fills visible to the authenticated user.

        Tradovate offers no date- or cursor-filtered fill endpoint, so incremental sync
        pulls the list and filters client-side on ``id`` (monotonic per user). That is
        the API's shape, not a shortcut: filtering server-side is simply not offered.
        """
        payload = await self._get(Endpoint.FILL_LIST)
        return [TradovateFill.model_validate(item) for item in _as_list(payload)]

    async def get_fill_fees(self, fill_ids: Sequence[int]) -> dict[int, TradovateFillFee]:
        """Costs per fill, batched. ``FillFee.id`` matches the fill id."""
        fees: dict[int, TradovateFillFee] = {}
        for batch in _batched(fill_ids, BATCH_SIZE):
            payload = await self._get(
                Endpoint.FILL_FEE_ITEMS, params={"ids": ",".join(str(i) for i in batch)}
            )
            for item in _as_list(payload):
                fee = TradovateFillFee.model_validate(item)
                fees[fee.id] = fee
        return fees

    async def get_orders(self, order_ids: Sequence[int]) -> dict[int, TradovateOrder]:
        """Orders by id, batched. This is how a fill learns which account it belongs to."""
        orders: dict[int, TradovateOrder] = {}
        for batch in _batched(order_ids, BATCH_SIZE):
            payload = await self._get(
                Endpoint.ORDER_ITEMS, params={"ids": ",".join(str(i) for i in batch)}
            )
            for item in _as_list(payload):
                try:
                    order = TradovateOrder.model_validate(item)
                except Exception as exc:
                    logger.warning("tradovate.order_unparsable", error=str(exc))
                    continue
                orders[order.id] = order
        return orders

    async def list_orders(self) -> list[TradovateOrder]:
        payload = await self._get(Endpoint.ORDER_LIST)
        orders = []
        for item in _as_list(payload):
            try:
                orders.append(TradovateOrder.model_validate(item))
            except Exception as exc:
                logger.warning("tradovate.order_unparsable", error=str(exc))
        return orders

    async def get_order_versions(self, order_ids: Sequence[int]) -> list[TradovateOrderVersion]:
        """Every revision of the given orders — the raw material for stop-movement analysis."""
        versions: list[TradovateOrderVersion] = []
        for batch in _batched(order_ids, BATCH_SIZE):
            payload = await self._get(
                Endpoint.ORDER_VERSION_LDEPS,
                params={"masterids": ",".join(str(i) for i in batch)},
            )
            for item in _as_list(payload):
                try:
                    versions.append(TradovateOrderVersion.model_validate(item))
                except Exception as exc:
                    logger.warning("tradovate.order_version_unparsable", error=str(exc))
        return versions

    async def get_contracts(self, contract_ids: Sequence[int]) -> dict[int, TradovateContract]:
        contracts: dict[int, TradovateContract] = {}
        for batch in _batched(contract_ids, BATCH_SIZE):
            payload = await self._get(
                Endpoint.CONTRACT_ITEMS, params={"ids": ",".join(str(i) for i in batch)}
            )
            for item in _as_list(payload):
                contract = TradovateContract.model_validate(item)
                contracts[contract.id] = contract
        return contracts

    async def get_contract_maturities(
        self, maturity_ids: Sequence[int]
    ) -> dict[int, TradovateContractMaturity]:
        maturities: dict[int, TradovateContractMaturity] = {}
        for batch in _batched(maturity_ids, BATCH_SIZE):
            payload = await self._get(
                Endpoint.CONTRACT_MATURITY_ITEMS, params={"ids": ",".join(str(i) for i in batch)}
            )
            for item in _as_list(payload):
                maturity = TradovateContractMaturity.model_validate(item)
                maturities[maturity.id] = maturity
        return maturities

    async def get_products(self, product_ids: Sequence[int]) -> dict[int, TradovateProduct]:
        products: dict[int, TradovateProduct] = {}
        for batch in _batched(product_ids, BATCH_SIZE):
            payload = await self._get(
                Endpoint.PRODUCT_ITEMS, params={"ids": ",".join(str(i) for i in batch)}
            )
            for item in _as_list(payload):
                product = TradovateProduct.model_validate(item)
                products[product.id] = product
        return products

    async def get_cash_balance_snapshot(self, account_id: int) -> TradovateCashBalanceSnapshot:
        """Broker-reported balance for reconciliation against our own numbers."""
        payload = await self._post(
            Endpoint.CASH_BALANCE_SNAPSHOT, json_body={"accountId": account_id}
        )
        if not isinstance(payload, dict):
            raise ExternalServiceError("unexpected cash balance payload")
        return TradovateCashBalanceSnapshot.model_validate(payload)

    # --- Transport --------------------------------------------------------------

    async def _get(self, endpoint: Endpoint, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", endpoint, params=params)

    async def _post(self, endpoint: Endpoint, json_body: Any = None) -> Any:
        return await self._request("POST", endpoint, json_body=json_body)

    async def _request(
        self,
        method: str,
        endpoint: Endpoint,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        _retrying: bool = False,
    ) -> Any:
        await self._limiter.acquire()
        token = await self._auth.token()

        try:
            response = await self._http.request(
                method,
                endpoint.value,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token.value}"},
            )
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                f"Tradovate request failed: {exc}", details={"endpoint": endpoint.value}
            ) from exc

        if response.status_code == 401 and not _retrying:
            # The token was rejected mid-flight — most often because the session was
            # invalidated elsewhere. Drop it and try once with a fresh one.
            logger.info("tradovate.token_rejected", endpoint=endpoint.value)
            self._auth.invalidate()
            return await self._request(
                method, endpoint, params=params, json_body=json_body, _retrying=True
            )

        if response.status_code in (401, 403):
            raise AuthenticationError(
                "Tradovate rejected the request", details={"endpoint": endpoint.value}
            )
        if response.status_code in (423, 429):
            retry_after = response.headers.get("Retry-After")
            raise RateLimitedError(
                "Tradovate rate limit reached",
                details={"endpoint": endpoint.value, "retry_after": retry_after},
            )
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"Tradovate returned HTTP {response.status_code}",
                details={"endpoint": endpoint.value, "body": response.text[:256]},
            )

        payload = response_json(response)
        # A 200 can still carry a business-level rejection in `errorText`.
        if isinstance(payload, dict) and payload.get("errorText"):
            raise ExternalServiceError(
                str(payload["errorText"]), details={"endpoint": endpoint.value}
            )
        return payload


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    raise ExternalServiceError("expected a JSON array from Tradovate")


def _batched(values: Sequence[int], size: int) -> list[list[int]]:
    """Chunk ids, de-duplicated and sorted so identical work produces identical URLs.

    Deterministic URLs are what make an HTTP cache — and a recorded test fixture —
    useful rather than incidental.
    """
    unique = sorted(set(values))
    return [unique[start : start + size] for start in range(0, len(unique), size)]
