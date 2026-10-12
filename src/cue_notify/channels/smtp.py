"""E-mail over SMTP using only the standard library. The address is an e-mail address."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid
from typing import ClassVar, Literal

from pydantic import Field, SecretStr

from cue_notify.channels import email as mail
from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    InvalidAddressError,
    Receipt,
)

SERVICE_CLOSING = 421


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


def single_address(address: str) -> str:
    """``address`` if it is exactly one mailbox. A list ("a@x.com, b@y.com") would copy
    every message to all of them, past the recipient's preferences and caps."""
    addresses = getaddresses([address])
    name, addr = addresses[0] if len(addresses) == 1 else ("", "")
    if name or addr != address.strip() or addr.count("@") != 1:
        raise InvalidAddressError("the address must be a single e-mail address")
    return addr


class SmtpChannel(Channel[SmtpConfig]):
    provider: ClassVar[str] = "smtp"
    config_model: ClassVar[type[ChannelConfig]] = SmtpConfig

    def build(self, delivery: Delivery) -> EmailMessage:
        config = self.config
        email = EmailMessage()
        email["Subject"] = mail.subject(delivery)
        email["From"] = formataddr((config.from_name or "", config.from_address))
        email["To"] = single_address(delivery.address)
        email["Message-ID"] = make_msgid(idstring=str(delivery.message_id))
        if config.reply_to:
            email["Reply-To"] = config.reply_to
        for name, value in mail.headers(delivery).items():
            email[name] = value
        email.set_content(mail.text_body(delivery))
        if delivery.content.html:
            email.add_alternative(delivery.content.html, subtype="html")
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
        try:
            email = self.build(delivery)
        except ValueError as exc:  # e.g. an unusable address or header; retrying won't help
            raise DeliveryError(f"cannot build e-mail: {exc}") from exc
        try:
            await asyncio.to_thread(self._send_blocking, email)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = {code for code, _ in exc.recipients.values()}
            if codes and all(400 <= code < 500 for code in codes):
                raise DeliveryError(
                    f"recipient deferred: {exc.recipients}",
                    retryable=True,
                    deferred=SERVICE_CLOSING not in codes,
                ) from exc
            raise InvalidAddressError(f"recipient refused: {exc.recipients}") from exc
        except smtplib.SMTPResponseException as exc:
            # 4xx replies are transient by definition (RFC 5321 §4.2.1).
            transient = 400 <= exc.smtp_code < 500
            raise DeliveryError(
                f"SMTP {exc.smtp_code}: {exc.smtp_error!r}",
                retryable=transient,
                deferred=transient
                and exc.smtp_code != SERVICE_CLOSING
                and isinstance(exc, smtplib.SMTPDataError),
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
        return Receipt(provider_message_id=email["Message-ID"])
