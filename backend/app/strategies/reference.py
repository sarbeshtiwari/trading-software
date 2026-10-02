"""Closed-candle breakout hypothesis; advisory only, never an order producer."""

from decimal import ROUND_FLOOR, Decimal

from app.core.clock import get_clock
from app.core.enums import MarketRegime, Product, SignalDirection
from app.strategies.base import Strategy, StrategySpec
from app.strategies.exits import Invalidation
from app.strategies.signal import Signal

REFERENCE_IDS = ("closed-candle-breakout", "long-option-breakout")


class ClosedCandleBreakout(Strategy):
    def __init__(
        self,
        instrument_id,
        instrument_key,
        tick_size,
        risk_fraction,
        *,
        clock=None,
        long_option=False,
    ):
        self.instrument_id = instrument_id
        self.instrument_key = instrument_key
        self.tick_size = Decimal(tick_size)
        if not self.tick_size.is_finite() or self.tick_size <= 0:
            raise ValueError("invalid instrument tick")
        self.clock = clock or get_clock()
        self.long_option = long_option
        self._spec = StrategySpec(
            id="long-option-breakout" if long_option else "closed-candle-breakout",
            version="1",
            timeframe_seconds=60,
            universe=(instrument_key,),
            permitted_regimes=(MarketRegime.TRENDING_UP,),
            required_inputs=("candles", "indicators", "regime"),
            product=Product.MIS,
            entry_condition={
                "operator": "AND",
                "children": (
                    {"operator": "LEAF", "input": "PRICE"},
                    {"operator": "LEAF", "input": "TECHNICAL"},
                ),
            },
            exit_rules=("STOP", "TARGET", "TRAILING", "TIME", "INVALIDATION"),
            risk={"requested_risk_fraction": risk_fraction, "minimum_reward_risk": 2},
            exit_policy={
                "trailing_r_multiple": 1,
                "max_holding_seconds": 3600,
                "invalidation_key": "close-below-sma20",
                "max_mark_age_seconds": 15,
            },
            requires_llm=False,
            requires_event_calendar=True,
            max_input_age_seconds=60,
            hypothesis=(
                "A long option premium breaking its closed one-minute high above SMA20 "
                "may continue "
                "during an underlying uptrend. Full premium is at risk; the fixed target is four "
                "times entry premium. This is an unvalidated hypothesis, not a profitability claim."
                if long_option
                else "A closed one-minute high breakout above SMA20 may continue in an uptrend."
            ),
            validation_plan=(
                "Fixed parameters; chronological OOS, walk-forward and PAPER evidence required."
            ),
        )

    @property
    def spec(self):
        return self._spec

    def entry(self, context):
        bars, indicators, regime = context.candles, context.indicators, context.regime
        if len(bars) < 21 or regime.label != MarketRegime.TRENDING_UP:
            return None
        latest = bars[-1]
        if latest.close <= max(bar.high for bar in bars[-21:-1]):
            return None
        if latest.close <= indicators["sma20"] or indicators["atr14"] <= 0:
            return None
        stop = ((latest.close - 2 * indicators["atr14"]) / self.tick_size).to_integral_value(
            rounding=ROUND_FLOOR
        ) * self.tick_size
        if stop <= 0 or latest.close % self.tick_size:
            return None
        return Signal(
            strategy_id=self.spec.id,
            strategy_version=self.spec.version,
            instrument_id=self.instrument_id,
            instrument_key=self.instrument_key,
            generated_at=context.as_of,
            data_origin=regime.inputs.data_origin,
            direction=SignalDirection.LONG,
            product=self.spec.product,
            entry=latest.close,
            stop=stop,
            targets=(
                latest.close * 4 if self.long_option else latest.close + 2 * (latest.close - stop),
            ),
            timeframe_seconds=60,
            confidence=Decimal(1),
            conditions_fired=(
                "closed-high-breakout-20",
                "close-above-sma20",
                "trending-up",
                "confidence-is-rule-completeness-not-win-probability",
            ),
        )

    def invalidation(self, context):
        return Invalidation(
            key=self.spec.exit_policy.invalidation_key,
            active=context.candles[-1].close < context.indicators["sma20"],
            observed_at=self.clock.now(),
            available_at=self.clock.now(),
        )
