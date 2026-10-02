"""Exact signed-unit FIFO matching; quantities already include contract lot size."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Lot:
    quantity: int
    price: Decimal
    source_id: str

    def __post_init__(self):
        if type(self.quantity) is not int or not self.quantity:
            raise ValueError("nonzero integer lot quantity required")
        if not isinstance(self.price, Decimal) or not self.price.is_finite() or self.price <= 0:
            raise ValueError("finite positive Decimal lot price required")
        if not self.source_id:
            raise ValueError("lot source required")


@dataclass(frozen=True)
class Match:
    entry_id: str
    exit_id: str
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal


@dataclass(frozen=True)
class Result:
    lots: tuple[Lot, ...]
    matches: tuple[Match, ...]

    @property
    def quantity(self):
        return sum(lot.quantity for lot in self.lots)

    @property
    def average_price(self):
        quantity = abs(self.quantity)
        return (
            sum((abs(lot.quantity) * lot.price for lot in self.lots), Decimal(0)) / quantity
            if quantity
            else Decimal(0)
        )

    @property
    def realised(self):
        return sum((match.gross_pnl for match in self.matches), Decimal(0))


def match_fill(lots: tuple[Lot, ...], fill: Lot) -> Result:
    if lots and any((lot.quantity > 0) != (lots[0].quantity > 0) for lot in lots):
        raise ValueError("open FIFO lots cannot contain opposing directions")
    remaining = fill.quantity
    kept = []
    matches = []
    for lot in lots:
        if not remaining or (lot.quantity > 0) == (remaining > 0):
            kept.append(lot)
            continue
        quantity = min(abs(lot.quantity), abs(remaining))
        direction = 1 if lot.quantity > 0 else -1
        matches.append(
            Match(
                lot.source_id,
                fill.source_id,
                quantity,
                lot.price,
                fill.price,
                (fill.price - lot.price) * quantity * direction,
            )
        )
        left = lot.quantity - quantity * direction
        remaining += quantity * direction
        if left:
            kept.append(Lot(left, lot.price, lot.source_id))
    if remaining:
        kept.append(Lot(remaining, fill.price, fill.source_id))
    return Result(tuple(kept), tuple(matches))
