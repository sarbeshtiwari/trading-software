"""Server-derived registration/evidence linkage; never grants LIVE permission."""

from typing import Literal

import sqlalchemy as sa

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.backtest.catalog import catalog_digest, load_catalog
from app.backtest.wf_integrity import report_integrity
from app.core.clock import get_clock
from app.db.models.audit import AuditEvent
from app.db.models.backtest import BacktestResult, BacktestRun
from app.modes import TradingMode
from app.strategies.approval import OOSValidationPolicy, evaluate_oos
from app.strategies.evidence import BindingSummary, StrategyBinding
from app.strategies.paper_evidence import PaperEvidence, collect_paper_evidence, timestamp
from app.strategies.paper_policy import evidence_chain


class StrategyEvidenceReview(EvidenceModel):
    strategy_id: str
    version: str
    parameter_hash: str
    paper: PaperEvidence
    report_states: dict[str, str]
    blockers: tuple[str, ...]
    live_approved: Literal[False] = False
    audit_event_id: str | None = None


async def report_state(session, row, identifier, *, walkforward):
    if identifier is None:
        return "UNAVAILABLE", None
    run = await session.get(BacktestRun, identifier)
    if (
        run is None
        or run.status != "COMPLETED"
        or run.strategy_id != row.strategy_id
        or run.strategy_version != row.version
    ):
        return "UNAVAILABLE_OR_STRATEGY_MISMATCH", None
    if (
        run.finished_at is None
        or _utc(run.finished_at) > get_clock().utcnow()
        or (
            not run.parameters.get("end_at")
            or timestamp(run.parameters["end_at"]) > get_clock().utcnow()
        )
    ):
        return "FUTURE_OR_UNDATED_REPORT", None
    reason = (
        await walkforward_blocker(session, row, run)
        if walkforward
        else await backtest_blocker(session, row, run)
    )
    if reason:
        return reason, None
    return "AUDIT_BOUND_PARAMETERS_MATCH; EXTERNAL_VALIDATION_UNAVAILABLE", catalog_digest(
        await load_catalog(session, identifier)
    )


async def walkforward_blocker(session, row, run):
    identifier = run.id
    if await report_integrity(session, run) != "AUDIT_BOUND":
        return "REPORT_INTEGRITY_UNAVAILABLE"
    aggregate = await session.scalar(
        sa.select(BacktestResult).where(
            BacktestResult.run_id == identifier, BacktestResult.window_kind == "OOS_AGGREGATE"
        )
    )
    summary = (
        (aggregate.window_parameters or {}).get("diagnostics", {}).get("binding_summary")
        if aggregate
        else None
    )
    if summary is None:
        return "STRATEGY_BINDING_UNAVAILABLE"
    binding = BindingSummary.model_validate(summary)
    if binding.state != "FIXED" or binding.parameter_hashes != (row.parameter_hash,):
        return "MIXED_OR_MISMATCHED_PARAMETERS"
    raw_policy = run.parameters.get("validation")
    checked = evaluate_oos(
        OOSValidationPolicy.model_validate(raw_policy) if raw_policy else None,
        aggregate,
        integrity="AUDIT_BOUND",
    )
    if checked.status != "PASSED":
        return f"OOS_THRESHOLDS_{checked.status}"
    return None


async def backtest_blocker(session, row, run):
    identifier = run.id
    if run.kind == "WALKFORWARD":
        return "BACKTEST_REQUIRED"
    records = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.chain_id == f"history:{identifier}")
                .order_by(AuditEvent.sequence)
            )
        ).all()
    )
    if (
        not records
        or not verify_records(records)
        or records[-1].event_type != "HISTORICAL_RUN_FINISHED"
        or records[-1].result.get("catalog_sha256")
        != catalog_digest(await load_catalog(session, identifier))
    ):
        return "REPORT_INTEGRITY_UNAVAILABLE"
    raw = run.assumptions.get("strategy_binding")
    if raw is None or StrategyBinding.model_validate(raw).parameter_hash != row.parameter_hash:
        return "STRATEGY_BINDING_UNAVAILABLE_OR_MISMATCHED"
    return None


async def review_strategy(
    session, row, *, backtest_id=None, walkforward_id=None, actor=None, reason=None
):
    clock = get_clock()
    paper = await collect_paper_evidence(session, row, clock.utcnow())
    backtest, backtest_digest = await report_state(session, row, backtest_id, walkforward=False)
    walkforward, walkforward_digest = await report_state(
        session, row, walkforward_id, walkforward=True
    )
    blockers = list(paper.blockers)
    if backtest_digest is None:
        blockers.append("BACKTEST_EVIDENCE_REQUIRED")
    if walkforward_digest is None:
        blockers.append("FIXED_PARAMETER_OOS_EVIDENCE_REQUIRED")
    blockers.extend(("EXTERNAL_DATA_VERIFICATION_REQUIRED", "LIVE_ARMING_UNAVAILABLE"))
    result = StrategyEvidenceReview(
        strategy_id=row.strategy_id,
        version=row.version,
        parameter_hash=row.parameter_hash,
        paper=paper,
        report_states={"backtest": backtest, "walkforward": walkforward},
        blockers=tuple(blockers),
    )
    if actor is not None:
        event = await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=evidence_chain("sev", row.id),
                event_type="STRATEGY_EVIDENCE_REVIEWED",
                actor=actor,
                mode=TradingMode.PAPER,
            ),
            {
                "strategy_id": row.strategy_id,
                "result": {
                    "registration_id": row.id,
                    "reason": reason,
                    "review": result.model_dump(mode="json"),
                    "backtest_id": backtest_id,
                    "walkforward_id": walkforward_id,
                    "backtest_digest": backtest_digest,
                    "walkforward_digest": walkforward_digest,
                },
            },
        )
        result = result.model_copy(update={"audit_event_id": event.id})
        row.live_approved = row.enabled_live = False
        row.approved_by = row.approved_at = row.approval_reason = None
        row.evidence_summary = result.model_dump(mode="json")
        row.backtest_run_id = backtest_id if backtest_digest else None
        row.walkforward_run_id = walkforward_id if walkforward_digest else None
        row.paper_sessions, row.paper_trades = paper.eligible_sessions, paper.eligible_trades
    return result
