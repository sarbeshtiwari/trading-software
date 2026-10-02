"""Reserve attempts durably before contacting the external token endpoint."""

from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.clock import get_clock
from app.core.errors import RateLimitError
from app.db import session as db_session
from app.db.models.broker_auth_budget import BrokerAuthBudget


class TokenAttemptBudget:
    def __init__(self, clock=None):
        self.clock = clock or get_clock()

    async def reserve(self, limit):
        if not isinstance(limit, int) or not 1 <= limit <= 150:
            raise ValueError("Invalid token attempt limit")
        async with db_session.session_scope() as session:
            dialect = session.bind.dialect.name
            insert = postgres_insert if dialect == "postgresql" else sqlite_insert
            await session.execute(
                insert(BrokerAuthBudget)
                .values(id="groww", version=0, state={"attempts": [], "last_seen": None})
                .on_conflict_do_nothing(index_elements=["id"])
            )
            row = await session.scalar(
                sa.select(BrokerAuthBudget).where(BrokerAuthBudget.id == "groww").with_for_update()
            )
            now = self.clock.utcnow()
            last_seen = row.state["last_seen"]
            if last_seen is not None and now < datetime.fromisoformat(last_seen):
                raise RateLimitError("Token budget clock moved backwards; refusing authentication")
            attempts = [datetime.fromisoformat(value) for value in row.state["attempts"]]
            if any(value.tzinfo is None or value > now for value in attempts):
                raise RateLimitError("Invalid token budget history; refusing authentication")
            retained = [value for value in attempts if value > now - timedelta(hours=24)]
            if len(retained) >= limit:
                raise RateLimitError("Shared rolling 24-hour token attempt budget exhausted")
            updated = await session.execute(
                sa.update(BrokerAuthBudget)
                .where(BrokerAuthBudget.id == "groww", BrokerAuthBudget.version == row.version)
                .values(
                    version=row.version + 1,
                    state={
                        "last_seen": now.isoformat(),
                        "attempts": [value.isoformat() for value in retained] + [now.isoformat()],
                    },
                )
            )
            if updated.rowcount != 1:
                raise RateLimitError("Concurrent token reservation; refusing authentication")
