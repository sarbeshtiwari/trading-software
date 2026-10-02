"""Versioned economic fingerprints, excluding only run-local lineage identities."""

import hashlib

from app.audit.snapshots import canonical


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fingerprints(catalog):
    if len(catalog["run"]) != 1 or len(catalog["results"]) != 1:
        raise ValueError("one finalized run and result required")
    run = catalog["run"][0]
    if run["status"] not in {"COMPLETED", "INCOMPLETE", "FAILED"} or not run["simulated"]:
        raise ValueError("terminal simulated evidence required")
    configuration = {
        key: run[key]
        for key in (
            "strategy_id",
            "strategy_version",
            "seed",
            "start_date",
            "end_date",
            "interval_minutes",
            "universe",
            "initial_capital",
            "assumptions",
            "risk_config_snapshot",
        )
    }
    configuration["parameters"] = {
        key: value for key, value in run["parameters"].items() if key != "run_id"
    }
    result = catalog["results"][0]
    metrics = {
        key: value
        for key, value in result.items()
        if key not in {"id", "run_id", "created_at", "updated_at", "window_parameters"}
    }
    parameters = result["window_parameters"] or {}
    samples = [
        {
            **{
                key: value
                for key, value in sample.items()
                if key not in {"audit_event_id", "position_ids"}
            },
            "open_position_count": len(sample["position_ids"]),
        }
        for sample in parameters.get("account_samples", [])
    ]
    metrics["window_parameters"] = {
        key: value
        for key, value in parameters.items()
        if key not in {"account_samples", "trade_links"}
    }
    metrics["account_samples"] = samples
    metrics["linked_trade_count"] = len(parameters.get("trade_links", []))
    trades = [
        {key: value for key, value in trade.items() if key not in {"id", "run_id"}}
        for trade in catalog["trades"]
    ]
    trades.sort(key=canonical)
    outcomes = {
        "status": run["status"],
        "error_detail": run["error_detail"],
        "metrics": metrics,
        "trades": trades,
    }
    return {
        "version": 1,
        "inputs_sha256": digest(configuration),
        "outcomes_sha256": digest(outcomes),
        "metrics_sha256": digest(metrics),
        "trades_sha256": digest(trades),
        "simulated": True,
    }
