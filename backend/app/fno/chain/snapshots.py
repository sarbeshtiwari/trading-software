"""OC-007: immutable snapshots using the existing option_chain_snapshots table.

The full model (Decimal strings, timestamps, raw response and origin) is retained
in a versioned JSON envelope. Reads constrain both observation and receipt time.
Cadence is scoped per underlying/expiry and survives restarts. Like other stores,
capture runs under the application's single-writer account ownership.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta

import sqlalchemy as sa

from app.core.clock import UTC, Clock, get_clock
from app.db import session as db_session
from app.db.models.market_data import OptionChainSnapshot
from app.fno.chain.maxpain import max_pain
from app.fno.chain.model import aware, chain_adapter, validate_chain
from app.fno.chain.pcr import pcr
from app.marketdata.models import OptionChain


class ChainSnapshotStore:
    """Persist accepted observations; a conflicting duplicate never rewrites history."""

    def __init__(self, *, cadence: timedelta, clock: Clock | None = None) -> None:
        if cadence <= timedelta(0):
            raise ValueError("positive snapshot cadence required")
        self.cadence = cadence
        self.clock = clock or get_clock()

    async def capture(self, chain: OptionChain) -> bool:
        validate_chain(chain)
        observed = chain.observed_at.astimezone(UTC)
        received = aware(self.clock.utcnow()).astimezone(UTC)
        if chain.expiry is None or observed > received:
            raise ValueError("missing expiry or future observation")
        payload = {"schema_version": 1, "chain": chain_adapter.dump_python(chain, mode="json")}
        async with db_session.session_scope() as session:
            identity = (
                OptionChainSnapshot.underlying == chain.underlying,
                OptionChainSnapshot.expiry_date == chain.expiry,
            )
            existing = await session.scalar(
                sa.select(OptionChainSnapshot).where(*identity, OptionChainSnapshot.ts == observed)
            )
            if existing is not None:
                if existing.strikes != payload:
                    raise ValueError("conflicting snapshot; historical overwrite forbidden")
                return False
            latest = await session.scalar(
                sa.select(OptionChainSnapshot)
                .where(*identity)
                .order_by(OptionChainSnapshot.ts.desc())
                .limit(1)
            )
            if latest is not None:
                latest_ts = latest.ts.replace(tzinfo=UTC) if latest.ts.tzinfo is None else latest.ts
                if observed < latest_ts:
                    raise ValueError("out-of-order snapshot")
                if observed - latest_ts < self.cadence:
                    return False
            session.add(
                OptionChainSnapshot(
                    underlying=chain.underlying,
                    expiry_date=chain.expiry,
                    ts=observed,
                    spot_price=chain.spot,
                    atm_strike=chain.atm_strike(),
                    pcr_oi=pcr(chain),
                    pcr_volume=pcr(chain, "volume"),
                    max_pain_strike=max_pain(chain),
                    strikes=payload,
                    data_origin=chain.data_origin,
                    created_at=received,
                    updated_at=received,
                )
            )
        return True

    async def read(
        self,
        underlying: str,
        expiry: date,
        *,
        as_of: datetime,
        start: datetime | None = None,
    ) -> list[OptionChain]:
        cutoff = aware(as_of).astimezone(UTC)
        if start is not None and aware(start) > as_of:
            raise ValueError("reversed snapshot window")
        statement = (
            sa.select(OptionChainSnapshot)
            .where(
                OptionChainSnapshot.underlying == underlying,
                OptionChainSnapshot.expiry_date == expiry,
                OptionChainSnapshot.ts <= cutoff,
                OptionChainSnapshot.created_at <= cutoff,
            )
            .order_by(OptionChainSnapshot.ts)
        )
        if start is not None:
            statement = statement.where(OptionChainSnapshot.ts >= start.astimezone(UTC))
        async with db_session.session_scope() as session:
            rows = (await session.scalars(statement)).all()
        result = []
        for row in rows:
            if row.strikes.get("schema_version") != 1:
                raise ValueError("unsupported chain snapshot schema")
            chain = chain_adapter.validate_python(row.strikes["chain"])
            result.append(validate_chain(replace(chain, strikes=tuple(chain.strikes))))
        return result
