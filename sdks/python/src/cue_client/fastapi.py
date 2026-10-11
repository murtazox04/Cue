"""Receive Cue's signed webhooks in FastAPI.

``CueWebhook`` is a dependency that checks the ``Cue-Signature`` header against the raw
body and returns the parsed payload, or answers 401::

    from fastapi import Depends, FastAPI
    from cue_client.fastapi import CueWebhook

    app = FastAPI()
    deliveries = CueWebhook(secret=os.environ["CUE_WEBHOOK_SECRET"])

    @app.post("/internal/cue/email")
    async def send_email(payload: dict = Depends(deliveries)):
        ...

Use it for delivery webhooks and for approval decisions (``approvals.callback_url``).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request

from cue_client.webhooks import DEFAULT_TOLERANCE, SIGNATURE_HEADER, InvalidSignatureError
from cue_client.webhooks import verify_webhook as _verify


class CueWebhook:
    """A FastAPI dependency for one webhook secret."""

    def __init__(self, secret: str, *, tolerance: int = DEFAULT_TOLERANCE) -> None:
        self.secret = secret
        self.tolerance = tolerance

    async def __call__(self, request: Request) -> dict[str, Any]:
        body: bytes = await request.body()
        try:
            _verify(
                body,
                request.headers.get(SIGNATURE_HEADER),
                self.secret,
                tolerance=self.tolerance,
            )
        except InvalidSignatureError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        payload: dict[str, Any] = json.loads(body)
        return payload
