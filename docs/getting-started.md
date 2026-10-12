# Getting started

This walk-through runs Cue locally, sends a notification through a rule, shows it
arriving, and shows how to see what happened. The `console` channel logs messages
instead of sending them, so no third-party accounts are needed.

## 1. Run Cue

=== "Docker Compose"

    PostgreSQL, a migration job, the API and a separate worker:

    ```bash
    git clone https://github.com/murtazox04/Cue && cd Cue
    docker compose up --build -d
    docker compose exec api cuectl keys create admin
    ```

    Messages are delivered by the `worker` service. Follow its log in a second terminal;
    the notification from step 4 appears there:

    ```bash
    docker compose logs -f worker
    ```

=== "Python"

    Python 3.12 or newer, SQLite in `./cue.db`, and the worker running inside the API
    process. Install into a virtual environment:

    ```bash
    python -m venv .venv && . .venv/bin/activate
    pip install cue-notify
    ```

    or as a tool with [uv](https://docs.astral.sh/uv/): `uv tool install cue-notify`.
    Then, in the directory where `cue.db` should live:

    ```bash
    export CUE_WORKER__EMBEDDED=true
    export CUE_CHANNELS__PUSH__PROVIDER=console CUE_CHANNELS__SMS__PROVIDER=console
    cuectl db upgrade
    cuectl keys create admin
    cuectl serve
    ```

    `cuectl serve` listens on `127.0.0.1:8000` and runs jobs in the same process; the
    notification from step 4 appears in its output. Run the `curl` commands below in a
    second terminal.

=== "uvx"

    Nothing to install besides [uv](https://docs.astral.sh/uv/):

    ```bash
    CUE_WORKER__EMBEDDED=true CUE_CHANNELS__PUSH__PROVIDER=console \
      CUE_CHANNELS__SMS__PROVIDER=console uvx cue-notify serve --migrate
    ```

    In a second terminal, in the same directory (both commands use `./cue.db`):

    ```bash
    uvx cue-notify keys create admin
    ```

`keys create` prints an API key once. Keep it in your shell:

```bash
export CUE_API_KEY=ck_…
```

Interactive API docs are now at <http://localhost:8000/docs>.

## 2. Create a template

Templates are [Jinja2](https://jinja.palletsprojects.com/). `data` holds the event
payload, `recipient` the person's profile and `event` the event itself; see
[templates](concepts/templates.md) for the rest.

```bash
curl -X POST localhost:8000/v1/templates \
  -H "Authorization: Bearer $CUE_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "key": "order-shipped",
    "default_locale": "en",
    "locales": {
      "en": {
        "title": "Your order is on its way",
        "body": "Order {{ data.order_id }} ships with {{ data.carrier }}.",
        "url": "https://shop.example/orders/{{ data.order_id }}",
        "channels": {"sms": {"body": "Order {{ data.order_id }} shipped."}}
      },
      "uz": {
        "title": "Buyurtmangiz yo‘lda",
        "body": "{{ data.order_id }} buyurtmangiz {{ data.carrier }} orqali jo‘natildi."
      }
    }
  }'
```

## 3. Create a rule

A rule says *when this event happens (and this condition holds), send this template on
these channels*.

```bash
curl -X POST localhost:8000/v1/rules \
  -H "Authorization: Bearer $CUE_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "key": "notify-order-shipped",
    "name": "Tell customers their order shipped",
    "event": "order.shipped",
    "template": "order-shipped",
    "category": "transactional",
    "channels": ["push", "sms"]
  }'
```

`channels` is a fallback order: push first, SMS if push is impossible or fails
permanently.

## 4. Send an event

Your backend reports what happened. The recipient can be referenced by id or created
inline, with their devices:

```bash
curl -X POST localhost:8000/v1/events \
  -H "Authorization: Bearer $CUE_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "order.shipped",
    "idempotency_key": "order-1001-shipped",
    "recipient": {
      "external_id": "customer-42",
      "locale": "uz",
      "timezone": "Asia/Tashkent",
      "addresses": [{"channel": "push", "value": "device-token-abc"}]
    },
    "data": {"order_id": "1001", "carrier": "UzPost"}
  }'
```

The response is `202 Accepted`; processing happens in the background. About a second
later the console channel prints the notification in the worker's log
(`docker compose logs -f worker`, or the terminal running `cuectl serve`):

```text
INFO    cue_notify.channels.console: [push → device-token-abc] Buyurtmangiz yo‘lda — 1001 buyurtmangiz UzPost orqali jo‘natildi. message_id=…
INFO    cue_notify.services.delivery: message sent message_id=… channel=push
```

The message is in Uzbek because the recipient's `locale` is `uz`. The `transactional`
category ignores quiet hours, so it goes out at once whatever the time in Tashkent.

Retrying the same request returns `200` with `"duplicate": true` and sends nothing;
always set `idempotency_key` in production. Inline `addresses` need the
`recipients:write` scope, which the admin key has; see
[events](concepts/events-and-rules.md#events).

## 5. See what happened

```bash
curl "localhost:8000/v1/events/<event id>" -H "Authorization: Bearer $CUE_API_KEY"
curl "localhost:8000/v1/messages?recipient=customer-42" -H "Authorization: Bearer $CUE_API_KEY"
```

The event's `outcome` lists every rule that was considered and why it did or did not
fire. Each message shows its `status` (`queued`, `sending`, `sent`, `delivered`,
`failed`, `suppressed`, `cancelled`, `pending_approval`) and a `status_reason`; every
value is explained in [statuses and reasons](reference/statuses.md). Only `queued`
messages can be cancelled; once a worker claims one it is `sending`.

To test a rule change without sending anything:

```bash
curl -X POST localhost:8000/v1/rules/evaluate \
  -H "Authorization: Bearer $CUE_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "order.shipped", "recipient": "customer-42", "data": {"order_id": "1", "carrier": "X"}}'
```

## 6. Optional: deliver to your own service

Swap the console for the `webhook` connector to see what your sending service would
receive. Save this receiver as `receiver.py`; it checks the signature and prints the
message:

```python
import hashlib
import hmac
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["CUE_CHANNELS__PUSH__SECRET"].encode()


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        signature = dict(part.split("=", 1) for part in self.headers["Cue-Signature"].split(","))
        signed = f"{signature['t']}.".encode() + body
        expected = hmac.new(SECRET, signed, hashlib.sha256).hexdigest()
        print("Cue-Signature:", self.headers["Cue-Signature"])
        print("valid:", hmac.compare_digest(expected, signature["v1"]))
        print(body.decode(), flush=True)
        self.send_response(204)
        self.end_headers()


HTTPServer(("127.0.0.1", 9100), Receiver).serve_forever()
```

Stop `cuectl serve`, then pick a secret, start the receiver and start Cue again with the
push route pointing at it (Python path shown; with Compose, put the variables in `.env`
and use an address the worker container can reach):

```bash
export CUE_CHANNELS__PUSH__SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
python receiver.py &
export CUE_CHANNELS__PUSH__PROVIDER=webhook CUE_CHANNELS__PUSH__URL=http://127.0.0.1:9100/push
cuectl serve
```

Send the event from step 4 again with a new `idempotency_key`. The receiver prints:

```text
Cue-Signature: t=1791773920,v1=3717d6a9…
valid: True
{"id":"01a12e0c-…","channel":"push","address":"device-token-abc","locale":"uz","importance":"normal","expires_at":null,"content":{"title":"Buyurtmangiz yo‘lda","body":"1001 buyurtmangiz UzPost orqali jo‘natildi.","data":{},"actions":[]},"metadata":{"cue_message_id":"01a12e0c-…","cue_tracking_token":"…"},"links":{}}
```

In production, verify with the SDKs' `verify_webhook` / `verifyWebhook`, which also
reject old timestamps, and deduplicate on the `Idempotency-Key` header. The payload and
signature are described in [channels](concepts/channels.md#webhook-http).

## Next

- Plug in real delivery: point a route at your own service with the
  [webhook connector](concepts/channels.md), or use a bundled one.
- Turn on [preference and unsubscribe links](concepts/preferences.md) with
  `api.public_url` and `api.secret_key`.
- Learn how [delivery policy](concepts/policy.md) protects your users' attention.
- Send a [direct message](reference/api.md#messages) for OTPs and receipts.
- Before production, read [deployment](guides/deployment.md).
