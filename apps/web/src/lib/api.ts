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

/** Query keys, centralised so an invalidation cannot miss a cache by typo. */
export const keys = {
  trades: (filters?: Record<string, unknown>) => ["trades", filters ?? {}] as const,
  trade: (id: string) => ["trade", id] as const,
  analytics: (accountId?: string) => ["analytics", accountId ?? "all"] as const,
  patterns: (accountId?: string) => ["patterns", accountId ?? "all"] as const,
  reports: (type?: string) => ["reports", type ?? "all"] as const,
  models: (head: string) => ["models", head] as const,
  jobs: (state?: string) => ["jobs", state ?? "all"] as const,
  queue: () => ["jobs", "queue"] as const,
};
