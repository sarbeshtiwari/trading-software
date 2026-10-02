"""OOS-only reports; independent window accounts are never a fabricated continuous equity curve."""

from decimal import Decimal
from itertools import pairwise

import sqlalchemy as sa

from app.agents.validation import _utc
from app.backtest.adequacy import WindowDisclosure, window_warning
from app.backtest.windows import Window
from app.core.clock import IST
from app.db.models.backtest import BacktestResult, BacktestRun, BacktestTrade
from app.execution.paper import stable_id
from app.portfolio.metrics import performance
from app.strategies.evidence import StrategyBinding, summarize_bindings


async def append_oos_window(
    session, identifier, index, window, selected, *, catalog, parameters, degradation
):
    parent = await session.get(BacktestRun, identifier)
    raw = catalog["run"][0]["assumptions"].get("window_disclosure")
    previous = parent.assumptions.get("window_disclosure")
    if raw is not None and (index == 0 or previous is not None):
        windows = list(WindowDisclosure.model_validate(previous).windows) if previous else []
        windows.extend(
            window.model_copy(update={"role": "OUT_OF_SAMPLE"})
            for window in WindowDisclosure.model_validate(raw).windows
        )
        disclosure = WindowDisclosure(windows=tuple(windows)).model_dump(mode="json")
        parent.assumptions = parent.assumptions | {"window_disclosure": disclosure}
        parent.data_window_warning = window_warning(disclosure)
    else:
        parent.assumptions = parent.assumptions | {"window_disclosure": None}
        parent.data_window_warning = (
            "UNAVAILABLE: one or more OOS windows lack captured duration policy"
        )
    source = catalog["results"][0]
    raw_binding = catalog["run"][0]["assumptions"].get("strategy_binding")
    binding = StrategyBinding.model_validate(raw_binding) if raw_binding is not None else None
    drop = (
        selected.net_return - source["total_return"] if source["total_return"] is not None else None
    )
    flagged = (
        drop > degradation.max_net_return_drop
        if degradation is not None and drop is not None
        else None
    )
    links = [
        {
            "report_trade_id": stable_id("btt", f"{identifier}:{index}:{trade['id']}"),
            "source_trade_id": trade["id"],
            "source_run_id": catalog["run"][0]["id"],
        }
        for trade in catalog["trades"]
    ]
    diagnostics = {
        "account_model": "INDEPENDENT_WINDOW_ACCOUNTS",
        "source_run_id": catalog["run"][0]["id"],
        "selected_candidate": selected.candidate,
        "strategy_risk_fraction": parameters["strategy_risk_fraction"],
        "training_net_return": str(selected.net_return),
        "oos_net_return": str(source["total_return"])
        if source["total_return"] is not None
        else None,
        "net_return_drop": str(drop) if drop is not None else None,
        "degradation_threshold": str(degradation.max_net_return_drop)
        if degradation is not None
        else None,
        "degradation_state": "UNAVAILABLE"
        if flagged is None
        else "FLAGGED"
        if flagged
        else "WITHIN_THRESHOLD",
        "trade_links": links,
        "strategy_binding": binding.model_dump(mode="json") if binding else None,
    }
    values = {
        key: value
        for key, value in source.items()
        if key
        not in {
            "id",
            "run_id",
            "created_at",
            "updated_at",
            "window_kind",
            "window_index",
            "window_start",
            "window_end",
            "window_parameters",
            "notes",
            "overfitting_flag",
        }
    }
    session.add(
        BacktestResult(
            **values,
            run_id=identifier,
            window_kind="OUT_OF_SAMPLE",
            window_index=index,
            window_start=window.test_start.astimezone(IST).date(),
            window_end=window.test_end.astimezone(IST).date(),
            overfitting_flag=flagged,
            window_parameters={
                "source_run_id": catalog["run"][0]["id"],
                "window": window.model_dump(mode="json"),
                "selected_training_score": selected.model_dump(mode="json"),
                "training_trades_excluded": True,
                "diagnostics": diagnostics,
            },
            notes=(
                f"SIMULATED OUT_OF_SAMPLE window {index}; selected {selected.candidate} "
                "using training only. Independent starting account; not LIVE validation."
            ),
        )
    )
    for trade in catalog["trades"]:
        values = {
            key: value
            for key, value in trade.items()
            if key not in {"id", "run_id", "window_index"}
        }
        session.add(
            BacktestTrade(
                **values,
                id=stable_id("btt", f"{identifier}:{index}:{trade['id']}"),
                run_id=identifier,
                window_index=index,
            )
        )


async def finish_oos_report(session, identifier, *, expected_windows):
    windows = list(
        (
            await session.scalars(
                sa.select(BacktestResult)
                .where(BacktestResult.run_id == identifier)
                .order_by(BacktestResult.window_index)
            )
        ).all()
    )
    trades = list(
        (
            await session.scalars(
                sa.select(BacktestTrade)
                .where(BacktestTrade.run_id == identifier)
                .order_by(BacktestTrade.entry_ts, BacktestTrade.id)
            )
        ).all()
    )
    if (
        len(windows) != expected_windows
        or [row.window_index for row in windows] != list(range(expected_windows))
        or any(row.window_kind != "OUT_OF_SAMPLE" for row in windows)
    ):
        raise ValueError("only completed OOS windows may be aggregated")
    if any(
        row.net_pnl is None or row.total_charges is None or row.gross_pnl is None for row in windows
    ):
        raise ValueError("OOS net accounting unavailable")
    for row in windows:
        matched = [trade for trade in trades if trade.window_index == row.window_index]
        window = Window.model_validate(row.window_parameters["window"])
        if len(matched) != row.trade_count or any(
            not window.test_start <= _utc(trade.entry_ts) <= _utc(trade.exit_ts) < window.test_end
            for trade in matched
        ):
            raise ValueError("OOS trade count or time lineage mismatch")
        for attribute, metric in (
            ("gross_pnl", "gross_pnl"),
            ("charges", "total_charges"),
            ("net_pnl", "net_pnl"),
        ):
            amounts = [getattr(trade, attribute) for trade in matched]
            if any(amount is None for amount in amounts) or sum(amounts, Decimal(0)) != getattr(
                row, metric
            ):
                raise ValueError("OOS window and trade accounting disagree")
    if len(trades) != sum(row.trade_count for row in windows):
        raise ValueError("unattributed OOS trade")
    holding = [
        Decimal(str((_utc(trade.exit_ts) - _utc(trade.entry_ts)).total_seconds()))
        for trade in trades
    ]
    metrics = performance([], [trade.net_pnl for trade in trades], holding)
    choices = [row.window_parameters["selected_training_score"]["candidate"] for row in windows]
    fractions = [
        Decimal(row.window_parameters["diagnostics"]["strategy_risk_fraction"]) for row in windows
    ]
    flagged = (
        True
        if any(row.overfitting_flag for row in windows)
        else None
        if any(row.overfitting_flag is None for row in windows)
        else False
    )
    diagnostics = {
        "account_model": "INDEPENDENT_WINDOW_ACCOUNTS",
        "window_count": len(windows),
        "binding_summary": summarize_bindings(
            [row.window_parameters["diagnostics"].get("strategy_binding") for row in windows]
        ).model_dump(mode="json"),
        "candidate_choices": choices,
        "risk_fraction_choices": [str(value) for value in fractions],
        "parameter_changes": sum(before != after for before, after in pairwise(choices)),
        "absolute_risk_fraction_drift": str(
            sum((abs(after - before) for before, after in pairwise(fractions)), Decimal(0))
        ),
        "profitable_window_fraction": str(
            Decimal(sum(row.net_pnl > 0 for row in windows)) / len(windows)
        ),
        "max_within_window_drawdown": str(max(row.max_drawdown for row in windows))
        if all(row.max_drawdown is not None for row in windows)
        else None,
        "degradation_state": "UNAVAILABLE"
        if flagged is None
        else "FLAGGED"
        if flagged
        else "WITHIN_THRESHOLD",
    }
    session.add(
        BacktestResult(
            run_id=identifier,
            window_kind="OOS_AGGREGATE",
            trade_count=len(trades),
            overfitting_flag=flagged,
            gross_pnl=sum((row.gross_pnl for row in windows), Decimal(0)),
            total_charges=sum((row.total_charges for row in windows), Decimal(0)),
            net_pnl=sum((row.net_pnl for row in windows), Decimal(0)),
            win_rate=metrics["win_rate"],
            profit_factor=metrics["profit_factor"],
            expectancy=metrics["expectancy"],
            avg_holding_seconds=int(metrics["average_holding_seconds"])
            if metrics["average_holding_seconds"] is not None
            else None,
            window_parameters={
                "account_model": "INDEPENDENT_WINDOW_ACCOUNTS",
                "training_trades_excluded": True,
                "window_count": len(windows),
                "diagnostics": diagnostics,
                "parameter_choices": choices,
                "parameter_changes": sum(before != after for before, after in pairwise(choices)),
                "profitable_window_fraction": str(
                    Decimal(sum(row.net_pnl > 0 for row in windows)) / len(windows)
                ),
            },
            notes=(
                "SIMULATED OOS_AGGREGATE: sum of separate reset-capital accounts, "
                "not a continuous portfolio. Training trades excluded. Continuous-equity "
                "return/drawdown and annualized metrics UNAVAILABLE; no LIVE approval."
            ),
        )
    )
