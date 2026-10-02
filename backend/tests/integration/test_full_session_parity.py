"""One recorded session through two production entry points, with no substitute fill engine."""

from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.backtest.bootstrap import prepare_run
from app.backtest.engine import run_prepared
from app.config import get_settings
from app.db import session as db_session
from app.db.base import Base
from app.db.models.audit import AuditEvent
from app.db.models.decision import ConsideredCandidate, Proposal, RiskDecision, SizingRecord
from app.db.models.journal import JournalEntry
from app.db.models.trading import Order, Position, Trade
from app.monitoring.gate import reset_trading_gate
from tests.integration.test_historical_session import full_session


async def economic_trace():
    selections = {
        "proposals": (
            Proposal,
            (
                "strategy_id",
                "direction",
                "entry_price",
                "stop_loss",
                "target_price",
                "approved_quantity",
                "status",
            ),
        ),
        "candidates": (ConsideredCandidate, ("strategy_id", "stopped_at_stage", "reason_code")),
        "sizing": (
            SizingRecord,
            (
                "method",
                "formula_version",
                "risk_budget",
                "risk_per_unit",
                "lot_size",
                "final_quantity",
                "binding_constraint",
            ),
        ),
        "risk": (
            RiskDecision,
            (
                "approved",
                "binding_rule",
                "rejection_code",
                "approved_quantity",
                "risk_amount",
                "is_preflight",
                "evaluated_at",
            ),
        ),
        "orders": (Order, ("role", "quantity", "filled_quantity", "average_fill_price", "status")),
        "fills": (Trade, ("quantity", "price", "executed_at", "cost_breakdown")),
        "positions": (
            Position,
            ("net_quantity", "average_price", "bought_quantity", "sold_quantity", "realised_pnl"),
        ),
        "journal": (
            JournalEntry,
            (
                "kind",
                "strategy_id",
                "actual_entry",
                "actual_exit",
                "quantity",
                "gross_pnl",
                "charges",
                "net_pnl",
                "opened_at",
                "closed_at",
            ),
        ),
    }
    trace = {}
    async with db_session.session_scope() as session:
        fill_ids = list(
            (await session.scalars(sa.select(Trade.id).order_by(Trade.executed_at))).all()
        )
        identities = {identifier: f"fill:{index}" for index, identifier in enumerate(fill_ids)}

        def normalize(value):
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return identities.get(value, value) if isinstance(value, str) else value

        for name, (model, fields) in selections.items():
            rows = list((await session.scalars(sa.select(model))).all())
            trace[name] = sorted(
                [tuple(normalize(getattr(row, field)) for field in fields) for row in rows],
                key=repr,
            )
        for event_type in ("REFERENCE_EVALUATION", "PAPER_SESSION_TRANSITION"):
            rows = list(
                (
                    await session.scalars(
                        sa.select(AuditEvent)
                        .where(
                            AuditEvent.event_type == event_type,
                        )
                        .order_by(AuditEvent.occurred_at)
                    )
                ).all()
            )
            trace[event_type] = [(row.occurred_at, row.result) for row in rows]
    return trace


async def test_full_recorded_session_matches_direct_paper_worker(fake_clock, tmp_path, monkeypatch):
    recording, manifest = full_session()
    traces = []
    for driver in ("historical", "paper"):
        directory = tmp_path / driver
        directory.mkdir()
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{directory / 'ats_history_fixture01.db'}"
        )
        worker = None
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            monkeypatch.setattr(db_session, "_engine", engine)
            monkeypatch.setattr(
                db_session, "_sessionmaker", async_sessionmaker(engine, expire_on_commit=False)
            )
            reset_trading_gate()
            fake_clock.set_to(manifest.start_at)
            settings = get_settings().model_copy(
                update={
                    "starting_capital": Decimal(100000),
                    "paper_cycle_seconds": 60,
                    "entry_blackout_open_minutes": 30,
                }
            )
            worker = await prepare_run(
                manifest,
                recording,
                settings=settings,
                clock=fake_clock,
                lock_path=directory / "worker.lock",
            )
            if driver == "historical":
                assert await run_prepared(worker, manifest) == "COMPLETED", worker.detail
            else:
                provider = worker.reference_runtime.provider
                await provider.prime()
                await worker.start(schedule=False)
                worker.executor.gate.clear("startup")
                await worker.cycle()
                while (
                    provider.next_event_at is not None and provider.next_event_at <= manifest.end_at
                ):
                    await provider.step()
                    await worker.cycle()
                    assert not worker.failed, worker.detail
                assert fake_clock.now() == manifest.end_at
                assert worker.phase == "MARKET_CLOSE"
            assert worker.executor.broker.account.cash == Decimal("100856.56")
            assert worker.executor.broker.account.used_margin == 0
            assert not worker.executor.broker.account.open_positions()
            traces.append(await economic_trace())
        finally:
            if worker is not None:
                await worker.stop()
            await engine.dispose()
    assert len(traces[0]["proposals"]) == 1
    assert len(traces[0]["orders"]) == 2 and len(traces[0]["fills"]) == 2
    assert traces[0] == traces[1]
