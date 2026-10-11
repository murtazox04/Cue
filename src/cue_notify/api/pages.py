"""The hosted preference page and the one-click unsubscribe endpoint.

Plain server-rendered HTML with no JavaScript, so it works in every mail client's browser.
The token in the URL is the credential, so pages are never cached, indexed or framed, and
send no referrer. ``GET`` never changes anything (mail scanners prefetch links); changes
happen on ``POST`` only, which is also what RFC 8058 one-click unsubscribe sends.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment
from markupsafe import Markup

from cue_notify.api.deps import RuntimeDep, SessionDep
from cue_notify.services import preferences
from cue_notify.services.errors import NotFoundError, ServiceError

router = APIRouter(prefix="/preferences", include_in_schema=False)

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}
CHANNEL_LABELS = {
    "push": "Push notifications",
    "sms": "Text messages (SMS)",
    "email": "E-mail",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
}
CategoryQuery = Annotated[str, Query(min_length=1, max_length=64)]


def _label(key: str) -> str:
    return key.replace("-", " ").replace("_", " ").capitalize()


STYLE = Markup("""<style>
:root { color-scheme: light dark; --fg:#1d1d1f; --muted:#6e6e73; --line:#d2d2d7;
  --bg:#fff; --card:#f5f5f7; --accent:#0a66c2; --on-accent:#fff; --ok:#1a7f37; }
@media (prefers-color-scheme: dark) { :root { --fg:#f5f5f7; --muted:#a1a1a6; --line:#3a3a3c;
  --bg:#000; --card:#1c1c1e; --accent:#4c9eff; --on-accent:#001a3d; --ok:#3fb950; } }
* { box-sizing: border-box; }
body { margin:0; font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  color:var(--fg); background:var(--bg); }
main { max-width:560px; margin:0 auto; padding:40px 16px 64px; }
h1 { font-size:1.5rem; margin:0 0 4px; }
p.lead { color:var(--muted); margin:0 0 24px; }
.notice { background:var(--card); border-left:4px solid var(--ok); padding:12px 16px;
  border-radius:8px; margin:0 0 24px; }
fieldset { border:1px solid var(--line); border-radius:12px; padding:8px 16px; margin:0 0 20px; }
legend { font-weight:600; padding:0 6px; }
label.row { display:flex; gap:12px; align-items:flex-start; padding:12px 0;
  border-top:1px solid var(--line); cursor:pointer; }
legend + label.row { border-top:0; }
input[type=checkbox] { width:20px; height:20px; margin-top:2px; accent-color:var(--accent); }
small { display:block; color:var(--muted); }
.actions { display:flex; flex-wrap:wrap; gap:12px; }
button { font:inherit; border-radius:10px; padding:10px 18px; cursor:pointer;
  border:1px solid var(--accent); background:var(--accent); color:var(--on-accent); }
button.secondary { background:transparent; color:var(--accent); }
a { color:var(--accent); }
</style>""")
ENV = Environment(autoescape=True)
ENV.filters["label"] = _label


def _channel(name: str) -> str:
    return CHANNEL_LABELS.get(name) or _label(name)


ENV.filters["channel"] = _channel
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
{% if view == "center" %}
<p class="lead">Choose what you hear about and where.</p>
{% if saved %}<p class="notice" role="status">Your preferences are saved.</p>{% endif %}
<form method="post">
<fieldset><legend>What we send you</legend>
{% for c in center.categories %}
<label class="row"><input type="checkbox" name="category" value="{{ c.key }}"
{%- if c.subscribed %} checked{% endif %}{% if not c.can_unsubscribe %} disabled{% endif %}>
<span><strong>{{ c.key | label }}</strong>
{%- if c.description %}<small>{{ c.description }}</small>{% endif %}
{%- if not c.can_unsubscribe %}
<small>Always sent: you need these to use the service.</small>{% endif %}
</span></label>
{% endfor %}
</fieldset>
{% if center.channels %}
<fieldset><legend>Where we reach you</legend>
{% for ch in center.channels %}
<label class="row"><input type="checkbox" name="channel" value="{{ ch.name }}"
{%- if not ch.muted %} checked{% endif %}>
<span><strong>{{ ch.name | channel }}</strong></span></label>
{% endfor %}
</fieldset>
{% endif %}
<div class="actions">
<button name="action" value="save">Save preferences</button>
<button name="action" value="unsubscribe_all" class="secondary">
Unsubscribe from all optional messages</button>
</div>
</form>
{% elif view == "confirm" %}
<p class="lead">You will stop receiving <strong>{{ category | label }}</strong> messages.</p>
<form method="post"><div class="actions">
<button>Unsubscribe</button>
</div></form>
<p><a href="{{ center_url }}">Manage all preferences</a></p>
{% elif view == "done" %}
<p class="notice" role="status">You are unsubscribed from {{ category | label }} messages.</p>
<p><a href="{{ center_url }}">Manage all preferences</a></p>
{% else %}
<p class="lead">{{ message }}</p>
{% endif %}
</main></body>
</html>
""")


def _render(request: Request, status_code: int = 200, **context: Any) -> HTMLResponse:
    product = request.app.state.runtime.settings.api.product_name
    heading = f"{product} notification preferences" if product else "Notification preferences"
    html = PAGE.render(heading=heading, style=STYLE, **context)
    return HTMLResponse(html, status_code=status_code, headers=HEADERS)


def _error(request: Request, exc: ServiceError) -> HTMLResponse:
    message = (
        "This link is no longer valid. Use the link in a more recent message."
        if isinstance(exc, NotFoundError)
        else exc.detail
    )
    return _render(request, exc.status_code, view="error", message=message)


def provenance(request: Request, method: str) -> dict[str, Any]:
    """What the consent ledger keeps about a change a person made themselves."""
    return {
        "method": method,
        "ip": request.client.host if request.client else None,
        "user_agent": (request.headers.get("user-agent") or "")[:300] or None,
    }


MAX_FORM_BYTES = 16_384


class _BadFormError(ServiceError):
    title = "Bad request"


async def form_fields(request: Request, *, keep_blank: bool = False) -> dict[str, list[str]]:
    """Parse a small urlencoded form without ever buffering more than the limit."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise _BadFormError("the form is too large")
    try:
        return parse_qs(
            body.decode("utf-8", errors="replace"),
            keep_blank_values=keep_blank,
            max_num_fields=200,
        )
    except ValueError as exc:
        raise _BadFormError("the form has too many fields") from exc


@router.get("/{token}", response_class=HTMLResponse)
async def page(
    token: str, request: Request, session: SessionDep, runtime: RuntimeDep, saved: bool = False
) -> Response:
    try:
        recipient = await preferences.by_token(session, runtime.settings, token, lock=False)
    except ServiceError as exc:
        return _error(request, exc)
    center = await preferences.center(session, runtime, recipient)
    return _render(request, view="center", center=center, saved=saved)


@router.post("/{token}", response_class=HTMLResponse)
async def save(token: str, request: Request, session: SessionDep, runtime: RuntimeDep) -> Response:
    try:
        # The body first: the token lookup locks the row (on SQLite, the database), and
        # a client sending its body slowly must not hold that lock.
        form = await form_fields(request)
        recipient = await preferences.by_token(session, runtime.settings, token)
    except ServiceError as exc:
        return _error(request, exc)
    if form.get("action") == ["unsubscribe_all"]:
        await preferences.unsubscribe_all(
            session, recipient, provenance=provenance(request, "hosted_page")
        )
    else:
        await preferences.save_choices(
            session,
            runtime,
            recipient,
            subscribed=set(form.get("category", [])),
            enabled_channels=set(form.get("channel", [])),
            provenance=provenance(request, "hosted_page"),
        )
    # Relative, so it stays correct behind proxies that rewrite the host.
    return RedirectResponse("?saved=true", status_code=303)


@router.get("/{token}/unsubscribe", response_class=HTMLResponse)
async def confirm_unsubscribe(
    token: str, category: CategoryQuery, request: Request, session: SessionDep, runtime: RuntimeDep
) -> Response:
    try:
        await preferences.by_token(session, runtime.settings, token, lock=False)
        known = await preferences.unsubscribable(session, category)
    except ServiceError as exc:
        return _error(request, exc)
    return _render(request, view="confirm", category=known.key, center_url=f"../{token}")


@router.post("/{token}/unsubscribe", response_class=HTMLResponse)
async def one_click_unsubscribe(
    token: str, category: CategoryQuery, request: Request, session: SessionDep, runtime: RuntimeDep
) -> Response:
    """RFC 8058 one-click target: mail clients POST ``List-Unsubscribe=One-Click`` here.
    The confirmation page's button posts here too."""
    try:
        recipient = await preferences.by_token(session, runtime.settings, token)
        unsubscribed = await preferences.unsubscribe(
            session, recipient, category, provenance=provenance(request, "one_click")
        )
    except ServiceError as exc:
        return _error(request, exc)
    return _render(request, view="done", category=unsubscribed.key, center_url=f"../{token}")
