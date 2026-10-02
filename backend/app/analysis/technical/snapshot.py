"""Technical snapshot — TA-011.

One typed object holding every indicator value for an instrument at a moment,
together with the parameters used and the bar it was computed at.

Why it exists in this shape: the audit trail must record *exactly* what the
decision saw (AUDIT-004). A snapshot that stores only the values, without the
parameters and the bar timestamp, cannot be replayed — and an unreplayable audit
record is a story, not evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional, Sequence

from app.analysis.technical import levels as levels_module
from app.analysis.technical.base import closes, last_value
from app.analysis.technical.ma import ema, sma, vwap
from app.analysis.technical.momentum import macd, rsi, stochastic
from app.analysis.technical.price_action import consolidation_score, geometry, range_expansion
from app.analysis.technical.trend import adx, donchian_channels, supertrend
from app.analysis.technical.volatility import atr, atr_percent, bollinger_bands
from app.analysis.technical.volume import obv, relative_volume, vwap_deviation
from app.core.clock import ensure_ist
from app.core.enums import SignalDirection
from app.core.errors import InsufficientHistoryError
from app.core.logging import get_logger
from app.marketdata.models import Bar

logger = get_logger("analysis.technical.snapshot")

__all__ = ["SnapshotParams", "TechnicalSnapshot", "build_snapshot", "SNAPSHOT_INDICATORS"]

#: Indicators the snapshot computes. The no-orphan test (TA-012) checks the
#: registry against this set plus whatever strategies declare.
SNAPSHOT_INDICATORS = frozenset(
    {
        "sma",
        "ema",
        "rsi",
        "macd",
        "stochastic",
        "atr",
        "bollinger",
        "adx",
        "supertrend",
        "donchian",
        "obv",
        "relative_volume",
        "vwap",
        "vwap_deviation",
        "classic_pivots",
        "range_expansion",
        "consolidation",
    }
)


@dataclass(frozen=True)
class SnapshotParams:
    """Every parameter the snapshot uses, recorded with the result."""

    fast_ma: int = 20
    slow_ma: int = 50
    trend_ma: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    bollinger_period: int = 20
    donchian_period: int = 20
    supertrend_period: int = 10
    volume_period: int = 20

    def to_dict(self) -> dict[str, int]:
        return {
            "fast_ma": self.fast_ma,
            "slow_ma": self.slow_ma,
            "trend_ma": self.trend_ma,
            "rsi_period": self.rsi_period,
            "atr_period": self.atr_period,
            "adx_period": self.adx_period,
            "bollinger_period": self.bollinger_period,
            "donchian_period": self.donchian_period,
            "supertrend_period": self.supertrend_period,
            "volume_period": self.volume_period,
        }

    @property
    def min_bars(self) -> int:
        """Bars needed for every field to be populated."""
        return max(
            self.trend_ma,
            self.adx_period * 2 + 1,
            self.bollinger_period,
            self.donchian_period + 1,
            self.volume_period + 1,
            self.rsi_period + 1,
            self.atr_period + 1,
        )


@dataclass(frozen=True)
class TechnicalSnapshot:
    """Indicator values for one instrument at one bar."""

    instrument: str
    interval_minutes: int
    bar_ts: datetime
    close: Decimal
    params: SnapshotParams

    # Trend
    ema_fast: Optional[Decimal] = None
    ema_slow: Optional[Decimal] = None
    sma_trend: Optional[Decimal] = None
    adx: Optional[Decimal] = None
    plus_di: Optional[Decimal] = None
    minus_di: Optional[Decimal] = None
    supertrend: Optional[Decimal] = None
    supertrend_direction: Optional[SignalDirection] = None
    donchian_upper: Optional[Decimal] = None
    donchian_lower: Optional[Decimal] = None

    # Momentum
    rsi: Optional[Decimal] = None
    macd: Optional[Decimal] = None
    macd_signal: Optional[Decimal] = None
    macd_histogram: Optional[Decimal] = None
    stochastic_k: Optional[Decimal] = None
    stochastic_d: Optional[Decimal] = None

    # Volatility
    atr: Optional[Decimal] = None
    atr_percent: Optional[Decimal] = None
    bollinger_upper: Optional[Decimal] = None
    bollinger_lower: Optional[Decimal] = None
    bollinger_bandwidth: Optional[Decimal] = None
    percent_b: Optional[Decimal] = None

    # Volume
    obv: Optional[Decimal] = None
    relative_volume: Optional[Decimal] = None
    vwap: Optional[Decimal] = None
    vwap_deviation: Optional[Decimal] = None

    # Structure
    pivot: Optional[Decimal] = None
    pivot_r1: Optional[Decimal] = None
    pivot_s1: Optional[Decimal] = None
    prior_high: Optional[Decimal] = None
    prior_low: Optional[Decimal] = None
    prior_close: Optional[Decimal] = None

    # Price action
    body_ratio: Optional[Decimal] = None
    range_expansion: Optional[Decimal] = None
    consolidation: Optional[Decimal] = None

    #: Fields that could not be computed, with the reason.
    unavailable: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Flat, JSON-safe form for the audit record."""
        payload: dict[str, Any] = {
            "instrument": self.instrument,
            "interval_minutes": self.interval_minutes,
            "bar_ts": self.bar_ts.isoformat(),
            "close": str(self.close),
            "params": self.params.to_dict(),
            "unavailable": dict(self.unavailable),
        }
        for name, value in self.__dict__.items():
            if name in payload or name in ("params", "unavailable"):
                continue
            if isinstance(value, Decimal):
                payload[name] = str(value)
            elif isinstance(value, SignalDirection):
                payload[name] = value.value
            elif isinstance(value, datetime):
                payload[name] = value.isoformat()
            elif not isinstance(value, dict):
                payload[name] = value
        return payload


def _safe(name: str, compute, unavailable: dict[str, str]):  # type: ignore[no-untyped-def]
    """Run one indicator, recording *why* it is missing rather than hiding it."""
    try:
        return compute()
    except InsufficientHistoryError as exc:
        unavailable[name] = str(exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a broken indicator must not kill the snapshot
        unavailable[name] = f"{type(exc).__name__}: {exc}"
        logger.warning("Indicator failed", extra={"indicator": name, "error": str(exc)})
        return None


def build_snapshot(
    bars: Sequence[Bar],
    *,
    instrument: str,
    interval_minutes: int,
    params: Optional[SnapshotParams] = None,
) -> TechnicalSnapshot:
    """Compute every snapshot indicator over ``bars``.

    Indicators without enough history are ``None`` **and** listed in
    ``unavailable`` with the reason. Silence would let a strategy treat "not
    enough data" as "no signal", which are very different things.
    """
    if not bars:
        raise InsufficientHistoryError(
            "cannot build a technical snapshot from an empty series",
            context={"instrument": instrument},
        )

    resolved = params or SnapshotParams()
    unavailable: dict[str, str] = {}
    price = closes(bars)
    last_bar = bars[-1]

    ema_fast = _safe("ema_fast", lambda: last_value(ema(price, resolved.fast_ma)), unavailable)
    ema_slow = _safe("ema_slow", lambda: last_value(ema(price, resolved.slow_ma)), unavailable)
    sma_trend = _safe("sma_trend", lambda: last_value(sma(price, resolved.trend_ma)), unavailable)

    adx_result = _safe("adx", lambda: adx(bars, resolved.adx_period), unavailable)
    supertrend_result = _safe(
        "supertrend", lambda: supertrend(bars, resolved.supertrend_period), unavailable
    )
    donchian = _safe(
        "donchian", lambda: donchian_channels(bars, resolved.donchian_period), unavailable
    )

    rsi_value = _safe("rsi", lambda: last_value(rsi(price, resolved.rsi_period)), unavailable)
    macd_result = _safe("macd", lambda: macd(price), unavailable)
    stochastic_result = _safe("stochastic", lambda: stochastic(bars), unavailable)

    atr_value = _safe("atr", lambda: last_value(atr(bars, resolved.atr_period)), unavailable)
    atr_pct = _safe(
        "atr_percent", lambda: last_value(atr_percent(bars, resolved.atr_period)), unavailable
    )
    bands = _safe(
        "bollinger", lambda: bollinger_bands(price, resolved.bollinger_period), unavailable
    )

    obv_value = _safe("obv", lambda: last_value(obv(bars)), unavailable)
    rel_volume = _safe(
        "relative_volume",
        lambda: last_value(relative_volume(bars, resolved.volume_period)),
        unavailable,
    )
    vwap_value = _safe("vwap", lambda: last_value(vwap(bars)), unavailable)
    vwap_dev = _safe("vwap_deviation", lambda: last_value(vwap_deviation(bars)), unavailable)

    previous = _safe(
        "prior_session",
        lambda: levels_module.previous_session(bars, ensure_ist(last_bar.ts).date()),
        unavailable,
    )
    pivots = None
    if previous is not None:
        pivots = _safe("pivots", lambda: levels_module.classic_pivots(previous), unavailable)

    expansion = _safe(
        "range_expansion", lambda: last_value(range_expansion(bars)), unavailable
    )
    consolidation = _safe(
        "consolidation", lambda: last_value(consolidation_score(bars)), unavailable
    )
    shape = geometry(last_bar)

    return TechnicalSnapshot(
        instrument=instrument,
        interval_minutes=interval_minutes,
        bar_ts=ensure_ist(last_bar.ts),
        close=last_bar.close,
        params=resolved,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        sma_trend=sma_trend,
        adx=last_value(adx_result.adx) if adx_result else None,
        plus_di=last_value(adx_result.plus_di) if adx_result else None,
        minus_di=last_value(adx_result.minus_di) if adx_result else None,
        supertrend=last_value(supertrend_result.value) if supertrend_result else None,
        supertrend_direction=(
            supertrend_result.direction[-1] if supertrend_result else None
        ),
        donchian_upper=last_value(donchian.upper) if donchian else None,
        donchian_lower=last_value(donchian.lower) if donchian else None,
        rsi=rsi_value,
        macd=last_value(macd_result.macd) if macd_result else None,
        macd_signal=last_value(macd_result.signal) if macd_result else None,
        macd_histogram=last_value(macd_result.histogram) if macd_result else None,
        stochastic_k=last_value(stochastic_result.k) if stochastic_result else None,
        stochastic_d=last_value(stochastic_result.d) if stochastic_result else None,
        atr=atr_value,
        atr_percent=atr_pct,
        bollinger_upper=last_value(bands.upper) if bands else None,
        bollinger_lower=last_value(bands.lower) if bands else None,
        bollinger_bandwidth=last_value(bands.bandwidth) if bands else None,
        percent_b=last_value(bands.percent_b) if bands else None,
        obv=obv_value,
        relative_volume=rel_volume,
        vwap=vwap_value,
        vwap_deviation=vwap_dev,
        pivot=pivots.pivot if pivots else None,
        pivot_r1=pivots.r1 if pivots else None,
        pivot_s1=pivots.s1 if pivots else None,
        prior_high=previous.high if previous else None,
        prior_low=previous.low if previous else None,
        prior_close=previous.close if previous else None,
        body_ratio=shape.body_ratio,
        range_expansion=expansion,
        consolidation=consolidation,
        unavailable=unavailable,
    )
