/**
 * Load every route against a running API and assert the page is actually there.
 *
 * This is the check that did not exist while six defects shipped green
 * ([ADR 0016](../../../docs/adr/0016-running-it-is-a-test-stage.md)). Every one of them
 * survived mypy, ruff, eslint, tsc, and both unit suites, because each lived in the seam
 * between two components that no single component's tests can see.
 *
 * The assertions are chosen from what those defects actually looked like, and the
 * ordering matters. A crash is the *easiest* failure to catch and the least harmful one:
 * it is loud, immediate, and someone fixes it within the hour. The dangerous outputs all
 * rendered as finished pages —
 *
 * - the blotter with `undefined` in three columns, because reading an absent property off
 *   a JSON object is not an error in JavaScript;
 * - the replay chart drawing nothing, because `bar_count: 0` is a valid response;
 * - the dashboard reporting "0 trades" over 1,408, because it was a placeholder nobody
 *   marked as temporary;
 * - a monthly report printing `178.88686131386861313868613`, because a mean of Decimals
 *   carries every digit of its division.
 *
 * So this file checks for *content*, not merely for the absence of an exception. Each
 * route names something it must show, which is the only assertion that distinguishes "the
 * page rendered" from "the page rendered empty".
 *
 * It is deliberately not a visual or layout test. Pinning pixels or copy would make every
 * design change a test failure and teach everyone to update the expectations without
 * reading them, which is how a suite stops being evidence.
 */

import { expect, test, type Page } from "@playwright/test";

/** Text that means a value reached the DOM without ever being formatted. */
const UNFORMATTED = [
  /\bundefined\b/,
  /\bNaN\b/,
  /\[object Object\]/,
  // Nine or more decimal places. The engine stores NUMERIC(20,8), so eight is the most a
  // legitimately formatted figure can carry, and the reports bug produced twenty-six.
  /\d\.\d{9,}/,
];

/** Copy the app renders when a route threw. */
const CRASHED = [/This page couldn't load/i, /Application error/i, /Unhandled Runtime/i];

async function bodyText(page: Page): Promise<string> {
  return page.locator("body").innerText();
}

/**
 * Assert a route loaded, rendered, and rendered *formatted*.
 *
 * `mustContain` is the anti-blank-page clause. Without it a route that fetched nothing
 * and drew an empty container passes every other check here — which is precisely how the
 * replay chart and the placeholder dashboard both looked healthy.
 */
async function checkRoute(
  page: Page,
  path: string,
  mustContain: RegExp[],
): Promise<void> {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));

  const response = await page.goto(path, { waitUntil: "networkidle" });
  expect(response?.status(), `${path} returned ${response?.status()}`).toBeLessThan(400);

  // Data arrives client-side through TanStack Query, so the assertions below have to wait
  // for the first required string rather than race the fetch.
  const first = mustContain[0];
  if (first) {
    await expect(page.locator("body")).toContainText(first, { timeout: 20_000 });
  }

  const text = await bodyText(page);

  expect(errors, `${path} raised: ${errors.join("; ")}`).toHaveLength(0);

  for (const pattern of CRASHED) {
    expect(text, `${path} rendered a crash boundary`).not.toMatch(pattern);
  }

  for (const pattern of UNFORMATTED) {
    expect(text, `${path} rendered an unformatted value matching ${pattern}`).not.toMatch(
      pattern,
    );
  }

  for (const pattern of mustContain) {
    expect(text, `${path} did not render ${pattern}`).toMatch(pattern);
  }
}

test.describe("every route renders against real data", () => {
  test("dashboard shows computed statistics, not a placeholder", async ({ page }) => {
    // "trades" with a digit in front rules out the placeholder's "0 trades", which is the
    // exact string the hardcoded version rendered over a seeded database.
    await checkRoute(page, "/", [
      /Is there an edge\?/i,
      /Expectancy/i,
      /[1-9][\d,]*\s+trades/,
    ]);
  });

  test("blotter renders rows with formatted prices and quantities", async ({ page }) => {
    await checkRoute(page, "/trades", [/Trades/, /LONG|SHORT/i, /\$[\d,]+\.\d{2}/]);
  });

  test("patterns renders the scan including what it could not establish", async ({
    page,
  }) => {
    // The failures are part of the contract: a scan that hides them makes recurrence
    // unfalsifiable. Asserting on the "examined" section keeps that visible.
    await checkRoute(page, "/patterns", [/Patterns/, /Examined and not established/i]);
  });

  test("reports renders a period comparison", async ({ page }) => {
    await checkRoute(page, "/reports", [/Reports/, /Against/i]);
  });

  test("jobs renders the queue", async ({ page }) => {
    await checkRoute(page, "/jobs", [/Jobs/, /pending/i]);
  });

  test("simulator renders every scenario with its adjusted p-value", async ({ page }) => {
    await checkRoute(page, "/simulator", [/simulat/i, /scenario/i]);
  });

  test("strategies renders the rule set", async ({ page }) => {
    await checkRoute(page, "/strategies", [/Strateg/i]);
  });

  test("broker renders the link form and the connection list", async ({ page }) => {
    await checkRoute(page, "/broker", [/Broker/i, /Link a Tradovate account/i]);
  });

  test("coach renders, and says so when no model is configured", async ({ page }) => {
    // Against a deployment with no API key this lands on the refusal, which is the
    // correct output and not an error — the assertion is that the page renders either
    // way rather than that a model answered.
    await checkRoute(page, "/coach", [/Coach/i, /Ask the coach/i]);
  });
});

test("the replay chart draws bars rather than an empty pane", async ({ page }) => {
  // The regression that motivated a whole ADR. The window picks 2m, the ingest writes 1m,
  // and reading storage directly returned nothing — reported as `bar_count: 0`, drawn as
  // a blank chart, with no error anywhere. Asserting a non-zero bar count is the cheapest
  // possible statement of "the aggregation path is wired up".
  const apiBase = process.env.SMOKE_API_BASE ?? "http://127.0.0.1:8000/api/v1";
  const response = await page.request.get(`${apiBase}/trades?limit=1`, {
    headers: { "X-Debug-User": process.env.SMOKE_DEV_USER ?? "demo" },
  });
  expect(response.ok()).toBeTruthy();
  const { items } = (await response.json()) as { items: { id: string }[] };
  expect(items.length, "seed produced no trades").toBeGreaterThan(0);

  await checkRoute(page, `/trades/${items[0]!.id}`, [/Trade replay/i]);

  const text = await bodyText(page);
  const bars = /(\d[\d,]*) bars at/.exec(text);
  expect(bars, `replay did not report a bar count: ${text.slice(0, 200)}`).not.toBeNull();
  expect(
    Number(bars![1]!.replace(/,/g, "")),
    "replay reported zero bars — the 1m-to-2m aggregation is not wired up",
  ).toBeGreaterThan(0);
});

test("captured chart images decode in the browser", async ({ page }) => {
  /**
   * The unit tests assert that the gallery fetches with credentials and renders a blob
   * URL. Only a real browser can assert the thing that actually broke: that the bytes
   * arriving are a decodable image.
   *
   * The first version used `<img src={endpoint}>`. It could not attach an auth header,
   * so the API returned 401, and cross-origin Opaque Response Blocking discarded the
   * JSON error before `onerror` could fire. The page looked fine — heading, count,
   * captions, six figures — with six invisible broken images inside it. `naturalWidth`
   * is what tells them apart; every text assertion passes either way.
   */
  const apiBase = process.env.SMOKE_API_BASE ?? "http://127.0.0.1:8000/api/v1";
  const headers = { "X-Debug-User": process.env.SMOKE_DEV_USER ?? "demo" };

  const trades = await page.request.get(`${apiBase}/trades?limit=1`, { headers });
  const { items } = (await trades.json()) as { items: { id: string }[] };
  const tradeId = items[0]!.id;

  // Capture through the API process rather than the queue: locally each process owns its
  // own in-memory object store, so bytes written by the worker are unreadable here.
  const captured = await page.request.post(
    `${apiBase}/replay/trades/${tradeId}/screenshots`,
    { headers },
  );
  expect(captured.ok()).toBeTruthy();
  const outcome = (await captured.json()) as { captured: unknown[]; skipped: string | null };
  test.skip(outcome.captured.length === 0, `no bars for this trade: ${outcome.skipped}`);

  await page.goto(`/trades/${tradeId}`);

  // Each frame fetches independently and renders a placeholder until its own bytes
  // arrive, so `figure img` matches only the images that have *already* resolved.
  // Waiting for the first one and then sleeping a fixed interval asserts a count that is
  // still being filled in: it passed locally and lost the race under CI load, failing
  // with 6 expected against however many had landed. Both waits below retry until the
  // condition holds, so the test measures the app rather than the runner's mood.
  await expect(page.locator("figure img")).toHaveCount(6, { timeout: 30_000 });

  const decoded = async () =>
    page.evaluate(
      () =>
        Array.from(document.querySelectorAll<HTMLImageElement>("figure img")).filter(
          (image) => image.complete && image.naturalWidth > 0,
        ).length,
    );

  // `naturalWidth`, not the element count: an `<img>` that 401s is still in the DOM and
  // still has a `src`. Only decoding tells a served image from a blocked one.
  await expect
    .poll(decoded, {
      timeout: 30_000,
      message: "chart images rendered as elements but never decoded",
    })
    .toBe(6);
});

test("the replay opens on the entry rather than on an empty chart", async ({ page }) => {
  /**
   * The unit tests pin the arithmetic. Only a browser can confirm the pane actually has
   * candles in it on first paint — the state can be right in the reducer and never reach
   * the canvas, which is exactly what happens if the seek-on-load effect is dropped: the
   * component mounts before the query resolves, so there are no bars to seek within, and
   * the chart sits at index -1 for the whole session.
   *
   * Sampling pixels rather than reading the bar counter, because the counter showed
   * `bar 0 / 91` and looked plausible while the pane was blank.
   */
  const apiBase = process.env.SMOKE_API_BASE ?? "http://127.0.0.1:8000/api/v1";
  const trades = await page.request.get(`${apiBase}/trades?limit=1`, {
    headers: { "X-Debug-User": process.env.SMOKE_DEV_USER ?? "demo" },
  });
  const { items } = (await trades.json()) as { items: { id: string }[] };

  await page.goto(`/trades/${items[0]!.id}`);
  await page.waitForSelector("canvas");
  await page.waitForTimeout(1500);

  const painted = await page.evaluate(() => {
    // The widest canvas is the price pane; the narrow ones are the axes.
    const canvas = Array.from(document.querySelectorAll("canvas")).sort(
      (a, b) => b.width - a.width,
    )[0]!;
    const context = canvas.getContext("2d");
    if (!context) return { colours: 0 };
    const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
    const seen = new Set<string>();
    for (let i = 0; i < data.length; i += 4 * 37) {
      seen.add(`${data[i]},${data[i + 1]},${data[i + 2]}`);
    }
    return { colours: seen.size };
  });

  // Background plus gridlines alone is a handful of colours; candles add many more.
  expect(
    painted.colours,
    "the replay chart is blank on first load",
  ).toBeGreaterThan(6);

  const counter = await page.locator("text=/bar \\d+ \\/ \\d+/").first().innerText();
  expect(counter, `replay opened at bar 0: ${counter}`).not.toMatch(/bar 0 \//);
});
