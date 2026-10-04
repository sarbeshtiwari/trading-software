"""Session timeout/cancellation must roll back writes and leave fresh sessions usable."""

import asyncio

import pytest

from app.config import get_settings
from app.db import session as db_session
from app.db.models.system import Heartbeat


@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_transaction_rolls_back(db_engine, fake_clock, monkeypatch, cancel):
    monkeypatch.setattr(get_settings(), "database_session_timeout_seconds", 0.5)
    flushed = asyncio.Event()

    async def interrupted_write():
        async with db_session.session_scope() as session:
            session.add(Heartbeat(id="deadline-test", beat_at=fake_clock.utcnow(), detail={}))
            await session.flush()
            flushed.set()
            await asyncio.Event().wait()

    operation = asyncio.create_task(interrupted_write())
    try:
        await asyncio.wait_for(flushed.wait(), timeout=5)
        if cancel:
            operation.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
            await operation
        async with db_session.session_scope() as session:
            assert await session.get(Heartbeat, "deadline-test") is None
            session.add(Heartbeat(id="after-deadline", beat_at=fake_clock.utcnow(), detail={}))
        async with db_session.session_scope() as session:
            assert await session.get(Heartbeat, "after-deadline") is not None
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
