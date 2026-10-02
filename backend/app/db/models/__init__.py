"""SQLAlchemy models.

Importing this package registers every model on the shared metadata, which is
what Alembic autogeneration and ``metadata.create_all`` both rely on. Adding a
new model file without importing it here means it silently never gets a table.
"""

from app.db.base import Base, metadata
from app.db.models.audit import AuditEvent, ConfigChange
from app.db.models.backtest import BacktestResult, BacktestRun, BacktestTrade
from app.db.models.config import RiskConfigVersion, StrategyRegistration
from app.db.models.decision import (
    ConsideredCandidate,
    Proposal,
    RiskDecision,
    SizingRecord,
)
from app.db.models.equity import EquityEvidence
from app.db.models.execution import PaperExecutionSlot, PaperStateRevision
from app.db.models.fundamental_versions import CorporateCalendarVersion, FundamentalVersion
from app.db.models.fundamentals import CorporateEvent, FundamentalSnapshot
from app.db.models.instrument import Instrument
from app.db.models.instrument_snapshot import InstrumentMasterSnapshot
from app.db.models.historical_jobs import HistoricalJob
from app.db.models.journal import JournalAnnotation, JournalEntry, JournalRevision
from app.db.models.llm import LLMBudgetDay, LLMCall, LLMProviderState
from app.db.models.market_data import Candle, OptionChainSnapshot, Tick
from app.db.models.news import NewsItem, NewsSource
from app.db.models.paper import PaperBrokerState
from app.db.models.regime import RegimeHistory
from app.db.models.security import DashboardSession, LoginGuard
from app.db.models.system import (
    Discrepancy,
    HealthRecord,
    Heartbeat,
    PortfolioSnapshot,
    SystemState,
)
from app.db.models.trading import Order, OrderEvent, Position, Trade
from app.db.models.walkforward import WalkForwardJob

__all__ = [
    "AuditEvent",
    "BacktestResult",
    "BacktestRun",
    "BacktestTrade",
    "Base",
    "Candle",
    "ConfigChange",
    "ConsideredCandidate",
    "CorporateCalendarVersion",
    "CorporateEvent",
    "DashboardSession",
    "Discrepancy",
    "EquityEvidence",
    "FundamentalSnapshot",
    "FundamentalVersion",
    "HealthRecord",
    "Heartbeat",
    "Instrument",
    "InstrumentMasterSnapshot",
    "JournalAnnotation",
    "JournalEntry",
    "JournalRevision",
    "LLMCall",
    "LLMBudgetDay",
    "LLMProviderState",
    "LoginGuard",
    "NewsItem",
    "NewsSource",
    "OptionChainSnapshot",
    "Order",
    "OrderEvent",
    "PaperBrokerState",
    "PaperExecutionSlot",
    "PaperStateRevision",
    "PortfolioSnapshot",
    "Position",
    "Proposal",
    "RegimeHistory",
    "RiskConfigVersion",
    "RiskDecision",
    "SizingRecord",
    "StrategyRegistration",
    "SystemState",
    "Tick",
    "Trade",
    "metadata",
    "HistoricalJob",
    "WalkForwardJob",
]
