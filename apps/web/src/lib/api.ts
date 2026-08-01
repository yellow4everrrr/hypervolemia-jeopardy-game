/**
 * The typed API client.
 *
 * One shape of error for everything. The backend returns `{error: {code, message,
 * details}}` for every failure including rate limits, so the client raises one exception
 * type carrying all three rather than letting each caller re-derive "what went wrong" from
 * a status code.
 *
 * **Nothing here parses a decimal into a number.** Money and probabilities stay strings
 * from Python's `Decimal` all the way to `Intl.NumberFormat` at the point of render. A
 * `JSON.parse` reviver that coerced them would undo, in one line, the reason the backend
 * uses exact arithmetic at all.
 */

import type { ApiErrorBody } from "@/types/evidence";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody | null, fallback: string) {
    super(body?.error?.message ?? fallback);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.error?.code ?? "unknown";
    this.details = body?.error?.details ?? {};
  }

  /** Whether retrying unchanged could plausibly succeed. */
  get isTransient(): boolean {
    return this.status === 429 || this.status >= 500;
  }

  /** Seconds to wait, when the server said. */
  get retryAfter(): number | null {
    const value = this.details["retry_after_seconds"];
    return typeof value === "number" ? value : null;
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  token?: string | null;
  signal?: AbortSignal;
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, token, signal } = options;

  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;

  // Mirrors the backend's `auth_dev_bypass`, which `get_settings` refuses to enable in
  // production. Lets the app run end to end locally without provisioning Clerk; sent to
  // a deployment with the bypass off it is simply ignored, and the request fails
  // authentication exactly as an unauthenticated one should.
  const devUser = process.env.NEXT_PUBLIC_DEV_USER;
  if (devUser && !token) headers["X-Debug-User"] = devUser;

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    // A body is not guaranteed — a proxy 502 is not JSON — so parsing failure must not
    // replace the real status with a confusing one.
    let parsed: ApiErrorBody | null = null;
    try {
      parsed = (await response.json()) as ApiErrorBody;
    } catch {
      parsed = null;
    }
    throw new ApiError(response.status, parsed, response.statusText);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * Fetch a binary endpoint with the same credentials `request` uses, as an object URL.
 *
 * Needed because **an `<img src>` cannot carry an `Authorization` header.** The browser
 * issues that request itself, with no hook to add one, so a private image endpoint
 * behind bearer auth returns 401 and the tag renders broken — which is exactly what the
 * screenshot gallery did: six `ERR_BLOCKED_BY_ORB` failures, because a cross-origin
 * `<img>` that receives a JSON error body is discarded by Opaque Response Blocking
 * before it ever reaches an `onerror` handler. Nothing appeared in the API logs beyond
 * six ordinary 401s.
 *
 * The alternative is a presigned bucket URL, which needs no header — and is a capability
 * that outlives the session, travels in a referrer, and cannot be revoked. For images of
 * a trader's positions, fetching the bytes and holding a blob URL is the better trade.
 *
 * The caller owns the returned URL and must `URL.revokeObjectURL` it, or the blob stays
 * alive for the life of the document.
 */
export async function requestObjectUrl(
  path: string,
  options: Pick<RequestOptions, "token" | "signal"> = {},
): Promise<string> {
  const headers: Record<string, string> = {};
  if (options.token) headers["Authorization"] = `Bearer ${options.token}`;
  const devUser = process.env.NEXT_PUBLIC_DEV_USER;
  if (devUser && !options.token) headers["X-Debug-User"] = devUser;

  const response = await fetch(`${API_BASE}${path}`, {
    headers,
    signal: options.signal,
  });

  if (!response.ok) {
    let parsed: ApiErrorBody | null = null;
    try {
      parsed = (await response.json()) as ApiErrorBody;
    } catch {
      parsed = null;
    }
    throw new ApiError(response.status, parsed, response.statusText);
  }

  return URL.createObjectURL(await response.blob());
}

/** Query keys, centralised so an invalidation cannot miss a cache by typo. */
export const keys = {
  trades: (filters?: Record<string, unknown>) => ["trades", filters ?? {}] as const,
  trade: (id: string) => ["trade", id] as const,
  analytics: (accountId?: string) => ["analytics", accountId ?? "all"] as const,
  patterns: (accountId?: string) => ["patterns", accountId ?? "all"] as const,
  reports: (type?: string) => ["reports", type ?? "all"] as const,
  models: (head: string) => ["models", head] as const,
  jobs: (state?: string) => ["jobs", state ?? "all"] as const,
  strategies: () => ["strategies"] as const,
  queue: () => ["jobs", "queue"] as const,
};
