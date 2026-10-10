"""Verify the signature on requests Cue's webhook connector sends to your service.

Cue signs the raw body: ``Cue-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of
"<t>.<raw body>">``. Verify against the bytes you received, before parsing them, and
reject old timestamps so a captured request cannot be replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "Cue-Signature"
DEFAULT_TOLERANCE = 300


class InvalidSignatureError(Exception):
    """The request was not signed by Cue with this secret, or is too old."""


def verify_webhook(
    body: bytes,
    signature: str | None,
    secret: str,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> None:
    """Raise :class:`InvalidSignatureError` unless ``signature`` (the ``Cue-Signature``
    header) is a valid, fresh signature of ``body``.

    ``tolerance`` is the accepted clock difference in seconds.
    """
    if not signature:
        raise InvalidSignatureError("missing Cue-Signature header")
    parts = dict(part.split("=", 1) for part in signature.split(",") if "=" in part)
    try:
        timestamp = int(parts["t"])
        candidate = parts["v1"]
    except (KeyError, ValueError) as exc:
        raise InvalidSignatureError("malformed Cue-Signature header") from exc
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance:
        raise InvalidSignatureError("signature timestamp is outside the tolerance")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    if not hmac.compare_digest(expected.hexdigest(), candidate):
        raise InvalidSignatureError("signature does not match")
