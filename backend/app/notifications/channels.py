"""Real Telegram HTTPS and SMTP STARTTLS transports; fixtures exist only in tests."""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

import httpx

from app.core.logging import register_secret
from app.notifications.models import Notification

logger = logging.getLogger(__name__)


class Channel(Protocol):
    async def send(self, notification: Notification) -> None: ...


class TelegramChannel:
    def __init__(self, token, chat_id, *, timeout=10, transport=None):
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout
        self._transport = transport
        register_secret(token.get_secret_value())

    async def send(self, notification):
        try:
            await self._send(notification)
        except Exception:
            raise RuntimeError("Telegram delivery failed") from None

    async def _send(self, notification):
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self._token.get_secret_value()}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": notification.text(),
                    "link_preview_options": {"is_disabled": True},
                },
            )
            if response.status_code != 200:
                raise RuntimeError("Telegram rejected notification")
            body = response.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise RuntimeError("Telegram did not acknowledge notification")
            result = body.get("result")
            if not isinstance(result, dict) or type(result.get("message_id")) is not int:
                raise RuntimeError("Telegram returned malformed acknowledgement")


class SMTPChannel:
    def __init__(self, settings, *, timeout=10):
        self._settings = settings
        self._timeout = timeout
        if settings.smtp_password:
            register_secret(settings.smtp_password.get_secret_value())

    async def send(self, notification):
        try:
            await asyncio.to_thread(self._send, notification)
        except Exception:
            raise RuntimeError("SMTP delivery failed or recipient refused") from None

    def _send(self, notification):
        settings = self._settings
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = settings.smtp_to
        message["Subject"] = f"ATS {notification.severity.value} notification"
        message.set_content(notification.text())
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=self._timeout) as client:
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
            if settings.smtp_user:
                client.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            if client.send_message(message):
                raise RuntimeError("SMTP recipient refused notification")


def configured_channels(settings, *, timeout=10):
    channels = {}
    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels["telegram"] = TelegramChannel(
            settings.telegram_bot_token, settings.telegram_chat_id, timeout=timeout
        )
    else:
        logger.warning("Telegram notification channel disabled: missing configuration")
    credentials_paired = bool(settings.smtp_user) == bool(settings.smtp_password)
    if settings.smtp_host and settings.smtp_from and settings.smtp_to and credentials_paired:
        channels["email"] = SMTPChannel(settings, timeout=timeout)
    else:
        logger.warning("SMTP notification channel disabled: missing or incomplete configuration")
    return channels
