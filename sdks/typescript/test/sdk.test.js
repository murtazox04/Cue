import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { Cue, CueError, InvalidSignatureError, verifyWebhook } from "../dist/index.js";

const BODY = '{"id":"m1","content":{"body":"Salom"}}';
const SECRET = "s3cret";
const NOW = 1_800_000_000;
// Produced by the Python server's signer (cue_notify.channels.webhook.sign).
const SERVER_SIGNATURE =
  "t=1800000000,v1=517ac2a1764a8fd7f56c80ad34182153be2c302da57624e627a5acfd2a890b8b";

const sign = (body, secret, t) =>
  `t=${t},v1=${createHmac("sha256", secret).update(`${t}.${body}`).digest("hex")}`;

test("accepts the server's signature, as a string or bytes", async () => {
  await verifyWebhook(BODY, SERVER_SIGNATURE, SECRET, { now: NOW });
  await verifyWebhook(new TextEncoder().encode(BODY), SERVER_SIGNATURE, SECRET, { now: NOW + 5 });
});

test("rejects tampered, foreign, stale and malformed signatures", async () => {
  const cases = [
    [BODY + " ", sign(BODY, SECRET, NOW), NOW, /does not match/],
    [BODY, sign(BODY, "other", NOW), NOW, /does not match/],
    [BODY, sign(BODY, SECRET, NOW), NOW + 301, /outside the tolerance/],
    [BODY, "t=abc,v1=00", NOW, /malformed/],
    [BODY, undefined, NOW, /missing/],
  ];
  for (const [body, header, now, message] of cases) {
    await assert.rejects(verifyWebhook(body, header, SECRET, { now }), (error) => {
      assert.ok(error instanceof InvalidSignatureError);
      assert.match(error.message, message);
      return true;
    });
  }
});

function client(responses, seen = []) {
  return new Cue({
    apiKey: "ck_test",
    baseUrl: "http://cue.test/",
    fetch: async (url, init) => {
      seen.push({ url, init });
      const next = responses.shift();
      return new Response(next.body === undefined ? null : JSON.stringify(next.body), {
        status: next.status,
        headers: next.headers ?? {},
      });
    },
  });
}

test("sends events with snake_case fields and auth", async () => {
  const seen = [];
  const cue = client([{ status: 202, body: { id: "e1", status: "pending" } }], seen);
  const result = await cue.sendEvent({
    name: "order.shipped",
    recipient: "user-1",
    data: { orderId: "A1" },
    idempotencyKey: "order-A1",
  });
  assert.equal(result.id, "e1");
  assert.equal(seen[0].url, "http://cue.test/v1/events");
  assert.equal(seen[0].init.headers.Authorization, "Bearer ck_test");
  assert.deepEqual(JSON.parse(seen[0].init.body), {
    name: "order.shipped",
    recipient: "user-1",
    data: { orderId: "A1" },
    idempotency_key: "order-A1",
  });
});

test("problem details become CueError", async () => {
  const cue = client([
    { status: 422, body: { title: "Unprocessable content", detail: "bad", errors: [{ loc: [] }] } },
  ]);
  await assert.rejects(cue.sendMessage({ recipient: "u", template: "t", channels: ["push"] }), (e) => {
    assert.ok(e instanceof CueError);
    assert.equal(e.status, 422);
    assert.equal(e.detail, "bad");
    assert.equal(e.errors.length, 1);
    return true;
  });
});

test("retries only requests that are safe to repeat", async () => {
  const busy = { status: 503, headers: { "Retry-After": "0" } };
  const ok = { status: 202, body: { id: "e1" } };
  const seen = [];
  const cue = client([busy, busy, ok], seen);
  await cue.sendEvent({ name: "x", recipient: "u", idempotencyKey: "k" });
  assert.equal(seen.length, 3);

  const once = [];
  await assert.rejects(client([busy, ok], once).sendEvent({ name: "x", recipient: "u" }), CueError);
  assert.equal(once.length, 1);
});

test("ids are escaped in paths", async () => {
  const seen = [];
  await client([{ status: 200, body: {} }], seen).upsertRecipient("../api-keys");
  assert.equal(seen[0].url, "http://cue.test/v1/recipients/..%2Fapi-keys");
});

test("asks for approval once and reads the decision", async () => {
  const busy = { status: 503, headers: { "Retry-After": "0" } };
  const once = [];
  await assert.rejects(
    client([busy], once).requestApproval({ title: "Refund?", reviewers: ["lead"] }),
    CueError,
  );
  assert.equal(once.length, 1);

  const seen = [];
  const cue = client(
    [{ status: 201, body: { id: "a1", status: "pending" } }, { status: 200, body: { id: "a1", status: "approved" } }],
    seen,
  );
  await cue.requestApproval({ title: "Refund?", reviewers: ["lead"], metadata: { order: "9" } });
  assert.deepEqual(JSON.parse(seen[0].init.body), {
    title: "Refund?",
    body: "",
    reviewers: ["lead"],
    importance: "high",
    expires_in_seconds: 3600,
    metadata: { order: "9" },
  });
  assert.equal((await cue.getApproval("a1")).status, "approved");
  assert.equal(seen[1].url, "http://cue.test/v1/approvals/a1");
});
