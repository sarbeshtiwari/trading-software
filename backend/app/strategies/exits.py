"""Deterministic exits from timestamped marks, never assumed OHLC execution order."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel, PriceObservation
from app.core.enums import SignalDirection
from app.fno.chain.model import aware


class ExitPolicy(EvidenceModel):
    trailing_r_multiple: Decimal = Field(gt=0)
    max_holding_seconds: int = Field(gt=0, strict=True)
    invalidation_key: str = Field(min_length=1)
    max_mark_age_seconds: int = Field(gt=0, strict=True)


class ExitState(EvidenceModel):
    position_id: str = Field(min_length=1)
    direction: Literal[SignalDirection.LONG, SignalDirection.SHORT]
    entry: Decimal = Field(gt=0)
    stop: Decimal = Field(gt=0)
    target: Decimal = Field(gt=0)
    favourable_price: Decimal = Field(gt=0)
    opened_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def protective(self):
        sign = 1 if self.direction == SignalDirection.LONG else -1
        if sign * (self.entry - self.stop) <= 0 or sign * (self.target - self.entry) <= 0:
            raise ValueError("invalid stop or target")
        if sign * (self.favourable_price - self.entry) < 0 or self.updated_at < self.opened_at:
            raise ValueError("invalid exit state")
        return self


class Invalidation(EvidenceModel):
    key: str = Field(min_length=1)
    active: bool
    observed_at: AwareDatetime
    available_at: AwareDatetime


class ExitDecision(EvidenceModel):
    reason: Literal["STOP", "TARGET", "TRAILING", "TIME", "INVALIDATION"] | None
    trailing_stop: Decimal
    state: ExitState
    invalidation_checked: bool


class ExitContext(EvidenceModel):
    mark: PriceObservation
    invalidation: Invalidation | None
    as_of: AwareDatetime


def evaluate_exit(
    state: ExitState,
    mark: PriceObservation,
    invalidation: Invalidation | None,
    policy: ExitPolicy,
    *,
    as_of: datetime,
) -> ExitDecision:
    aware(as_of)
    state = ExitState.model_validate(state.model_dump())
    mark = PriceObservation.model_validate(mark.model_dump())
    invalidation = (
        Invalidation.model_validate(invalidation.model_dump()) if invalidation is not None else None
    )
    policy = ExitPolicy.model_validate(policy.model_dump())
    max_age = timedelta(seconds=policy.max_mark_age_seconds)
    for item in (mark, invalidation) if invalidation is not None else (mark,):
        if not item.observed_at <= item.available_at <= as_of or as_of - item.observed_at > max_age:
            raise ValueError("future or stale exit evidence")
    if state.updated_at > mark.observed_at or (
        invalidation is not None and invalidation.key != policy.invalidation_key
    ):
        raise ValueError("exit state or invalidation identity mismatch")
    sign = 1 if state.direction == SignalDirection.LONG else -1
    best = (
        max(state.favourable_price, mark.value)
        if sign == 1
        else min(state.favourable_price, mark.value)
    )
    distance = abs(state.entry - state.stop) * policy.trailing_r_multiple
    trailing_stop = best - sign * distance
    trailing_stop = max(state.stop, trailing_stop) if sign == 1 else min(state.stop, trailing_stop)
    triggers = (
        ("STOP", sign * (mark.value - state.stop) <= 0),
        ("TARGET", sign * (mark.value - state.target) >= 0),
        ("TRAILING", sign * (mark.value - trailing_stop) <= 0),
        ("TIME", as_of - state.opened_at >= timedelta(seconds=policy.max_holding_seconds)),
        ("INVALIDATION", invalidation is not None and invalidation.active),
    )
    reason = next((reason for reason, triggered in triggers if triggered), None)
    updated = state.model_copy(update={"favourable_price": best, "updated_at": mark.observed_at})
    return ExitDecision(
        reason=reason,
        trailing_stop=trailing_stop,
        state=updated,
        invalidation_checked=invalidation is not None,
    )
