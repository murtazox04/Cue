from __future__ import annotations

import hashlib
import hmac

import pytest

from cue_client import InvalidSignatureError, verify_webhook

BODY = b'{"id":"m1","content":{"body":"Hi"}}'
SECRET = "s3cret"
NOW = 1_800_000_000


def _sign(body: bytes, secret: str, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def test_valid_signature_passes() -> None:
    verify_webhook(BODY, _sign(BODY, SECRET, NOW), SECRET, now=NOW + 10)


def test_matches_the_server_signer() -> None:
    webhook = pytest.importorskip("cue_notify.channels.webhook")
    verify_webhook(BODY, webhook.sign(BODY, SECRET, NOW), SECRET, now=NOW)


@pytest.mark.parametrize(
    ("body", "header", "now", "message"),
    [
        (BODY + b" ", _sign(BODY, SECRET, NOW), NOW, "does not match"),
        (BODY, _sign(BODY, "other", NOW), NOW, "does not match"),
        (BODY, _sign(BODY, SECRET, NOW), NOW + 301, "outside the tolerance"),
        (BODY, "t=abc,v1=00", NOW, "malformed"),
        (BODY, "v1=00", NOW, "malformed"),
        (BODY, None, NOW, "missing"),
    ],
)
def test_invalid_signatures_are_rejected(
    body: bytes, header: str | None, now: int, message: str
) -> None:
    with pytest.raises(InvalidSignatureError, match=message):
        verify_webhook(body, header, SECRET, now=now)
