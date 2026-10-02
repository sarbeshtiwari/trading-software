"""Explicit severity routing and IST quiet hours, independent of transport."""

from datetime import datetime, time

from pydantic import Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.clock import IST
from app.core.enums import Severity


class ChannelRoute(EvidenceModel):
    channel: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    severities: tuple[Severity, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_severities(self):
        if len(set(self.severities)) != len(self.severities):
            raise ValueError("duplicate channel severity")
        return self


class RoutingPolicy(EvidenceModel):
    routes: tuple[ChannelRoute, ...]
    quiet_start: time | None = None
    quiet_end: time | None = None

    @model_validator(mode="after")
    def validate_policy(self):
        if len({route.channel for route in self.routes}) != len(self.routes):
            raise ValueError("duplicate channel route")
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("quiet hours require both endpoints")
        if self.quiet_start is not None:
            if self.quiet_start.tzinfo is not None or self.quiet_end.tzinfo is not None:
                raise ValueError("quiet hours are local IST wall times")
            if self.quiet_start == self.quiet_end:
                raise ValueError("equal quiet-hour endpoints are ambiguous")
        return self


class RoutingDecision(EvidenceModel):
    channels: tuple[str, ...]
    quiet_suppressed: bool


def route_notification(severity: Severity, now: datetime, policy: RoutingPolicy) -> RoutingDecision:
    policy = RoutingPolicy.model_validate(policy.model_dump())
    severity = Severity(severity)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("notification routing requires an aware delivery time")
    current = now.astimezone(IST).time()
    quiet = False
    if policy.quiet_start is not None:
        if policy.quiet_start < policy.quiet_end:
            quiet = policy.quiet_start <= current < policy.quiet_end
        else:
            quiet = current >= policy.quiet_start or current < policy.quiet_end
    suppressed = quiet and severity != Severity.CRITICAL
    return RoutingDecision(
        channels=()
        if suppressed
        else tuple(route.channel for route in policy.routes if severity in route.severities),
        quiet_suppressed=suppressed,
    )
