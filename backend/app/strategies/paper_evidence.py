"""Reconstruct eligible PAPER evidence from observed coverage and costed journal audits."""

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.agents.validation import _utc
from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.core.clock import IST
from app.db.models.audit import AuditEvent
from app.db.models.journal import JournalEntry, JournalRevision
from app.modes import TradingMode
from app.portfolio.journal_integrity import journal_verified
from app.portfolio.metrics import performance
from app.strategies.base import StrategySpec
from app.strategies.paper_policy import PaperEvidencePolicy, current_policy
from app.strategies.registry import specification_hash


class SessionEvidence(EvidenceModel):
    session_date: str
    coverage_fraction: Decimal
    observed_seconds: Decimal
    ended_flat: bool
    eligible: bool


class PaperEvidence(EvidenceModel):
    policy: PaperEvidencePolicy | None
    policy_event_id: str | None
    eligible_sessions: int
    eligible_trades: int
    sessions: list[SessionEvidence]
    excluded_trades: dict[str, str]
    net_pnl: Decimal | None
    gross_pnl: Decimal | None
    charges: Decimal | None
    expectancy: Decimal | None
    win_rate: Decimal | None
    threshold_passed: bool
    blockers: tuple[str, ...]
    source_status: str = "EXTERNAL_VERIFICATION_UNAVAILABLE; LIVE_ORIGIN_REQUIRED_FOR_ELIGIBILITY"


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("aware coverage timestamp required")
    return _utc(parsed)


def session_coverage(events, policy, policy_event, row, as_of):
    if not verify_records(events):
        raise ValueError("coverage audit integrity failure")
    seconds, previous, ended = Decimal(0), None, False
    bounds = None
    for event in events:
        raw = event.result
        observed = timestamp(raw["observed_at"])
        start, end = timestamp(raw["session_start"]), timestamp(raw["session_end"])
        if (
            raw["registration_id"] != row.id
            or raw["parameter_hash"] != row.parameter_hash
            or raw["policy_event_id"] != policy_event.id
            or not observed <= _utc(event.occurred_at)
            or (_utc(event.occurred_at) - observed).total_seconds()
            > policy.maximum_sample_gap_seconds
            or end <= start
            or (bounds is not None and bounds != (start, end))
            or observed < _utc(policy_event.occurred_at)
        ):
            raise ValueError("coverage lineage mismatch")
        bounds = start, end
        if observed > as_of or _utc(event.occurred_at) > as_of:
            continue
        healthy = raw["healthy"] and raw["data_origin"] == "LIVE"
        if previous is not None:
            earlier, earlier_raw, earlier_healthy = previous
            gap = (observed - earlier).total_seconds()
            if gap < 0:
                raise ValueError("coverage chronology mismatch")
            if (
                healthy
                and earlier_healthy
                and raw["owner"] == earlier_raw["owner"]
                and 0 < gap <= policy.maximum_sample_gap_seconds
            ):
                covered = (min(observed, end) - max(earlier, start)).total_seconds()
                seconds += Decimal(str(max(0, covered)))
        ended = ended or bool(
            healthy and raw["ended_flat"] and end <= observed <= end + timedelta(minutes=10)
        )
        previous = observed, raw, healthy
    if bounds is None:
        raise ValueError("coverage unavailable")
    start, end = bounds
    fraction = seconds / Decimal(str((end - start).total_seconds()))
    if fraction > 1:
        raise ValueError("overlapping coverage")
    return SessionEvidence(
        session_date=start.astimezone(IST).date().isoformat(),
        coverage_fraction=fraction,
        observed_seconds=seconds,
        ended_flat=ended,
        eligible=ended and as_of >= end and fraction >= policy.minimum_session_coverage_fraction,
    )


async def collect_paper_evidence(session, row, as_of):
    policy, policy_event = await current_policy(session, row)
    result = PaperEvidence(
        policy=policy,
        policy_event_id=policy_event.id if policy_event else None,
        eligible_sessions=0,
        eligible_trades=0,
        sessions=[],
        excluded_trades={},
        net_pnl=None,
        gross_pnl=None,
        charges=None,
        expectancy=None,
        win_rate=None,
        threshold_passed=False,
        blockers=("PAPER_POLICY_UNAVAILABLE",),
    )
    if policy is None:
        return result
    events = list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.event_type == "PAPER_COVERAGE_SAMPLE",
                    AuditEvent.result["policy_event_id"].as_string() == policy_event.id,
                )
                .order_by(AuditEvent.chain_id, AuditEvent.sequence)
                .limit(100001)
            )
        ).all()
    )
    if len(events) > 100000:
        raise ValueError("coverage evidence review bound exceeded")
    chains = defaultdict(list)
    for event in events:
        chains[event.chain_id].append(event)
    sessions = [
        session_coverage(events, policy, policy_event, row, as_of) for events in chains.values()
    ]
    eligible_days = {item.session_date for item in sessions if item.eligible}
    journals = list(
        (
            await session.scalars(
                sa.select(JournalEntry)
                .where(
                    JournalEntry.strategy_id == row.strategy_id,
                    JournalEntry.strategy_version == row.version,
                    JournalEntry.kind == "TRADE",
                )
                .order_by(JournalEntry.opened_at)
                .limit(10001)
            )
        ).all()
    )
    if len(journals) > 10000:
        raise ValueError("journal evidence review bound exceeded")
    pnls, holding, gross, charges = [], [], [], []
    excluded = {}
    for journal in journals:
        reason = await trade_blocker(
            session, journal, row, policy_event, eligible_days, as_of=as_of
        )
        if reason:
            excluded[journal.id] = reason
            continue
        pnls.append(journal.net_pnl)
        gross.append(journal.gross_pnl)
        charges.append(journal.charges)
        holding.append(
            Decimal(str((_utc(journal.closed_at) - _utc(journal.opened_at)).total_seconds()))
        )
    metrics = performance([], pnls, holding)
    blockers = tuple(
        reason
        for reason, blocked in (
            ("INSUFFICIENT_PAPER_SESSIONS", len(eligible_days) < policy.minimum_sessions),
            ("INSUFFICIENT_PAPER_TRADES", len(pnls) < policy.minimum_trades),
        )
        if blocked
    )
    return PaperEvidence(
        policy=policy,
        policy_event_id=policy_event.id,
        eligible_sessions=len(eligible_days),
        eligible_trades=len(pnls),
        sessions=sessions,
        excluded_trades=excluded,
        net_pnl=sum(pnls, Decimal(0)) if pnls else None,
        gross_pnl=sum(gross, Decimal(0)) if gross else None,
        charges=sum(charges, Decimal(0)) if charges else None,
        expectancy=metrics["expectancy"],
        win_rate=metrics["win_rate"],
        blockers=blockers,
        threshold_passed=not blockers,
    )


async def trade_blocker(session, journal, row, policy_event, eligible_days, *, as_of):
    if (
        journal.version != 1
        or await session.scalar(
            sa.select(JournalRevision.entry_id)
            .where(JournalRevision.root_id == journal.id)
            .limit(1)
        )
        or await session.scalar(
            sa.select(AuditEvent.id)
            .where(AuditEvent.chain_id == journal.id, AuditEvent.event_type == "JOURNAL_CORRECTED")
            .limit(1)
        )
    ):
        return "JOURNAL_CORRECTION_REQUIRES_REVIEW"
    if journal.mode != TradingMode.PAPER or journal.superseded_by is not None:
        return "NOT_CURRENT_PAPER_TRADE"
    if (
        not journal.opened_at
        or not journal.closed_at
        or not (
            _utc(policy_event.occurred_at)
            <= _utc(journal.opened_at)
            <= _utc(journal.closed_at)
            <= as_of
        )
    ):
        return "OUTSIDE_POLICY_OR_FUTURE_TRADE"
    if not await journal_verified(session, journal.id):
        return "JOURNAL_AUDIT_UNAVAILABLE_OR_CHANGED"
    context = (journal.indicator_snapshot or {}).get("decision_context", {})
    try:
        market = context["market"]
        checks = (
            (
                "FUTURE_MARKET_EVIDENCE",
                not timestamp(market["observed_at"])
                <= timestamp(market["available_at"])
                <= _utc(journal.opened_at),
            ),
            (
                "COST_ESTIMATE_UNAVAILABLE",
                journal.indicator_snapshot.get("cost_status") != "ESTIMATED",
            ),
            (
                "STRATEGY_PARAMETER_MISMATCH",
                specification_hash(StrategySpec.model_validate(context["strategy"]))
                != row.parameter_hash,
            ),
            ("NONLIVE_MARKET_ORIGIN", context["market"]["data_origin"] != "LIVE"),
            (
                "SESSION_COVERAGE_NOT_ELIGIBLE",
                _utc(journal.opened_at).astimezone(IST).date().isoformat() not in eligible_days,
            ),
            (
                "NET_COST_ACCOUNTING_UNAVAILABLE",
                any(
                    value is None for value in (journal.net_pnl, journal.gross_pnl, journal.charges)
                )
                or journal.gross_pnl - journal.charges != journal.net_pnl,
            ),
        )
        return next((reason for reason, blocked in checks if blocked), None)
    except (KeyError, ValueError, TypeError):
        return "DECISION_CONTEXT_UNAVAILABLE"
