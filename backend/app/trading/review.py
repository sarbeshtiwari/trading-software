"""Owner-reviewed recovery discards unexecuted decisions; never replays them."""

import sqlalchemy as sa
from sqlalchemy.orm import aliased

from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.enums import HealthStatus, OrderStatus
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.decision import Proposal
from app.db.models.system import Heartbeat
from app.db.models.trading import Order, Position
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.monitoring.healthchecks import get_health_registry
from app.notifications.incidents import PaperIncidents


async def review_worker(worker, *, actor, reason):
    if get_settings().trading_mode != TradingMode.PAPER:
        raise SafetyError("PAPER_MODE_REQUIRED")
    if worker is None or not worker.running or not worker.executor.ready:
        raise SafetyError("RUNNING_RECOVERED_WORKER_REQUIRED")
    async with worker.cycle_lock:
        report = await get_health_registry().run_all()
        if not any(result.critical for result in report.results) or any(
            result.critical and result.status != HealthStatus.PASS for result in report.results
        ):
            raise SafetyError("WORKER_REVIEW_HEALTH_CHECKS_FAILED")
        await worker.executor._reconcile()
        async with db_session.session_scope() as session:
            open_position = await session.scalar(
                sa.select(Position.id)
                .where(
                    Position.mode == TradingMode.PAPER,
                    Position.net_quantity != 0,
                )
                .limit(1)
            )
            pending_order = await session.scalar(
                sa.select(Order.id)
                .where(
                    Order.mode == TradingMode.PAPER,
                    Order.status.not_in(
                        [OrderStatus.EXECUTED, OrderStatus.CANCELLED, OrderStatus.REJECTED]
                    ),
                )
                .limit(1)
            )
            if open_position or pending_order:
                raise SafetyError("WORKER_REVIEW_REQUIRES_FLAT_RECONCILED_ACCOUNT")
            terminal = aliased(AuditEvent)
            claims = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent).where(
                            AuditEvent.mode == TradingMode.PAPER,
                            AuditEvent.event_type == "REFERENCE_CYCLE_CLAIM",
                            ~sa.exists(
                                sa.select(terminal.id).where(
                                    terminal.chain_id == AuditEvent.chain_id,
                                    terminal.event_type.in_(
                                        ["REFERENCE_CYCLE_RESULT", "REFERENCE_STAND_DOWN"]
                                    ),
                                )
                            ),
                        )
                    )
                ).all()
            )
            proposals = list(
                (
                    await session.scalars(
                        sa.select(Proposal).where(
                            Proposal.mode == TradingMode.PAPER,
                            Proposal.status == "RISK_APPROVED",
                            ~sa.exists(sa.select(Order.id).where(Order.proposal_id == Proposal.id)),
                        )
                    )
                ).all()
            )
            for proposal in proposals:
                proposal.status = "EXECUTION_BLOCKED"
                proposal.rejection_code = "OWNER_REVIEW_DISCARDED_UNEXECUTED_DECISION"
            for claim in claims:
                await AuditService(worker.clock).append_in_session(
                    session,
                    AuditIdentity(
                        chain_id=claim.chain_id,
                        event_type="REFERENCE_STAND_DOWN",
                        actor=actor,
                        mode=TradingMode.PAPER,
                    ),
                    {
                        "result": {
                            "reason": reason,
                            "code": "OWNER_REVIEW_DISCARDED_INTERRUPTED_CYCLE",
                        }
                    },
                    expected_count=1,
                )
            heartbeat = await session.get(Heartbeat, "paper-worker", with_for_update=True)
            if heartbeat is None:
                heartbeat = Heartbeat(id="paper-worker")
                session.add(heartbeat)
            heartbeat.beat_at = worker.clock.utcnow()
            heartbeat.detail = {
                "failed": False,
                "phase": worker.phase,
                "detail": "Owner reviewed; other gates remain",
                "mode": "PAPER",
                "execution_realism": "SIMULATED",
            }
            result = {
                "discarded_proposals": [row.id for row in proposals],
                "resolved_claims": [row.chain_id for row in claims],
            }
            await AuditService(worker.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=new_id("rev"),
                    event_type="PAPER_WORKER_REVIEWED",
                    actor=actor,
                    mode=TradingMode.PAPER,
                ),
                {"result": result | {"reason": reason, "health": report.to_dict()}},
            )
            incidents = PaperIncidents(worker.clock)
            await incidents.observe_in_session(session, "WORKER_FAILURE", active=False)
            await incidents.observe_in_session(session, "FEED_OUTAGE", active=False)
        worker.failed = False
        worker.detail = "Owner reviewed; other gates remain"
        get_trading_gate().clear("paper_worker_error")
    return result
