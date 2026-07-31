import { defineConfig, devices } from "@playwright/test";

/**
 * Configuration for the route smoke test.
 *
 * Separate from `vitest.config.ts` on purpose, and the two do not overlap: vitest globs
 * `tests/**\/*.test.{ts,tsx}` and runs the pure component and formatting tests in jsdom;
 * this globs `*.spec.ts` and drives a real browser against a running stack. Naming is the
 * whole separation — a `.test.ts` file never needs a server, a `.spec.ts` file always
 * does.
 *
 * The servers are **not** started here. A `webServer` block would have to bring up
 * Postgres, run migrations and seed a history before the API could even boot, which is
 * orchestration that belongs in the CI workflow (or a developer's two terminals) rather
 * than buried in a test config where a failure reads as a test failure. This file assumes
 * both are already up and says so loudly when they are not.
 */
export default defineConfig({
  testDir: "./tests",
  testMatch: /.*\.spec\.ts/,

  // A failing smoke test means a route is broken, and a broken route does not become
  // unbroken on the second attempt. Retries here would convert a real defect into a flaky
  // one and hide it.
  retries: 0,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],

  use: {
    baseURL: process.env.SMOKE_BASE_URL ?? "http://127.0.0.1:3000",
    // Kept on failure only: an artefact per passing route is noise, and the screenshot of
    // a failure is the fastest way to tell a crash from a blank page.
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: {
          // Set `PLAYWRIGHT_CHROMIUM_PATH` where a Chromium is already provisioned and
          // Playwright's own download is unavailable or pinned to a different build —
          // sandboxes and locked-down CI images both do this. Left unset, Playwright
          // resolves its usual managed browser and nothing changes.
          ...(process.env.PLAYWRIGHT_CHROMIUM_PATH
            ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
            : {}),
        },
      },
    },
  ],
});
