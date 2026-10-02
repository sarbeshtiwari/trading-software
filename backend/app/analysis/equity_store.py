"""Durable point-in-time equity evidence; no historical correction overwrites."""

from datetime import datetime

import sqlalchemy as sa

from app.analysis.equity import EquitySnapshot
from app.analysis.universe import UniversePolicy, UniverseResult, resolve_universe
from app.core.clock import UTC, Clock, get_clock
from app.core.data_origin import DataOrigin
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.equity import EquityEvidence
from app.fno.chain.model import aware


class EquityStore:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or get_clock()

    async def write(self, snapshot: EquitySnapshot) -> str:
        receipt = self.clock.utcnow()
        if snapshot.known_at > receipt:
            raise ValueError("future equity snapshot")
        payload = snapshot.model_dump(mode="json")
        async with db_session.session_scope() as session:
            existing = await session.scalar(
                sa.select(EquityEvidence).where(
                    EquityEvidence.key == snapshot.key,
                    EquityEvidence.origin == snapshot.data_origin.value,
                    EquityEvidence.known_at == snapshot.known_at.astimezone(UTC),
                )
            )
            if existing:
                if existing.payload != payload:
                    raise ValueError("conflicting equity snapshot")
                return existing.id
            record_id = new_id("eqs")
            session.add(
                EquityEvidence(
                    id=record_id,
                    key=snapshot.key,
                    origin=snapshot.data_origin.value,
                    known_at=snapshot.known_at.astimezone(UTC),
                    received_at=receipt.astimezone(UTC),
                    payload=payload,
                )
            )
        return record_id

    async def at(self, as_of: datetime, origin: DataOrigin) -> list[EquitySnapshot]:
        cutoff = aware(as_of).astimezone(UTC)
        async with db_session.session_scope() as session:
            rows = (
                await session.scalars(
                    sa.select(EquityEvidence)
                    .where(
                        EquityEvidence.origin == origin.value,
                        EquityEvidence.known_at <= cutoff,
                        EquityEvidence.received_at <= cutoff,
                    )
                    .order_by(EquityEvidence.known_at.desc())
                )
            ).all()
        latest = {}
        for row in rows:
            if row.key not in latest:
                latest[row.key] = EquitySnapshot.model_validate(row.payload)
        return [latest[key] for key in sorted(latest)]

    async def universe(
        self,
        *,
        as_of: datetime,
        origin: DataOrigin,
        quantities: dict[str, int],
        policy: UniversePolicy,
    ) -> UniverseResult:
        return resolve_universe(
            await self.at(as_of, origin), as_of=as_of, quantities=quantities, policy=policy
        )
