"""Typed payloads for the Tradovate entities we consume.

These are *boundary* models, not domain models. They exist to validate what arrives
before it reaches anything that matters, and they are deliberately lenient about
fields we do not use: Tradovate's published OpenAPI schema is incomplete for several
runtime entities (the ``Order`` component omits ``accountId``, which the live API
plainly returns), so a strict model would reject valid data.

The rule applied throughout: **strict about what we depend on, permissive about the
rest.** A missing ``price`` is a hard error; an unrecognised extra field is ignored
and preserved in the raw payload.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TradovateModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class AccessTokenResponse(TradovateModel):
    """Reply from ``auth/accesstokenrequest`` and ``auth/renewaccesstoken``.

    A failed request can still return HTTP 200 with ``errorText`` set — Tradovate uses
    the field for business-level rejections — so success is decided by the presence of
    a token, not by the status code alone.
    """

    access_token: str | None = Field(default=None, alias="accessToken")
    md_access_token: str | None = Field(default=None, alias="mdAccessToken")
    expiration_time: datetime | None = Field(default=None, alias="expirationTime")
    user_id: int | None = Field(default=None, alias="userId")
    name: str | None = None
    has_live: bool | None = Field(default=None, alias="hasLive")
    user_status: str | None = Field(default=None, alias="userStatus")
    error_text: str | None = Field(default=None, alias="errorText")

    @property
    def succeeded(self) -> bool:
        return bool(self.access_token) and not self.error_text


class TimePenalty(TradovateModel):
    """Rate-limit response: ``{"p-ticket": ..., "p-time": ..., "p-captcha": ...}``.

    The request was *not* handled. It may be retried after ``p_time`` seconds with the
    ticket echoed in the body — unless ``p_captcha`` is set, which means a third-party
    application cannot recover and the user must wait it out.
    """

    ticket: str = Field(alias="p-ticket")
    time_seconds: int = Field(alias="p-time")
    captcha: bool = Field(default=False, alias="p-captcha")


class TradovateAccount(TradovateModel):
    id: int
    name: str
    user_id: int = Field(alias="userId")
    account_type: str | None = Field(default=None, alias="accountType")
    active: bool = True
    #: ``Demo`` or ``Live`` — Tradovate's own label for the account's nature.
    legal_status: str | None = Field(default=None, alias="legalStatus")


class TradovateFill(TradovateModel):
    """A fill.

    Note what is *absent*: there is no ``accountId``. A fill is attributed to an
    account only through its order, which is why ingestion resolves orders first and
    refuses to guess when one is unavailable.
    """

    id: int
    order_id: int = Field(alias="orderId")
    contract_id: int = Field(alias="contractId")
    timestamp: datetime
    action: Literal["Buy", "Sell"]
    qty: int
    price: Decimal
    active: bool = True
    #: Quantity already matched against opposing fills, per Tradovate's own pairing.
    finally_paired: int | None = Field(default=None, alias="finallyPaired")
    trade_date: dict[str, Any] | None = Field(default=None, alias="tradeDate")


class TradovateFillFee(TradovateModel):
    """Per-fill costs. ``id`` matches the fill's id.

    Tradovate itemises costs; we keep the broker's own commission separate from
    everything the exchange, clearing house, NFA and platform charge, because those
    two behave differently — commissions are negotiable, fees are not — and a trader
    evaluating whether scalping is viable needs to see which is eating the edge.
    """

    id: int
    commission: Decimal | None = None
    clearing_fee: Decimal | None = Field(default=None, alias="clearingFee")
    exchange_fee: Decimal | None = Field(default=None, alias="exchangeFee")
    nfa_fee: Decimal | None = Field(default=None, alias="nfaFee")
    brokerage_fee: Decimal | None = Field(default=None, alias="brokerageFee")
    ip_fee: Decimal | None = Field(default=None, alias="ipFee")

    @property
    def total_commission(self) -> Decimal:
        return self.commission or Decimal(0)

    @property
    def total_fees(self) -> Decimal:
        parts = (
            self.clearing_fee,
            self.exchange_fee,
            self.nfa_fee,
            self.brokerage_fee,
            self.ip_fee,
        )
        return sum((part for part in parts if part is not None), start=Decimal(0))


class TradovateOrder(TradovateModel):
    """An order.

    ``account_id`` is required here even though Tradovate's published schema omits it:
    without it a fill cannot be attributed to an account, and a fill attributed to the
    wrong account is worse than a failed sync.
    """

    id: int
    account_id: int = Field(alias="accountId")
    contract_id: int = Field(alias="contractId")
    timestamp: datetime | None = None
    action: Literal["Buy", "Sell"] | None = None
    ord_status: str | None = Field(default=None, alias="ordStatus")
    admin: bool | None = None


class TradovateOrderVersion(TradovateModel):
    """A revision of an order's parameters.

    This is the evidence base for "you move your stops": every modification to a stop
    or limit price is a new version with its own timestamp, so the claim becomes a
    measurement rather than an impression.
    """

    id: int
    order_id: int = Field(alias="orderId")
    order_qty: int | None = Field(default=None, alias="orderQty")
    order_type: str | None = Field(default=None, alias="orderType")
    price: Decimal | None = None
    stop_price: Decimal | None = Field(default=None, alias="stopPrice")
    timestamp: datetime | None = None


class TradovateContract(TradovateModel):
    id: int
    name: str
    contract_maturity_id: int | None = Field(default=None, alias="contractMaturityId")


class TradovateContractMaturity(TradovateModel):
    id: int
    product_id: int = Field(alias="productId")
    expiration_month: int | None = Field(default=None, alias="expirationMonth")
    expiration_date: datetime | None = Field(default=None, alias="expirationDate")
    is_front: bool | None = Field(default=None, alias="isFront")


class TradovateProduct(TradovateModel):
    """Contract specification — the source of point value and tick size.

    ``value_per_point`` and ``tick_size`` are the two numbers that convert a price move
    into money. Everything downstream depends on them being right.
    """

    id: int
    name: str
    description: str | None = None
    currency_id: int | None = Field(default=None, alias="currencyId")
    product_type: str | None = Field(default=None, alias="productType")
    exchange_id: int | None = Field(default=None, alias="exchangeId")
    value_per_point: Decimal = Field(alias="valuePerPoint")
    tick_size: Decimal = Field(alias="tickSize")
    price_format_type: str | None = Field(default=None, alias="priceFormatType")
    price_format: int | None = Field(default=None, alias="priceFormat")


class TradovateCashBalanceSnapshot(TradovateModel):
    """Broker-reported balance, used to reconcile our reconstructed P&L."""

    total_cash_value: Decimal | None = Field(default=None, alias="totalCashValue")
    total_pnl: Decimal | None = Field(default=None, alias="totalPnL")
    open_pnl: Decimal | None = Field(default=None, alias="openPnL")
    realized_pnl: Decimal | None = Field(default=None, alias="realizedPnL")
    week_realized_pnl: Decimal | None = Field(default=None, alias="weekRealizedPnL")
    initial_margin: Decimal | None = Field(default=None, alias="initialMargin")
    net_liquidating_value: Decimal | None = Field(default=None, alias="netLiquidatingValue")
