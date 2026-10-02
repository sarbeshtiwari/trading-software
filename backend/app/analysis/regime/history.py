"""REG-005: append-only application history with durable hysteresis state.

Like other writers, this service requires single-writer ownership. No journal
entry is retroactively relabelled. Callers attribute an entry to the exact returned
record id, rather than reading the current label after a trade has closed.
"""

from datetime import datetime

import sqlalchemy as sa

from app.analysis.regime.classifier import RegimeDecision, RegimePolicy, classify
from app.analysis.regime.inputs import RegimeInputs
from app.core.clock import UTC, Clock, get_clock
from app.core.data_origin import DataOrigin
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.regime import RegimeHistory
from app.fno.chain.model import aware


class RegimeStore:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or get_clock()

    async def record(
        self, inputs: RegimeInputs, policy: RegimePolicy
    ) -> tuple[str, RegimeDecision]:
        receipt = self.clock.utcnow().astimezone(UTC)
        if inputs.as_of > receipt:
            raise ValueError("future regime decision")
        async with db_session.session_scope() as session:
            latest = await session.scalar(
                sa.select(RegimeHistory)
                .where(
                    RegimeHistory.underlying == inputs.underlying,
                    RegimeHistory.data_origin == inputs.data_origin.value,
                )
                .order_by(RegimeHistory.ts.desc())
                .limit(1)
            )
            previous = RegimeDecision.model_validate(latest.decision) if latest else None
            if previous is not None and inputs.as_of == previous.inputs.as_of:
                if inputs != previous.inputs or policy != previous.policy:
                    raise ValueError("conflicting regime decision")
                return latest.id, previous
            result = classify(inputs, policy, previous)
            record_id = new_id("reg")
            session.add(
                RegimeHistory(
                    id=record_id,
                    underlying=inputs.underlying,
                    data_origin=inputs.data_origin.value,
                    ts=inputs.as_of.astimezone(UTC),
                    decision=result.model_dump(mode="json"),
                    created_at=receipt,
                    updated_at=receipt,
                )
            )
        return record_id, result

    async def at(
        self,
        underlying: str,
        data_origin: DataOrigin,
        as_of: datetime,
    ) -> tuple[str, RegimeDecision] | None:
        cutoff = aware(as_of).astimezone(UTC)
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(RegimeHistory)
                .where(
                    RegimeHistory.underlying == underlying,
                    RegimeHistory.data_origin == data_origin.value,
                    RegimeHistory.ts <= cutoff,
                    RegimeHistory.created_at <= cutoff,
                )
                .order_by(RegimeHistory.ts.desc())
                .limit(1)
            )
        return (row.id, RegimeDecision.model_validate(row.decision)) if row is not None else None
