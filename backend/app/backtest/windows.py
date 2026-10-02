"""Half-open chronological experiment windows and bounded owner specifications."""

from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.clock import UTC
from app.strategies.approval import OOSValidationPolicy


class Window(EvidenceModel):
    train_start: AwareDatetime
    train_end: AwareDatetime
    test_start: AwareDatetime
    test_end: AwareDatetime


def rolling_windows(start, end, *, train_seconds, test_seconds, step_seconds, embargo_seconds=0):
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("aware ordered experiment bounds required")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end:
        raise ValueError("aware ordered experiment bounds required")
    if any(
        type(value) is not int or value <= 0
        for value in (train_seconds, test_seconds, step_seconds)
    ):
        raise ValueError("positive integral window durations required")
    if step_seconds < test_seconds:
        raise ValueError("out-of-sample windows must not overlap")
    if type(embargo_seconds) is not int or embargo_seconds < 0:
        raise ValueError("nonnegative integral embargo required")
    windows = []
    cursor = start
    while cursor + timedelta(seconds=train_seconds + embargo_seconds + test_seconds) <= end:
        boundary = cursor + timedelta(seconds=train_seconds)
        test_start = boundary + timedelta(seconds=embargo_seconds)
        windows.append(
            Window(
                train_start=cursor,
                train_end=boundary,
                test_start=test_start,
                test_end=test_start + timedelta(seconds=test_seconds),
            )
        )
        if len(windows) > 25:
            raise ValueError("at most 25 walk-forward windows")
        cursor += timedelta(seconds=step_seconds)
    if not windows:
        raise ValueError("experiment has no complete train/test window")
    return tuple(windows)


class CandidatePair(EvidenceModel):
    name: str = Field(min_length=1, max_length=64)
    train_plan: str = Field(pattern=r"^[a-z0-9]{8,26}$")
    test_plan: str = Field(pattern=r"^[a-z0-9]{8,26}$")


class DegradationPolicy(EvidenceModel):
    max_net_return_drop: Decimal = Field(ge=0)
    units: Literal["RETURN_FRACTION"] = "RETURN_FRACTION"


class WalkForwardPlan(EvidenceModel):
    id: str = Field(pattern=r"^[a-z0-9]{8,26}$")
    label: str = Field(min_length=1, max_length=100)
    start_at: AwareDatetime
    end_at: AwareDatetime
    train_seconds: int = Field(gt=0, strict=True)
    test_seconds: int = Field(gt=0, strict=True)
    step_seconds: int = Field(gt=0, strict=True)
    embargo_seconds: int = Field(default=0, ge=0, strict=True)
    objective: Literal["NET_RETURN"] = "NET_RETURN"
    minimum_training_trades: int = Field(ge=1, strict=True)
    degradation: DegradationPolicy | None = None
    validation: OOSValidationPolicy | None = None
    candidates: tuple[tuple[CandidatePair, ...], ...] = Field(min_length=1, max_length=25)

    def windows(self):
        return rolling_windows(
            self.start_at,
            self.end_at,
            train_seconds=self.train_seconds,
            test_seconds=self.test_seconds,
            step_seconds=self.step_seconds,
            embargo_seconds=self.embargo_seconds,
        )

    @model_validator(mode="after")
    def coherent(self):
        if len(self.candidates) != len(self.windows()):
            raise ValueError("candidate groups must match generated windows")
        names = None
        identifiers = []
        for group in self.candidates:
            if not 1 <= len(group) <= 8 or len({pair.name for pair in group}) != len(group):
                raise ValueError("one to eight uniquely named candidates required")
            current = tuple(pair.name for pair in group)
            if names is not None and names != current:
                raise ValueError("candidate names/order must remain fixed across windows")
            names = current
            identifiers.extend(
                identifier for pair in group for identifier in (pair.train_plan, pair.test_plan)
            )
        if (
            len(set(identifiers)) != len(identifiers)
            or self.id in identifiers
            or len(identifiers) > 50
        ):
            raise ValueError("unique isolated plan identities required; at most 50 child runs")
        return self
