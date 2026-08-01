"""Tradovate payloads → domain objects.

This is the only module that knows Tradovate's data shape, and it is deliberately
strict. Three rules:

**Never guess an account.** A Tradovate fill carries no ``accountId``; it is attributed
through its order. If the order is unavailable the fill is *deferred*, not assigned to
a plausible account. A fill in the wrong account corrupts two accounts' statistics and
is nearly impossible to detect later.

**Never guess a contract specification.** Point value and tick size come from the
product record. Without them a price move cannot be converted to money, so the fill is
deferred rather than valued with an assumed multiplier.

**Never let a float in.** Prices arrive as JSON doubles and are parsed as ``Decimal``
at the transport layer; nothing here reintroduces one.

Deferred fills are returned to the caller rather than dropped, so the sync run can
report exactly what it could not process and retry on the next pass once the missing
reference data has been fetched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.logging import get_logger
from app.domain.common.enums import AssetClass, Side
from app.domain.trading.execution import Execution
from app.domain.trading.instrument import InstrumentSpec
from app.infrastructure.brokers.tradovate.models import (
    TradovateContract,
    TradovateContractMaturity,
    TradovateFill,
    TradovateFillFee,
    TradovateOrder,
    TradovateProduct,
)

logger = get_logger(__name__)

#: Tradovate reports every price in the product's own currency; the product record
#: carries a currency *id*, not a code, and resolving it needs another lookup. USD
#: covers every product a futures trader on Tradovate touches, and the id is preserved
#: in the raw payload for the day that stops being true.
DEFAULT_CURRENCY = "USD"

#: Exchange timezone per exchange name. Session dates are derived from these, so a
#: wrong entry silently shifts a trade into the wrong session.
EXCHANGE_TIMEZONES = {
    "CME": "America/Chicago",
    "CBOT": "America/Chicago",
    "COMEX": "America/New_York",
    "NYMEX": "America/New_York",
    "ICE": "America/New_York",
    "CFE": "America/Chicago",
}

#: How long to wait for a fill's fee record before accepting that there isn't one.
#:
#: Fee records can lag their fill by moments, and ingesting at zero cost would
#: overstate P&L permanently — the trade would never be re-costed, because ingestion is
#: idempotent and would treat the fill as already known. So a recent fill without fees
#: is deferred and retried.
#:
#: But some fills genuinely have no fee record at all — simulated accounts are the
#: common case — and deferring those forever would wedge the sync cursor and stop the
#: journal dead. The grace period bounds the wait: recent means retry, old means this
#: fill really is free.
FEE_GRACE_PERIOD = timedelta(minutes=5)

_PRODUCT_TYPE_TO_ASSET_CLASS = {
    "Futures": AssetClass.FUTURE,
    "Options": AssetClass.FUTURE_OPTION,
    "Spread": AssetClass.FUTURE,
    "Continuous": AssetClass.FUTURE,
    "Cryptocurrency": AssetClass.CRYPTO,
    "CommonStock": AssetClass.EQUITY,
}


@dataclass(frozen=True, slots=True)
class DeferredFill:
    """A fill that could not be mapped yet, with the reason why."""

    fill_id: int
    reason: str
    contract_id: int | None = None
    order_id: int | None = None


@dataclass(frozen=True, slots=True)
class MappingResult:
    executions: tuple[Execution, ...]
    deferred: tuple[DeferredFill, ...]
    #: Contract specifications discovered while mapping, keyed by symbol. The caller
    #: upserts these so the instrument table stays current without a separate job.
    specs: dict[str, InstrumentSpec]
    #: Broker account id per execution, so the caller can route fills to the right
    #: local account without re-deriving the join.
    account_by_execution: dict[str, int]


@dataclass(frozen=True, slots=True)
class ReferenceData:
    """Everything needed to interpret a batch of fills."""

    orders: dict[int, TradovateOrder]
    contracts: dict[int, TradovateContract]
    maturities: dict[int, TradovateContractMaturity]
    products: dict[int, TradovateProduct]
    fees: dict[int, TradovateFillFee]
    exchanges: dict[int, str]

    def resolve_product(self, contract_id: int) -> TradovateProduct | None:
        """Walk contract → maturity → product, the chain that defines point value."""
        contract = self.contracts.get(contract_id)
        if contract is None or contract.contract_maturity_id is None:
            return None
        maturity = self.maturities.get(contract.contract_maturity_id)
        if maturity is None:
            return None
        return self.products.get(maturity.product_id)


def build_instrument_spec(
    contract: TradovateContract, product: TradovateProduct, exchange: str
) -> InstrumentSpec:
    """Derive a contract specification from Tradovate reference data.

    Tradovate gives ``valuePerPoint`` and ``tickSize``; the domain works in tick value,
    so tick value is derived as ``valuePerPoint × tickSize``. For ES that is
    ``50 × 0.25 = 12.50`` — the published figure, which is the check that this
    conversion is the right way round.
    """
    tick_value = product.value_per_point * product.tick_size
    precision = _precision_from_tick(product.tick_size)

    return InstrumentSpec(
        symbol=contract.name,
        exchange=exchange,
        asset_class=_PRODUCT_TYPE_TO_ASSET_CLASS.get(product.product_type or "", AssetClass.FUTURE),
        currency=DEFAULT_CURRENCY,
        tick_size=product.tick_size,
        tick_value=tick_value,
        exchange_timezone=EXCHANGE_TIMEZONES.get(exchange, "America/Chicago"),
        price_precision=precision,
    )


def map_fills(
    fills: list[TradovateFill],
    reference: ReferenceData,
    *,
    account_filter: set[int] | None = None,
    now: datetime | None = None,
) -> MappingResult:
    """Convert Tradovate fills into domain executions.

    Args:
        fills: Fills as returned by ``fill/list`` or pushed over the WebSocket.
        reference: Orders, contracts, maturities, products, fees and exchange names.
        account_filter: When given, only fills belonging to these broker account ids
            are mapped. Used to sync one account without pulling in others on the
            same login.
        now: Reference time for the fee grace period. Injected so the rule is testable
            without waiting five minutes.
    """
    moment = now or datetime.now(UTC)
    executions: list[Execution] = []
    deferred: list[DeferredFill] = []
    specs: dict[str, InstrumentSpec] = {}
    account_by_execution: dict[str, int] = {}

    for fill in fills:
        order = reference.orders.get(fill.order_id)
        if order is None:
            deferred.append(
                DeferredFill(
                    fill_id=fill.id,
                    reason="order not available; a fill cannot be attributed without one",
                    order_id=fill.order_id,
                    contract_id=fill.contract_id,
                )
            )
            continue

        if account_filter is not None and order.account_id not in account_filter:
            continue

        contract = reference.contracts.get(fill.contract_id)
        product = reference.resolve_product(fill.contract_id)
        if contract is None or product is None:
            deferred.append(
                DeferredFill(
                    fill_id=fill.id,
                    reason="contract specification unavailable; refusing to assume a point value",
                    order_id=fill.order_id,
                    contract_id=fill.contract_id,
                )
            )
            continue

        exchange = reference.exchanges.get(product.exchange_id or -1, "CME")
        spec = build_instrument_spec(contract, product, exchange)
        specs[spec.symbol] = spec

        fee = reference.fees.get(fill.id)
        if fee is None and fill.timestamp.astimezone(UTC) > moment - FEE_GRACE_PERIOD:
            deferred.append(
                DeferredFill(
                    fill_id=fill.id,
                    reason="fee record has not settled yet; retrying rather than costing at zero",
                    order_id=fill.order_id,
                    contract_id=fill.contract_id,
                )
            )
            continue

        external_id = str(fill.id)
        executions.append(
            Execution(
                external_id=external_id,
                account_key=str(order.account_id),
                instrument_symbol=contract.name,
                side=Side.BUY if fill.action == "Buy" else Side.SELL,
                quantity=Decimal(fill.qty),
                price=fill.price,
                executed_at=fill.timestamp.astimezone(UTC),
                commission=fee.total_commission if fee else Decimal(0),
                fees=fee.total_fees if fee else Decimal(0),
                # Tradovate fill ids are monotonic per user, so they order fills that
                # share a timestamp exactly as the broker sequenced them.
                sequence=fill.id,
                metadata={
                    "broker": "tradovate",
                    "fill_id": fill.id,
                    "order_id": fill.order_id,
                    "contract_id": fill.contract_id,
                    "account_id": order.account_id,
                    "product_id": product.id,
                    "fees_available": fee is not None,
                },
            )
        )
        account_by_execution[external_id] = order.account_id

    if deferred:
        logger.info(
            "tradovate.fills_deferred",
            count=len(deferred),
            reasons=sorted({item.reason for item in deferred}),
        )

    return MappingResult(
        executions=tuple(executions),
        deferred=tuple(deferred),
        specs=specs,
        account_by_execution=account_by_execution,
    )


def _precision_from_tick(tick_size: Decimal) -> int:
    """Decimal places implied by a tick size (``0.25`` → 2, ``0.0000005`` → 7)."""
    exponent = tick_size.normalize().as_tuple().exponent
    return max(0, -int(exponent)) if isinstance(exponent, int) else 2
