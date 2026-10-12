/**
 * TypeScript client for Cue: send events and messages, and verify Cue's signed webhooks.
 *
 * No dependencies: it uses `fetch` and Web Crypto, so it runs on Node 18+, Deno, Bun,
 * Cloudflare Workers and in browsers (keep API keys on the server).
 */

export const VERSION = "0.3.0";
export const SIGNATURE_HEADER = "Cue-Signature";

export type Importance = "low" | "normal" | "high" | "critical";
export type Json = Record<string, unknown>;

export interface Address {
  channel: string;
  value: string;
  label?: string;
}

export interface RecipientProfile {
  locale?: string;
  timezone?: string;
  attributes?: Json;
  addresses?: Address[];
}

export interface EventInput {
  name: string;
  /** External id, or an inline profile `{ external_id, … }`. */
  recipient: string | (RecipientProfile & { external_id: string });
  data?: Json;
  idempotencyKey?: string;
  occurredAt?: Date;
}

export interface MessageInput {
  recipient: string;
  template: string;
  channels: string[];
  data?: Json;
  category?: string;
  importance?: Importance;
  expiresAt?: Date;
  sendAt?: Date;
  idempotencyKey?: string;
}

export type Feedback = "delivered" | "bounce" | "soft_bounce" | "complaint";

export interface FeedbackInput {
  type: Feedback;
  channel: string;
  address?: string;
  messageId?: string;
  providerMessageId?: string;
  reportId?: string;
  reason?: string;
}

export interface ApprovalInput {
  /** What needs a decision. */
  title: string;
  /** External ids of the people who may decide. */
  reviewers: string[];
  body?: string;
  importance?: Importance;
  /** Undecided requests expire after this (default 3600). */
  expiresInSeconds?: number;
  /** Returned with the decision, e.g. the id of the paused workflow. */
  metadata?: Json;
}

export interface CueOptions {
  apiKey: string;
  baseUrl?: string;
  /** Request timeout in milliseconds. */
  timeout?: number;
  /** Retries for rate limits and gateway errors on requests that are safe to repeat. */
  retries?: number;
  fetch?: typeof fetch;
}

/** An error response from Cue (an RFC 9457 problem document). */
export class CueError extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
    readonly detail: string,
    readonly errors: unknown[] = [],
  ) {
    super(`${status} ${title}: ${detail}`);
    this.name = "CueError";
  }
}

const RETRY_STATUSES = new Set([429, 502, 503, 504]);

function compact(values: Json): Json {
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value !== undefined));
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

function withoutTrailingSlashes(url: string): string {
  let end = url.length;
  while (end > 0 && url[end - 1] === "/") end -= 1;
  return url.slice(0, end);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class Cue {
  readonly #apiKey: string;
  readonly #baseUrl: string;
  readonly #timeout: number;
  readonly #retries: number;
  readonly #fetch: typeof fetch;

  constructor(options: CueOptions) {
    this.#apiKey = options.apiKey;
    this.#baseUrl = withoutTrailingSlashes(options.baseUrl ?? "http://127.0.0.1:8000");
    this.#timeout = options.timeout ?? 10_000;
    this.#retries = options.retries ?? 2;
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  /** Report that something happened; Cue's rules decide what to send. */
  sendEvent(event: EventInput): Promise<Json> {
    const body = compact({
      name: event.name,
      recipient: event.recipient,
      data: event.data ?? {},
      idempotency_key: event.idempotencyKey,
      occurred_at: event.occurredAt?.toISOString(),
    });
    return this.#request("POST", "/v1/events", body, event.idempotencyKey !== undefined);
  }

  /** Send a template directly. Check `status`: it may be `suppressed`. */
  sendMessage(message: MessageInput): Promise<Json> {
    const body = compact({
      recipient: message.recipient,
      template: message.template,
      channels: message.channels,
      data: message.data ?? {},
      category: message.category ?? "transactional",
      importance: message.importance ?? "normal",
      expires_at: message.expiresAt?.toISOString(),
      send_at: message.sendAt?.toISOString(),
      idempotency_key: message.idempotencyKey,
    });
    return this.#request("POST", "/v1/messages", body, message.idempotencyKey !== undefined);
  }

  upsertRecipient(externalId: string, profile: RecipientProfile = {}): Promise<Json> {
    return this.#request("PUT", `/v1/recipients/${segment(externalId)}`, compact({ ...profile }), true);
  }

  getMessage(id: string): Promise<Json> {
    return this.#request("GET", `/v1/messages/${segment(id)}`, undefined, true);
  }

  /** Filters: `recipient`, `status`, `event_id`, `agent`, `limit`, `cursor`. */
  listMessages(filters: Record<string, string | number> = {}): Promise<Json> {
    const query = new URLSearchParams(
      Object.entries(filters).map(([key, value]) => [key, String(value)]),
    ).toString();
    return this.#request("GET", `/v1/messages${query ? `?${query}` : ""}`, undefined, true);
  }

  /** Ask people to approve or reject an action. Never retried: it notifies people. */
  requestApproval(approval: ApprovalInput): Promise<Json> {
    const body = {
      title: approval.title,
      body: approval.body ?? "",
      reviewers: approval.reviewers,
      importance: approval.importance ?? "high",
      expires_in_seconds: approval.expiresInSeconds ?? 3600,
      metadata: approval.metadata ?? {},
    };
    return this.#request("POST", "/v1/approvals", body, false);
  }

  reportFeedback(feedback: FeedbackInput): Promise<Json> {
    const body: Json = { type: feedback.type, channel: feedback.channel };
    if (feedback.address !== undefined) body.address = feedback.address;
    if (feedback.messageId !== undefined) body.message_id = feedback.messageId;
    if (feedback.providerMessageId !== undefined) {
      body.provider_message_id = feedback.providerMessageId;
    }
    if (feedback.reportId !== undefined) body.report_id = feedback.reportId;
    if (feedback.reason !== undefined) body.reason = feedback.reason;
    return this.#request("POST", "/v1/feedback", body, false);
  }

  /** `status` is `pending`, `approved`, `rejected` or `expired`. */
  getApproval(id: string): Promise<Json> {
    return this.#request("GET", `/v1/approvals/${segment(id)}`, undefined, true);
  }

  async #request(method: string, path: string, body: Json | undefined, safe: boolean): Promise<Json> {
    const attempts = safe ? this.#retries + 1 : 1;
    let response = await this.#send(method, path, body);
    for (let attempt = 1; attempt < attempts && RETRY_STATUSES.has(response.status); attempt++) {
      const retryAfter = Number(response.headers.get("Retry-After"));
      await sleep(Number.isFinite(retryAfter) && retryAfter > 0
        ? Math.min(retryAfter, 30) * 1000
        : Math.min(500 * 2 ** (attempt - 1), 8000));
      response = await this.#send(method, path, body);
    }
    const text = await response.text();
    const payload = text ? (JSON.parse(text) as Json) : {};
    if (!response.ok) {
      throw new CueError(
        response.status,
        String(payload.title ?? response.statusText),
        String(payload.detail ?? text.slice(0, 300)),
        Array.isArray(payload.errors) ? payload.errors : [],
      );
    }
    return payload;
  }

  #send(method: string, path: string, body: Json | undefined): Promise<Response> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.#apiKey}`,
      "User-Agent": `cue-client-js/${VERSION}`,
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    return this.#fetch(`${this.#baseUrl}${path}`, {
      method,
      headers,
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(this.#timeout),
    });
  }
}

/** The request was not signed by Cue with this secret, or is too old. */
export class InvalidSignatureError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvalidSignatureError";
  }
}

export interface VerifyOptions {
  /** Accepted clock difference in seconds (default 300). */
  tolerance?: number;
  /** Current time in seconds, for tests. */
  now?: number;
}

const encoder = new TextEncoder();

function toBytes(body: string | Uint8Array): Uint8Array {
  return typeof body === "string" ? encoder.encode(body) : body;
}

function hex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}

/**
 * Throw `InvalidSignatureError` unless `signature` (the `Cue-Signature` header) is a valid,
 * fresh signature of `body`. Pass the raw body exactly as received, before parsing it.
 */
export async function verifyWebhook(
  body: string | Uint8Array,
  signature: string | null | undefined,
  secret: string,
  options: VerifyOptions = {},
): Promise<void> {
  if (!signature) throw new InvalidSignatureError("missing Cue-Signature header");
  const parts = new Map(
    signature.split(",").flatMap((part) => {
      const index = part.indexOf("=");
      return index > 0 ? [[part.slice(0, index), part.slice(index + 1)] as const] : [];
    }),
  );
  const timestamp = Number(parts.get("t"));
  const candidate = parts.get("v1");
  if (!Number.isInteger(timestamp) || !candidate) {
    throw new InvalidSignatureError("malformed Cue-Signature header");
  }
  const now = options.now ?? Date.now() / 1000;
  if (Math.abs(now - timestamp) > (options.tolerance ?? 300)) {
    throw new InvalidSignatureError("signature timestamp is outside the tolerance");
  }
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const payload = new Uint8Array([...encoder.encode(`${timestamp}.`), ...toBytes(body)]);
  const expected = hex(await crypto.subtle.sign("HMAC", key, payload));
  if (!constantTimeEqual(expected, candidate)) {
    throw new InvalidSignatureError("signature does not match");
  }
}
