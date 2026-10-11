"""Hosted review page: approve or reject a held message from a one-time link.

Reviewers get the link in the ``cue.approval_requested`` event (``data.review_url``), so
they can decide from the notification itself. Like the preference pages, ``GET`` only
shows the message (mail scanners prefetch links) and the decision is a ``POST``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from cue_notify.api.deps import RuntimeDep, SessionDep
from cue_notify.api.pages import ENV, HEADERS, STYLE, form_fields
from cue_notify.core.content import DEFAULT_VARIANT
from cue_notify.db.models import Agent, Recipient
from cue_notify.services import agents
from cue_notify.services.errors import NotFoundError, ServiceError

router = APIRouter(prefix="/review", include_in_schema=False)

PAGE = ENV.from_string("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Review a message</title>
{{ style }}
</head>
<body><main>
<h1>Review a message</h1>
{% if view == "review" %}
<p class="lead"><strong>{{ agent }}</strong> wants to send this to
<strong>{{ recipient }}</strong>{% if importance != "normal" %}
({{ importance }} importance){% endif %}.</p>
<fieldset><legend>{{ title or "Message" }}</legend>
<p>{{ body }}</p>
{% if url %}<small>Link: {{ url }}</small>{% endif %}
{% if reason %}<small>Held because: {{ reason }}</small>{% endif %}
</fieldset>
<form method="post">
<p><label>Reason, if you reject it (optional)<br>
<input name="reason" maxlength="400" style="width:100%;font:inherit;padding:8px"></label></p>
<div class="actions">
<button name="action" value="approve">Approve and send</button>
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
    return HTMLResponse(
        PAGE.render(style=STYLE, **context), status_code=status_code, headers=HEADERS
    )


def _error(exc: ServiceError) -> HTMLResponse:
    message = "This review link is not valid." if isinstance(exc, NotFoundError) else exc.detail
    return _render(exc.status_code, view="error", message=message)


@router.get("/{token}", response_class=HTMLResponse)
async def page(token: str, session: SessionDep) -> Response:
    try:
        _, message = await agents.by_review_token(session, token, lock=False)
    except ServiceError as exc:
        return _error(exc)
    agent = await session.get(Agent, message.agent_key) if message.agent_key else None
    recipient = await session.get(Recipient, message.recipient_id)
    content = message.content.get(DEFAULT_VARIANT, {})
    return _render(
        view="review",
        agent=agent.name if agent else message.agent_key or "An agent",
        recipient=recipient.external_id if recipient else "someone",
        importance=message.importance.value,
        title=content.get("title"),
        body=content.get("body", ""),
        url=content.get("url"),
        reason=message.status_reason,
        expires_at=message.expires_at.strftime("%Y-%m-%d %H:%M") if message.expires_at else None,
    )


@router.post("/{token}", response_class=HTMLResponse)
async def decide(
    token: str, request: Request, session: SessionDep, runtime: RuntimeDep
) -> Response:
    try:
        form = await form_fields(request)
        action = (form.get("action") or [""])[0]
        if action not in ("approve", "reject"):
            return RedirectResponse(".", status_code=303)
        reason = (form.get("reason") or [""])[0].strip()[:400] or None
        message = await agents.decide_by_link(
            session, runtime, token, approve=action == "approve", reason=reason
        )
    except ServiceError as exc:
        return _error(exc)
    if action == "reject":
        text = "Rejected. The message will not be sent."
    elif message.status_reason == "expired":
        text = "Approved, but the message had already expired, so it will not be sent."
    else:
        text = "Approved. The message is on its way."
    return _render(view="done", message=text)
