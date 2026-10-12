# cue-client

TypeScript client for [Cue](https://github.com/murtazox04/Cue), a self-hosted
notification decision engine. It has no dependencies: it uses `fetch` and Web
Crypto, so it runs on Node 18+, Deno, Bun, Cloudflare Workers and other edge runtimes.

```bash
npm install cue-client
```

```ts
import { Cue } from "cue-client";

const cue = new Cue({ apiKey: process.env.CUE_API_KEY!, baseUrl: "https://cue.example.com" });

await cue.upsertRecipient("user-42", {
  timezone: "Europe/Berlin",
  addresses: [{ channel: "email", value: "ann@example.com" }],
});
await cue.sendEvent({
  name: "order.shipped",
  recipient: "user-42",
  data: { orderId: "1001" },
  idempotencyKey: "order-1001-shipped",
});
```

Errors throw `CueError` with the problem details Cue returns (`status`, `title`,
`detail`, `errors`). Rate limits and gateway errors are retried automatically for
requests that are safe to repeat: reads, profile updates, and sends with an idempotency
key.

## Verifying webhooks

```ts
import { InvalidSignatureError, verifyWebhook } from "cue-client";

export async function POST(request: Request) {
  const body = await request.text();
  try {
    await verifyWebhook(body, request.headers.get("Cue-Signature"), process.env.CUE_WEBHOOK_SECRET!);
  } catch (error) {
    if (error instanceof InvalidSignatureError) return new Response(null, { status: 401 });
    throw error;
  }
  const message = JSON.parse(body);
  // …send it with your provider
  return new Response(null, { status: 204 });
}
```

Verify the raw body, before parsing it. Requests signed more than five minutes ago are
rejected (`{ tolerance }` changes this).
