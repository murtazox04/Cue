# Getting started

This walk-through runs Cue locally, sends a notification through a rule, and shows
how to see what happened. The `console` channel logs messages instead of sending them,
so no third-party accounts are needed.

## 1. Run Cue

=== "Docker Compose"

    ```bash
    git clone https://github.com/murtazox04/Cue && cd cue
    docker compose up --build -d
    docker compose exec api cuectl keys create admin
    ```

=== "Python (uv)"

    ```bash
    pip install 'cue-notify[postgres]'   # or: uv tool install cue-notify
    export CUE_WORKER__EMBEDDED=true       # run jobs inside the API process
    export CUE_CHANNELS__PUSH__PROVIDER=console
    cuectl db upgrade                         # SQLite at ./cue.db by default
    cuectl keys create admin
    cuectl serve
    ```

`keys create` prints an API key once — keep it:

```bash
export KEY=ck_…
```

Interactive API docs are now at <http://localhost:8000/docs>.

## 2. Create a template

Templates are [Jinja2](https://jinja.palletsprojects.com/) with three variables:
`data` (the event payload), `recipient` and `event`.

```bash
curl -X POST localhost:8000/v1/templates \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
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
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
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
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
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

The response is `202 Accepted`; processing happens in the background. In the logs:

```text
[push → device-token-abc] Buyurtmangiz yo‘lda — 1001 buyurtmangiz UzPost orqali jo‘natildi.
```

Retrying the same request returns `200` with `"duplicate": true` and sends nothing —
always set `idempotency_key` in production.

## 5. See what happened

```bash
curl "localhost:8000/v1/events/<event id>" -H "Authorization: Bearer $KEY"
curl "localhost:8000/v1/messages?recipient=customer-42" -H "Authorization: Bearer $KEY"
```

The event's `outcome` lists every rule that was considered and why it did or did not
fire. Each message shows its `status` (`queued`, `sending`, `sent`, `delivered`,
`failed`, `suppressed`, `cancelled`) and a `status_reason`. Only `queued` messages can be
cancelled; once a worker claims one it is `sending`.

To test a rule change without sending anything:

```bash
curl -X POST localhost:8000/v1/rules/evaluate \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"name": "order.shipped", "recipient": "customer-42", "data": {"order_id": "1", "carrier": "X"}}'
```

## Next

- Plug in real delivery: point a route at your own service with the
  [webhook connector](concepts/channels.md), or use a bundled one.
- Learn how [delivery policy](concepts/policy.md) protects your users' attention.
- Send a [direct message](reference/api.md#messages) for OTPs and receipts.
