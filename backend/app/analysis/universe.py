"""EQ-001: deterministic daily universe from point-in-time stored snapshots."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import Field, model_validator

from app.analysis.equity import EquitySnapshot, EvidenceModel
from app.analysis.liquidity import LiquidityPolicy, screen_liquidity
from app.core.clock import IST
from app.core.enums import Exchange
from app.fno.chain.model import aware


class UniversePolicy(EvidenceModel):
    indices: tuple[str, ...]
    exchanges: tuple[Exchange, ...]
    minimum_price: Decimal = Field(gt=0)
    maximum_price: Decimal = Field(gt=0)
    require_fno: bool = False
    liquidity: LiquidityPolicy

    @model_validator(mode="after")
    def bounds(self):
        if self.minimum_price > self.maximum_price or not self.exchanges:
            raise ValueError("invalid universe policy")
        return self


class UniverseResult(EvidenceModel):
    session: date
    included: tuple[str, ...]
    excluded: dict[str, tuple[str, ...]]
    policy: UniversePolicy


def resolve_universe(
    snapshots: list[EquitySnapshot],
    *,
    as_of: datetime,
    quantities: dict[str, int],
    policy: UniversePolicy,
) -> UniverseResult:
    aware(as_of)
    if len({snapshot.key for snapshot in snapshots}) != len(snapshots):
        raise ValueError("duplicate universe snapshot")
    included, excluded = [], {}
    for snapshot in sorted(snapshots, key=lambda item: item.key):
        reasons = _exclusions(snapshot, policy)
        if snapshot.key not in quantities:
            reasons.append("POSITION_SIZE_UNAVAILABLE")
        else:
            try:
                result = screen_liquidity(
                    snapshot,
                    quantity=quantities[snapshot.key],
                    as_of=as_of,
                    policy=policy.liquidity,
                )
                reasons.extend(result.reasons)
            except ValueError as exc:
                reasons.append(str(exc))
        if snapshot.known_at > as_of:
            reasons.append("FUTURE_METADATA")
        if reasons:
            excluded[snapshot.key] = tuple(reasons)
        else:
            included.append(snapshot.key)
    return UniverseResult(
        session=as_of.astimezone(IST).date(),
        included=tuple(included),
        excluded=excluded,
        policy=policy,
    )


def _exclusions(snapshot, policy):
    checks = {
        "INACTIVE": not snapshot.active,
        "RESTRICTED": snapshot.restricted,
        "EXCHANGE": snapshot.exchange not in policy.exchanges,
        "INDEX_MEMBERSHIP": bool(policy.indices)
        and not set(policy.indices).intersection(snapshot.indices),
        "FNO_INELIGIBLE": policy.require_fno and not snapshot.fno_eligible,
        "PRICE_BAND": not policy.minimum_price <= snapshot.price.value <= policy.maximum_price,
        "SECTOR_UNMAPPED": snapshot.sector is None,
    }
    return [reason for reason, rejected in checks.items() if rejected]
