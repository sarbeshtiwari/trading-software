"""OPT-003: select only supplied contract expiries, never infer exchange weekdays."""

from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Literal

from app.core.clock import IST
from app.fno.chain.model import aware
from app.fno.options import OptionContract


def select_expiry(
    contracts: Sequence[OptionContract],
    *,
    as_of: datetime,
    min_days: int,
    kind: Literal["ANY", "WEEKLY", "MONTHLY"] = "ANY",
) -> date | None:
    aware(as_of)
    if type(min_days) is not int or min_days < 0 or kind not in ("ANY", "WEEKLY", "MONTHLY"):
        raise ValueError("invalid expiry selection policy")
    if len({(contract.underlying, contract.exchange) for contract in contracts}) > 1:
        raise ValueError("mixed underlying or exchange")
    dates = set()
    for contract in contracts:
        if kind != "ANY" and contract.weekly is not (kind == "WEEKLY"):
            continue
        if (contract.expiry - as_of.astimezone(IST).date()).days < min_days:
            continue
        if as_of >= datetime.combine(contract.expiry, time(15, 30), tzinfo=IST):
            continue
        dates.add(contract.expiry)
    return min(dates) if dates else None
