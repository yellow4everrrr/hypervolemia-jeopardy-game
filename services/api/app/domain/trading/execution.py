"""Execution (fill) — the atomic, immutable unit of truth.

Everything else in Ledgerline is derived. Trades are reconstructed from fills, metrics
are computed from trades, AI commentary is computed from metrics. Fills themselves are
never edited: if the broker restates one, the correction arrives as a new fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.errors import DomainError
from app.core.money import ZERO
from app.domain.common.enums import Side


@dataclass(frozen=True, slots=True)
class Execution:
    """A single fill as reported by the broker.

    Attributes:
        external_id: The broker's own identifier. This is the idempotency key for
            ingestion — re-syncing a day must not duplicate fills.
        account_key: Opaque account identifier; fills are matched per account because
            the same instrument can be traded independently across accounts.
        instrument_symbol: Symbol the fill refers to.
        side: Buy or sell.
        quantity: Absolute contract count, always positive. Direction lives in ``side``.
        price: Fill price.
        executed_at: Timezone-aware fill timestamp (UTC).
        commission: Broker commission for this fill, positive as a cost.
        fees: Exchange/clearing/regulatory fees, positive as a cost.
        sequence: Broker-provided ordering hint, used to break timestamp ties
            deterministically when several fills share a millisecond.
    """

    external_id: str
    account_key: str
    instrument_symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    commission: Decimal = ZERO
    fees: Decimal = ZERO
    sequence: int = 0
    metadata: dict[str, object] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise DomainError(
                f"execution {self.external_id}: quantity must be positive, got {self.quantity}"
            )
        if self.price < 0:
            raise DomainError(
                f"execution {self.external_id}: price must not be negative, got {self.price}"
            )
        if self.commission < 0 or self.fees < 0:
            raise DomainError(
                f"execution {self.external_id}: costs are expressed as positive amounts"
            )
        if self.executed_at.tzinfo is None:
            raise DomainError(
                f"execution {self.external_id}: executed_at must be timezone-aware (UTC)"
            )

    @property
    def signed_quantity(self) -> Decimal:
        """Position delta this fill applies: positive for buys, negative for sells."""
        return self.quantity * self.side.signed_multiplier

    @property
    def total_cost(self) -> Decimal:
        return self.commission + self.fees

    @property
    def sort_key(self) -> tuple[datetime, int, str]:
        """Deterministic ordering.

        Sequence breaks timestamp ties; the external id breaks sequence ties, so the
        same input set always reconstructs to the same trades regardless of the order
        the broker's pages happened to arrive in.
        """
        return (self.executed_at, self.sequence, self.external_id)

    def cost_for_quantity(self, quantity: Decimal) -> tuple[Decimal, Decimal]:
        """Pro-rata share of ``(commission, fees)`` attributable to part of this fill.

        A single fill can straddle two trades when a position flips (sell 5 while long
        2 closes one trade and opens another). Splitting the costs by quantity keeps
        the sum of per-trade costs exactly equal to what the broker charged.
        """
        if quantity <= 0 or quantity > self.quantity:
            raise DomainError(
                f"execution {self.external_id}: cannot allocate {quantity} of {self.quantity}"
            )
        ratio = quantity / self.quantity
        return self.commission * ratio, self.fees * ratio
