"""E-mail over SMTP using only the standard library. The address is an e-mail address."""

from __future__ import annotations

import asyncio
import hashlib
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import ClassVar, Literal

from pydantic import Field, SecretStr

from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    InvalidAddressError,
    Receipt,
)


class SmtpConfig(ChannelConfig):
    host: str
    port: int = 587
    security: Literal["starttls", "tls", "none"] = "starttls"
    username: str | None = None
    password: SecretStr | None = None
    from_address: str
    from_name: str | None = None
    reply_to: str | None = None
    timeout: float = Field(default=15.0, gt=0)


class SmtpChannel(Channel[SmtpConfig]):
    provider: ClassVar[str] = "smtp"
    config_model: ClassVar[type[ChannelConfig]] = SmtpConfig

    def build(self, delivery: Delivery) -> EmailMessage:
        content = delivery.content
        config = self.config
        email = EmailMessage()
        email["Subject"] = content.title or ""
        email["From"] = formataddr((config.from_name or "", config.from_address))
        email["To"] = delivery.address
        email["Message-ID"] = make_msgid(idstring=str(delivery.message_id))
        if content.thread:
            # A stable root id per thread lets mail clients stack the conversation.
            digest = hashlib.sha256(content.thread.encode()).hexdigest()[:32]
            email["References"] = f"<{digest}@thread.cue>"
        if config.reply_to:
            email["Reply-To"] = config.reply_to
        text = f"{content.body}\n\n{content.url}" if content.url else content.body
        email.set_content(text)
        if content.html:
            email.add_alternative(content.html, subtype="html")
        return email

    def _send_blocking(self, email: EmailMessage) -> None:
        config = self.config
        context = ssl.create_default_context()
        client = (
            smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout, context=context)
            if config.security == "tls"
            else smtplib.SMTP(config.host, config.port, timeout=config.timeout)
        )
        with client:
            if config.security == "starttls":
                client.starttls(context=context)
            if config.username and config.password:
                client.login(config.username, config.password.get_secret_value())
            client.send_message(email)

    async def send(self, delivery: Delivery) -> Receipt:
        email = self.build(delivery)
        try:
            await asyncio.to_thread(self._send_blocking, email)
        except smtplib.SMTPRecipientsRefused as exc:
            raise InvalidAddressError(f"recipient refused: {exc.recipients}") from exc
        except smtplib.SMTPResponseException as exc:
            # 4xx replies are transient by definition (RFC 5321 §4.2.1).
            raise DeliveryError(
                f"SMTP {exc.smtp_code}: {exc.smtp_error!r}", retryable=400 <= exc.smtp_code < 500
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
        return Receipt(provider_message_id=email["Message-ID"])
