"""Explicit quantitative fixtures for lifecycle tests, never a production receipt bypass."""

from app.agents.proposal import TradeProposal
from app.strategies.signal import Signal


async def quant_decision(pipeline, body, context):
    proposal = TradeProposal.model_validate(body)
    signal = Signal(
        strategy_id=proposal.strategy,
        strategy_version=context.strategy.version,
        instrument_id=proposal.instrument,
        instrument_key=context.strategy.universe[0],
        generated_at=context.market.as_of,
        data_origin=context.market.data_origin,
        direction=proposal.direction,
        product=context.strategy.product,
        entry=proposal.entry,
        stop=proposal.stop_loss,
        targets=(proposal.target,),
        timeframe_seconds=context.strategy.timeframe_seconds,
        confidence=proposal.confidence,
        conditions_fired=(proposal.thesis,),
    )
    return await pipeline.process(signal, context, evidence=proposal.evidence)
