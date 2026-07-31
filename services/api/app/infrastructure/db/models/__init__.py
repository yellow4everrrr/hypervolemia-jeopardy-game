"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata`` — which is what
Alembic autogeneration and the test schema builder rely on. Import models from here,
not from their defining modules, so a new table is never silently missing from a
migration because nothing imported it.
"""

from app.infrastructure.db.base import Base
from app.infrastructure.db.models.ai import AiAnalysis, AiRecommendation, DetectedPattern
from app.infrastructure.db.models.analytics import (
    EquityCurvePoint,
    PerformanceMetric,
    Report,
    RiskMetric,
)
from app.infrastructure.db.models.broker import (
    Account,
    AccountBalanceSnapshot,
    BrokerConnection,
    SyncRun,
)
from app.infrastructure.db.models.catalog import (
    MarketCondition,
    RuleEvaluation,
    Setup,
    Strategy,
    StrategyRule,
    Tag,
    TradeTag,
)
from app.infrastructure.db.models.identity import User, UserSetting
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.journal import DailyJournal, Note, Screenshot
from app.infrastructure.db.models.marketdata import MarketBar, ReplayMetadata
from app.infrastructure.db.models.trading import (
    Execution,
    Order,
    Position,
    Trade,
    TradeExecution,
    TradingSession,
)

__all__ = [
    "Account",
    "AccountBalanceSnapshot",
    "AiAnalysis",
    "AiRecommendation",
    "Base",
    "BrokerConnection",
    "DailyJournal",
    "DetectedPattern",
    "EquityCurvePoint",
    "Execution",
    "Instrument",
    "MarketBar",
    "MarketCondition",
    "Note",
    "Order",
    "PerformanceMetric",
    "Position",
    "ReplayMetadata",
    "Report",
    "RiskMetric",
    "RuleEvaluation",
    "Screenshot",
    "Setup",
    "Strategy",
    "StrategyRule",
    "SyncRun",
    "Tag",
    "Trade",
    "TradeExecution",
    "TradeTag",
    "TradingSession",
    "User",
    "UserSetting",
]
