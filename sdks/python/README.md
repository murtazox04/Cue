# cue-client

Python client for [Cue](https://github.com/murtazox04/Cue), the attention layer between
software, AI agents and people. Its only dependency is `httpx`.

```bash
pip install cue-client
```

```python
from cue_client import Cue

with Cue(api_key="ck_…", base_url="https://cue.example.com") as cue:
    cue.upsert_recipient(
        "user-42",
        timezone="Europe/Berlin",
        addresses=[{"channel": "email", "value": "ann@example.com"}],
    )
    cue.send_event(
        "order.shipped", "user-42", {"order_id": "1001"}, idempotency_key="order-1001-shipped"
    )
```

`AsyncCue` has the same methods for `asyncio`. Errors raise `CueError` with the problem
details Cue returns (`status`, `title`, `detail`, `errors`). Rate limits and gateway
errors are retried automatically for requests that are safe to repeat: reads, profile
updates, and sends with an idempotency key.

## Verifying webhooks

If Cue delivers to your service through its webhook connector, check every request
before trusting it:

```python
from cue_client import InvalidSignatureError, verify_webhook


def handle(request):
    try:
        verify_webhook(request.body, request.headers.get("Cue-Signature"), secret=WEBHOOK_SECRET)
    except InvalidSignatureError:
        return 401
    message = json.loads(request.body)
    ...
```

Verify the raw body bytes, before parsing them. Requests signed more than five minutes
ago are rejected (`tolerance=` changes this).
