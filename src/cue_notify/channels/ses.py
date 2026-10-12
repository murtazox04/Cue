"""E-mail through Amazon SES (API v2, ``SendEmail``). The address is an e-mail address.

Requests are signed with AWS Signature Version 4 using the channel's keys, or the
standard ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY`` and ``AWS_SESSION_TOKEN``
environment variables, so no AWS SDK is needed.

SES reports bounces, complaints and deliveries through Amazon SNS: subscribe
``<api.public_url>/v1/inbound/ses/<channel>`` (HTTPS) to the topic and list the topic in
``sns_topic_arns``. Cue checks each notification's signature against Amazon's
certificate, accepts only the listed topics, and confirms the subscription itself.
Verifying signatures needs the ``ses`` extra (``pip install 'cue-notify[ses]'``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from datetime import UTC, datetime
from typing import Any, ClassVar, Self
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, model_validator

from cue_notify.channels import email as mail
from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    Receipt,
    finite_delay,
    transport_error,
)
from cue_notify.channels.smtp import single_address
from cue_notify.core.enums import FeedbackType

SERVICE = "ses"
TRANSIENT_ERRORS = frozenset(
    {"TooManyRequestsException", "LimitExceededException", "ThrottlingException"}
)


class SesConfig(ChannelConfig):
    region: str = Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$", examples=["eu-west-1"])
    from_address: str = Field(description="A verified identity in SES.")
    from_name: str | None = None
    reply_to: str | None = None
    configuration_set: str | None = Field(
        default=None, description="Configuration set whose event destination publishes to SNS."
    )
    access_key_id: str | None = Field(default=None, description="Defaults to AWS_ACCESS_KEY_ID.")
    secret_access_key: SecretStr | None = Field(
        default=None, description="Defaults to AWS_SECRET_ACCESS_KEY."
    )
    session_token: SecretStr | None = Field(
        default=None, description="Defaults to AWS_SESSION_TOKEN, for temporary credentials."
    )
    sns_topic_arns: list[str] = Field(
        default_factory=list,
        description="SNS topics allowed to report feedback for this channel.",
    )
    endpoint: str | None = Field(default=None, description="Override for tests or a VPC endpoint.")

    @model_validator(mode="after")
    def _credentials(self) -> Self:
        if (self.access_key_id is None) != (self.secret_access_key is None):
            raise ValueError("set both access_key_id and secret_access_key, or neither")
        return self

    def credentials(self) -> tuple[str, str, str | None]:
        if self.access_key_id and self.secret_access_key:
            token = self.session_token.get_secret_value() if self.session_token else None
            return self.access_key_id, self.secret_access_key.get_secret_value(), token
        key, secret = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
        if not (key and secret):
            raise DeliveryError("SES needs access_key_id and secret_access_key (or AWS_* vars)")
        return key, secret, os.environ.get("AWS_SESSION_TOKEN")


def sign_v4(
    method: str,
    url: str,
    body: bytes,
    *,
    region: str,
    service: str,
    credentials: tuple[str, str, str | None],
    now: datetime | None = None,
) -> dict[str, str]:
    """Headers that sign a request with AWS Signature Version 4."""
    access_key, secret_key, token = credentials
    moment = now or datetime.now(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    day = moment.strftime("%Y%m%d")
    parts = urlsplit(url)
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/json",
        "host": parts.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if token:
        headers["x-amz-security-token"] = token
    names = sorted(headers)
    canonical = "\n".join(
        [
            method,
            parts.path or "/",
            parts.query,
            "".join(f"{name}:{headers[name].strip()}\n" for name in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = f"AWS4{secret_key}".encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    del headers["host"]  # httpx sets it from the URL
    return headers


class SesChannel(Channel[SesConfig]):
    provider: ClassVar[str] = "ses"
    config_model: ClassVar[type[ChannelConfig]] = SesConfig

    @property
    def url(self) -> str:
        base = self.config.endpoint or f"https://email.{self.config.region}.amazonaws.com"
        return f"{base.rstrip('/')}/v2/email/outbound-emails"

    def payload(self, delivery: Delivery) -> dict[str, Any]:
        config = self.config
        sender = (
            f"{config.from_name} <{config.from_address}>"
            if config.from_name
            else config.from_address
        )
        body: dict[str, Any] = {"Text": {"Data": mail.text_body(delivery), "Charset": "UTF-8"}}
        if delivery.content.html:
            body["Html"] = {"Data": delivery.content.html, "Charset": "UTF-8"}
        payload: dict[str, Any] = {
            "FromEmailAddress": sender,
            "Destination": {"ToAddresses": [single_address(delivery.address)]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": mail.subject(delivery), "Charset": "UTF-8"},
                    "Body": body,
                    "Headers": [
                        {"Name": name, "Value": value}
                        for name, value in mail.headers(delivery).items()
                    ],
                }
            },
            "EmailTags": [{"Name": "cue_message_id", "Value": str(delivery.message_id)}],
        }
        if config.reply_to:
            payload["ReplyToAddresses"] = [config.reply_to]
        if config.configuration_set:
            payload["ConfigurationSetName"] = config.configuration_set
        return payload

    async def send(self, delivery: Delivery) -> Receipt:
        body = json.dumps(self.payload(delivery), separators=(",", ":")).encode()
        headers = sign_v4(
            "POST",
            self.url,
            body,
            region=self.config.region,
            service=SERVICE,
            credentials=self.config.credentials(),
        )
        try:
            response = await self.http.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        data: dict[str, Any] = response.json() if response.content else {}
        if response.is_success:
            return Receipt(provider_message_id=str(data.get("MessageId") or "") or None)
        error = (
            response.headers.get("x-amzn-errortype", "").split(":")[0]
            or str(data.get("__type", "")).rsplit("#", 1)[-1]
        )
        detail = f"ses {error or response.status_code}: {data.get('message', '')}"[:300]
        if response.status_code == 429 or response.status_code >= 500 or error in TRANSIENT_ERRORS:
            retry_after = finite_delay(response.headers.get("retry-after"))
            raise DeliveryError(detail, retryable=True, retry_after=retry_after)
        # MessageRejected is about the message or the account (sandbox, unverified
        # sender), not the recipient; dead recipients arrive later as bounces through SNS.
        raise DeliveryError(detail)


# --------------------------------------------------------------------------- SNS


CERT_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
_NOTIFICATION_FIELDS = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SUBSCRIPTION_FIELDS = (
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
)


class SnsError(Exception):
    """The SNS message is not authentic or not for us."""


def is_sns_url(url: str, *, suffix: str = "") -> bool:
    """Only Amazon SNS's own HTTPS endpoints may serve certificates or confirmations."""
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and CERT_HOST.match(parts.hostname or "") is not None
        and parts.port in (None, 443)
        and parts.path.endswith(suffix)
    )


def string_to_sign(message: dict[str, Any]) -> bytes:
    kind = message.get("Type")
    fields = _NOTIFICATION_FIELDS if kind == "Notification" else _SUBSCRIPTION_FIELDS
    return "".join(
        f"{name}\n{message[name]}\n" for name in fields if message.get(name) is not None
    ).encode()


def verify_signature(message: dict[str, Any], certificate_pem: bytes) -> None:
    try:
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:  # pragma: no cover
        raise SnsError("verifying SNS needs: pip install 'cue-notify[ses]'") from exc
    version = str(message.get("SignatureVersion", ""))
    if version not in ("1", "2"):
        raise SnsError(f"unsupported SignatureVersion {version!r}")
    key = x509.load_pem_x509_certificate(certificate_pem).public_key()
    if not isinstance(key, rsa.RSAPublicKey):
        raise SnsError("unexpected certificate key type")
    try:
        signature = base64.b64decode(str(message.get("Signature", "")), validate=True)
        algorithm = hashes.SHA1() if version == "1" else hashes.SHA256()  # noqa: S303 — SNS v1
        key.verify(signature, string_to_sign(message), padding.PKCS1v15(), algorithm)
    except (InvalidSignature, ValueError) as exc:
        raise SnsError("invalid SNS signature") from exc


def feedback(notification: dict[str, Any]) -> list[tuple[FeedbackType, str, str, str]]:
    """(type, reason, SES message id, address) for each recipient an SES event names.

    Accepts both event publishing (``eventType``) and identity notifications
    (``notificationType``)."""
    kind = str(notification.get("eventType") or notification.get("notificationType") or "")
    message_id = str((notification.get("mail") or {}).get("messageId") or "")
    reports: list[tuple[FeedbackType, str, str, str]] = []
    if kind == "Bounce":
        bounce: dict[str, Any] = notification.get("bounce") or {}
        permanent = bounce.get("bounceType") == "Permanent"
        for recipient in bounce.get("bouncedRecipients") or []:
            reason = str(
                recipient.get("diagnosticCode") or bounce.get("bounceSubType") or "bounced"
            )
            reports.append(
                (
                    FeedbackType.BOUNCE if permanent else FeedbackType.SOFT_BOUNCE,
                    reason,
                    message_id,
                    str(recipient.get("emailAddress", "")),
                )
            )
    elif kind == "Complaint":
        complaint: dict[str, Any] = notification.get("complaint") or {}
        reason = str(complaint.get("complaintFeedbackType") or "marked as spam")
        for recipient in complaint.get("complainedRecipients") or []:
            reports.append(
                (FeedbackType.COMPLAINT, reason, message_id, str(recipient.get("emailAddress", "")))
            )
    elif kind == "Delivery":
        delivery: dict[str, Any] = notification.get("delivery") or {}
        for address in delivery.get("recipients") or []:
            reports.append((FeedbackType.DELIVERED, "delivered", message_id, str(address)))
    elif kind == "DeliveryDelay":
        delay: dict[str, Any] = notification.get("deliveryDelay") or {}
        for recipient in delay.get("delayedRecipients") or []:
            reports.append(
                (
                    FeedbackType.SOFT_BOUNCE,
                    str(delay.get("delayType") or "delivery delayed"),
                    message_id,
                    str(recipient.get("emailAddress", "")),
                )
            )
    return [report for report in reports if report[3]]
