"""Simulated account — PAPER-003.

Tracks cash, margin and positions the way a broker would, so the engine above it
cannot tell the difference between paper and live except through
``execution_realism``.

Margin is **estimated**, and says so. Real SPAN+exposure for F&O depends on
exchange files this system does not have, so the paper account applies a
per-product estimate and tags the result ``ESTIMATED``. These estimates do not
prove live affordability. Identified long options reserve their full premium;
they cannot become naked short positions through the simulated account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Any, Optional

from app.core.enums import InstrumentType, Product, Segment, TransactionType
from app.core.errors import ValidationError
from app.core.money import PAISE, quantize_money, to_decimal
from app.portfolio.fifo import Lot, match_fill

__all__ = ["PaperPosition", "PaperAccount", "MarginModel", "position_key"]


def position_key(trading_symbol: str, segment: Segment, product: Product) -> str:
    return f"{segment.value}:{product.value}:{trading_symbol}"


@dataclass
class MarginModel:
    """Fraction of notional required, per product.

    Deliberately conservative defaults:

    * ``CNC`` delivery — full value.
    * ``MIS`` intraday equity — 20% (5x), the common retail ceiling.
    * ``NRML`` derivatives — 20% of notional, a defensive stand-in for SPAN.
    """

    cnc: Decimal = Decimal("1.00")
    mis: Decimal = Decimal("0.20")
    nrml: Decimal = Decimal("0.20")
    #: Applied on top for derivatives, because the estimate is the weaker leg.
    fno_safety_multiplier: Decimal = Decimal("1.20")

    def requirement(self, notional: Decimal, product: Product, segment: Segment) -> Decimal:
        fraction = {
            Product.CNC: self.cnc,
            Product.MIS: self.mis,
            Product.NRML: self.nrml,
        }[product]
        required = notional * fraction
        if segment is Segment.FNO:
            required *= self.fno_safety_multiplier
        return quantize_money(required)


@dataclass
class PaperPosition:
    trading_symbol: str
    segment: Segment
    product: Product
    net_quantity: int = 0
    average_price: Decimal = Decimal("0")
    bought_quantity: int = 0
    sold_quantity: int = 0
    realised_pnl: Decimal = Decimal("0")
    last_price: Optional[Decimal] = None
    reserved_margin: Decimal = Decimal("0")
    fifo_lots: tuple[Lot, ...] = ()
    realised_exact: Decimal = Decimal("0")
    fill_sequence: int = 0
    instrument_type: InstrumentType | None = None

    @property
    def key(self) -> str:
        return position_key(self.trading_symbol, self.segment, self.product)

    @property
    def is_open(self) -> bool:
        return self.net_quantity != 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trading_symbol": self.trading_symbol,
            "segment": self.segment.value,
            "product": self.product.value,
            "net_quantity": self.net_quantity,
            "average_price": str(self.average_price),
            "bought_quantity": self.bought_quantity,
            "sold_quantity": self.sold_quantity,
            "realised_pnl": str(self.realised_pnl),
            "last_price": str(self.last_price) if self.last_price is not None else None,
            "reserved_margin": str(self.reserved_margin),
            "fifo_lots": [
                {"quantity": lot.quantity, "price": str(lot.price), "source_id": lot.source_id}
                for lot in self.fifo_lots
            ],
            "realised_exact": str(self.realised_exact),
            "fill_sequence": self.fill_sequence,
            "instrument_type": self.instrument_type.value if self.instrument_type else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperPosition":
        kind = InstrumentType(data["instrument_type"]) if data.get("instrument_type") else None
        if kind == InstrumentType.OPTION and (
            Segment(data["segment"]) != Segment.FNO
            or Product(data["product"]) == Product.CNC
            or int(data["net_quantity"]) < 0
            or Decimal(data.get("reserved_margin", "0"))
            != (Decimal(data["average_price"]) * int(data["net_quantity"])).quantize(
                PAISE, rounding=ROUND_CEILING
            )
        ):
            raise ValidationError("Invalid persisted long-option premium reservation")
        if data["net_quantity"] and "fifo_lots" not in data:
            raise ValidationError("Legacy open PAPER position needs FIFO reconstruction from fills")
        lots = tuple(
            Lot(row["quantity"], Decimal(row["price"]), row["source_id"])
            for row in data.get("fifo_lots", [])
        )
        if sum(lot.quantity for lot in lots) != int(data["net_quantity"]):
            raise ValidationError("PAPER FIFO quantity mismatch")
        if lots:
            if any((lot.quantity > 0) != (lots[0].quantity > 0) for lot in lots):
                raise ValidationError("PAPER FIFO opposing lots")
            average = sum((abs(lot.quantity) * lot.price for lot in lots), Decimal(0)) / abs(
                int(data["net_quantity"])
            )
            if average != Decimal(data["average_price"]):
                raise ValidationError("PAPER FIFO average mismatch")
        if quantize_money(
            Decimal(data.get("realised_exact", data.get("realised_pnl", "0")))
        ) != Decimal(data.get("realised_pnl", "0")):
            raise ValidationError("PAPER FIFO realised mismatch")
        return cls(
            trading_symbol=data["trading_symbol"],
            segment=Segment(data["segment"]),
            product=Product(data["product"]),
            net_quantity=int(data["net_quantity"]),
            average_price=Decimal(data["average_price"]),
            bought_quantity=int(data.get("bought_quantity", 0)),
            sold_quantity=int(data.get("sold_quantity", 0)),
            realised_pnl=Decimal(data.get("realised_pnl", "0")),
            last_price=Decimal(data["last_price"]) if data.get("last_price") else None,
            reserved_margin=Decimal(data.get("reserved_margin", "0")),
            fifo_lots=lots,
            realised_exact=Decimal(data.get("realised_exact", data.get("realised_pnl", "0"))),
            fill_sequence=int(data.get("fill_sequence", 0)),
            instrument_type=kind,
        )


@dataclass
class PaperAccount:
    """Cash, margin and positions for the simulated account."""

    starting_capital: Decimal
    cash: Decimal = field(default=Decimal("0"))
    realised_pnl: Decimal = Decimal("0")
    charges_paid: Decimal = Decimal("0")
    used_margin: Decimal = Decimal("0")
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    margin_model: MarginModel = field(default_factory=MarginModel)

    def __post_init__(self) -> None:
        self.starting_capital = to_decimal(self.starting_capital, field="starting_capital")
        if self.starting_capital <= 0:
            raise ValidationError(f"starting capital must be positive, got {self.starting_capital}")
        if self.cash == 0:
            self.cash = self.starting_capital

    # --- Margin -----------------------------------------------------------

    @property
    def available_margin(self) -> Decimal:
        return quantize_money(self.cash - self.used_margin)

    def requirement_for(
        self,
        quantity: int,
        price: Decimal,
        product: Product,
        segment: Segment,
        instrument_type: InstrumentType | None = None,
    ) -> Decimal:
        notional = to_decimal(price, field="price") * abs(int(quantity))
        if instrument_type == InstrumentType.OPTION:
            if segment != Segment.FNO or product == Product.CNC or notional <= 0:
                raise ValidationError("Invalid option product or segment")
            return notional.quantize(PAISE, rounding=ROUND_CEILING)
        return self.margin_model.requirement(notional, product, segment)

    def can_afford(
        self,
        quantity: int,
        price: Decimal,
        product: Product,
        segment: Segment,
        instrument_type: InstrumentType | None = None,
    ) -> bool:
        return (
            self.requirement_for(quantity, price, product, segment, instrument_type)
            <= self.available_margin
        )

    # --- Fills ------------------------------------------------------------

    def apply_fill(
        self,
        *,
        trading_symbol: str,
        segment: Segment,
        product: Product,
        transaction_type: TransactionType,
        quantity: int,
        price: Decimal,
        charges: Decimal = Decimal("0"),
        instrument_type: InstrumentType | None = None,
    ) -> PaperPosition:
        """Apply a fill, updating the position, realised P&L, cash and margin."""
        if quantity <= 0:
            raise ValidationError(f"fill quantity must be positive, got {quantity}")

        price = to_decimal(price, field="price")
        charges = to_decimal(charges, field="charges")
        key = position_key(trading_symbol, segment, product)
        position = self.positions.get(key) or PaperPosition(
            trading_symbol=trading_symbol, segment=segment, product=product
        )

        kind = (
            InstrumentType(instrument_type)
            if instrument_type is not None
            else position.instrument_type
        )
        if position.instrument_type is not None and kind != position.instrument_type:
            raise ValidationError("PAPER position instrument type changed")
        if (
            position.net_quantity
            and position.instrument_type is None
            and kind == InstrumentType.OPTION
        ):
            raise ValidationError("Legacy option position requires source reconstruction")

        signed = quantity * transaction_type.sign
        previous_qty = position.net_quantity
        new_qty = previous_qty + signed
        if kind == InstrumentType.OPTION:
            if new_qty < 0:
                raise ValidationError("NAKED_SHORT_DISABLED")
            self.requirement_for(quantity, price, product, segment, kind)

        if sum(lot.quantity for lot in position.fifo_lots) != previous_qty:
            raise ValidationError("PAPER FIFO quantity mismatch")
        sequence = position.fill_sequence + 1
        result = match_fill(position.fifo_lots, Lot(signed, price, f"{key}:{sequence}"))
        exact = position.realised_exact + result.realised
        realised = quantize_money(exact) - position.realised_pnl
        position.fifo_lots = result.lots
        position.fill_sequence = sequence
        position.instrument_type = kind
        position.realised_exact = exact
        position.realised_pnl = quantize_money(exact)
        position.average_price = result.average_price
        self.realised_pnl += realised
        self.cash += realised

        position.net_quantity = new_qty
        if transaction_type is TransactionType.BUY:
            position.bought_quantity += quantity
        else:
            position.sold_quantity += quantity
        position.last_price = price

        self.cash -= charges
        self.charges_paid += charges

        # Re-reserve margin against the resulting position.
        self.used_margin -= position.reserved_margin
        position.reserved_margin = (
            self.requirement_for(
                abs(new_qty), position.average_price or price, product, segment, kind
            )
            if new_qty
            else Decimal("0")
        )
        self.used_margin += position.reserved_margin
        self.used_margin = max(Decimal("0"), quantize_money(self.used_margin))

        if new_qty == 0:
            position.average_price = Decimal("0")
        self.positions[key] = position
        return position

    # --- Valuation --------------------------------------------------------

    def mark(self, trading_symbol: str, segment: Segment, product: Product, price: Decimal) -> None:
        position = self.positions.get(position_key(trading_symbol, segment, product))
        if position is not None:
            position.last_price = to_decimal(price, field="price")

    def unrealised_pnl(self) -> Decimal:
        total = Decimal("0")
        for position in self.positions.values():
            if position.net_quantity and position.last_price is not None:
                total += (position.last_price - position.average_price) * position.net_quantity
        return quantize_money(total)

    @property
    def equity(self) -> Decimal:
        return quantize_money(self.cash + self.unrealised_pnl())

    def open_positions(self) -> list[PaperPosition]:
        return [p for p in self.positions.values() if p.is_open]

    # --- Persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "starting_capital": str(self.starting_capital),
            "cash": str(self.cash),
            "realised_pnl": str(self.realised_pnl),
            "charges_paid": str(self.charges_paid),
            "used_margin": str(self.used_margin),
            "positions": [p.to_dict() for p in self.positions.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperAccount":
        account = cls(
            starting_capital=Decimal(data["starting_capital"]),
            cash=Decimal(data["cash"]),
            realised_pnl=Decimal(data.get("realised_pnl", "0")),
            charges_paid=Decimal(data.get("charges_paid", "0")),
            used_margin=Decimal(data.get("used_margin", "0")),
        )
        account.cash = Decimal(data["cash"])
        for row in data.get("positions", []):
            position = PaperPosition.from_dict(row)
            account.positions[position.key] = position
        if any(
            position.instrument_type == InstrumentType.OPTION
            for position in account.positions.values()
        ):
            expected = sum(
                (position.reserved_margin for position in account.positions.values()), Decimal(0)
            )
            if account.used_margin != quantize_money(expected):
                raise ValidationError("Persisted option account margin mismatch")
        return account
