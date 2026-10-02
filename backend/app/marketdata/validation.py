"""Market-data quality checks — MD-009.

Bad ticks are normal. A crossed book during a fast move, a zero print, a price
outside the circuit band — these arrive from real feeds, and a system that acts on
them takes real losses. Every anomaly here is *classified and rejected*, and the
rejection is recorded so the operator can see how noisy the feed is (MON-008).

Rejecting is not the same as ignoring: a rejected quote means "do not act", and
the caller treats it exactly as it would treat missing data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional, Sequence

from app.core.logging import get_logger
from app.marketdata.models import Bar, Quote

logger = get_logger("marketdata.validation")

__all__ = ["Anomaly", "ValidationResult", "validate_quote", "validate_bar", "QualityCounters"]


class Anomaly(str, Enum):
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    CROSSED_BOOK = "CROSSED_BOOK"
    OUTSIDE_CIRCUIT = "OUTSIDE_CIRCUIT"
    IMPLAUSIBLE_JUMP = "IMPLAUSIBLE_JUMP"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    INCONSISTENT_OHLC = "INCONSISTENT_OHLC"
    ZERO_VOLUME_WITH_MOVE = "ZERO_VOLUME_WITH_MOVE"
    NEGATIVE_OPEN_INTEREST = "NEGATIVE_OPEN_INTEREST"
    WIDE_SPREAD = "WIDE_SPREAD"


#: Anomalies that make the datum unusable rather than merely suspicious.
_FATAL = {
    Anomaly.NON_POSITIVE_PRICE,
    Anomaly.CROSSED_BOOK,
    Anomaly.OUTSIDE_CIRCUIT,
    Anomaly.INCONSISTENT_OHLC,
    Anomaly.NEGATIVE_VOLUME,
}


@dataclass
class ValidationResult:
    anomalies: list[Anomaly] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def add(self, anomaly: Anomaly, detail: str) -> None:
        self.anomalies.append(anomaly)
        self.details.append(detail)

    @property
    def ok(self) -> bool:
        """True when nothing fatal was found. Warnings do not block use."""
        return not any(anomaly in _FATAL for anomaly in self.anomalies)

    @property
    def fatal(self) -> list[Anomaly]:
        return [anomaly for anomaly in self.anomalies if anomaly in _FATAL]

    @property
    def warnings(self) -> list[Anomaly]:
        return [anomaly for anomaly in self.anomalies if anomaly not in _FATAL]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "anomalies": [a.value for a in self.anomalies],
            "details": list(self.details),
        }


@dataclass
class QualityCounters:
    """Running counts for the data-quality monitor (MON-008)."""

    checked: int = 0
    rejected: int = 0
    by_anomaly: dict[str, int] = field(default_factory=dict)

    def record(self, result: ValidationResult) -> None:
        self.checked += 1
        if not result.ok:
            self.rejected += 1
        for anomaly in result.anomalies:
            self.by_anomaly[anomaly.value] = self.by_anomaly.get(anomaly.value, 0) + 1

    @property
    def rejection_rate(self) -> float:
        return self.rejected / self.checked if self.checked else 0.0


def validate_quote(
    quote: Quote,
    *,
    previous_price: Optional[Decimal] = None,
    max_jump_pct: Decimal = Decimal("20"),
    max_spread_pct: Decimal = Decimal("2"),
) -> ValidationResult:
    """Check a quote for the anomalies that make it unsafe to trade on."""
    result = ValidationResult()

    if quote.ltp <= 0:
        result.add(Anomaly.NON_POSITIVE_PRICE, f"ltp={quote.ltp}")

    if quote.is_crossed:
        result.add(
            Anomaly.CROSSED_BOOK,
            f"bid {quote.best_bid} is above ask {quote.best_ask}",
        )

    if quote.volume is not None and quote.volume < 0:
        result.add(Anomaly.NEGATIVE_VOLUME, f"volume={quote.volume}")

    if quote.open_interest is not None and quote.open_interest < 0:
        result.add(Anomaly.NEGATIVE_OPEN_INTEREST, f"oi={quote.open_interest}")

    if quote.upper_circuit is not None and quote.ltp > quote.upper_circuit:
        result.add(
            Anomaly.OUTSIDE_CIRCUIT,
            f"ltp {quote.ltp} is above the upper circuit {quote.upper_circuit}",
        )
    if quote.lower_circuit is not None and quote.ltp < quote.lower_circuit:
        result.add(
            Anomaly.OUTSIDE_CIRCUIT,
            f"ltp {quote.ltp} is below the lower circuit {quote.lower_circuit}",
        )

    if previous_price is not None and previous_price > 0:
        move = abs(quote.ltp - previous_price) / previous_price * Decimal(100)
        if move > max_jump_pct:
            # A warning, not fatal: real gaps and circuit moves do happen, and
            # refusing to see them would be its own failure.
            result.add(
                Anomaly.IMPLAUSIBLE_JUMP,
                f"{move:.2f}% move from {previous_price} to {quote.ltp}",
            )

    spread = quote.spread
    if spread is not None and quote.ltp > 0:
        spread_pct = spread / quote.ltp * Decimal(100)
        if spread_pct > max_spread_pct:
            result.add(
                Anomaly.WIDE_SPREAD,
                f"spread {spread} is {spread_pct:.2f}% of price",
            )

    if not result.ok:
        logger.warning(
            "Rejected a market-data quote",
            extra={
                "trading_symbol": quote.instrument.trading_symbol,
                "anomalies": [a.value for a in result.fatal],
                "details": result.details,
            },
        )
    return result


def validate_bar(bar: Bar, *, previous: Optional[Bar] = None) -> ValidationResult:
    """Check an OHLCV bar for internal consistency."""
    result = ValidationResult()

    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        result.add(Anomaly.NON_POSITIVE_PRICE, "bar contains a non-positive price")

    if not bar.is_valid:
        result.add(
            Anomaly.INCONSISTENT_OHLC,
            f"o={bar.open} h={bar.high} l={bar.low} c={bar.close}",
        )

    if bar.volume < 0:
        result.add(Anomaly.NEGATIVE_VOLUME, f"volume={bar.volume}")

    if bar.volume == 0 and bar.range > 0:
        # Price cannot move without trades; this is usually a synthetic or
        # mis-stitched bar.
        result.add(
            Anomaly.ZERO_VOLUME_WITH_MOVE,
            f"range {bar.range} with zero volume",
        )

    if previous is not None and previous.close > 0:
        move = abs(bar.close - previous.close) / previous.close * Decimal(100)
        if move > Decimal("20"):
            result.add(Anomaly.IMPLAUSIBLE_JUMP, f"{move:.2f}% bar-to-bar move")

    return result


def validate_series(bars: Sequence[Bar]) -> tuple[list[Bar], QualityCounters]:
    """Filter a series to the bars that are safe to compute indicators on."""
    counters = QualityCounters()
    clean: list[Bar] = []
    previous: Optional[Bar] = None

    for bar in bars:
        result = validate_bar(bar, previous=previous)
        counters.record(result)
        if result.ok:
            clean.append(bar)
            previous = bar

    if counters.rejected:
        logger.warning(
            "Dropped inconsistent bars from a series",
            extra={
                "rejected": counters.rejected,
                "checked": counters.checked,
                "by_anomaly": counters.by_anomaly,
            },
        )
    return clean, counters
