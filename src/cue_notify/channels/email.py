"""What every e-mail connector (SMTP, Resend, Amazon SES) puts in a message."""

from __future__ import annotations

import hashlib

from cue_notify.channels.base import Delivery
from cue_notify.core.enums import Importance


def subject(delivery: Delivery) -> str:
    # Rendered titles may contain line breaks from event data; headers cannot.
    return " ".join((delivery.content.title or "").split())


def text_body(delivery: Delivery) -> str:
    content = delivery.content
    parts = [content.body]
    if content.url:
        parts.append(content.url)
    parts += [f"{a.label}: {a.url}" for a in content.actions if a.url]
    return "\n\n".join(parts)


def headers(delivery: Delivery) -> dict[str, str]:
    """Threading, importance and the RFC 8058 one-click unsubscribe Gmail and Yahoo require."""
    result: dict[str, str] = {}
    if delivery.content.thread:
        # A stable root id per thread lets mail clients stack the conversation.
        digest = hashlib.sha256(delivery.content.thread.encode()).hexdigest()[:32]
        result["References"] = f"<{digest}@thread.cue>"
    if unsubscribe := delivery.links.get("unsubscribe"):
        result["List-Unsubscribe"] = f"<{unsubscribe}>"
        result["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if delivery.importance in (Importance.HIGH, Importance.CRITICAL):
        result["Importance"] = "high"
    elif delivery.importance is Importance.LOW:
        result["Importance"] = "low"
    return result
