"""Deterministic suppression windows; fixture channels do not imply live delivery."""

from datetime import timedelta

import pytest

from app.notifications.dedupe import NotificationLimiter, SuppressionPolicy
from app.notifications.models import DeliveryPolicy
from app.notifications.service import NotificationService
from tests.unit.test_notification_routing import policy
from tests.unit.test_notifications import RecordingChannel, notice


async def test_repeated_condition_sends_once_and_counts_suppression(fake_clock):
    channel = RecordingChannel()
    service = NotificationService({"email": channel}, policy(), DeliveryPolicy(), clock=fake_clock)
    await service.start()
    try:
        first = notice(fake_clock, condition_key="loss-limit")
        assert service.submit(first) == "QUEUED"
        for index in range(99):
            assert (
                service.submit(
                    notice(fake_clock, event_id=f"repeat-{index}", condition_key="loss-limit")
                )
                == "SUPPRESSED"
            )
        await service.drain()
        assert len(channel.messages) == 1
        assert service.limiter.suppressed_total == 99
        assert service.limiter.suppression_counts()[service.limiter.key(first)] == 99
        fake_clock.advance(timedelta(seconds=60))
        assert service.submit(notice(fake_clock, condition_key="loss-limit")) == "QUEUED"
        await service.drain()
        assert len(channel.messages) == 2
    finally:
        await service.stop()


async def test_queue_failure_does_not_consume_admission(fake_clock):
    service = NotificationService({}, policy(), DeliveryPolicy(queue_size=1), clock=fake_clock)
    await service.start()
    try:
        first = notice(fake_clock, condition_key="first")
        second = notice(fake_clock, condition_key="second")
        assert service.submit(first) == "QUEUED"
        assert service.submit(second) == "QUEUE_FULL"
        assert service.limiter.key(second) not in service.limiter.conditions
        await service.drain()
        assert service.submit(second) == "QUEUED"
        await service.drain()
    finally:
        await service.stop()


def test_rate_limits_expiry_and_severity_escalation(fake_clock):
    limiter = NotificationLimiter(SuppressionPolicy(max_events_per_severity=1, max_conditions=1))
    info = notice(fake_clock, severity="INFO")
    critical = notice(fake_clock)
    assert limiter.check(info, 100) == "ALLOW"
    limiter.admit(info, 100)
    assert (
        limiter.check(notice(fake_clock, severity="INFO", message="new condition"), 101)
        == "RATE_LIMITED"
    )
    assert limiter.check(critical, 101) == "ALLOW"
    limiter.admit(critical, 101)
    assert limiter.check(info, 159.99) == "SUPPRESSED"
    assert limiter.check(info, 160) == "ALLOW"
    assert limiter.suppressed_total == 1


def test_condition_bound_does_not_evict_active_dedupe_keys(fake_clock):
    limiter = NotificationLimiter(SuppressionPolicy(max_conditions=1))
    original = notice(fake_clock)
    assert limiter.check(original, 1) == "ALLOW"
    limiter.admit(original, 1)
    assert limiter.check(notice(fake_clock, message="second condition"), 2) == "TRACKING_FULL"
    assert limiter.check(original, 3) == "SUPPRESSED"
    assert len(limiter.conditions) == 1


def test_keys_isolate_event_kind_severity_and_condition(fake_clock):
    limiter = NotificationLimiter(SuppressionPolicy())
    original = notice(fake_clock)
    key = limiter.key(original)
    assert limiter.key(notice(fake_clock, event_id="another-id")) == key
    assert limiter.key(notice(fake_clock, event_type="DRAWDOWN")) != key
    assert limiter.key(notice(fake_clock, severity="WARNING")) != key
    assert limiter.key(notice(fake_clock, message="other instrument")) != key
    assert limiter.key(notice(fake_clock, condition_key="instrument-A")) != limiter.key(
        notice(fake_clock, condition_key="instrument-B")
    )


def test_backwards_monotonic_time_does_not_release_conditions(fake_clock):
    limiter = NotificationLimiter(SuppressionPolicy())
    notification = notice(fake_clock)
    assert limiter.check(notification, 100) == "ALLOW"
    limiter.admit(notification, 100)
    with pytest.raises(ValueError, match="backwards"):
        limiter.check(notification, 99)
    assert limiter.check(notification, 101) == "SUPPRESSED"
