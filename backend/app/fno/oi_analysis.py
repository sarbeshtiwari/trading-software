"""SENT-003: descriptive derivatives context; OI concentration is not a direction forecast."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.analysis.equity import EvidenceModel
from app.marketdata.models import OptionChain
from app.strategies.context import ChainContext, build_chain_context


class DerivativeSentiment(EvidenceModel):
    chain: ChainContext
    oi_balance: Literal["PUT_HEAVY", "CALL_HEAVY", "BALANCED", "UNAVAILABLE"]
    heuristic: Literal[True] = True
    standalone_trigger_allowed: Literal[False] = False


def derivative_sentiment(
    current: OptionChain,
    previous: OptionChain | None,
    *,
    as_of: datetime,
    max_age: timedelta,
    max_comparison_gap: timedelta,
    low_pcr: Decimal,
    high_pcr: Decimal,
) -> DerivativeSentiment:
    if not low_pcr.is_finite() or not high_pcr.is_finite() or not 0 <= low_pcr < high_pcr:
        raise ValueError("invalid PCR interpretation thresholds")
    context = build_chain_context(
        current,
        as_of=as_of,
        max_age=max_age,
        previous=previous,
        max_comparison_gap=max_comparison_gap,
    )
    balance = "UNAVAILABLE"
    if context.pcr_oi is not None:
        balance = (
            "CALL_HEAVY"
            if context.pcr_oi < low_pcr
            else ("PUT_HEAVY" if context.pcr_oi > high_pcr else "BALANCED")
        )
    return DerivativeSentiment(chain=context, oi_balance=balance)
