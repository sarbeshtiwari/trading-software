"""Reference market observations converge on the existing deterministic decision path."""

import sqlalchemy as sa

from app.agents.pipeline import DecisionContext, DecisionPipeline
from app.agents.proposal import EvidenceReference
from app.agents.validation import ValidationMarket
from app.analysis.regime.classifier import RegimeDecision
from app.audit.service import AuditIdentity, AuditService
from app.core.calendar import get_trading_calendar
from app.core.clock import IST
from app.core.enums import SessionPhase
from app.core.sessions import phase_at
from app.db import session as db_session
from app.db.models.regime import RegimeHistory
from app.modes import TradingMode
from app.risk.models import BookLevel
from app.strategies.context import ContextValue
from app.strategies.engine import StrategyEngine
from app.strategies.registry import StrategyRegistry


class ReferenceDecisionService:
    def __init__(self, ingestion, validator, *, calendar=None):
        self.ingestion = ingestion
        self.clock = ingestion.clock
        self.pipeline = DecisionPipeline(validator, calendar=calendar)
        self.engine = StrategyEngine(StrategyRegistry(), self.clock)
        self.calendar = calendar or get_trading_calendar()

    async def evaluate(self, strategy, context: DecisionContext, *, observation=None):
        if context.market.mode != TradingMode.PAPER or strategy.spec != context.strategy:
            raise ValueError("PAPER reference context required")
        now = self.clock.now().astimezone(IST)
        if (
            not self.calendar.is_year_complete(now.year)
            or phase_at(now, calendar=self.calendar) != SessionPhase.REGULAR
        ):
            raise ValueError("reference session unavailable or closed")
        observation = observation or await self.ingestion.collect(context.market.instrument_id)
        quote = observation.quote
        as_of = self.clock.now()
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(RegimeHistory).where(RegimeHistory.id == context.regime_id)
            )
        if row is None:
            raise ValueError("persisted regime unavailable")
        regime = RegimeDecision.model_validate(row.decision)
        available = observation.available | {
            "regime": ContextValue(
                observed_at=regime.inputs.as_of,
                available_at=regime.inputs.as_of,
                origin=regime.inputs.data_origin,
                value=regime,
            )
        }
        evaluation = await self.engine.evaluate(
            strategy,
            instrument_id=context.market.instrument_id,
            instrument_key=strategy.instrument_key,
            as_of=as_of,
            regime_id=context.regime_id,
            regime=regime,
            available=available,
            llm_available=False,
            blackout=context.blackout,
        )
        context = context.model_copy(
            update={
                "cycle_id": observation.chain_id,
                "prices": ValidationMarket(
                    instrument_id=context.market.instrument_id,
                    as_of=as_of,
                    observed_at=quote.observed_at,
                    available_at=as_of,
                    data_origin=quote.data_origin,
                    price=quote.ltp,
                    atr=available["indicators"].value["atr14"],
                ),
                "market": context.market.model_copy(
                    update={
                        "as_of": as_of,
                        "observed_at": quote.observed_at,
                        "available_at": as_of,
                        "source_id": observation.chain_id,
                        "data_origin": quote.data_origin,
                        "bids": tuple(
                            BookLevel(price=level.price, quantity=level.quantity)
                            for level in quote.bids
                        ),
                        "asks": tuple(
                            BookLevel(price=level.price, quantity=level.quantity)
                            for level in quote.asks
                        ),
                    }
                ),
            }
        )
        result = await self.pipeline.process(
            evaluation.signal,
            context,
            evidence=(
                EvidenceReference(kind="MARKET", source_id=observation.chain_id),
                *(
                    EvidenceReference(kind="NEWS", source_id=article["source_id"])
                    for article in evaluation.news_evidence
                ),
            ),
            archive_fallback=True,
        )
        await AuditService(self.clock).append(
            AuditIdentity(
                chain_id=result.candidate_id,
                event_type="REFERENCE_EVALUATION",
                actor="reference_strategy",
                mode=TradingMode.PAPER,
            ),
            {
                "instrument_id": context.market.instrument_id,
                "proposal_id": result.proposal_id,
                "strategy_id": strategy.spec.id,
                "correlation_id": observation.chain_id,
                "data_used": {
                    "news": evaluation.news_evidence,
                    **(
                        {"news_sentiment": evaluation.news_sentiment}
                        if evaluation.news_sentiment
                        else {}
                    ),
                },
                "result": {"strategy_reason": evaluation.reason, "pipeline_code": result.code},
            },
        )
        return result
