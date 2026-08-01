"""The fact set a rule can reference.

Rules name fields by string, so this module defines the vocabulary: every key a trader
can write a rule against, built from one trade plus its surrounding session.

Deliberately flat and explicitly enumerated. A rule engine that reflected over arbitrary
object attributes would let a rule reference an internal field that changes name in a
refactor, silently becoming unevaluable. :data:`AVAILABLE_FIELDS` is the contract, and
the API validates new rules against it so a typo is caught at save time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.common.enums import Direction, SessionSegment

#: Every field a rule may reference, with a description used by the rule builder UI.
AVAILABLE_FIELDS: dict[str, str] = {
    # --- Trade shape ---------------------------------------------------------
    "direction": "long or short",
    "instrument": "instrument root symbol, e.g. ES",
    "quantity": "contracts at peak exposure",
    "entry_price": "average entry price",
    "exit_price": "average exit price",
    # --- Timing --------------------------------------------------------------
    "entry_time": "entry time of day in the exchange's timezone",
    "entry_hour": "entry hour, 0-23",
    "weekday": "ISO weekday, 1 = Monday",
    "session_segment": "session segment such as rth_open",
    "duration_seconds": "holding period in seconds",
    # --- Risk ----------------------------------------------------------------
    "has_stop": "whether an initial stop was recorded",
    "has_target": "whether an initial target was recorded",
    "risk_amount": "currency at risk at entry",
    "risk_pct_of_equity": "risk as a fraction of account equity",
    "planned_r_multiple": "planned reward-to-risk ratio",
    "stop_moved": "whether the stop was moved after entry",
    "stop_widened": "whether the stop was moved further from entry",
    # --- Outcome -------------------------------------------------------------
    "net_pnl": "net profit or loss",
    "r_multiple": "result in units of initial risk",
    "mae_r": "maximum adverse excursion in R",
    "mfe_r": "maximum favourable excursion in R",
    # --- Context -------------------------------------------------------------
    "setup": "classified setup name",
    "market_condition": "market regime",
    "confidence": "self-rated confidence, 1-5",
    "emotion": "self-reported emotional state",
    # --- Session discipline --------------------------------------------------
    "trades_today": "trades already taken this session, before this one",
    "session_pnl_before": "session P&L before this trade",
    "consecutive_losses": "losing trades immediately preceding this one",
    "minutes_since_last_trade": "minutes since the previous trade closed",
    "is_after_daily_loss_limit": "whether the daily loss limit was already breached",
}


@dataclass(frozen=True, slots=True)
class TradeFacts:
    """Everything the rule engine needs about one trade and its session position."""

    direction: Direction
    instrument: str | None = None
    quantity: Decimal | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None

    entry_at: datetime | None = None
    #: IANA zone of the instrument's exchange. Required to answer "what time was this
    #: for the trader?" — ``entry_at`` is UTC, and a rule about the open means the
    #: exchange's clock, not Greenwich's.
    exchange_timezone: str | None = None
    entry_hour: int | None = None
    weekday: int | None = None
    session_segment: SessionSegment | None = None
    duration_seconds: int | None = None

    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    final_stop_price: Decimal | None = None
    risk_amount: Decimal | None = None
    account_equity: Decimal | None = None
    planned_r_multiple: Decimal | None = None

    net_pnl: Decimal = Decimal(0)
    r_multiple: Decimal | None = None
    mae_r: Decimal | None = None
    mfe_r: Decimal | None = None

    setup: str | None = None
    market_condition: str | None = None
    confidence: int | None = None
    emotion: str | None = None

    trades_today: int = 0
    session_pnl_before: Decimal = Decimal(0)
    consecutive_losses: int = 0
    minutes_since_last_trade: int | None = None
    daily_loss_limit: Decimal | None = None


def _local_entry(facts: TradeFacts) -> datetime | None:
    """Entry timestamp in the exchange's timezone, or ``None`` if it cannot be known.

    An unknown or invalid zone returns ``None`` rather than raising: a stale IANA name
    on one instrument should make time-of-day rules unevaluable for that instrument,
    not fail the whole compliance run.
    """
    if facts.entry_at is None or facts.exchange_timezone is None:
        return None
    try:
        return facts.entry_at.astimezone(ZoneInfo(facts.exchange_timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def build_context(facts: TradeFacts) -> dict[str, Any]:
    """Flatten facts into the dictionary rules evaluate against.

    Keys are **omitted** rather than set to ``None`` when unknown. That is what makes a
    rule unevaluable instead of failed, and it is the whole reason the compliance score
    can be trusted: a trader who has not recorded stops does not get marked
    undisciplined for it.
    """
    context: dict[str, Any] = {"direction": facts.direction.value}

    def put(key: str, value: Any) -> None:
        if value is not None:
            context[key] = value

    put("instrument", facts.instrument)
    put("quantity", facts.quantity)
    put("entry_price", facts.entry_price)
    put("exit_price", facts.exit_price)

    # Entry time is the exchange's clock, never the server's and never UTC. A trader
    # writing "no trades before 09:30" means the cash open, and reading the hour off a
    # UTC timestamp would score a 09:30 New York entry as 14:30 — passing a rule it
    # broke, or breaking one it passed. Without a known zone the field is omitted, so
    # the rule reads as unevaluable rather than as a confident wrong answer.
    local_entry = _local_entry(facts)
    if local_entry is not None:
        context["entry_time"] = time(local_entry.hour, local_entry.minute)
    put("entry_hour", facts.entry_hour)
    put("weekday", facts.weekday)
    put("session_segment", facts.session_segment.value if facts.session_segment else None)
    put("duration_seconds", facts.duration_seconds)

    # Presence flags are always defined — "was a stop recorded?" is answerable even
    # when the answer is no, unlike "how far away was it?".
    context["has_stop"] = facts.stop_price is not None
    context["has_target"] = facts.target_price is not None
    put("risk_amount", facts.risk_amount)
    put("planned_r_multiple", facts.planned_r_multiple)

    if facts.risk_amount is not None and facts.account_equity:
        context["risk_pct_of_equity"] = facts.risk_amount / facts.account_equity

    if facts.stop_price is not None and facts.final_stop_price is not None:
        context["stop_moved"] = facts.final_stop_price != facts.stop_price
        # Widening is the one that matters: moving a stop *closer* is risk reduction,
        # moving it further is the behaviour that turns a planned loss into a large one.
        moved_further = (
            facts.final_stop_price < facts.stop_price
            if facts.direction is Direction.LONG
            else facts.final_stop_price > facts.stop_price
        )
        context["stop_widened"] = moved_further

    context["net_pnl"] = facts.net_pnl
    put("r_multiple", facts.r_multiple)
    put("mae_r", facts.mae_r)
    put("mfe_r", facts.mfe_r)

    put("setup", facts.setup)
    put("market_condition", facts.market_condition)
    put("confidence", facts.confidence)
    put("emotion", facts.emotion)

    context["trades_today"] = facts.trades_today
    context["session_pnl_before"] = facts.session_pnl_before
    context["consecutive_losses"] = facts.consecutive_losses
    put("minutes_since_last_trade", facts.minutes_since_last_trade)

    if facts.daily_loss_limit is not None:
        context["is_after_daily_loss_limit"] = (
            facts.session_pnl_before <= -abs(facts.daily_loss_limit)
        )

    return context


def unknown_fields(referenced: set[str]) -> set[str]:
    """Fields a rule references that the journal does not record.

    Checked when a strategy is saved, so a typo produces an error the trader can fix
    rather than a rule that reads as unevaluable forever.
    """
    return referenced - set(AVAILABLE_FIELDS)


#: Starter rules offered during onboarding. Chosen because each targets a leak that
#: shows up in most traders' histories and each is checkable from data the journal
#: already imports — a template rule that is always unevaluable would teach the wrong lesson
#: about what the compliance score means.
STARTER_RULES: list[dict[str, Any]] = [
    {
        "code": "always_use_a_stop",
        "label": "Every trade has a stop recorded",
        "rule_type": "risk",
        "severity": "critical",
        "expression": {"field": "has_stop", "op": "eq", "value": True},
    },
    {
        "code": "never_widen_a_stop",
        "label": "Stops are never moved further from entry",
        "rule_type": "management",
        "severity": "critical",
        "expression": {"field": "stop_widened", "op": "eq", "value": False},
    },
    {
        "code": "risk_at_most_1_percent",
        "label": "Risk at most 1% of equity per trade",
        "rule_type": "risk",
        "severity": "major",
        "expression": {"field": "risk_pct_of_equity", "op": "lte", "value": "0.01"},
    },
    {
        "code": "minimum_reward_to_risk",
        "label": "Planned reward is at least 1.5x risk",
        "rule_type": "target",
        "severity": "minor",
        "expression": {"field": "planned_r_multiple", "op": "gte", "value": "1.5"},
    },
    {
        "code": "stop_after_daily_loss_limit",
        "label": "No trades after the daily loss limit is hit",
        "rule_type": "risk",
        "severity": "critical",
        "expression": {"field": "is_after_daily_loss_limit", "op": "eq", "value": False},
    },
    {
        "code": "max_trades_per_session",
        "label": "At most 6 trades in a session",
        "rule_type": "filter",
        "severity": "major",
        "expression": {"field": "trades_today", "op": "lt", "value": 6},
    },
    {
        "code": "pause_after_two_losses",
        "label": "Wait at least 10 minutes after two consecutive losses",
        "rule_type": "management",
        "severity": "major",
        "expression": {
            "any": [
                {"field": "consecutive_losses", "op": "lt", "value": 2},
                {"field": "minutes_since_last_trade", "op": "gte", "value": 10},
            ]
        },
    },
    {
        "code": "no_revenge_sizing",
        "label": "Do not increase size while down on the session",
        "rule_type": "risk",
        "severity": "major",
        "expression": {
            "any": [
                {"field": "session_pnl_before", "op": "gte", "value": 0},
                {"field": "quantity", "op": "lte", "value": 2},
            ]
        },
    },
]
