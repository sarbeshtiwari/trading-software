"""Freeze owner plans and validate chronology before any experiment child executes."""

from datetime import timedelta
from decimal import Decimal

from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.backtest.fno import require_chain_history
from app.backtest.plans import read_plan
from app.backtest.reproducibility import digest


def input_digest(inputs):
    plan, manifest, recording, settings = inputs
    return digest(
        {
            "plan": plan.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
            "recording": recording.content_hash(),
            "settings": settings,
        }
    )


def freeze_experiment(registry, experiment):
    frozen = {}
    candidate_parameters = {}
    capital = None
    observations = {}
    strategy_id = None
    for window, group in zip(experiment.windows(), experiment.candidates, strict=True):
        baselines = {}
        for pair in group:
            manifests = []
            for role, identifier, start, end in (
                ("train", pair.train_plan, window.train_start, window.train_end),
                ("test", pair.test_plan, window.test_start, window.test_end),
            ):
                inputs = read_plan(registry, identifier)
                _, manifest, recording, configuration = inputs
                if strategy_id is not None and strategy_id != manifest.strategy_id:
                    raise ValueError("experiment strategy family must remain constant")
                strategy_id = manifest.strategy_id
                require_chain_history(manifest, recording)
                check_recording_consistency(observations, recording)
                if manifest.start_at != start or manifest.end_at != end - timedelta(microseconds=1):
                    raise ValueError(
                        "child manifest must match half-open experiment window exactly"
                    )
                if capital is not None and capital != manifest.risk.capital:
                    raise ValueError("experiment capital must be constant across isolated accounts")
                capital = manifest.risk.capital
                frozen[identifier] = {
                    "sha256": input_digest(inputs),
                    "manifest": manifest.model_dump(mode="json"),
                }
                comparable = {
                    "manifest": manifest.model_dump(
                        mode="json", exclude={"run_id", "strategy_risk_fraction"}
                    ),
                    "execution": {
                        key: value for key, value in configuration.items() if key != "database_url"
                    },
                }
                if role in baselines and baselines[role] != comparable:
                    raise ValueError(
                        "candidate comparison may vary only declared strategy risk fraction"
                    )
                baselines[role] = comparable
                manifests.append(manifest)
            train, test = manifests
            parameters = {
                "strategy_instrument_id": train.strategy_instrument_id,
                "strategy_risk_fraction": str(train.strategy_risk_fraction),
                "risk": train.risk.model_dump(mode="json"),
            }
            test_parameters = {
                "strategy_instrument_id": test.strategy_instrument_id,
                "strategy_risk_fraction": str(test.strategy_risk_fraction),
                "risk": test.risk.model_dump(mode="json"),
            }
            if parameters != test_parameters or (
                pair.name in candidate_parameters and candidate_parameters[pair.name] != parameters
            ):
                raise ValueError(
                    "candidate parameters/risk limits must be frozen for training and OOS"
                )
            candidate_parameters[pair.name] = parameters
    return frozen


def check_recording_consistency(observations, recording):
    def remember(key, value):
        if key in observations and observations[key] != value:
            raise ValueError("conflicting point-in-time observations across experiment inputs")
        if key not in observations and len(observations) >= 100000:
            raise ValueError("experiment exceeds 100000 unique observation limit")
        observations[key] = value

    for series in recording.candles:
        for bar in series.bars:
            remember(
                ("candle", series.instrument.key, series.interval_minutes, bar.ts),
                (bar, series.original_data_origin),
            )
    for snapshot in recording.recorded_snapshots():
        remember(("snapshot", snapshot.key, snapshot.available_at), snapshot.value)


class TrainingScore(EvidenceModel):
    candidate: str
    train_run_id: str
    net_return: Decimal
    trade_count: int = Field(ge=0)
    outcome_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def select_candidate(scores: tuple[TrainingScore, ...], minimum_trades: int):
    if (
        type(minimum_trades) is not int
        or minimum_trades < 1
        or len({score.candidate for score in scores}) != len(scores)
    ):
        raise ValueError("explicit positive training threshold and unique candidates required")
    eligible = [
        TrainingScore.model_validate(score.model_dump())
        for score in scores
        if score.trade_count >= minimum_trades
    ]
    if not eligible:
        raise ValueError("no eligible in-sample candidate; OOS must not run")
    return min(eligible, key=lambda score: (-score.net_return, score.candidate))
