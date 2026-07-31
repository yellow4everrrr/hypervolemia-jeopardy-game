"""Tests for Tradovate → domain mapping.

The theme throughout: **refuse rather than guess.** Every case where the mapper lacks
information needed to value a fill correctly produces a deferral, not an approximation.
An approximation here becomes a wrong number in the trader's statistics that nothing
downstream can detect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.common.enums import AssetClass, Side
from app.infrastructure.brokers.tradovate.mapping import (
    ReferenceData,
    build_instrument_spec,
    map_fills,
)
from app.infrastructure.brokers.tradovate.models import (
    TradovateContract,
    TradovateContractMaturity,
    TradovateFill,
    TradovateFillFee,
    TradovateOrder,
    TradovateProduct,
)

NOW = datetime(2026, 3, 5, 20, 0, tzinfo=UTC)
FILL_TIME = datetime(2026, 3, 5, 15, 30, tzinfo=UTC)

ES_PRODUCT = TradovateProduct.model_validate(
    {
        "id": 10,
        "name": "ES",
        "valuePerPoint": Decimal("50"),
        "tickSize": Decimal("0.25"),
        "productType": "Futures",
        "exchangeId": 1,
    }
)
ES_CONTRACT = TradovateContract.model_validate(
    {"id": 560901, "name": "ESM6", "contractMaturityId": 900}
)
ES_MATURITY = TradovateContractMaturity.model_validate({"id": 900, "productId": 10})


def reference(
    *,
    orders: dict[int, TradovateOrder] | None = None,
    contracts: dict[int, TradovateContract] | None = None,
    maturities: dict[int, TradovateContractMaturity] | None = None,
    products: dict[int, TradovateProduct] | None = None,
    fees: dict[int, TradovateFillFee] | None = None,
) -> ReferenceData:
    return ReferenceData(
        orders=orders if orders is not None else {700: order(700, account_id=25)},
        contracts=contracts if contracts is not None else {560901: ES_CONTRACT},
        maturities=maturities if maturities is not None else {900: ES_MATURITY},
        products=products if products is not None else {10: ES_PRODUCT},
        fees=fees if fees is not None else {1: fee(1)},
        exchanges={1: "CME"},
    )


def order(order_id: int, account_id: int = 25) -> TradovateOrder:
    return TradovateOrder.model_validate(
        {"id": order_id, "accountId": account_id, "contractId": 560901, "action": "Buy"}
    )


def fee(fill_id: int, commission: str = "1.29", exchange: str = "1.18") -> TradovateFillFee:
    return TradovateFillFee.model_validate(
        {
            "id": fill_id,
            "commission": Decimal(commission),
            "exchangeFee": Decimal(exchange),
            "nfaFee": Decimal("0.02"),
        }
    )


def fill(
    fill_id: int = 1,
    *,
    order_id: int = 700,
    action: str = "Buy",
    qty: int = 2,
    price: str = "5000.25",
    contract_id: int = 560901,
    when: datetime = FILL_TIME,
) -> TradovateFill:
    return TradovateFill.model_validate(
        {
            "id": fill_id,
            "orderId": order_id,
            "contractId": contract_id,
            "timestamp": when,
            "action": action,
            "qty": qty,
            "price": Decimal(price),
        }
    )


# --- Instrument specifications ---------------------------------------------------


def test_tick_value_is_derived_from_value_per_point() -> None:
    """ES: $50 a point at a 0.25 tick is $12.50 a tick — the published figure.

    Getting this multiplication backwards would scale every ES P&L by 16x, so the
    check is against a number a futures trader knows by heart.
    """
    spec = build_instrument_spec(ES_CONTRACT, ES_PRODUCT, "CME")

    assert spec.tick_value == Decimal("12.50")
    assert spec.point_value == Decimal("50")
    assert spec.tick_size == Decimal("0.25")


def test_micro_contract_specification() -> None:
    mes = TradovateProduct.model_validate(
        {"id": 11, "name": "MES", "valuePerPoint": Decimal("5"), "tickSize": Decimal("0.25")}
    )
    spec = build_instrument_spec(ES_CONTRACT, mes, "CME")
    assert spec.tick_value == Decimal("1.25")
    assert spec.point_value == Decimal("5")


def test_exchange_timezone_follows_the_exchange() -> None:
    """Session dates derive from this; a wrong zone files trades under the wrong day."""
    assert build_instrument_spec(ES_CONTRACT, ES_PRODUCT, "CME").exchange_timezone == (
        "America/Chicago"
    )
    assert build_instrument_spec(ES_CONTRACT, ES_PRODUCT, "NYMEX").exchange_timezone == (
        "America/New_York"
    )


def test_price_precision_is_derived_from_tick_size() -> None:
    crude = TradovateProduct.model_validate(
        {"id": 12, "name": "CL", "valuePerPoint": Decimal("1000"), "tickSize": Decimal("0.01")}
    )
    assert build_instrument_spec(ES_CONTRACT, crude, "NYMEX").price_precision == 2

    yen = TradovateProduct.model_validate(
        {
            "id": 13,
            "name": "6J",
            "valuePerPoint": Decimal("12500000"),
            "tickSize": Decimal("0.0000005"),
        }
    )
    assert build_instrument_spec(ES_CONTRACT, yen, "CME").price_precision == 7


def test_product_type_maps_to_asset_class() -> None:
    crypto = TradovateProduct.model_validate(
        {
            "id": 14,
            "name": "BTC",
            "valuePerPoint": Decimal("5"),
            "tickSize": Decimal("5"),
            "productType": "Cryptocurrency",
        }
    )
    assert build_instrument_spec(ES_CONTRACT, crypto, "CME").asset_class is AssetClass.CRYPTO


# --- Fill mapping ---------------------------------------------------------------


def test_maps_a_fill_to_an_execution() -> None:
    result = map_fills([fill()], reference(), now=NOW)

    assert len(result.executions) == 1
    execution = result.executions[0]
    assert execution.external_id == "1"
    assert execution.account_key == "25"
    assert execution.instrument_symbol == "ESM6"
    assert execution.side is Side.BUY
    assert execution.quantity == Decimal(2)
    assert execution.price == Decimal("5000.25")
    assert execution.executed_at == FILL_TIME
    assert result.deferred == ()


def test_sell_maps_to_the_sell_side() -> None:
    result = map_fills([fill(action="Sell")], reference(), now=NOW)
    assert result.executions[0].side is Side.SELL


def test_commission_and_fees_are_kept_separate() -> None:
    """Commission is negotiable, exchange and regulatory fees are not.

    A trader deciding whether one-tick scalping is viable needs to know which of the
    two is eating the edge, so they are never summed into a single cost.
    """
    result = map_fills([fill()], reference(), now=NOW)
    execution = result.executions[0]

    assert execution.commission == Decimal("1.29")
    assert execution.fees == Decimal("1.20")  # 1.18 exchange + 0.02 NFA
    assert execution.total_cost == Decimal("2.49")


def test_fill_id_becomes_the_ordering_sequence() -> None:
    """Tradovate fill ids are monotonic, so they order same-millisecond fills exactly."""
    result = map_fills([fill(fill_id=99)], reference(fees={99: fee(99)}), now=NOW)
    assert result.executions[0].sequence == 99


def test_raw_identifiers_are_preserved_in_metadata() -> None:
    """Enough to trace any execution back to the broker records that produced it."""
    metadata = map_fills([fill()], reference(), now=NOW).executions[0].metadata
    assert metadata["broker"] == "tradovate"
    assert metadata["fill_id"] == 1
    assert metadata["order_id"] == 700
    assert metadata["account_id"] == 25
    assert metadata["product_id"] == 10


def test_specs_discovered_while_mapping_are_returned() -> None:
    """So instrument reference data self-heals and a new contract never blocks a sync."""
    result = map_fills([fill()], reference(), now=NOW)
    assert "ESM6" in result.specs
    assert result.specs["ESM6"].tick_value == Decimal("12.50")


# --- Refusals -------------------------------------------------------------------


def test_fill_without_its_order_is_deferred_not_attributed() -> None:
    """A Tradovate fill has no accountId. Guessing one corrupts two accounts at once."""
    result = map_fills([fill()], reference(orders={}), now=NOW)

    assert result.executions == ()
    assert len(result.deferred) == 1
    assert "order not available" in result.deferred[0].reason
    assert result.deferred[0].fill_id == 1


def test_fill_without_a_product_is_deferred() -> None:
    """No point value means no way to convert a price move into money."""
    result = map_fills([fill()], reference(products={}), now=NOW)

    assert result.executions == ()
    assert "refusing to assume a point value" in result.deferred[0].reason


def test_fill_without_a_contract_is_deferred() -> None:
    result = map_fills([fill()], reference(contracts={}), now=NOW)
    assert result.executions == ()
    assert len(result.deferred) == 1


def test_broken_maturity_chain_is_deferred() -> None:
    """contract → maturity → product: a gap anywhere breaks the valuation."""
    result = map_fills([fill()], reference(maturities={}), now=NOW)
    assert result.executions == ()
    assert len(result.deferred) == 1


def test_recent_fill_without_fees_is_deferred() -> None:
    """Fee records lag. Ingesting at zero cost would overstate P&L permanently.

    Ingestion is idempotent, so a fill written at zero cost is never re-costed — the
    trade would carry a wrong number forever. Deferring is the only recoverable choice.
    """
    recent = fill(when=NOW - timedelta(minutes=1))
    result = map_fills([recent], reference(fees={}), now=NOW)

    assert result.executions == ()
    assert "fee record has not settled" in result.deferred[0].reason


def test_old_fill_without_fees_is_accepted_as_free() -> None:
    """Simulated accounts genuinely have no fees; deferring forever would wedge the sync."""
    old = fill(when=NOW - timedelta(hours=2))
    result = map_fills([old], reference(fees={}), now=NOW)

    assert len(result.executions) == 1
    assert result.executions[0].commission == Decimal(0)
    assert result.executions[0].metadata["fees_available"] is False
    assert result.deferred == ()


# --- Filtering ------------------------------------------------------------------


def test_account_filter_excludes_other_accounts() -> None:
    """One Tradovate login can hold several accounts; a sync targets one."""
    fills = [fill(fill_id=1, order_id=700), fill(fill_id=2, order_id=701)]
    orders = {700: order(700, account_id=25), 701: order(701, account_id=99)}
    fees = {1: fee(1), 2: fee(2)}

    result = map_fills(
        fills, reference(orders=orders, fees=fees), account_filter={25}, now=NOW
    )

    assert len(result.executions) == 1
    assert result.executions[0].account_key == "25"


def test_filtered_out_fills_are_not_deferred() -> None:
    """They are not missing data — they belong to an account we were not asked about."""
    fills = [fill(fill_id=2, order_id=701)]
    orders = {701: order(701, account_id=99)}

    result = map_fills(
        fills, reference(orders=orders, fees={2: fee(2)}), account_filter={25}, now=NOW
    )

    assert result.executions == ()
    assert result.deferred == ()


def test_mixed_batch_maps_what_it_can_and_defers_the_rest() -> None:
    fills = [fill(fill_id=1, order_id=700), fill(fill_id=2, order_id=999)]
    result = map_fills(fills, reference(fees={1: fee(1), 2: fee(2)}), now=NOW)

    assert len(result.executions) == 1
    assert len(result.deferred) == 1
    assert result.deferred[0].fill_id == 2
