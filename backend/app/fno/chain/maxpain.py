"""OC-002: expiry intrinsic payout weighted by OI; lowest strike wins ties.

OI must use consistent units across the ladder. This is a payout statistic,
not a prediction. Incomplete or zero-total OI has no meaningful max-pain level.
"""

from decimal import Decimal

from app.fno.chain.model import band, complete_metric
from app.marketdata.models import OptionChain


def max_pain(chain: OptionChain) -> Decimal | None:
    rows = band(chain, None, None)
    if not complete_metric(rows, "open_interest"):
        return None
    if sum(leg.open_interest for row in rows for leg in (row.call, row.put)) == 0:
        return None
    payouts = {
        candidate.strike: sum(
            max(candidate.strike - row.strike, 0) * row.call.open_interest
            + max(row.strike - candidate.strike, 0) * row.put.open_interest
            for row in rows
        )
        for candidate in rows
    }
    return min(payouts, key=lambda strike: (payouts[strike], strike))
