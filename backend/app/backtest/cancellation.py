"""Audited local-task cancellation; unknown process ownership is never inferred."""

import asyncio

from app.audit.service import AuditIdentity, AuditService
from app.db import session as db_session
from app.db.models.historical_jobs import HistoricalJob
from app.modes import TradingMode


def cancellation_code(error, *, owner_requested=False):
    return (
        "OWNER_CANCELLED"
        if owner_requested or error.args == ("OWNER_CANCELLED",)
        else "CONTROLLER_STOPPED"
    )


async def cancel_owned(manager, identifier, model, prefix, *, actor, reason):
    if not actor.strip() or not 10 <= len(reason.strip()) <= 500:
        raise ValueError("cancellation actor and reason required")
    async with manager.lock:
        if manager.stopping:
            raise ValueError("controller stopping")
        operation = manager.cancellations.get(identifier)
        if operation is None:
            async with db_session.session_scope() as session:
                row = await session.get(model, identifier, with_for_update=True)
                if row is None:
                    raise ValueError("unknown job")
                if row.slot is None:
                    return row.status
                task = manager.tasks.get(identifier)
                if row.owner_id != manager.owner_id or task is None or task.done():
                    raise ValueError("job ownership unverified")
                if model is HistoricalJob and row.actor.startswith("walkforward-"):
                    raise ValueError("cancel the owning experiment")
                await AuditService(manager.clock).append_in_session(
                    session,
                    AuditIdentity(
                        chain_id=f"{prefix}:{identifier}",
                        event_type="HISTORICAL_CANCEL_REQUESTED",
                        actor=actor,
                        mode=TradingMode.PAPER,
                    ),
                    {"result": {"reason": reason, "job_id": identifier}},
                )
            task.cancel("OWNER_CANCELLED")
            operation = asyncio.create_task(_finish(manager, identifier, task, model))
            manager.cancellations[identifier] = operation
            operation.add_done_callback(lambda completed: _finished(manager, identifier, completed))
    return await asyncio.shield(operation)


async def _finish(manager, identifier, task, model):
    await asyncio.gather(task, return_exceptions=True)
    await manager.finish_cancelled(identifier, task, error="OWNER_CANCELLED")
    async with db_session.session_scope() as session:
        row = await session.get(model, identifier)
        if row is None or row.slot is not None:
            raise ValueError("cancellation cleanup incomplete; reservation retained")
        return row.status


def _finished(manager, identifier, task):
    manager.cancellations.pop(identifier, None)
    if not task.cancelled() and task.exception() is not None:
        manager.last_error = type(task.exception()).__name__
