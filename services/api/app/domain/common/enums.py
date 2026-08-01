"""Domain enumerations.

These are `StrEnum` so they serialise to readable strings in JSON, in the database,
and in AI prompts without a translation table. Values are lowercase snake_case and are
part of the persisted contract — renaming one requires a migration.
"""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    """Direction of a single fill."""

    BUY = "buy"
    SELL = "sell"

    @property
    def signed_multiplier(self) -> int:
        """+1 for a buy, -1 for a sell — used to sign quantity into position delta."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class Direction(StrEnum):
    """Direction of a completed round-turn trade."""

    LONG = "long"
    SHORT = "short"

    @property
    def signed_multiplier(self) -> int:
        return 1 if self is Direction.LONG else -1

    @classmethod
    def from_signed_quantity(cls, signed_quantity: object) -> Direction:
        if signed_quantity > 0:  # type: ignore[operator]
            return cls.LONG
        if signed_quantity < 0:  # type: ignore[operator]
            return cls.SHORT
        raise ValueError("a flat position has no direction")


class TradeStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class AssetClass(StrEnum):
    FUTURE = "future"
    FUTURE_OPTION = "future_option"
    EQUITY = "equity"
    EQUITY_OPTION = "equity_option"
    FOREX = "forex"
    CRYPTO = "crypto"
    CFD = "cfd"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"
    MARKET_IF_TOUCHED = "market_if_touched"
    OTHER = "other"


class OrderStatus(StrEnum):
    PENDING = "pending"
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    GTD = "gtd"


class ExecutionRole(StrEnum):
    """Whether a fill increased or decreased exposure within its trade."""

    ENTRY = "entry"
    EXIT = "exit"


class SessionSegment(StrEnum):
    """Intraday segment, resolved in the instrument's exchange timezone.

    Segmentation is by *liquidity regime*, not clock convenience: the statistics for
    the opening drive and the lunch chop describe different markets, and averaging
    them together hides the edge in both.
    """

    OVERNIGHT = "overnight"
    ASIA = "asia"
    LONDON = "london"
    PRE_MARKET = "pre_market"
    RTH_OPEN = "rth_open"
    RTH_MORNING = "rth_morning"
    RTH_MIDDAY = "rth_midday"
    RTH_AFTERNOON = "rth_afternoon"
    RTH_CLOSE = "rth_close"
    POST_MARKET = "post_market"


class AccountType(StrEnum):
    LIVE = "live"
    EVALUATION = "evaluation"
    FUNDED = "funded"
    SIMULATED = "simulated"


class BrokerKind(StrEnum):
    TRADOVATE = "tradovate"
    MANUAL = "manual"
    CSV = "csv"


class ConnectionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DEGRADED = "degraded"
    REVOKED = "revoked"
    ERROR = "error"


class SyncStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


class ScreenshotKind(StrEnum):
    BEFORE_ENTRY = "before_entry"
    ENTRY = "entry"
    EXIT = "exit"
    AFTER_EXIT = "after_exit"
    HIGHER_TIMEFRAME = "higher_timeframe"
    EXECUTION_TIMEFRAME = "execution_timeframe"


class Emotion(StrEnum):
    CALM = "calm"
    CONFIDENT = "confident"
    ANXIOUS = "anxious"
    FEARFUL = "fearful"
    GREEDY = "greedy"
    FRUSTRATED = "frustrated"
    BORED = "bored"
    REVENGEFUL = "revengeful"
    FOMO = "fomo"


class RuleType(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    STOP = "stop"
    TARGET = "target"
    FILTER = "filter"
    RISK = "risk"
    MANAGEMENT = "management"
    CHECKLIST = "checklist"


class RuleSeverity(StrEnum):
    """How badly a violation should count against the compliance score."""

    ADVISORY = "advisory"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class AnalysisSubject(StrEnum):
    TRADE = "trade"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    STRATEGY = "strategy"
    SETUP = "setup"


class ReportType(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    COMPLIANCE = "compliance"


class ReportStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"


class MetricScope(StrEnum):
    """The slice a computed metric set describes.

    Metrics are stored once per (scope, scope_key, period) rather than in a wide table
    of named columns, because the set of interesting slices grows continuously and a
    new breakdown must never require a migration.
    """

    OVERALL = "overall"
    ACCOUNT = "account"
    INSTRUMENT = "instrument"
    STRATEGY = "strategy"
    SETUP = "setup"
    MARKET_CONDITION = "market_condition"
    DIRECTION = "direction"
    HOUR_OF_DAY = "hour_of_day"
    WEEKDAY = "weekday"
    MONTH = "month"
    SESSION_SEGMENT = "session_segment"
    DURATION_BUCKET = "duration_bucket"
    CONSECUTIVE_WINS = "consecutive_wins"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    DRAWDOWN_STATE = "drawdown_state"


class Timeframe(StrEnum):
    """Bar intervals supported by the market-data and replay engines."""

    S1 = "1s"
    S5 = "5s"
    S15 = "15s"
    S30 = "30s"
    M1 = "1m"
    M2 = "2m"
    M3 = "3m"
    M5 = "5m"
    M10 = "10m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"

    @property
    def seconds(self) -> int:
        unit = self.value[-1]
        amount = int(self.value[:-1])
        factor = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}[unit]
        return amount * factor


class JobKind(StrEnum):
    """What a background job does.

    Each maps to one handler. The three expensive surfaces — pattern scanning, model
    training and report generation — are the reason this queue exists: all three cost
    seconds, and until now all three were explicit ``POST`` endpoints a user waited on.
    """

    SYNC_BROKER = "sync_broker"
    COMPUTE_ANALYTICS = "compute_analytics"
    DETECT_PATTERNS = "detect_patterns"
    TRAIN_MODELS = "train_models"
    GENERATE_REPORTS = "generate_reports"
    #: The what-if sweep. Added after measurement rather than by anticipation: nine
    #: scenarios re-priced across 1,447 trades takes 65 seconds, which is past most proxy
    #: and load-balancer timeouts and far past the point where a person believes the page
    #: is broken. Reducing the resampling does not help — the cost is the re-pricing.
    RUN_SIMULATION = "run_simulation"
    #: Render and store the six chart images per trade. Queued after a sync rather than
    #: done inline: a backfill importing six months produces thousands of trades, and
    #: rendering during ingestion would make the import time a function of how many
    #: charts a PNG encoder can draw. One sweeping job rather than a job per trade — a
    #: fan-out would bury every other kind in the queue behind a backfill, and a sweep
    #: is idempotent by construction because a re-run simply finds fewer trades.
    CAPTURE_SCREENSHOTS = "capture_screenshots"
    MEASURE_EXCURSIONS = "measure_excursions"


class JobState(StrEnum):
    """Where a job is in its life.

    ``DEAD`` is separate from ``FAILED``: a failed job will be retried, a dead one has
    exhausted its attempts and needs a human. Collapsing them would either retry forever
    or give up on the first transient error.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"
