/**
 * How the captured chart images are actually loaded.
 *
 * The obvious implementation — `<img src={`${API_BASE}/replay/screenshots/${id}/image`}>`
 * — does not work, and fails in a way that is nearly invisible. The browser issues that
 * request itself and there is no hook to attach the bearer token, so the API answers 401.
 * Because the response is cross-origin and carries a JSON body, Opaque Response Blocking
 * discards it before `onerror` fires: no broken-image icon with a useful cause, no error
 * in the UI, just `ERR_BLOCKED_BY_ORB` in the console and an empty gallery. The API log
 * shows six unremarkable 401s.
 *
 * It also renders perfectly in any environment where auth is disabled, which is every
 * local dev setup — so the first place it can fail is a deployment with real users.
 *
 * The fix is to fetch the bytes through the API client, which attaches credentials, and
 * render an object URL. These tests pin that: the request must carry auth, and the object
 * URL must be revoked so a scrolled-through gallery does not retain every decoded chart.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Screenshots } from "@/components/replay/screenshots";

const TRADE = "019fba3d-b94e-70b7-8604-4b8e11e74fee";
const SHOT = "019fbc59-2542-758e-9676-4c4575e11a94";

const listing = {
  items: [
    {
      id: SHOT,
      kind: "before_entry",
      timeframe: "2m",
      content_type: "image/png",
      width: 960,
      height: 540,
      byte_size: 5582,
      captured_at: "2026-08-01T08:03:06.492366+00:00",
      source: "auto",
    },
  ],
};

function renderGallery() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Screenshots tradeId={TRADE} />
    </QueryClientProvider>,
  );
}

let revoked: string[];

beforeEach(() => {
  revoked = [];
  vi.stubEnv("NEXT_PUBLIC_DEV_USER", "demo");
  // jsdom implements neither, and the component's correctness is entirely about them.
  URL.createObjectURL = vi.fn(() => "blob:mock/chart-1");
  URL.revokeObjectURL = vi.fn((url: string) => void revoked.push(url));

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.endsWith("/image")) {
        // A byte array rather than a `Blob`: jsdom's `Blob` has no `.stream()`, so
        // `Response.blob()` throws on it — a harness quirk, not a browser one.
        return new Response(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), {
          status: 200,
          headers: { "Content-Type": "image/png" },
        });
      }
      return new Response(JSON.stringify(listing), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("the chart gallery", () => {
  it("sends credentials with the image request", async () => {
    renderGallery();

    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls;
      const image = calls.find(([url]) => String(url).endsWith("/image"));
      expect(image, "no image request was made").toBeDefined();

      // The assertion that a bare `<img src>` cannot satisfy. Without it the request is
      // anonymous, the API returns 401, and the gallery is silently empty.
      const headers = (image![1] as RequestInit).headers as Record<string, string>;
      expect(headers["X-Debug-User"]).toBe("demo");
    });
  });

  it("renders the image from the fetched bytes, not from the endpoint URL", async () => {
    renderGallery();

    const image = await screen.findByRole("img", { name: /before entry/i });
    expect(image.getAttribute("src")).toBe("blob:mock/chart-1");
    expect(image.getAttribute("src")).not.toContain("/replay/screenshots");
  });

  it("revokes the object URL on unmount", async () => {
    const view = renderGallery();
    await screen.findByRole("img", { name: /before entry/i });

    view.unmount();

    // Without this the decoded PNG is retained for the life of the document — for a
    // gallery a trader scrolls, every chart they have ever opened.
    await waitFor(() => expect(revoked).toContain("blob:mock/chart-1"));
  });

  it("says an image failed rather than showing a blank frame", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/image")) {
          return new Response(
            JSON.stringify({ error: { code: "not_found", message: "screenshot not found" } }),
            { status: 404, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify(listing), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );

    renderGallery();

    expect(await screen.findByText(/screenshot not found/i)).toBeInTheDocument();
  });
});
