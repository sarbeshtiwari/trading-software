"""Adversarial isolated-process evidence from the real strategy/risk/account path."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.snapshots import canonical
from app.backtest.catalog import load_catalog
from app.backtest.launcher import launch_async
from app.backtest.reproducibility import fingerprints
from app.db.base import Base
from app.db.models.audit import AuditEvent
from app.db.models.decision import RiskDecision
from app.db.models.trading import Order
from app.marketdata.recordings import RecordingBundle, write_recording
from tests.integration.test_historical_engine import recorded_trade


async def execute_case(root, *, alteration=None, fill_overrides=None):
    root.mkdir()
    recording, manifest = recorded_trade()
    if fill_overrides:
        manifest = manifest.model_copy(
            update={"fill_config": replace(manifest.fill_config, **fill_overrides)}
        )
        if fill_overrides.get("latency_ms"):
            manifest = manifest.model_copy(
                update={
                    "end_at": manifest.end_at + timedelta(milliseconds=fill_overrides["latency_ms"])
                }
            )
    if alteration in {"thin", "thin_refreshed"}:
        first = recording.snapshots[0]
        thin = first.model_copy(
            update={
                "value": replace(
                    first.value,
                    asks=(
                        replace(first.value.asks[0], quantity=40),
                        replace(first.value.asks[0], price=Decimal("100.05"), quantity=1000),
                    ),
                )
            }
        )
        snapshots = [thin]
        if alteration == "thin_refreshed":
            refreshed_at = manifest.start_at + timedelta(seconds=2)
            snapshots.append(
                thin.model_copy(
                    update={
                        "available_at": refreshed_at,
                        "value": replace(thin.value, observed_at=refreshed_at),
                    }
                )
            )
        snapshots.extend(recording.snapshots[1:])
        recording = RecordingBundle.model_validate(
            recording.model_copy(update={"snapshots": tuple(snapshots)}).model_dump()
        )
        manifest = manifest.model_copy(update={"recording_sha256": recording.content_hash()})
    if alteration == "future":
        series = recording.candles[0]
        future = replace(
            series.bars[-1],
            ts=manifest.end_at + timedelta(minutes=1),
            open=Decimal(900),
            high=Decimal(1000),
            low=Decimal(800),
            close=Decimal(950),
        )
        recording = RecordingBundle.model_validate(
            recording.model_copy(
                update={
                    "candles": (
                        series.model_copy(update={"bars": (*series.bars, future)}),
                        *recording.candles[1:],
                    )
                }
            ).model_dump()
        )
        manifest = manifest.model_copy(update={"recording_sha256": recording.content_hash()})
    if alteration == "risk":
        manifest = manifest.model_copy(
            update={
                "risk": manifest.risk.model_copy(
                    update={"max_instrument_exposure_amount": Decimal(1)}
                )
            }
        )
    if alteration == "cost":
        manifest = manifest.model_copy(
            update={
                "fees": tuple(
                    fee.model_copy(update={"stamp_buy_rate": Decimal("0.00013")})
                    for fee in manifest.fees
                )
            }
        )
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'ats_history_fixture01.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        (root / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
        (root / "settings.json").write_text(
            json.dumps(
                {
                    "database_url": str(engine.url),
                    "starting_capital": "100000",
                    **(
                        {"paper_cycle_seconds": 1}
                        if alteration in {"thin", "thin_refreshed"}
                        else {}
                    ),
                }
            ),
            encoding="utf-8",
        )
        write_recording(root / "recording.json", recording)
        outcome = await launch_async(
            root / "manifest.json",
            root / "recording.json",
            root / "settings.json",
            timeout_seconds=60,
        )
        assert outcome.returncode in (0, 3), (outcome.stdout, outcome.stderr)
        async with async_sessionmaker(engine)() as session:
            catalog = await load_catalog(session, manifest.run_id)
            finished = await session.scalar(
                sa.select(AuditEvent).where(AuditEvent.event_type == "HISTORICAL_RUN_FINISHED")
            )
            assert finished.result["reproducibility"] == fingerprints(catalog)
            if fill_overrides and fill_overrides.get("latency_ms"):
                waiting = await session.scalar(
                    sa.select(AuditEvent).where(
                        AuditEvent.event_type == "REFERENCE_EXIT_WAITING_FOR_POST_ENTRY_MARK"
                    )
                )
                assert waiting is not None and waiting.result["fixed_protection_required"]
            if alteration == "risk":
                assert (
                    await session.scalar(sa.select(sa.func.count()).select_from(RiskDecision)) > 0
                )
                assert await session.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
            if alteration in {"thin", "thin_refreshed"}:
                entry = await session.scalar(sa.select(Order).where(Order.role == "ENTRY"))
                assert entry.quantity == 111
                assert entry.filled_quantity == (40 if alteration == "thin" else 80)
            if fill_overrides and fill_overrides.get("reject_probability") == 1:
                orders = list((await session.scalars(sa.select(Order))).all())
                assert orders and all(order.status.value == "REJECTED" for order in orders)
                assert all(order.filled_quantity == 0 for order in orders)
            if fill_overrides and fill_overrides.get("partial_fill_probability") == 1:
                events = list(
                    (
                        await session.scalars(
                            sa.select(AuditEvent).where(
                                AuditEvent.event_type == "ORDER_SYNCHRONIZED"
                            )
                        )
                    ).all()
                )
                partials = [
                    event for event in events if event.result["status"] == "PARTIALLY_FILLED"
                ]
                assert partials and all(event.result["filled_quantity"] > 0 for event in partials)
            return catalog, finished.result["reproducibility"]
    finally:
        await engine.dispose()


async def test_real_replays_reproduce_and_refuse_future_information(tmp_path):
    baseline, first = await execute_case(tmp_path / "baseline")
    repeated, second = await execute_case(tmp_path / "repeated")
    future, altered = await execute_case(tmp_path / "future", alteration="future")
    assert canonical(first) == canonical(second)
    assert first["inputs_sha256"] != altered["inputs_sha256"]
    assert first["outcomes_sha256"] == altered["outcomes_sha256"]
    changed = deepcopy(baseline)
    changed["trades"][0]["quantity"] += 1
    assert fingerprints(changed)["trades_sha256"] != first["trades_sha256"]
    changed = deepcopy(baseline)
    changed["results"][0]["net_pnl"] += 1
    assert fingerprints(changed)["metrics_sha256"] != first["metrics_sha256"]
    changed = deepcopy(baseline)
    changed["run"][0]["seed"] += 1
    assert fingerprints(changed)["inputs_sha256"] != first["inputs_sha256"]
    for catalog in (baseline, repeated, future):
        result = catalog["results"][0]
        assert result["trade_count"] == 1
        assert result["gross_pnl"] == Decimal(888)
        assert result["total_charges"] == Decimal("31.44")
        assert result["net_pnl"] == Decimal("856.56")
        assert catalog["trades"][0]["quantity"] == 111


async def test_historical_risk_veto_and_charges_change_actual_results(tmp_path):
    blocked, risk_fingerprint = await execute_case(tmp_path / "risk", alteration="risk")
    costed, cost_fingerprint = await execute_case(tmp_path / "cost", alteration="cost")
    result = blocked["results"][0]
    assert result["trade_count"] == 0 and blocked["trades"] == []
    assert result["rejections"].get("INSTRUMENT_CONCENTRATION_EXCEEDED", 0) > 0
    result = costed["results"][0]
    assert result["trade_count"] == 1 and result["gross_pnl"] == 888
    assert result["total_charges"] == Decimal("32.55")
    assert result["net_pnl"] == Decimal("855.45")
    assert risk_fingerprint["outcomes_sha256"] != cost_fingerprint["outcomes_sha256"]


async def test_actual_historical_fallback_slippage_changes_net_results_monotonically(tmp_path):
    results = []
    for bps, expected_exit, expected_gross in (
        (0, "108", "888"),
        (10, "107.85", "871.35"),
        (20, "107.75", "860.25"),
    ):
        catalog, _ = await execute_case(
            tmp_path / f"slippage-{bps}",
            fill_overrides={"use_depth": False, "slippage_bps": Decimal(bps)},
        )
        assert catalog["run"][0]["status"] == "COMPLETED"
        assert (
            catalog["run"][0]["assumptions"]["fill_convention"]
            == "RECORDED_LTP_WITH_ADVERSE_SLIPPAGE"
        )
        trade = catalog["trades"][0]
        assert trade["quantity"] == 111 and trade["entry_price"] == 100
        assert trade["exit_price"] == Decimal(expected_exit)
        assert trade["gross_pnl"] == Decimal(expected_gross)
        assert trade["net_pnl"] == trade["gross_pnl"] - trade["charges"]
        results.append(trade["net_pnl"])
    assert results[0] > results[1] > results[2]
    depth, _ = await execute_case(tmp_path / "depth", fill_overrides={"slippage_bps": Decimal(20)})
    assert depth["run"][0]["assumptions"]["fill_convention"] == "RECORDED_DEPTH_AT_PUBLICATION"
    assert depth["trades"][0]["gross_pnl"] == 888


async def test_actual_historical_partial_and_rejected_fills_are_not_full_success(tmp_path):
    rejected, _ = await execute_case(
        tmp_path / "rejected", fill_overrides={"reject_probability": 1}
    )
    assert rejected["results"][0]["trade_count"] == 0
    assert rejected["trades"] == []
    partial, _ = await execute_case(
        tmp_path / "partial", fill_overrides={"partial_fill_probability": 1}
    )
    assert partial["run"][0]["status"] == "INCOMPLETE"


async def test_historical_latency_uses_injected_clock_not_wall_sleep(tmp_path):
    catalog, _ = await execute_case(tmp_path / "latency", fill_overrides={"latency_ms": 150})
    _, manifest = recorded_trade()
    assert catalog["run"][0]["status"] == "COMPLETED"
    trade = catalog["trades"][0]
    assert trade["entry_ts"] == manifest.start_at + timedelta(milliseconds=150)
    assert trade["exit_ts"] == manifest.end_at + timedelta(milliseconds=150)
    assert trade["gross_pnl"] == 888 and trade["net_pnl"] == Decimal("856.56")


async def test_worker_polling_cannot_refill_a_thin_recorded_limit_book(tmp_path):
    for alteration, expected_quantity in (("thin", 40), ("thin_refreshed", 80)):
        catalog, _ = await execute_case(tmp_path / alteration, alteration=alteration)
        assert catalog["run"][0]["status"] == "COMPLETED"
        trade = catalog["trades"][0]
        assert trade["quantity"] == expected_quantity
        assert trade["entry_price"] == 100 and trade["exit_price"] == 108
        assert trade["gross_pnl"] == Decimal(8) * expected_quantity
        assert trade["net_pnl"] == trade["gross_pnl"] - trade["charges"]
