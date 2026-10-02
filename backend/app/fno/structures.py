"""OPT-005: named, finite-loss option structures; no naked-short builder.

Legs are supplied in the documented order below. This creates analysis objects,
not execution instructions: account holdings, fills, margin and aggregate risk
must still be checked independently by the future risk/execution engines.
"""

from decimal import Decimal
from enum import Enum
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.enums import OptionType, TransactionType
from app.fno.options import OptionContract
from app.fno.payoff import PayoffLeg, StockHolding, payoff_summary, validate_legs


class StructureKind(str, Enum):
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    COVERED_CALL = "COVERED_CALL"
    PROTECTIVE_PUT = "PROTECTIVE_PUT"
    BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
    BEAR_CALL_SPREAD = "BEAR_CALL_SPREAD"
    BULL_PUT_SPREAD = "BULL_PUT_SPREAD"
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
    LONG_STRADDLE = "LONG_STRADDLE"
    LONG_STRANGLE = "LONG_STRANGLE"
    IRON_CONDOR = "IRON_CONDOR"


PATTERNS = {
    StructureKind.LONG_CALL: ((OptionType.CE,), (1,)),
    StructureKind.LONG_PUT: ((OptionType.PE,), (1,)),
    StructureKind.COVERED_CALL: ((OptionType.CE,), (-1,)),
    StructureKind.PROTECTIVE_PUT: ((OptionType.PE,), (1,)),
    StructureKind.BULL_CALL_SPREAD: ((OptionType.CE, OptionType.CE), (1, -1)),
    StructureKind.BEAR_CALL_SPREAD: ((OptionType.CE, OptionType.CE), (-1, 1)),
    StructureKind.BULL_PUT_SPREAD: ((OptionType.PE, OptionType.PE), (1, -1)),
    StructureKind.BEAR_PUT_SPREAD: ((OptionType.PE, OptionType.PE), (-1, 1)),
    StructureKind.LONG_STRADDLE: ((OptionType.CE, OptionType.PE), (1, 1)),
    StructureKind.LONG_STRANGLE: ((OptionType.PE, OptionType.CE), (1, 1)),
    StructureKind.IRON_CONDOR: (
        (OptionType.PE, OptionType.PE, OptionType.CE, OptionType.CE),
        (1, -1, -1, 1),
    ),
}


class OptionStructure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: StructureKind
    legs: tuple[PayoffLeg, ...]
    stock: StockHolding | None = None

    @model_validator(mode="after")
    def validate_structure(self):
        validate_legs(self.legs, self.stock)
        types, signs = PATTERNS[self.kind]
        if tuple(leg.contract.option_type for leg in self.legs) != types:
            raise ValueError("incorrect option types or leg count")
        if tuple(leg.side.sign for leg in self.legs) != signs:
            raise ValueError("incorrect leg directions")
        if len({leg.quantity for leg in self.legs}) != 1:
            raise ValueError("unmatched leg quantities")
        strikes = tuple(leg.contract.strike for leg in self.legs)
        if self.kind == StructureKind.LONG_STRADDLE:
            if strikes[0] != strikes[1]:
                raise ValueError("straddle strikes must match")
        elif any(left >= right for left, right in pairwise(strikes)):
            raise ValueError("strikes must strictly increase")
        needs_stock = self.kind in (StructureKind.COVERED_CALL, StructureKind.PROTECTIVE_PUT)
        if needs_stock:
            if self.stock is None or self.stock.shares != self.legs[0].quantity:
                raise ValueError("exact underlying share coverage required")
        elif self.stock is not None:
            raise ValueError("unexpected stock leg")
        if payoff_summary(self.legs, self.stock).max_loss is None:
            raise ValueError("unbounded loss is not supported")
        return self


def build_structure(
    kind: StructureKind,
    contracts: tuple[OptionContract, ...],
    premiums: tuple[Decimal, ...],
    *,
    lots: int,
    stock: StockHolding | None = None,
) -> OptionStructure:
    """Generate signed lot-multiple legs and validate the named structure."""
    types, signs = PATTERNS[kind]
    if len(contracts) != len(types) or len(premiums) != len(types):
        raise ValueError("incorrect number of contracts or premiums")
    legs = tuple(
        PayoffLeg(
            contract=contract,
            premium=premium,
            lots=lots,
            side=TransactionType.BUY if sign > 0 else TransactionType.SELL,
        )
        for contract, premium, sign in zip(contracts, premiums, signs, strict=True)
    )
    return OptionStructure(kind=kind, legs=legs, stock=stock)
