"""Receive Cue's signed webhooks in Django.

``cue_webhook`` wraps a view: it checks the ``Cue-Signature`` header against the raw body,
answers 401 when it is wrong, exempts the view from CSRF (the signature replaces it) and
passes the parsed payload::

    from cue_client.django import cue_webhook

    @cue_webhook(secret=settings.CUE_WEBHOOK_SECRET)
    def send_email(request, payload):
        ...
        return HttpResponse(status=204)
"""

from __future__ import annotations

import functools
import importlib
import json
from collections.abc import Callable
from typing import Any

from cue_client.webhooks import DEFAULT_TOLERANCE, SIGNATURE_HEADER, InvalidSignatureError
from cue_client.webhooks import verify_webhook as _verify


def cue_webhook(
    secret: str, *, tolerance: int = DEFAULT_TOLERANCE
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
        http: Any = importlib.import_module("django.http")
        csrf: Any = importlib.import_module("django.views.decorators.csrf")
        methods: Any = importlib.import_module("django.views.decorators.http")

        @functools.wraps(view)
        def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                _verify(
                    request.body,
                    request.headers.get(SIGNATURE_HEADER),
                    secret,
                    tolerance=tolerance,
                )
            except InvalidSignatureError as exc:
                return http.HttpResponse(str(exc), status=401, content_type="text/plain")
            return view(request, json.loads(request.body), *args, **kwargs)

        result: Callable[..., Any] = csrf.csrf_exempt(methods.require_POST(wrapper))
        return result

    return decorate
