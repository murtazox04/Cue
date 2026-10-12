"""Hosted review page: decide on a held message or an approval request from a one-time link.

Reviewers get the link in the ``cue.approval_requested`` event (``data.review_url``), so
they can decide from the notification itself. Like the preference pages, ``GET`` only
shows what is asked (mail scanners prefetch links) and the decision is a ``POST``.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.ai.screening import html_text
from cue_notify.api.deps import RuntimeDep, SessionDep
from cue_notify.api.pages import ENV, HEADERS, STYLE, form_fields
from cue_notify.core.content import DEFAULT_VARIANT
from cue_notify.core.enums import MessageStatus
from cue_notify.db.models import Agent, Recipient, ReviewLink
from cue_notify.services import agents, approvals, review_links
from cue_notify.services.errors import NotFoundError, ServiceError

_LINK = re.compile(r"(?:https?://|www\.)[^\s<>\"'\]]+")

router = APIRouter(prefix="/review", include_in_schema=False)

PAGE = ENV.from_string("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{{ heading }}</title>
{{ style }}
</head>
<body><main>
<h1>{{ heading }}</h1>
{% if view == "review" %}
<p class="lead"><strong>{{ agent }}</strong> {{ asks }}{% if importance != "normal" %}
({{ importance }} importance){% endif %}.</p>
<fieldset><legend>{{ title or "Message" }}</legend>
<p style="white-space:pre-line">{{ body }}</p>
{% if url %}<small>Link: {{ url }}</small>{% endif %}
{% if links %}<small>Every link in the message, on any channel:</small>
<ul>{% for link in links %}<li><small>{{ link }}</small></li>{% endfor %}</ul>{% endif %}
{% if reason %}<small>Held because: {{ reason }}</small>{% endif %}
</fieldset>
<form method="post">
<p><label>{{ comment_label }}<br>
<input name="reason" maxlength="400" style="width:100%;font:inherit;padding:8px"></label></p>
<div class="actions">
<button name="action" value="approve">{{ approve_label }}</button>
<button name="action" value="reject" class="secondary">Reject</button>
</div>
</form>
{% if expires_at %}<p><small>This link works until {{ expires_at }} UTC.</small></p>{% endif %}
{% elif view == "done" %}
<p class="notice" role="status">{{ message }}</p>
{% else %}
<p class="lead">{{ message }}</p>
{% endif %}
</main></body>
</html>
""")


def _render(status_code: int = 200, **context: Any) -> HTMLResponse:
    context.setdefault("heading", "Review a request")
    return HTMLResponse(
        PAGE.render(style=STYLE, **context), status_code=status_code, headers=HEADERS
    )


def _error(exc: ServiceError) -> HTMLResponse:
    message = "This review link is not valid." if isinstance(exc, NotFoundError) else exc.detail
    return _render(exc.status_code, view="error", message=message)


async def _agent_name(session: AsyncSession, key: str | None, fallback: str) -> str:
    agent = await session.get(Agent, key) if key else None
    return agent.name if agent else key or fallback


def _links(variants: dict[str, dict[str, Any]]) -> list[str]:
    """Link targets from every variant (HTML, push data and buttons included), so the
    reviewer approves what each channel would actually show, not only the default text."""
    found: set[str] = set()
    for variant in variants.values():
        found.update(str(variant[key]) for key in ("url", "image_url") if variant.get(key))
        found.update(str(a["url"]) for a in variant.get("actions") or [] if a.get("url"))
        text = " ".join(
            [str(v) for v in (variant.get("data") or {}).values()]
            + [str(variant.get(key) or "") for key in ("title", "body")]
        )
        found.update(_LINK.findall(text))
        if variant.get("html"):
            found.update(_LINK.findall(html_text(variant["html"])))
    return sorted(found)[:20]


async def _message_view(session: AsyncSession, link: ReviewLink) -> dict[str, Any]:
    message = await agents.by_review_link(session, link, lock=False)
    recipient = await session.get(Recipient, message.recipient_id)
    content = message.content.get(DEFAULT_VARIANT, {})
    return {
        "heading": "Review a message",
        "agent": await _agent_name(session, message.agent_key, "An agent"),
        "asks": f"wants to send this to {recipient.external_id if recipient else 'someone'}",
        "importance": message.importance.value,
        "title": content.get("title"),
        "body": content.get("body", ""),
        "url": content.get("url"),
        "links": _links(message.content),
        "reason": message.status_reason,
        "comment_label": "Reason, if you reject it (optional)",
        "approve_label": "Approve and send",
        "expires_at": message.expires_at,
    }


async def _approval_view(session: AsyncSession, link: ReviewLink) -> dict[str, Any]:
    approval = await approvals.by_review_link(session, link, lock=False)
    return {
        "heading": "Approve a request",
        "agent": await _agent_name(session, approval.agent_key, approval.requested_by),
        "asks": "asks for your decision",
        "importance": approval.importance.value,
        "title": approval.title,
        "body": approval.body,
        "url": None,
        "links": [],
        "reason": None,
        "comment_label": "Comment for the requester (optional)",
        "approve_label": "Approve",
        "expires_at": approval.expires_at,
    }


@router.get("/{token}", response_class=HTMLResponse)
async def page(token: str, session: SessionDep) -> Response:
    try:
        link = await review_links.find(session, token, lock=False)
        view = await (_message_view if link.message_id else _approval_view)(session, link)
    except ServiceError as exc:
        return _error(exc)
    expires_at = view.pop("expires_at")
    view["expires_at"] = expires_at.strftime("%Y-%m-%d %H:%M") if expires_at else None
    return _render(view="review", **view)


@router.post("/{token}", response_class=HTMLResponse)
async def decide(
    token: str, request: Request, session: SessionDep, runtime: RuntimeDep
) -> Response:
    try:
        form = await form_fields(request)
        action = (form.get("action") or [""])[0]
        if action not in ("approve", "reject"):
            return RedirectResponse(request.url.path, status_code=303)
        approve = action == "approve"
        comment = (form.get("reason") or [""])[0].strip()[:400] or None
        # Not locked: the decision locks the target, and locking the link first would
        # deadlock two reviewers deciding at once (each holds its link, both close both).
        link = await review_links.find(session, token, lock=False)
        if link.message_id is None:
            await approvals.decide_by_link(session, runtime, link, approve=approve, comment=comment)
            text = "Approved. The requester has been told." if approve else "Rejected."
            return _render(view="done", message=text)
        message = await agents.decide_by_link(
            session, runtime, link, approve=approve, reason=comment
        )
    except ServiceError as exc:
        return _error(exc)
    if not approve:
        text = "Rejected. The message will not be sent."
    elif message.status is MessageStatus.SUPPRESSED:
        text = "Approved, but the message had already expired, so it will not be sent."
    else:
        text = "Approved. The message is on its way."
    return _render(view="done", message=text)
