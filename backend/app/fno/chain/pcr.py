"""OC-003: put/call ratios; missing observations and zero denominators are unavailable."""

from decimal import Decimal
from typing import Literal

from app.fno.chain.model import band, complete_metric
from app.marketdata.models import OptionChain


def pcr(
    chain: OptionChain,
    metric: Literal["open_interest", "volume"] = "open_interest",
    *,
    lower: Decimal | None = None,
    upper: Decimal | None = None,
) -> Decimal | None:
    if metric not in ("open_interest", "volume"):
        raise ValueError("unsupported PCR metric")
    rows = band(chain, lower, upper)
    if not complete_metric(rows, metric):
        return None
    calls = sum(getattr(row.call, metric) for row in rows)
    puts = sum(getattr(row.put, metric) for row in rows)
    return Decimal(puts) / Decimal(calls) if calls else None
