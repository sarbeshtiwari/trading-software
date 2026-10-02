"""Allowlisted committed audit facts for read-only operational consumers."""

from app.core.events import EventType
from app.execution.event_types import PAPER_EVENT_TYPES

RUNTIME_EVENT_TYPES = {
    **PAPER_EVENT_TYPES,
    "RISK_SAFETY": EventType.RISK_STATE_CHANGED,
    "RISK_REARM": EventType.RISK_STATE_CHANGED,
    "HEALTH_TRANSITION": EventType.HEALTH_CHANGED,
    "PAPER_DISCREPANCY_OBSERVED": EventType.RECONCILIATION_DISCREPANCY,
    "PAPER_DISCREPANCY_RESOLVED": EventType.RECONCILIATION_DISCREPANCY,
}


def expected_actor(phase, settings):
    if phase in PAPER_EVENT_TYPES:
        return "paper_execution"
    return {
        "RISK_SAFETY": "risk_safety",
        "RISK_REARM": settings.dashboard_username,
        "HEALTH_TRANSITION": "health_watchdog",
        "PAPER_DISCREPANCY_OBSERVED": "paper_reconciliation",
        "PAPER_DISCREPANCY_RESOLVED": settings.dashboard_username,
    }.get(phase)
