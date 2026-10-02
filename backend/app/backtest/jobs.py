"""Bounded owner-requested jobs around the isolated runner and report publisher."""

import asyncio
import json
import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.service import AuditIdentity, AuditService
from app.backtest.cancellation import cancellation_code
from app.backtest.launcher import launch_async
from app.backtest.plans import load_registry, read_plan
from app.backtest.publication import database_name, publish_run
from app.config import validate_historical_settings
from app.core.clock import get_clock
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.backtest import BacktestRun
from app.db.models.historical_jobs import HistoricalJob
from app.marketdata.recordings import write_recording
from app.modes import TradingMode


class HistoricalJobs:
    def __init__(self, settings):
        self.settings = settings
        self.clock = get_clock()
        self.owner_id = new_id("howner")
        self.tasks = {}
        self.cancellations = {}
        self.stopping = False
        self.last_error = None
        self.lock = asyncio.Lock()

    def plans(self):
        return load_registry(self.settings.historical_plans_file)

    async def enqueue(self, identifier, *, actor, reason):
        async with self.lock:
            return await self._enqueue(identifier, actor=actor, reason=reason)

    async def _enqueue(self, identifier, *, actor, reason):
        if self.stopping or self.settings.trading_mode != TradingMode.PAPER:
            raise ValueError("historical jobs unavailable")
        async with db_session.session_scope() as session:
            if await session.get(HistoricalJob, identifier) is not None:
                return identifier
        plan, manifest, recording, configuration = await asyncio.to_thread(
            read_plan, self.settings.historical_plans_file, identifier
        )
        return await self._reserve(
            plan, manifest, recording, configuration, actor=actor, reason=reason
        )

    async def enqueue_prepared(self, inputs, *, actor, reason):
        async with self.lock:
            if self.stopping or self.settings.trading_mode != TradingMode.PAPER:
                raise ValueError("historical jobs unavailable")
            async with db_session.session_scope() as session:
                if await session.get(HistoricalJob, inputs[0].id) is not None:
                    raise ValueError("experiment child identity already used")
            return await self._reserve(*inputs, actor=actor, reason=reason)

    async def _reserve(self, plan, manifest, recording, configuration, *, actor, reason):
        identifier = plan.id
        settings = validate_historical_settings(configuration)
        source = create_async_engine(settings.database_url)
        try:
            if (
                database_name(source) != f"ats_history_{identifier}"
                or database_name(db_session.get_engine()).startswith("ats_history_")
                or settings.starting_capital != manifest.risk.capital
            ):
                raise ValueError("historical job database or capital mismatch")
        finally:
            await source.dispose()
        directory = tempfile.TemporaryDirectory(prefix="ats-job-inputs-")
        root = Path(directory.name)
        try:
            (root / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
            (root / "settings.json").write_text(json.dumps(configuration), encoding="utf-8")
            write_recording(root / "recording.json", recording)
            async with db_session.session_scope() as session:
                session.add(
                    HistoricalJob(
                        id=identifier,
                        status="QUEUED",
                        slot="historical",
                        owner_id=self.owner_id,
                        actor=actor,
                        recording_sha256=manifest.recording_sha256,
                        updated_at=self.clock.utcnow(),
                        progress_pct=0,
                        published=False,
                    )
                )
                await AuditService(self.clock).append_in_session(
                    session,
                    AuditIdentity(
                        chain_id=f"hjob:{identifier}",
                        event_type="HISTORICAL_JOB_REQUESTED",
                        actor=actor,
                        mode=TradingMode.PAPER,
                    ),
                    {
                        "result": {
                            "plan_id": identifier,
                            "reason": reason,
                            "recording_sha256": manifest.recording_sha256,
                        }
                    },
                    expected_count=0,
                )
            task = asyncio.create_task(self._execute(plan, manifest, configuration, directory))
            self.tasks[identifier] = task
            task.add_done_callback(lambda completed: self._finished(identifier, completed))
            task.add_done_callback(lambda completed: directory.cleanup())
        except BaseException:
            directory.cleanup()
            raise
        return identifier

    def _finished(self, identifier, task):
        self.tasks.pop(identifier, None)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.last_error = type(error).__name__

    async def _update(
        self, identifier, status, *, progress=None, error=None, terminal=False, published=False
    ):
        async with self.lock, db_session.session_scope() as session:
            row = await session.get(HistoricalJob, identifier, with_for_update=True)
            if row is None or row.owner_id != self.owner_id:
                raise ValueError("historical job ownership mismatch")
            changed = row.status != status
            row.status, row.error_code, row.updated_at = status, error, self.clock.utcnow()
            if progress is not None:
                row.progress_pct = progress
            row.published = published
            if terminal:
                row.slot = None
            if changed:
                await AuditService(self.clock).append_in_session(
                    session,
                    AuditIdentity(
                        chain_id=f"hjob:{identifier}",
                        event_type="HISTORICAL_JOB_STATE",
                        actor="historical-job-controller",
                        mode=TradingMode.PAPER,
                    ),
                    {"result": {"status": status, "error_code": error, "published": published}},
                )

    async def _progress(self, source, manifest):
        async with async_sessionmaker(source)() as session:
            row = await session.get(BacktestRun, manifest.run_id)
            if row is not None:
                if row.parameters.get("recording_sha256") != manifest.recording_sha256:
                    raise ValueError("historical job source changed")
                await self._update(manifest.run_id, "RUNNING", progress=row.progress_pct)

    async def _execute(self, plan, manifest, configuration, directory):
        source = create_async_engine(configuration["database_url"])
        execution = None
        try:
            await self._update(plan.id, "RUNNING")
            root = Path(directory.name)
            execution = asyncio.create_task(
                launch_async(
                    root / "manifest.json",
                    root / "recording.json",
                    root / "settings.json",
                    timeout_seconds=plan.timeout_seconds,
                )
            )
            while True:
                done, _ = await asyncio.wait({execution}, timeout=1)
                if done:
                    break
                await self._progress(source, manifest)
            outcome = await execution
            report = json.loads(outcome.stdout)
            expected = {0: "COMPLETED", 1: "FAILED", 3: "INCOMPLETE"}
            if (
                outcome.returncode not in expected
                or report.get("simulated") is not True
                or report.get("run_id") != plan.id
                or report.get("status") != expected[outcome.returncode]
            ):
                raise ValueError("historical child failed or returned invalid report")
            await self._update(plan.id, "PUBLISHING")
            await publish_run(
                source,
                plan.id,
                manifest.recording_sha256,
                actor="historical-job-controller",
                clock=self.clock,
            )
            async with async_sessionmaker(source)() as session:
                completed = await session.get(BacktestRun, plan.id)
                progress = completed.progress_pct
            await self._update(
                plan.id, report["status"], progress=progress, terminal=True, published=True
            )
        except asyncio.CancelledError as error:
            if execution is not None:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            await self._update(
                plan.id, "INTERRUPTED", error=cancellation_code(error), terminal=True
            )
            raise
        except Exception as error:
            if execution is not None and not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            await self._update(plan.id, "FAILED", error=type(error).__name__, terminal=True)
        finally:
            await source.dispose()
            directory.cleanup()

    async def stop(self):
        async with self.lock:
            self.stopping = True
        await asyncio.gather(*tuple(self.cancellations.values()), return_exceptions=True)
        async with self.lock:
            owned = tuple(self.tasks.items())
        tasks = tuple(task for _, task in owned)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for identifier, task in owned:
            await self.finish_cancelled(identifier, task)

    async def finish_cancelled(self, identifier, task, *, error="CONTROLLER_STOPPED"):
        if task.cancelled():
            async with db_session.session_scope() as session:
                row = await session.get(HistoricalJob, identifier)
                pending = (
                    row is not None and row.owner_id == self.owner_id and row.status == "QUEUED"
                )
            if pending:
                await self._update(identifier, "INTERRUPTED", error=error, terminal=True)
