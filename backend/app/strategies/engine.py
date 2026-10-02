"""Deterministic pre-signal gates; no execution layer or broker access."""

from datetime import datetime, timedelta

from app.analysis.equity import EvidenceModel
from app.analysis.events import EventBlackout
from app.analysis.regime.classifier import RegimeDecision, evidence_fresh_at
from app.core.clock import Clock, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import MarketRegime
from app.core.logging import get_logger
from app.fno.chain.model import aware
from app.modes import TradingMode
from app.news.availability import research_at
from app.news.research_sentiment import sentiment_at
from app.strategies.base import Strategy
from app.strategies.context import ContextValue, build_strategy_context, validate_context_inputs
from app.strategies.registry import StrategyRegistry, specification_hash
from app.strategies.signal import Signal

logger = get_logger("strategies.engine")


class Evaluation(EvidenceModel):
    signal: Signal | None
    reason: str
    regime_id: str
    news_evidence: tuple[dict, ...] = ()
    news_sentiment: dict | None = None


class StrategyEngine:
    def __init__(self, registry: StrategyRegistry, clock: Clock | None = None) -> None:
        self.registry = registry
        self.clock = clock or get_clock()

    async def evaluate(
        self,
        strategy: Strategy,
        *,
        instrument_id: str,
        instrument_key: str,
        as_of: datetime,
        regime_id: str,
        regime: RegimeDecision,
        available: dict,
        mode: TradingMode = TradingMode.PAPER,
        llm_available: bool,
        blackout: EventBlackout | None = None,
    ) -> Evaluation:
        aware(as_of)
        spec = strategy.spec
        row = await self.registry.get(spec.id, spec.version)
        reason = _gate(
            row,
            spec,
            mode=mode,
            instrument_key=instrument_key,
            as_of=as_of,
            regime=regime,
            llm_available=llm_available,
            blackout=blackout,
        )
        if not regime_id.strip():
            reason = "MISSING_REGIME_ID"
        if as_of > self.clock.now():
            reason = "FUTURE_DECISION"
        signal = None
        news_evidence = ()
        news_sentiment = None
        if reason is None and "news" in spec.required_inputs:
            research = await research_at(
                instrument_id,
                as_of=as_of,
                origin=regime.inputs.data_origin,
                max_age_seconds=spec.max_input_age_seconds,
            )
            if research.status != "AVAILABLE":
                reason = research.code
            else:
                news_evidence = research.articles
                available = {
                    **available,
                    "news": ContextValue(
                        observed_at=min(
                            datetime.fromisoformat(item["published_at"])
                            for item in research.articles
                        ),
                        available_at=as_of,
                        origin=regime.inputs.data_origin,
                        value=research.articles,
                    ),
                }
        if reason is None and {"news", "sentiment"}.issubset(spec.required_inputs):
            available, news_sentiment, reason = await _sentiment_context(
                available,
                instrument_id,
                as_of=as_of,
                origin=regime.inputs.data_origin,
                max_age_seconds=spec.max_input_age_seconds,
            )
        if reason is None:
            try:
                validate_context_inputs(
                    spec.required_inputs,
                    available,
                    as_of=as_of,
                    max_age=timedelta(seconds=spec.max_input_age_seconds),
                    origin=regime.inputs.data_origin,
                    timeframe_seconds=spec.timeframe_seconds,
                )
                if "regime" in spec.required_inputs and available["regime"].value != regime:
                    raise ValueError("strategy regime context disagrees with gate")
                context = build_strategy_context(spec.required_inputs, available, as_of=as_of)
                proposal = strategy.entry(context)
                if proposal is not None:
                    signal = Signal.model_validate(proposal.model_dump())
                    if (
                        signal.strategy_id,
                        signal.strategy_version,
                        signal.instrument_id,
                        signal.instrument_key,
                        signal.generated_at,
                        signal.timeframe_seconds,
                        signal.product,
                        signal.data_origin,
                    ) != (
                        spec.id,
                        spec.version,
                        instrument_id,
                        instrument_key,
                        as_of,
                        spec.timeframe_seconds,
                        spec.product,
                        regime.inputs.data_origin,
                    ):
                        raise ValueError("signal identity or provenance mismatch")
                reason = "SIGNAL" if signal is not None else "NO_SIGNAL"
            except IndexError:
                reason, signal = "STRATEGY_DATA_BOUNDARY_VIOLATION", None
            except (ValueError, TypeError, AttributeError):
                reason, signal = "INVALID_OR_UNAVAILABLE_STRATEGY_INPUT_OUTPUT", None
        logger.info(
            "Strategy evaluated",
            extra={
                "strategy_id": spec.id,
                "regime_id": regime_id,
                "reason": reason,
                "mode": mode.value,
            },
        )
        return Evaluation(
            signal=signal,
            reason=reason,
            regime_id=regime_id,
            news_evidence=news_evidence,
            news_sentiment=news_sentiment,
        )


async def _sentiment_context(available, instrument_id, *, as_of, origin, max_age_seconds):
    sentiment = await sentiment_at(
        instrument_id, as_of=as_of, origin=origin, max_age_seconds=max_age_seconds
    )
    if sentiment.status != "AVAILABLE":
        return available, None, sentiment.code
    resolved = {
        **available,
        "sentiment": ContextValue(
            observed_at=min(item.evidence.published_at for item in sentiment.result.contributions),
            available_at=as_of,
            origin=origin,
            value=sentiment.result,
        ),
    }
    return resolved, sentiment.model_dump(mode="json"), None


def _gate(row, spec, *, mode, instrument_key, as_of, regime, llm_available, blackout):
    if row is None or row.parameter_hash != specification_hash(spec):
        return "UNREGISTERED_OR_CHANGED_STRATEGY"
    enabled = {
        TradingMode.PAPER: row.enabled_paper,
        TradingMode.SUPERVISED: row.enabled_supervised,
        TradingMode.LIVE: row.enabled_live,
    }[mode]
    max_age = timedelta(seconds=spec.max_input_age_seconds)
    invalid_calendar = spec.requires_event_calendar and blackout is None
    if blackout is not None:
        invalid_calendar = (
            blackout.symbol not in {instrument_key, instrument_key.split(":")[-1]}
            or not timedelta(0) <= as_of - blackout.as_of <= max_age
            or (
                (spec.requires_event_calendar or spec.id in blackout.restricted_strategies)
                and blackout.status != "CLEAR"
            )
        )
    gates = (
        ("STRATEGY_MODE_DISABLED", not enabled or row.auto_disabled or mode == TradingMode.LIVE),
        (
            "NONLIVE_DATA",
            mode != TradingMode.PAPER and regime.inputs.data_origin != DataOrigin.LIVE,
        ),
        ("OUTSIDE_UNIVERSE", instrument_key not in spec.universe),
        (
            "STALE_OR_FUTURE_REGIME",
            not timedelta(0) <= as_of - regime.inputs.as_of <= max_age
            or not evidence_fresh_at(regime, as_of),
        ),
        (
            "REGIME_NOT_PERMITTED",
            regime.label == MarketRegime.UNKNOWN or regime.label not in spec.permitted_regimes,
        ),
        ("LLM_UNAVAILABLE", spec.requires_llm and not llm_available),
        ("EVENT_BLACKOUT_OR_UNAVAILABLE", invalid_calendar),
    )
    return next((reason for reason, blocked in gates if blocked), None)
