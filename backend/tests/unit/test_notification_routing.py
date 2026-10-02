"""Routing-only tests: selecting a channel is not evidence of message delivery."""

from datetime import datetime, time

import pytest
from pydantic import ValidationError

from app.core.clock import IST, UTC
from app.notifications.routing import ChannelRoute, RoutingPolicy, route_notification


def policy(**changes):
    return RoutingPolicy(
        **(
            {
                "routes": (
                    ChannelRoute(channel="telegram", severities=("WARNING", "CRITICAL")),
                    ChannelRoute(channel="email", severities=("INFO", "CRITICAL")),
                ),
                "quiet_start": time(22),
                "quiet_end": time(7),
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    "severity,channels",
    [
        ("INFO", ("email",)),
        ("WARNING", ("telegram",)),
        ("CRITICAL", ("telegram", "email")),
    ],
)
def test_notification_routing_matrix(severity, channels):
    result = route_notification(severity, datetime(2026, 9, 21, 10, tzinfo=IST), policy())
    assert result.channels == channels and not result.quiet_suppressed


@pytest.mark.parametrize(
    "hour,minute,suppressed",
    [
        (21, 59, False),
        (22, 0, True),
        (23, 59, True),
        (0, 0, True),
        (6, 59, True),
        (7, 0, False),
    ],
)
def test_quiet_hours_and_critical_bypass(hour, minute, suppressed):
    now = datetime(2026, 9, 21, hour, minute, tzinfo=IST).astimezone(UTC)
    result = route_notification("WARNING", now, policy())
    assert result.quiet_suppressed is suppressed
    assert result.channels == (() if suppressed else ("telegram",))
    critical = route_notification("CRITICAL", now, policy())
    assert critical.channels == ("telegram", "email") and not critical.quiet_suppressed


def test_daytime_quiet_window_disabled_window_and_empty_routes():
    now = datetime(2026, 9, 21, 12, tzinfo=IST)
    daytime = policy(quiet_start=time(11), quiet_end=time(13))
    assert route_notification("INFO", now, daytime).quiet_suppressed
    assert not route_notification("INFO", now.replace(hour=13), daytime).quiet_suppressed
    disabled = policy(quiet_start=None, quiet_end=None)
    assert route_notification("WARNING", now.replace(hour=23), disabled).channels == ("telegram",)
    assert route_notification("CRITICAL", now, policy(routes=())).channels == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"quiet_start": None},
        {"quiet_end": None},
        {"quiet_end": time(22)},
        {"quiet_start": time(22, tzinfo=UTC)},
        {"routes": (ChannelRoute(channel="email", severities=("INFO",)),) * 2},
    ],
)
def test_invalid_routing_policy(changes):
    with pytest.raises(ValidationError):
        policy(**changes)


def test_invalid_time_severity_and_channels():
    with pytest.raises(ValueError):
        route_notification("INFO", datetime(2026, 9, 21), policy())
    with pytest.raises(ValueError):
        route_notification("UNKNOWN", datetime(2026, 9, 21, tzinfo=IST), policy())
    with pytest.raises(ValidationError):
        ChannelRoute(channel="email", severities=("INFO", "INFO"))
    with pytest.raises(ValidationError):
        ChannelRoute(channel="", severities=("INFO",))
