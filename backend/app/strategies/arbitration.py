"""One winner per instrument: explicit priority, then strategy/version lexical order."""

import hashlib

from app.analysis.equity import EvidenceModel
from app.core.logging import get_logger
from app.strategies.signal import Signal

logger = get_logger("strategies.arbitration")


class SuppressedSignal(EvidenceModel):
    signal: Signal
    reason: str


class ArbitrationResult(EvidenceModel):
    winners: tuple[Signal, ...]
    suppressed: tuple[SuppressedSignal, ...]


def resolve_conflicts(signals: tuple[Signal, ...], priorities: dict[str, int]) -> ArbitrationResult:
    groups = {}
    seen = set()
    for original in signals:
        signal = Signal.model_validate(original.model_dump())
        if signal.strategy_id not in priorities or type(priorities[signal.strategy_id]) is not int:
            raise ValueError("explicit integer strategy priority required")
        identity = (signal.instrument_id, signal.strategy_id, signal.strategy_version)
        if identity in seen:
            raise ValueError("duplicate strategy signal")
        seen.add(identity)
        groups.setdefault(signal.instrument_id, []).append(signal)
    winners, suppressed = [], []
    for instrument_id, candidates in sorted(groups.items()):
        if (
            len({(item.generated_at, item.data_origin, item.instrument_key) for item in candidates})
            != 1
        ):
            raise ValueError("arbitration requires a common decision timestamp and provenance")
        ordered = sorted(
            candidates,
            key=lambda item: (
                priorities[item.strategy_id],
                item.strategy_id,
                item.strategy_version,
            ),
        )
        winner = ordered[0]
        winners.append(winner)
        for candidate in ordered[1:]:
            reason = (
                "OPPOSING_SIGNAL"
                if candidate.direction != winner.direction
                else "SAME_DIRECTION_DUPLICATE"
            )
            suppressed.append(SuppressedSignal(signal=candidate, reason=reason))
            logger.info(
                "Signal suppressed",
                extra={
                    "instrument_id": instrument_id,
                    "winner_strategy": winner.strategy_id,
                    "suppressed_strategy": candidate.strategy_id,
                    "winner_digest": hashlib.sha256(winner.model_dump_json().encode()).hexdigest(),
                    "suppressed_digest": hashlib.sha256(
                        candidate.model_dump_json().encode()
                    ).hexdigest(),
                    "reason": reason,
                    "policy": "PRIORITY_V1",
                },
            )
    return ArbitrationResult(winners=tuple(winners), suppressed=tuple(suppressed))
