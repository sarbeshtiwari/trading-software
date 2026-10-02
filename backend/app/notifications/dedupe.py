"""Bounded loop-local condition suppression and per-severity admission limits."""

import hashlib
from collections import deque
from dataclasses import dataclass

from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.audit.snapshots import canonical
from app.core.enums import Severity


class SuppressionPolicy(EvidenceModel):
    window_seconds: float = Field(default=60, gt=0, le=86400)
    max_events_per_severity: int = Field(default=100, ge=1, le=10000, strict=True)
    max_conditions: int = Field(default=1000, ge=1, le=10000, strict=True)


@dataclass
class Condition:
    admitted_at: float
    severity: Severity
    suppressed: int = 0


class NotificationLimiter:
    def __init__(self, policy: SuppressionPolicy):
        self.policy = SuppressionPolicy.model_validate(policy.model_dump())
        self.conditions: dict[str, Condition] = {}
        self.admissions = {severity: deque() for severity in Severity}
        self.last_time = None
        self.suppressed_total = 0

    @staticmethod
    def key(notification):
        value = (
            notification.event_type,
            notification.severity,
            notification.condition_key or notification.message,
        )
        return hashlib.sha256(canonical(value).encode()).hexdigest()

    def check(self, notification, now):
        if self.last_time is not None and now < self.last_time:
            raise ValueError("notification monotonic clock moved backwards")
        self.last_time = now
        cutoff = now - self.policy.window_seconds
        self.conditions = {
            key: condition
            for key, condition in self.conditions.items()
            if condition.admitted_at > cutoff
        }
        for admissions in self.admissions.values():
            while admissions and admissions[0] <= cutoff:
                admissions.popleft()
        key = self.key(notification)
        if key in self.conditions:
            self.conditions[key].suppressed += 1
            self.suppressed_total += 1
            return "SUPPRESSED"
        if len(self.admissions[notification.severity]) >= self.policy.max_events_per_severity:
            return "RATE_LIMITED"
        if (
            sum(
                condition.severity == notification.severity
                for condition in self.conditions.values()
            )
            >= self.policy.max_conditions
        ):
            return "TRACKING_FULL"
        return "ALLOW"

    def admit(self, notification, now):
        self.conditions[self.key(notification)] = Condition(
            admitted_at=now, severity=notification.severity
        )
        self.admissions[notification.severity].append(now)

    def suppression_counts(self):
        return {key: condition.suppressed for key, condition in self.conditions.items()}
