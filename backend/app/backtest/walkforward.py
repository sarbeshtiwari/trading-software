"""Audited training-only selection followed by isolated OOS jobs through the shared worker."""

import asyncio
from decimal import Decimal

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.backtest.bootstrap import HistoricalManifest
from app.backtest.cancellation import cancellation_code
from app.backtest.catalog import catalog_digest, load_catalog
from app.backtest.plans import load_experiments, read_plan
from app.backtest.reproducibility import fingerprints
from app.backtest.universe import SURVIVORSHIP_NOTE, disclose_universe
from app.backtest.wf_inputs import TrainingScore, freeze_experiment, input_digest, select_candidate
from app.backtest.wf_report import append_oos_window, finish_oos_report
from app.core.clock import IST
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.backtest import BacktestRun
from app.db.models.historical_jobs import HistoricalJob
from app.db.models.walkforward import WalkForwardJob
from app.modes import TradingMode


class WalkForwardJobs:
    def __init__(self, historical_jobs):
        self.jobs = historical_jobs
        self.clock = historical_jobs.clock
        self.owner_id = new_id("wfowner")
        self.tasks = {}
        self.cancellations = {}
        self.lock = asyncio.Lock()
        self.stopping = False
        self.last_error = None

    def plans(self):
        return load_experiments(self.jobs.settings.historical_plans_file)

    async def audit(self, session, identifier, event, actor, result, *, expected_count=None):
        await AuditService(self.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=f"wf:{identifier}", event_type=event, actor=actor, mode=TradingMode.PAPER
            ),
            {"result": result},
            expected_count=expected_count,
        )

    async def enqueue(self, identifier, *, actor, reason):
        async with self.lock:
            if self.stopping or self.jobs.settings.trading_mode != TradingMode.PAPER:
                raise ValueError("walk-forward unavailable")
            async with db_session.session_scope() as session:
                if await session.get(WalkForwardJob, identifier) is not None:
                    return identifier
            plan = next((item for item in self.plans() if item.id == identifier), None)
            if plan is None:
                raise ValueError("unknown experiment")
            frozen = await asyncio.to_thread(
                freeze_experiment, self.jobs.settings.historical_plans_file, plan
            )
            first = next(iter(frozen.values()))["manifest"]
            async with db_session.session_scope() as session:
                if await session.scalar(
                    sa.select(HistoricalJob.id).where(HistoricalJob.id.in_(frozen)).limit(1)
                ) or await session.scalar(
                    sa.select(BacktestRun.id).where(BacktestRun.id.in_(frozen)).limit(1)
                ):
                    raise ValueError("experiment child identities must be unused, including OOS")
                session.add(
                    WalkForwardJob(
                        id=identifier,
                        status="QUEUED",
                        slot="walkforward",
                        owner_id=self.owner_id,
                        actor=actor,
                        specification=plan.model_dump(mode="json"),
                        frozen_inputs=frozen,
                        progress_pct=0,
                        updated_at=self.clock.utcnow(),
                    )
                )
                session.add(
                    BacktestRun(
                        id=identifier,
                        kind="WALKFORWARD",
                        strategy_id=HistoricalManifest.model_validate(first).strategy_id,
                        strategy_version="1",
                        parameters=plan.model_dump(mode="json"),
                        seed=first["fill_config"]["seed"],
                        start_date=plan.start_at.astimezone(IST).date(),
                        end_date=plan.end_at.astimezone(IST).date(),
                        interval_minutes=1,
                        universe=[item["trading_symbol"] for item in first["instruments"]],
                        initial_capital=Decimal(first["risk"]["capital"]),
                        risk_config_snapshot=first["risk"],
                        assumptions={
                            "account_model": "INDEPENDENT_WINDOW_ACCOUNTS",
                            "selection": "TRAINING_NET_RETURN_ONLY",
                            "live_approval": False,
                            "universe_disclosure": disclose_universe(
                                [value["manifest"] for value in frozen.values()]
                            ),
                        },
                        combinations_tested=sum(len(group) for group in plan.candidates),
                        status="QUEUED",
                        simulated=True,
                        data_window_warning=(
                            "Statistical adequacy and external validation unverified"
                        ),
                        survivorship_note=SURVIVORSHIP_NOTE,
                    )
                )
                await self.audit(
                    session,
                    identifier,
                    "WALKFORWARD_REQUESTED",
                    actor,
                    {
                        "reason": reason,
                        "specification": plan.model_dump(mode="json"),
                        "frozen_input_hashes": {
                            key: value["sha256"] for key, value in frozen.items()
                        },
                    },
                    expected_count=0,
                )
            task = asyncio.create_task(self._execute(plan, frozen))
            self.tasks[identifier] = task
            task.add_done_callback(lambda completed: self._finished(identifier, completed))
            return identifier

    def _finished(self, identifier, task):
        self.tasks.pop(identifier, None)
        if not task.cancelled() and task.exception() is not None:
            self.last_error = type(task.exception()).__name__

    async def _state(self, identifier, status, *, error=None, progress=None, terminal=False):
        async with self.lock, db_session.session_scope() as session:
            job = await session.get(WalkForwardJob, identifier, with_for_update=True)
            if job is None or job.owner_id != self.owner_id:
                raise ValueError("experiment ownership mismatch")
            run = await session.get(BacktestRun, identifier)
            job.status = run.status = status
            job.error_code = run.error_detail = error
            job.updated_at = self.clock.utcnow()
            if progress is not None:
                job.progress_pct = run.progress_pct = progress
            if status == "RUNNING" and run.started_at is None:
                run.started_at = self.clock.utcnow()
            if terminal:
                job.slot = None
                run.finished_at = self.clock.utcnow()
            await self.audit(
                session,
                identifier,
                "WALKFORWARD_STATE",
                "walkforward-controller",
                {"status": status, "error_code": error, "progress_pct": str(job.progress_pct)},
            )

    async def _child(self, identifier, frozen, actor):
        inputs = await asyncio.to_thread(
            read_plan, self.jobs.settings.historical_plans_file, identifier
        )
        if input_digest(inputs) != frozen[identifier]["sha256"]:
            raise ValueError("frozen experiment inputs changed")
        reservation = asyncio.create_task(
            self.jobs.enqueue_prepared(
                inputs, actor=actor, reason="Frozen chronological walk-forward child"
            )
        )
        try:
            await asyncio.shield(reservation)
        except asyncio.CancelledError as error:
            await asyncio.gather(reservation, return_exceptions=True)
            child = self.jobs.tasks.get(identifier)
            if child is not None:
                child.cancel(cancellation_code(error))
                await asyncio.gather(child, return_exceptions=True)
                await self.jobs.finish_cancelled(identifier, child, error=cancellation_code(error))
            raise
        task = self.jobs.tasks[identifier]
        try:
            await task
        except asyncio.CancelledError as error:
            await self.jobs.finish_cancelled(identifier, task, error=cancellation_code(error))
            raise
        async with db_session.session_scope() as session:
            job = await session.get(HistoricalJob, identifier)
            if job.status != "COMPLETED" or not job.published:
                raise ValueError("incomplete or failed experiment child")
            catalog = await load_catalog(session, identifier)
            if catalog["run"][0]["parameters"] != frozen[identifier]["manifest"]:
                raise ValueError("child configuration differs from frozen manifest")
        audit = AuditService(self.clock)
        chain = await audit.chain(f"history:{identifier}")
        if not await audit.verify(f"history:{identifier}") or chain[-1].result.get(
            "catalog_sha256"
        ) != catalog_digest(catalog):
            raise ValueError("child report integrity unavailable")
        return catalog

    async def _execute(self, plan, frozen):
        try:
            await self._state(plan.id, "RUNNING")
            for index, (window, group) in enumerate(
                zip(plan.windows(), plan.candidates, strict=True)
            ):
                scores = []
                for pair in group:
                    catalog = await self._child(pair.train_plan, frozen, "walkforward-training")
                    result = catalog["results"][0]
                    if result["net_pnl"] is None or result["total_return"] is None:
                        raise ValueError("training net metrics unavailable")
                    scores.append(
                        TrainingScore(
                            candidate=pair.name,
                            train_run_id=pair.train_plan,
                            net_return=result["total_return"],
                            trade_count=result["trade_count"],
                            outcome_sha256=fingerprints(catalog)["outcomes_sha256"],
                        )
                    )
                selected = select_candidate(tuple(scores), plan.minimum_training_trades)
                pair = next(item for item in group if item.name == selected.candidate)
                async with self.lock, db_session.session_scope() as session:
                    await self.audit(
                        session,
                        plan.id,
                        "WALKFORWARD_SELECTION_FROZEN",
                        "walkforward-controller",
                        {
                            "window_index": index,
                            "window": window.model_dump(mode="json"),
                            "scores": [score.model_dump(mode="json") for score in scores],
                            "selected": selected.model_dump(mode="json"),
                            "test_plan": pair.test_plan,
                            "test_input_sha256": frozen[pair.test_plan]["sha256"],
                        },
                    )
                tested = await self._child(pair.test_plan, frozen, "walkforward-out-of-sample")
                async with self.lock, db_session.session_scope() as session:
                    await append_oos_window(
                        session,
                        plan.id,
                        index,
                        window,
                        selected,
                        catalog=tested,
                        parameters=frozen[pair.test_plan]["manifest"],
                        degradation=plan.degradation,
                    )
                    await self.audit(
                        session,
                        plan.id,
                        "WALKFORWARD_OOS_RECORDED",
                        "walkforward-controller",
                        {
                            "window_index": index,
                            "source_run_id": pair.test_plan,
                            "outcomes_sha256": fingerprints(tested)["outcomes_sha256"],
                            "selected": selected.candidate,
                        },
                    )
                await self._state(
                    plan.id, "RUNNING", progress=Decimal(index + 1) * 100 / len(plan.candidates)
                )
            async with self.lock, db_session.session_scope() as session:
                await finish_oos_report(session, plan.id, expected_windows=len(plan.candidates))
                job = await session.get(WalkForwardJob, plan.id, with_for_update=True)
                if job.owner_id != self.owner_id:
                    raise ValueError("experiment ownership mismatch")
                run = await session.get(BacktestRun, plan.id)
                run.status = job.status = "COMPLETED"
                run.finished_at = job.updated_at = self.clock.utcnow()
                run.progress_pct = job.progress_pct = 100
                job.slot = None
                await session.flush()
                report_digest = catalog_digest(await load_catalog(session, plan.id))
                await self.audit(
                    session,
                    plan.id,
                    "WALKFORWARD_COMPLETED",
                    "walkforward-controller",
                    {
                        "windows": len(plan.candidates),
                        "training_trades_excluded": True,
                        "live_approval": False,
                        "catalog_sha256": report_digest,
                    },
                )
        except asyncio.CancelledError as error:
            await self._state(
                plan.id,
                "INTERRUPTED",
                error=cancellation_code(error, owner_requested=plan.id in self.cancellations),
                terminal=True,
            )
            raise
        except Exception as error:
            await self._state(plan.id, "FAILED", error=type(error).__name__, terminal=True)

    async def stop(self):
        async with self.lock:
            self.stopping = True
        await asyncio.gather(*tuple(self.cancellations.values()), return_exceptions=True)
        async with self.lock:
            owned = tuple(self.tasks.items())
        for _, task in owned:
            task.cancel()
        await asyncio.gather(*(task for _, task in owned), return_exceptions=True)
        for identifier, task in owned:
            await self.finish_cancelled(identifier, task)

    async def finish_cancelled(self, identifier, task, *, error="CONTROLLER_STOPPED"):
        if task.cancelled():
            async with db_session.session_scope() as session:
                row = await session.get(WalkForwardJob, identifier)
                pending = (
                    row is not None and row.owner_id == self.owner_id and row.status == "QUEUED"
                )
            if pending:
                await self._state(identifier, "INTERRUPTED", error=error, terminal=True)
