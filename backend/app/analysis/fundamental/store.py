"""Versioned fundamental imports and metric-level freshness, with explicit exclusions."""

from datetime import datetime, timedelta

import sqlalchemy as sa

from app.analysis.fundamental.score import fundamental_score
from app.analysis.fundamental.source import (
    METRICS,
    FundamentalEvidence,
    FundamentalSource,
    FundamentalView,
)
from app.core.clock import UTC, Clock, get_clock
from app.core.ids import new_id
from app.core.logging import get_logger
from app.db import session as db_session
from app.db.models.fundamental_versions import FundamentalVersion
from app.db.models.instrument import Instrument
from app.fno.chain.model import aware

logger = get_logger("analysis.fundamental")


class FundamentalStore:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or get_clock()

    async def import_source(
        self, source: FundamentalSource, content: str, *, max_age: timedelta
    ) -> tuple[str, ...]:
        if max_age < timedelta(0):
            raise ValueError("invalid fundamental freshness policy")
        records = source.load(content)
        receipt = self.clock.utcnow().astimezone(UTC)
        identifiers = []
        async with db_session.session_scope() as session:
            for record in records:
                if record.known_at > receipt:
                    raise ValueError("future fundamental knowledge")
                if await session.get(Instrument, record.instrument_id) is None:
                    raise ValueError("unknown fundamental instrument")
                existing = await session.scalar(
                    sa.select(FundamentalVersion).where(
                        FundamentalVersion.instrument_id == record.instrument_id,
                        FundamentalVersion.source == record.source,
                        FundamentalVersion.known_at == record.known_at.astimezone(UTC),
                    )
                )
                payload = record.model_dump(mode="json")
                if existing:
                    if existing.payload["evidence"] != payload:
                        raise ValueError("conflicting fundamental revision")
                    identifiers.append(existing.id)
                else:
                    record_id = new_id("fnv")
                    view = _view(record_id, record, record.known_at, max_age)
                    payload = {
                        "schema_version": 1,
                        "evidence": payload,
                        "initial_score": fundamental_score(view).model_dump(mode="json"),
                        "freshness_seconds": str(max_age.total_seconds()),
                    }
                    session.add(
                        FundamentalVersion(
                            id=record_id,
                            instrument_id=record.instrument_id,
                            source=record.source,
                            known_at=record.known_at.astimezone(UTC),
                            received_at=receipt,
                            payload=payload,
                        )
                    )
                    await session.flush()
                    identifiers.append(record_id)
        return tuple(identifiers)

    async def at(
        self,
        instrument_id: str,
        *,
        source: str,
        as_of: datetime,
        max_age: timedelta,
    ) -> FundamentalView:
        cutoff = aware(as_of).astimezone(UTC)
        if max_age < timedelta(0):
            raise ValueError("invalid fundamental freshness policy")
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(FundamentalVersion)
                .where(
                    FundamentalVersion.instrument_id == instrument_id,
                    FundamentalVersion.source == source,
                    FundamentalVersion.known_at <= cutoff,
                    FundamentalVersion.received_at <= cutoff,
                )
                .order_by(FundamentalVersion.known_at.desc())
                .limit(1)
            )
        evidence = FundamentalEvidence.model_validate(row.payload["evidence"]) if row else None
        return _view(row.id if row else None, evidence, as_of, max_age)


def _view(record_id, evidence, as_of, max_age):
    metrics, exclusions = {}, {}
    for name in sorted(METRICS):
        metric = evidence.metrics.get(name) if evidence else None
        if metric is None:
            exclusions[name] = "UNAVAILABLE"
        elif as_of - metric.as_of > max_age:
            exclusions[name] = "STALE"
            metric = None
            logger.warning(
                "Fundamental metric excluded as stale",
                extra={
                    "instrument_id": evidence.instrument_id,
                    "metric": name,
                    "record_id": record_id,
                },
            )
        metrics[name] = metric
    return FundamentalView(
        record_id=record_id, evidence=evidence, metrics=metrics, exclusions=exclusions
    )
