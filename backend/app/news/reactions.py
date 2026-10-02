"""Opt-in conservative entry inhibition from admitted, uncalibrated news labels."""

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa
from pydantic import Field, model_validator

from app.analysis.equity import EvidenceModel
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import get_clock
from app.db import session as db_session
from app.db.models.decision import Proposal
from app.db.models.trading import Position
from app.modes import TradingMode
from app.news.entities import aware, history
from app.news.interpret import read_interpretation
from app.news.research import admitted_for_instrument, record_admission
from app.news.research_sentiment import claim_key
from app.risk.news_halts import inhibit

POLICY_CHAIN = "news-entry-reaction-policy"


class ReactionPolicy(EvidenceModel):
    enabled: bool
    accept_uncalibrated_labels: bool
    minimum_confidence: Decimal = Field(ge=0, le=1)
    max_age_seconds: int = Field(ge=1, le=86400, strict=True)

    @model_validator(mode="after")
    def consent(self):
        if self.enabled and not self.accept_uncalibrated_labels:
            raise ValueError("Owner acceptance of uncalibrated labels required")
        return self


class ReactionPolicyView(EvidenceModel):
    event_id: str | None = None
    policy: ReactionPolicy | None = None


async def policy_at(session):
    records = await history(session, POLICY_CHAIN, get_clock().utcnow())
    if not records:
        return ReactionPolicyView()
    if records[-1].event_type != "NEWS_REACTION_POLICY":
        raise ValueError("News reaction policy integrity unavailable")
    return ReactionPolicyView(
        event_id=records[-1].id, policy=ReactionPolicy.model_validate(records[-1].result["policy"])
    )


async def configure(session, policy, *, expected_event_id, reason, actor):
    if get_settings().trading_mode != TradingMode.PAPER:
        raise ValueError("News reactions are PAPER-only")
    records = await history(session, POLICY_CHAIN)
    if (
        (records[-1].id if records else None) != expected_event_id
        or (records and aware(records[-1].occurred_at) > get_clock().utcnow())
        or len(reason.strip()) < 10
    ):
        raise ValueError("News reaction policy changed or review reason unavailable")
    await AuditService().append_in_session(
        session,
        AuditIdentity(
            chain_id=POLICY_CHAIN,
            event_type="NEWS_REACTION_POLICY",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {"result": {"policy": policy.model_dump(mode="json"), "reason": reason.strip()}},
        expected_count=len(records),
    )
    await refresh(session)
    return await policy_at(session)


async def refresh(session, instrument_id=None):
    policy = await policy_at(session)
    if policy.policy is None or not policy.policy.enabled or not get_settings().news_enabled:
        return
    now = get_clock().utcnow()
    query = (
        sa.select(Position, Proposal)
        .join(Proposal, Position.proposal_id == Proposal.id)
        .where(
            Position.mode == TradingMode.PAPER,
            Position.net_quantity != 0,
            Position.opened_at <= now,
        )
    )
    if instrument_id is not None:
        query = query.where(Position.instrument_id == instrument_id)
    holdings = {}
    for position, proposal in (await session.execute(query)).all():
        origin = proposal.context_snapshot["market"]["data_origin"]
        holdings.setdefault((position.instrument_id, origin), []).append(position.id)
    for (identifier, origin), positions in holdings.items():
        snapshots = await admitted_for_instrument(
            session,
            identifier,
            as_of=now,
            origin=origin,
            max_age=timedelta(seconds=policy.policy.max_age_seconds),
        )
        for snapshot in snapshots:
            review = await read_interpretation(session, snapshot["article_id"], as_of=now)
            if review is None or review.event_id != snapshot["interpretation_event_id"]:
                continue
            parsed = review.result.result.interpretation
            claims = {
                claim_key(support["claim"]) for group in snapshot["claims"] for support in group
            }
            if (
                parsed.instrument_ids != (identifier,)
                or parsed.direction != "NEGATIVE"
                or parsed.magnitude != "HIGH"
                or parsed.confidence < policy.policy.minimum_confidence
                or any(claim_key(claim.model_dump()) not in claims for claim in parsed.claims)
            ):
                continue
            await inhibit(
                session,
                identifier,
                origin,
                {
                    "admission_id": snapshot["source_id"],
                    "origin": origin,
                    "policy_event_id": policy.event_id,
                    "policy": policy.policy.model_dump(mode="json"),
                    "positions": sorted(positions),
                    "evidence": snapshot,
                    "interpretation": parsed.model_dump(mode="json"),
                    "basis": "SOURCE_POLICY_QUOTATIONS_AND_UNCALIBRATED_MODEL_LABELS",
                },
            )


async def cycle():
    if get_settings().trading_mode == TradingMode.PAPER:
        async with db_session.session_scope() as session:
            await refresh(session)


async def admit(session, article_id, instrument_id, interpretation_id, *, actor):
    receipt = await record_admission(
        session, article_id, instrument_id, interpretation_id, actor=actor
    )
    await refresh(session, instrument_id)
    return receipt
