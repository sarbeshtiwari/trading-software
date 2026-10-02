"""Session-aware execution supervisor over persisted proposals and real ticks."""

import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.agents.validation import _utc
from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.calendar import get_trading_calendar
from app.core.clock import IST, get_clock
from app.core.data_origin import DataOrigin
from app.core.entry_windows import entry_window
from app.core.enums import ExitReason, OrderStatus, Product
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.decision import Proposal
from app.db.models.instrument import Instrument
from app.db.models.market_data import Tick
from app.db.models.system import Heartbeat
from app.db.models.trading import Order, Position
from app.emergency.controls import EmergencyControls
from app.execution.hygiene import PaperOrderHygiene
from app.execution.paper import PaperExecution
from app.marketdata.live import LiveMarketDataProvider
from app.marketdata.models import DepthLevel, Quote
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.news.reactions import cycle as news_reaction_cycle
from app.notifications.incidents import PaperIncidents
from app.notifications.recovery import PaperSummaryRecovery
from app.notifications.summary import PaperDailySummary
from app.strategies.monitor import monitor_all
from app.trading.exits import ReferenceExitMonitor
from app.trading.paper_coverage import PaperCoverage
from app.trading.runtime import ReferenceRuntime

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class WorkerLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            raise SafetyError("WORKER_LOCK_ALREADY_HELD")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if not self.path.stat().st_size:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SafetyError("ANOTHER_PAPER_WORKER_OWNS_THIS_HOST") from None
        self.handle = handle

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None


class StoredQuoteSource:
    def __init__(self, clock=None):
        self.clock = clock or get_clock()

    async def __call__(self, instrument):
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(Tick)
                .join(Instrument)
                .where(
                    Instrument.trading_symbol == instrument.trading_symbol,
                    Instrument.exchange == instrument.exchange,
                    Instrument.segment == instrument.segment,
                    Tick.ts <= self.clock.utcnow(),
                    Tick.data_origin == DataOrigin.LIVE,
                )
                .order_by(Tick.ts.desc())
                .limit(1)
            )
        if row is None:
            return None
        return Quote(
            instrument=instrument,
            ltp=row.ltp,
            observed_at=_utc(row.ts),
            data_origin=row.data_origin,
            volume=row.volume,
            bids=(DepthLevel(row.bid_price, row.bid_quantity),)
            if row.bid_price is not None and row.bid_quantity
            else (),
            asks=(DepthLevel(row.ask_price, row.ask_quantity),)
            if row.ask_price is not None and row.ask_quantity
            else (),
        )


class PaperWorker:
    def __init__(self, executor, *, calendar=None, lock_path=None, provider=None):
        self.executor = executor
        self.settings, self.clock = executor.settings, executor.clock
        self.calendar = calendar or get_trading_calendar()
        self.executor.calendar = self.calendar
        self.reference_runtime = (
            ReferenceRuntime(executor, provider, calendar=self.calendar) if provider else None
        )
        self.reference_exits = ReferenceExitMonitor(
            executor, self.reference_runtime.ingestion if self.reference_runtime else None
        )
        self.lock = WorkerLock(lock_path or self.settings.log_dir / "paper-worker.lock")
        self.scheduler = AsyncIOScheduler(timezone=IST)
        self.cycle_lock = asyncio.Lock()
        self.running = False
        self.phase = "STOPPED"
        self.detail = "Worker has not started"
        self.last_cycle = None
        self.failed = False
        self.paper_coverage = PaperCoverage(self)

    async def start(self, *, schedule=True):
        self.lock.acquire()
        try:
            if self.reference_runtime:
                await self.reference_runtime.connect()
            await self.executor.recover()
            await self.executor.verify_protection()
            await EmergencyControls(self.clock).restore()
            async with db_session.session_scope() as session:
                previous = await session.get(Heartbeat, "paper-worker")
                self.failed = bool(previous and previous.detail and previous.detail.get("failed"))
            if self.failed:
                get_trading_gate().block("paper_worker_error", "PAPER_WORKER_REVIEW_REQUIRED")
            get_trading_gate().clear("paper_worker_stopped")
            self.running = True
            if schedule:
                self.scheduler.add_job(
                    self.cycle,
                    "interval",
                    seconds=self.settings.paper_cycle_seconds,
                    max_instances=1,
                    coalesce=True,
                    id="paper-cycle",
                )
                self.scheduler.start()
        except BaseException:
            self.lock.release()
            if self.reference_runtime:
                await self.reference_runtime.close()
            raise

    def session_phase(self):
        return entry_window(self.clock.now(), self.settings, calendar=self.calendar).phase

    async def cycle(self):
        async with self.cycle_lock:
            if not self.running:
                return
            self.phase = self.session_phase()
            gate = get_trading_gate()
            if self.phase != "INTRADAY":
                gate.block("paper_session", self.phase)
            else:
                gate.clear("paper_session")
            failure_code = None
            try:
                await self._work()
                self.detail = await self._cycle_detail()
            except Exception as error:
                self.failed = True
                gate.block("paper_worker_error", "PAPER_WORKER_REVIEW_REQUIRED")
                code = error.message if isinstance(error, SafetyError) else type(error).__name__
                self.detail = f"Cycle failed closed: {code}"
                failure_code = code
            self.last_cycle = self.clock.utcnow()
            try:
                await self.paper_coverage.sample()
                await self._heartbeat()
                if failure_code is not None:
                    incidents = PaperIncidents(self.clock)
                    await incidents.observe("WORKER_FAILURE", active=True)
                    if failure_code in {
                        "STALE_OR_INVALID_QUOTE",
                        "QUOTE_DEPTH_OR_PROVENANCE_UNAVAILABLE",
                        "PAPER_MONITOR_DATA_UNAVAILABLE",
                    }:
                        await incidents.observe("FEED_OUTAGE", active=True)
                await PaperDailySummary(self).publish_if_due(cycle_error=failure_code)
                await PaperSummaryRecovery(self).catch_up()
            except Exception:
                self.failed = True
                gate.block("paper_worker_error", "PAPER_WORKER_HEARTBEAT_FAILED")
                await self._heartbeat()
                raise

    async def _cycle_detail(self):
        if self.phase == "INTRADAY":
            detail = (
                self.reference_runtime.detail
                if self.reference_runtime
                else "Execution supervisor active; reference market provider unavailable"
            )
        else:
            async with db_session.session_scope() as session:
                positions = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Position)
                    .where(
                        Position.mode == TradingMode.PAPER,
                        Position.product == Product.MIS,
                        Position.net_quantity != 0,
                    )
                )
                orders = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Order)
                    .where(
                        Order.mode == TradingMode.PAPER,
                        ~Order.status.in_([status for status in OrderStatus if status.is_terminal]),
                    )
                )
            detail = (
                f"{self.phase}: {positions} open MIS positions; "
                f"{orders} unresolved orders; entries blocked"
            )
        return f"PAPER_WORKER_REVIEW_REQUIRED; {detail}" if self.failed else detail

    async def _heartbeat(self):
        async with db_session.session_scope() as session:
            row = await session.get(Heartbeat, "paper-worker")
            previous = row.detail if row else None
            if row is None:
                row = Heartbeat(id="paper-worker")
                session.add(row)
            row.beat_at = self.last_cycle
            row.process_id = str(os.getpid())
            row.detail = {
                "failed": self.failed,
                "phase": self.phase,
                "detail": self.detail,
                "mode": "PAPER",
                "execution_realism": "SIMULATED",
            }
            if previous is None or any(
                previous.get(key) != row.detail[key] for key in ("phase", "failed")
            ):
                await AuditService(self.clock).append_in_session(
                    session,
                    AuditIdentity(
                        chain_id=new_id("ses"),
                        event_type="PAPER_SESSION_TRANSITION",
                        actor="paper_worker",
                        mode=TradingMode.PAPER,
                    ),
                    {"data_used": {"previous": previous}, "result": row.detail},
                )

    async def _work(self):
        await EmergencyControls(self.clock).restore()
        hygiene_ok = True
        if self.phase in {"EXIT_WINDOW", "EOD_RECONCILIATION", "MARKET_CLOSE"}:
            await self.flatten()
            await self.executor._reconcile()
        else:
            hygiene_ok = await PaperOrderHygiene(self.executor).run()
        await self.executor.monitor_once()
        await self.reference_exits.cycle()
        await monitor_all(clock=self.clock)
        await news_reaction_cycle()
        if not hygiene_ok:
            raise SafetyError("PAPER_ORDER_CANCELLATION_UNRESOLVED")
        if self.phase != "INTRADAY":
            return
        if self.reference_runtime:
            await self.reference_runtime.refresh_quotes()
        if not get_trading_gate().new_entries_allowed:
            return
        if self.reference_runtime:
            await self.reference_runtime.cycle()
        async with db_session.session_scope() as session:
            proposal = await session.scalar(
                sa.select(Proposal)
                .where(
                    Proposal.mode == TradingMode.PAPER,
                    Proposal.status == "RISK_APPROVED",
                    ~sa.exists(sa.select(Order.id).where(Order.proposal_id == Proposal.id)),
                )
                .order_by(Proposal.created_at)
                .limit(1)
            )
        if proposal is not None:
            try:
                await self.executor.submit(proposal.id)
            except SafetyError as error:
                async with db_session.session_scope() as session:
                    row = await session.get(Proposal, proposal.id)
                    row.status = "EXECUTION_BLOCKED"
                    row.rejection_code = error.code
                    await AuditService(self.clock).append_in_session(
                        session,
                        AuditIdentity(
                            chain_id=new_id("wrk"),
                            event_type="EXECUTION_STAND_DOWN",
                            actor="paper_worker",
                            mode=TradingMode.PAPER,
                        ),
                        {"proposal_id": proposal.id, "result": {"reason": error.message}},
                    )

    async def flatten(self, reason=ExitReason.SQUARE_OFF):
        resolved = await PaperOrderHygiene(self.executor).run(all_pending=True)
        first_error = None
        async with db_session.session_scope() as session:
            positions = list(
                (
                    await session.scalars(
                        sa.select(Position).where(
                            Position.mode == TradingMode.PAPER,
                            Position.net_quantity != 0,
                            sa.true()
                            if reason == ExitReason.EMERGENCY
                            else Position.product == Product.MIS,
                        )
                    )
                ).all()
            )
        for position in positions:
            try:
                await self.executor.exit(position.id, reason)
            except Exception as error:
                resolved = False
                first_error = first_error or error
        if first_error is not None:
            raise first_error
        if not resolved:
            raise SafetyError("PAPER_CUTOFF_REQUIRES_RECONCILIATION")

    async def stop(self):
        self.running = False
        if self.scheduler.running:
            self.scheduler.pause()
        async with self.cycle_lock:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=True)
            self.phase = "STOPPED"
            get_trading_gate().block("paper_worker_stopped", "PAPER_WORKER_STOPPED")
            self.lock.release()
            if self.reference_runtime:
                await self.reference_runtime.close()


@dataclass
class WorkerRuntime:
    worker: PaperWorker | None = None
    error: str | None = None


_runtime = WorkerRuntime()


def worker_status():
    _worker, _startup_error = _runtime.worker, _runtime.error
    if _worker is None:
        return {
            "status": "UNAVAILABLE" if _startup_error else "DISABLED",
            "detail": _startup_error or "Execution supervisor is not enabled",
        }
    fresh = _worker.last_cycle is not None and (
        _worker.clock.utcnow() - _worker.last_cycle
        <= timedelta(seconds=_worker.settings.paper_cycle_seconds * 3)
    )
    return {
        "status": ("DEGRADED" if _worker.failed else _worker.phase)
        if fresh and _worker.running
        else "STALE_OR_STOPPED",
        "detail": _worker.detail,
    }


def active_worker():
    worker = _runtime.worker
    return worker if worker is not None and worker.running else None


async def start_worker(settings=None):
    settings = settings or get_settings()
    _runtime.error = None
    if _runtime.worker is not None:
        raise SafetyError("PAPER_WORKER_ALREADY_RUNNING")
    if not settings.paper_worker_enabled:
        return
    try:
        provider = LiveMarketDataProvider(settings) if settings.has_groww_credentials else None
        _runtime.worker = PaperWorker(
            PaperExecution(StoredQuoteSource(), settings=settings), provider=provider
        )
        await _runtime.worker.start()
    except Exception as error:
        _runtime.worker = None
        _runtime.error = f"Execution supervisor unavailable: {type(error).__name__}"
        get_trading_gate().block("paper_worker_startup", "PAPER_WORKER_UNAVAILABLE")


async def stop_worker():
    if _runtime.worker:
        await _runtime.worker.stop()
        _runtime.worker = None
