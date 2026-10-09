# Engagement tracking

Messages record when they were `delivered`, `opened`, `clicked` and `converted`.
Later stages imply earlier ones (a click implies delivery and opening), and the first
timestamp of each kind wins, so reporting is idempotent.

## From your backend

```http
POST /v1/messages/{message_id}/engagement
Authorization: Bearer <key with events:write>

{"type": "converted", "value": 49.90}
```

## From client apps — no API key

Every push and webhook delivery carries two fields in its data payload:
`cue_message_id` and `cue_tracking_token`. The token is a random secret unique to
the message, so apps can report engagement directly without holding an API key:

```http
POST /v1/track/{cue_tracking_token}

{"type": "opened"}
```

A token only allows recording engagement for its own message.

## Provider receipts

Providers that confirm delivery (for example Twilio status callbacks) can be bridged by a
small handler in your infrastructure that calls the engagement endpoint with
`{"type": "delivered"}`.
