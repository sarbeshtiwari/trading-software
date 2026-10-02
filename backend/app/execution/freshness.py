"""Entry authorization never renews the decision's underlying evidence."""

from datetime import timedelta

from app.agents.pipeline import DecisionContext
from app.analysis.regime.classifier import RegimeDecision, evidence_fresh_at
from app.core.enums import Segment
from app.core.errors import SafetyError
from app.db import session as db_session
from app.db.models.instrument import Instrument
from app.execution.option_policy import require_long_option_policy
from app.news.availability import research_at
from app.news.research_sentiment import AdvisorySentiment, sentiment_at
from app.risk.event_controls import require_event_entries
from app.risk.instrument_blocks import require_instrument_entries
from app.risk.news_halts import require_no_news_halt
from app.trading.options import require_option_observation


def require_fresh_sources(proposal, limits, now):
    snapshot = proposal.context_snapshot
    context = DecisionContext.from_snapshot(snapshot)
    if (
        not timedelta(0)
        <= now - context.market.as_of
        <= timedelta(seconds=limits.max_market_age_seconds)
    ):
        raise SafetyError("STALE_DECISION_CONTEXT")
    costs = context.costs
    if costs.valid_until is not None and now >= costs.valid_until:
        raise SafetyError("EXPIRED_CONTRACT_POLICY")
    if costs.fee_schedule is not None:
        try:
            costs.fee_schedule.require_at(now)
        except ValueError as error:
            raise SafetyError("EXPIRED_CONTRACT_FEE_SOURCE") from error
    if not costs.observed_at <= costs.available_at <= now or now - costs.observed_at > timedelta(
        seconds=limits.max_market_age_seconds
    ):
        raise SafetyError("STALE_CONTRACT_EVIDENCE")
    regime = RegimeDecision.model_validate(snapshot["regime_snapshot"])
    if not evidence_fresh_at(regime, now) or not timedelta(
        0
    ) <= now - regime.inputs.as_of <= timedelta(seconds=context.strategy.max_input_age_seconds):
        raise SafetyError("STALE_REGIME_EVIDENCE")
    if context.strategy.requires_event_calendar:
        calendar = regime.inputs.calendar
        if calendar is None or calendar.active(proposal.trading_symbol, now) != ():
            raise SafetyError("EVENT_BLACKOUT_OR_UNAVAILABLE")
    return context


async def require_entry_sources(proposal, limits, now):
    context = require_fresh_sources(proposal, limits, now)
    async with db_session.session_scope() as session:
        await require_no_news_halt(session, context.market)
        await require_instrument_entries(session, context.market)
        await require_event_entries(session, context.market, strategy_id=context.strategy.id)
        instrument = await session.get(Instrument, proposal.instrument_id)
    if instrument is not None and (instrument.is_option or context.market.greeks is not None):
        try:
            await require_option_observation(
                instrument,
                context.market.greeks,
                as_of=now,
                origin=context.market.data_origin,
                max_age_seconds=limits.max_greeks_age_seconds,
            )
        except ValueError as error:
            raise SafetyError("OPTION_OBSERVATION_UNAVAILABLE_OR_CHANGED") from error
    if proposal.segment != Segment.CASH:
        try:
            await require_long_option_policy(proposal, instrument, context, now)
        except (ValueError, KeyError, TypeError) as error:
            raise SafetyError("OPTION_CONTRACT_SOURCE_INVALID") from error
    await require_current_news(proposal, now)


async def require_current_news(proposal, now):
    context = DecisionContext.from_snapshot(proposal.context_snapshot)
    references = {item["source_id"] for item in proposal.evidence if item["kind"] == "NEWS"}
    if not references and "news" not in context.strategy.required_inputs:
        return
    if not references:
        raise SafetyError("NEWS_EXECUTION_EVIDENCE_REQUIRED")
    research = await research_at(
        proposal.instrument_id,
        as_of=now,
        origin=context.market.data_origin,
        max_age_seconds=context.strategy.max_input_age_seconds,
    )
    current = {item["source_id"]: item for item in research.articles}
    sealed = proposal.context_snapshot["validated_evidence"]
    if research.status != "AVAILABLE" or any(
        identifier not in current or current[identifier] != sealed.get(f"NEWS:{identifier}")
        for identifier in references
    ):
        raise SafetyError("NEWS_EXECUTION_EVIDENCE_UNAVAILABLE_OR_CHANGED")
    if {"news", "sentiment"}.issubset(context.strategy.required_inputs):
        await _require_sentiment(proposal, context, now, references)


async def _require_sentiment(proposal, context, now, references):
    try:
        sealed = AdvisorySentiment.model_validate(proposal.context_snapshot.get("news_sentiment"))
    except ValueError as error:
        raise SafetyError("NEWS_SENTIMENT_EXECUTION_EVIDENCE_REQUIRED") from error
    current = await sentiment_at(
        proposal.instrument_id,
        as_of=now,
        origin=context.market.data_origin,
        max_age_seconds=context.strategy.max_input_age_seconds,
    )
    if (
        current.status != "AVAILABLE"
        or sealed.status != "AVAILABLE"
        or current.instrument_id != sealed.instrument_id
        or current.data_origin != sealed.data_origin
        or current.policy != sealed.policy
        or set(current.admission_ids) != set(sealed.admission_ids)
        or not set(current.admission_ids).issubset(references)
        or current.result is None
        or sealed.result is None
        or {item.evidence.id: item.evidence for item in current.result.contributions}
        != {item.evidence.id: item.evidence for item in sealed.result.contributions}
    ):
        raise SafetyError("NEWS_SENTIMENT_EXECUTION_EVIDENCE_UNAVAILABLE_OR_CHANGED")
