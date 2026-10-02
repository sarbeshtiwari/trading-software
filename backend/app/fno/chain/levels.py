"""OC-005: OI concentration is a heuristic, never a guaranteed market level."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.fno.chain.model import band
from app.marketdata.models import OptionChain


class OILevels(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    support: Decimal | None
    resistance: Decimal | None
    heuristic: Literal[True] = True


def oi_levels(chain: OptionChain) -> OILevels:
    rows = band(chain, None, None)

    def highest(side: str) -> Decimal | None:
        if not rows or any(
            getattr(row, side) is None or getattr(row, side).open_interest is None for row in rows
        ):
            return None
        chosen = min(rows, key=lambda row: (-getattr(row, side).open_interest, row.strike))
        return chosen.strike if getattr(chosen, side).open_interest > 0 else None

    return OILevels(support=highest("put"), resistance=highest("call"))
