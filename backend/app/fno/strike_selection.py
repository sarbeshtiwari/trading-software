"""OPT-002: deterministic selectors with explicit freshness and liquidity policies.

ATM means nearest listed strike, lower on ties. N-strikes-OTM steps from that
strike toward OTM; an illiquid target is unavailable, never silently substituted.
Delta targeting uses signed delta and requires individually timestamped Greeks.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.core.enums import OptionType
from app.fno.chain.model import require_as_of
from app.fno.liquidity import OptionLiquidityPolicy, liquidity_failures
from app.fno.options import OptionContract
from app.marketdata.models import OptionChain, OptionLeg


def select_strike(
    chain: OptionChain,
    contracts: Sequence[OptionContract],
    side: OptionType,
    *,
    method: Literal["ATM", "OTM", "DELTA", "PREMIUM"],
    as_of: datetime,
    max_age: timedelta,
    liquidity: OptionLiquidityPolicy,
    steps: int = 1,
    target: Decimal | None = None,
) -> OptionContract | None:
    require_as_of(chain, as_of, max_age)
    if side not in (OptionType.CE, OptionType.PE) or method not in (
        "ATM",
        "OTM",
        "DELTA",
        "PREMIUM",
    ):
        raise ValueError("invalid strike selection method or side")
    mapping = _contract_map(chain, contracts, side)
    desired = None
    if method in ("ATM", "OTM"):
        desired = _desired_strike(chain, side, method, steps)
        if desired is None:
            return None
    else:
        _validate_target(method, side, target)
    candidates = []
    for row in chain.strikes:
        contract = mapping.get(row.strike)
        leg = row.call if side == OptionType.CE else row.put
        if contract is None or leg is None or leg.trading_symbol != contract.symbol:
            continue
        if not _eligible_quote(leg, contract, liquidity):
            continue
        if desired is not None:
            if row.strike == desired:
                return contract
            continue
        value = _target_value(leg, method, as_of, max_age)
        if value is not None:
            candidates.append((abs(value - target), row.strike, contract))
    return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def _contract_map(chain, contracts, side):
    if len({contract.exchange for contract in contracts}) > 1:
        raise ValueError("ambiguous exchange")
    mapping = {}
    for contract in contracts:
        if (contract.underlying, contract.expiry, contract.option_type) != (
            chain.underlying,
            chain.expiry,
            side,
        ):
            continue
        if contract.strike in mapping:
            raise ValueError("ambiguous strike identity")
        mapping[contract.strike] = contract
    return mapping


def _desired_strike(chain, side, method, steps):
    ladder = sorted(row.strike for row in chain.strikes)
    if not ladder or chain.spot is None:
        return None
    atm = min(ladder, key=lambda strike: (abs(strike - chain.spot), strike))
    offset = 0
    if method == "OTM":
        if type(steps) is not int or steps < 1:
            raise ValueError("positive OTM steps required")
        offset = steps if side == OptionType.CE else -steps
    index = ladder.index(atm) + offset
    return ladder[index] if 0 <= index < len(ladder) else None


def _validate_target(method, side, target):
    if target is None or not target.is_finite():
        raise ValueError("finite target required")
    if method == "PREMIUM" and target < 0:
        raise ValueError("negative premium target")
    if method == "DELTA" and not (
        (side == OptionType.CE and 0 <= target <= 1)
        or (side == OptionType.PE and -1 <= target <= 0)
    ):
        raise ValueError("delta target must have the option side's sign")


def _eligible_quote(leg, contract, liquidity):
    if liquidity_failures(leg, liquidity):
        return False
    return all(value % contract.tick_size == 0 for value in (leg.bid, leg.ask))


def _target_value(
    leg: OptionLeg,
    method: str,
    as_of: datetime,
    max_age: timedelta,
) -> Decimal | None:
    if method != "DELTA":
        return leg.ltp
    greek = leg.greeks
    if (
        greek is None
        or greek.computed_at is None
        or not timedelta(0) <= as_of - greek.computed_at <= max_age
    ):
        return None
    value = greek.delta
    if value is not None and (
        (leg.option_type == OptionType.CE and 0 <= value <= 1)
        or (leg.option_type == OptionType.PE and -1 <= value <= 0)
    ):
        return value
    return None
