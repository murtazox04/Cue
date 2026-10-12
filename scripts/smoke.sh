#!/usr/bin/env bash
# Walk through docs/getting-started.md against a running Cue and fail loudly if any
# step does not behave as documented. Usage: CUE_API_KEY=ck_… scripts/smoke.sh [base url]
set -euo pipefail

URL="${1:-http://localhost:8000}"
: "${CUE_API_KEY:?set CUE_API_KEY to an admin API key}"
AUTH=(-H "Authorization: Bearer $CUE_API_KEY")
JSON=(-H "Content-Type: application/json")

post() {  # post <path> <expected status> <body>
  local status
  status=$(curl -s -o /tmp/cue-smoke.json -w '%{http_code}' "$URL$1" "${AUTH[@]}" "${JSON[@]}" -d "$3")
  if [ "$status" != "$2" ]; then
    echo "POST $1: expected $2, got $status: $(cat /tmp/cue-smoke.json)" >&2
    exit 1
  fi
}

post /v1/templates 201 '{
  "key": "order-shipped", "default_locale": "en",
  "locales": {
    "en": {"title": "Your order is on its way",
           "body": "Order {{ data.order_id }} ships with {{ data.carrier }}.",
           "url": "https://shop.example/orders/{{ data.order_id }}",
           "channels": {"sms": {"body": "Order {{ data.order_id }} shipped."}}},
    "uz": {"title": "Buyurtmangiz yo‘lda",
           "body": "{{ data.order_id }} buyurtmangiz {{ data.carrier }} orqali jo‘natildi."}
  }
}'
post /v1/rules 201 '{
  "key": "notify-order-shipped", "name": "Tell customers their order shipped",
  "event": "order.shipped", "template": "order-shipped",
  "category": "transactional", "channels": ["push", "sms"]
}'
EVENT='{
  "name": "order.shipped", "idempotency_key": "order-1001-shipped",
  "recipient": {"external_id": "customer-42", "locale": "uz", "timezone": "Asia/Tashkent",
                "addresses": [{"channel": "push", "value": "device-token-abc"}]},
  "data": {"order_id": "1001", "carrier": "UzPost"}
}'
post /v1/events 202 "$EVENT"
post /v1/events 200 "$EVENT"  # the same idempotency key is a duplicate
grep -q '"duplicate":true' /tmp/cue-smoke.json

for _ in $(seq 30); do
  status=$(curl -s "$URL/v1/messages?recipient=customer-42" "${AUTH[@]}" \
    | python3 -c 'import json, sys; items = json.load(sys.stdin)["items"]; print(items[0]["status"] if items else "none")')
  [ "$status" = "sent" ] && break
  sleep 1
done
if [ "$status" != "sent" ]; then
  echo "message status is $status, expected sent" >&2
  exit 1
fi

post /v1/rules/evaluate 200 '{"name": "order.shipped", "recipient": "customer-42",
  "data": {"order_id": "1", "carrier": "X"}}'
grep -q '"matched":true' /tmp/cue-smoke.json
echo "getting-started walkthrough: OK"
