"""Isolated transports; no test constitutes remote message-delivery validation."""

import asyncio
import json
import logging
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.logging import register_secret
from app.notifications.channels import SMTPChannel, TelegramChannel, configured_channels
from app.notifications.models import DeliveryPolicy, Notification
from app.notifications.service import NotificationService
from tests.unit.test_notification_routing import policy


def notice(clock, **changes):
    return Notification(
        **(
            {
                "event_id": "test-event",
                "event_type": "DAILY_LOSS_LIMIT",
                "occurred_at": clock.now(),
                "severity": "CRITICAL",
                "message": "Synthetic test alert",
            }
            | changes
        )
    )


class RecordingChannel:
    def __init__(self, *, failures=0):
        self.messages = []
        self.calls = 0
        self.failures = failures

    async def send(self, notification):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("test-only transport failure")
        self.messages.append(notification)


async def test_telegram_request_and_acknowledgement(fake_clock):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    channel = TelegramChannel(
        SecretStr("test-only-token"), "test-chat", transport=httpx.MockTransport(handler)
    )
    await channel.send(notice(fake_clock))
    request = captured[0]
    assert request.url.host == "api.telegram.org" and request.url.scheme == "https"
    body = json.loads(request.content)
    assert body["chat_id"] == "test-chat" and "test-event" in body["text"]
    assert "parse_mode" not in body and body["link_preview_options"]["is_disabled"]


@pytest.mark.parametrize(
    "status,body",
    [
        (500, {}),
        (302, {}),
        (200, {"ok": False}),
        (200, {"ok": True}),
        (200, {"ok": True, "result": {"message_id": True}}),
        (200, []),
    ],
)
async def test_telegram_rejects_unacknowledged_delivery(fake_clock, status, body):
    channel = TelegramChannel(
        SecretStr("test-only-token"),
        "test-chat",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
    )
    with pytest.raises(RuntimeError):
        await channel.send(notice(fake_clock))


async def test_telegram_exception_never_exposes_token(fake_clock):
    def unavailable(request):
        raise httpx.ConnectError("test-only-token", request=request)

    channel = TelegramChannel(
        SecretStr("test-only-token"), "test-chat", transport=httpx.MockTransport(unavailable)
    )
    with pytest.raises(RuntimeError) as caught:
        await channel.send(notice(fake_clock))
    assert "test-only-token" not in str(caught.value)


async def test_smtp_tls_failure_has_no_plaintext_fallback(fake_clock, monkeypatch):
    calls = []

    class NoTLS:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self, **kwargs):
            raise RuntimeError("test-password")

        def login(self, *args):
            raise AssertionError("plaintext login forbidden")

    monkeypatch.setattr("app.notifications.channels.smtplib.SMTP", NoTLS)
    settings = Settings(
        smtp_host="test.invalid",
        smtp_from="a@test.invalid",
        smtp_to="b@test.invalid",
        smtp_user="test-user",
        smtp_password=SecretStr("test-password"),
    )
    with pytest.raises(RuntimeError) as caught:
        await SMTPChannel(settings).send(notice(fake_clock))
    assert "test-password" not in str(caught.value) and calls == ["ehlo"]


@pytest.mark.parametrize("refused", [False, True])
async def test_smtp_requires_tls_and_checks_recipients(fake_clock, monkeypatch, refused):
    calls = []

    class SMTPFixture:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("test.invalid", 587, 10)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self, context):
            assert context.check_hostname
            calls.append("tls")

        def login(self, user, password):
            assert user == "test-user" and password == "test-password"
            calls.append("login")

        def send_message(self, message):
            calls.append("send")
            assert "test-event" in message.get_content()
            return {"recipient@test.invalid": (550, b"refused")} if refused else {}

    monkeypatch.setattr("app.notifications.channels.smtplib.SMTP", SMTPFixture)
    settings = Settings(
        smtp_host="test.invalid",
        smtp_from="sender@test.invalid",
        smtp_to="recipient@test.invalid",
        smtp_user="test-user",
        smtp_password=SecretStr("test-password"),
    )
    if refused:
        with pytest.raises(RuntimeError, match="refused"):
            await SMTPChannel(settings).send(notice(fake_clock))
    else:
        await SMTPChannel(settings).send(notice(fake_clock))
    assert calls == ["ehlo", "tls", "ehlo", "login", "send"]


def test_missing_channel_configuration_warns_without_network(caplog):
    with caplog.at_level(logging.WARNING):
        assert configured_channels(Settings()) == {}
    assert "Telegram notification channel disabled" in caplog.text
    assert "SMTP notification channel disabled" in caplog.text
    incomplete = Settings(
        smtp_host="test.invalid",
        smtp_from="a@test.invalid",
        smtp_to="b@test.invalid",
        smtp_user="user-without-password",
    )
    assert configured_channels(incomplete) == {}
    configured = Settings(
        telegram_bot_token=SecretStr("test-token"),
        telegram_chat_id="test-chat",
        smtp_host="test.invalid",
        smtp_from="a@test.invalid",
        smtp_to="b@test.invalid",
    )
    assert set(configured_channels(configured)) == {"telegram", "email"}


async def test_retry_failure_isolation_redaction_and_routing(fake_clock):
    good = RecordingChannel(failures=1)
    bad = RecordingChannel(failures=10)
    service = NotificationService(
        {"telegram": bad, "email": good},
        policy(),
        DeliveryPolicy(retry_delay_seconds=0),
        clock=fake_clock,
    )
    register_secret("test-notification-secret")
    await service.start()
    try:
        assert service.submit(notice(fake_clock, message="test-notification-secret")) == "QUEUED"
        await service.drain()
        assert good.calls == 2 and bad.calls == 3
        assert "test-notification-secret" not in good.messages[0].message
        assert ("test-event", "email", "ACKNOWLEDGED", 2) in service.outcomes
        assert ("test-event", "telegram", "FAILED", 3) in service.outcomes
    finally:
        await service.stop()


async def test_submit_never_waits_for_transport_and_timeout_is_bounded(fake_clock):
    blocked = asyncio.Event()

    class WaitingChannel:
        async def send(self, notification):
            await blocked.wait()

    service = NotificationService(
        {"email": WaitingChannel()},
        policy(),
        DeliveryPolicy(max_attempts=2, timeout_seconds=0.01, retry_delay_seconds=0, queue_size=1),
        clock=fake_clock,
    )
    assert service.submit(notice(fake_clock)) == "NOT_RUNNING"
    await service.start()
    try:
        assert service.submit(notice(fake_clock)) == "QUEUED"
        assert service.submit(notice(fake_clock, condition_key="different")) == "QUEUE_FULL"
        await asyncio.wait_for(service.drain(), 1)
        assert ("test-event", "email", "FAILED", 2) in service.outcomes
        assert ("test-event", "telegram", "DISABLED", 0) in service.outcomes
    finally:
        await service.stop()


async def test_quiet_hours_critical_bypass_and_invalid_notice(fake_clock):
    fake_clock.set_to(fake_clock.now().replace(hour=23))
    channel = RecordingChannel()
    service = NotificationService({"email": channel}, policy(), DeliveryPolicy(), clock=fake_clock)
    await service.start()
    try:
        assert service.submit(notice(fake_clock, severity="INFO")) == "QUEUED"
        assert (
            service.submit(notice(fake_clock, occurred_at=fake_clock.now() + timedelta(seconds=1)))
            == "INVALID"
        )
        assert service.submit(None) == "INVALID"
        await service.drain()
        assert not channel.messages
        assert service.submit(notice(fake_clock)) == "QUEUED"
        await service.drain()
        assert len(channel.messages) == 1
    finally:
        await service.stop()


async def test_stop_records_cancellation_and_can_restart(fake_clock):
    started = asyncio.Event()

    class WaitingChannel:
        async def send(self, notification):
            started.set()
            await asyncio.Event().wait()

    service = NotificationService(
        {"email": WaitingChannel()}, policy(), DeliveryPolicy(), clock=fake_clock
    )
    await service.start()
    service.submit(notice(fake_clock))
    await asyncio.wait_for(started.wait(), 1)
    service.submit(notice(fake_clock, event_id="queued-test", condition_key="different"))
    await service.stop()
    await asyncio.wait_for(service.drain(), 1)
    assert ("test-event", None, "CANCELLED", 0) in service.outcomes
    assert ("queued-test", None, "CANCELLED", 0) in service.outcomes
    assert service.submit(notice(fake_clock)) == "NOT_RUNNING"
    await service.start()
    await service.stop()
