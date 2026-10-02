"""Whitelisted public contract for verified PAPER session observations."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel


class LimitUsage(BaseModel):
    amount: Decimal
    limit: Decimal
    utilisation_pct: Decimal | None
    at_or_above_limit: bool


class SummaryLimits(BaseModel):
    positions_used: int | None
    max_concurrent_positions: int | None
    risk_config_version: int | None
    daily_loss: LimitUsage | None
    drawdown: LimitUsage | None
    exposure: LimitUsage | None = None
    unavailable_reason: str | None


class SummaryPosition(BaseModel):
    id: str
    symbol: str
    quantity: int


class SummaryMark(BaseModel):
    position_id: str
    price: Decimal
    quantity: int
    marked_at: AwareDatetime


class SummaryValuation(BaseModel):
    status: Literal["ESTIMATED", "UNAVAILABLE"]
    reason: str | None
    reconciled_at: AwareDatetime | None = None
    account_fill_ids: list[str] = []
    peak_snapshot_ids: list[str] = []
    marks: list[SummaryMark] = []
    equity: Decimal | None = None
    cash: Decimal | None = None
    peak_equity: Decimal | None = None
    unrealised_pnl: Decimal | None = None
    gross_exposure: Decimal | None = None


class CancellationObservation(BaseModel):
    audit_id: str
    chain_id: str
    order_id: str
    reason: str
    status: str
    filled_quantity: int | None
    terminal: bool | None
    error: str | None


class OrderHygieneObservation(BaseModel):
    cancellations: list[CancellationObservation]
    protection_failure_ids: list[str]
    scope: str


class PaperSummaryBody(BaseModel):
    session_date: date
    session_close: AwareDatetime
    as_of: AwareDatetime
    mode: Literal["PAPER"]
    execution_realism: Literal["SIMULATED"]
    scope: Literal[
        "IST_DAY_TO_FIRST_POST_CLOSE_OBSERVATION",
        "POST_CLOSE_RECOVERY_OBSERVATION",
        "MISSED_SESSION_RECOVERY",
    ]
    cycle_error: str | None
    worker_review_required: bool | None
    reconciliation: Literal["NOT_CONFIRMED", "COMPLETED_THIS_CYCLE"]
    fill_count: int | None
    traded_position_count: int | None
    fill_ids: list[str]
    gross_realised_pnl: Decimal | None
    charges: Decimal | None
    net_realised_pnl: Decimal | None
    cost_status: Literal["ESTIMATED", "NO_FILLS", "UNAVAILABLE"]
    open_positions: list[SummaryPosition]
    unresolved_order_ids: list[str]
    order_hygiene: OrderHygieneObservation | None = None
    critical_event_ids: list[str]
    limit_utilisation: SummaryLimits
    valuation: SummaryValuation | None = None
    previous_summary_id: str | None = None
    position_history_available: bool = True
    order_history_available: bool = True
    error_history_available: bool = True
    reconstruction_status: str | None = None


class PaperSummaryView(BaseModel):
    audit_id: str
    chain_id: str
    sequence: int
    event_type: str
    recorded_at: AwareDatetime
    summary: PaperSummaryBody


class PaperSummaryList(BaseModel):
    generated_at: AwareDatetime
    reports: list[PaperSummaryView]
