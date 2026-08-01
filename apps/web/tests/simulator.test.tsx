/**
 * The simulator remembering what it already computed.
 *
 * The page used to hold the job id in React state and nothing else, so it could only
 * display a sweep that *this component instance* had started. Reload the page, arrive
 * from another screen, or come back the next day, and it said "No sweep has been run
 * yet" — while completed sweeps sat in the database with their full nine-scenario
 * results. Observed on the demo history with five successful `run_simulation` jobs, each
 * a minute and a half of bootstrapping, all invisible.
 *
 * The wasted compute is the smaller half. The natural response to "no sweep has been run
 * yet" is to run the sweep, and re-running a corrected sweep until something clears the
 * threshold is precisely the behaviour this module is built to discourage. A screen that
 * hides its own previous answer is an invitation to keep asking.
 *
 * These tests also pin the provenance line, because a restored sweep and one just watched
 * render identically and only one of them was computed against the current history.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SimulatorPage from "@/app/simulator/page";

const REPORT = {
  best: null,
  results: [
    {
      scenario: { label: "Take profit at 1R", kind: "target" },
      difference: "-168867.50",
      adjusted_p_value: "0.0002",
      delta_interval: ["-127.93", "-105.59"],
      coverage: "1.0",
      is_actionable: true,
      is_improvement: false,
      trades: { total: 1447, repriced: 698, skipped: 0 },
    },
  ],
  sample_size: 1447,
  scenarios_tested: 9,
  actionable: 4,
  interpretation: "Counterfactual, not causal.",
  notes: [],
  engine_version: 1,
};

const STORED_JOB = {
  id: "019fbc90-c77b-704e-a60d-cf623404d6b8",
  kind: "run_simulation",
  state: "succeeded",
  attempts: 1,
  max_attempts: 5,
  run_after: "2026-08-01T09:03:54Z",
  created_at: "2026-08-01T09:03:52Z",
  started_at: "2026-08-01T09:03:54Z",
  finished_at: "2026-08-01T09:05:26Z",
  duration_ms: 92000,
  result: REPORT,
  last_error: null,
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SimulatorPage />
    </QueryClientProvider>,
  );
}

function stubJobs(jobs: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify({ jobs, count: jobs.length }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_DEV_USER", "demo");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("the what-if simulator", () => {
  it("shows the last completed sweep on a fresh page load", async () => {
    stubJobs([STORED_JOB]);

    renderPage();

    // The finding, not the empty state — no button was pressed in this session.
    expect(await screen.findByText(/No rule change was established/i)).toBeInTheDocument();
    expect(screen.getByText(/Take profit at 1R/)).toBeInTheDocument();
  });

  it("asks the API for the newest succeeded sweep only", async () => {
    stubJobs([STORED_JOB]);

    renderPage();

    await waitFor(() => {
      const called = vi
        .mocked(fetch)
        .mock.calls.map(([url]) => String(url))
        .join(" ");
      // Filtering server-side rather than fetching the job list and scanning it: a busy
      // queue can push the last sweep past any page size worth requesting.
      expect(called).toContain("kind=run_simulation");
      expect(called).toContain("state=succeeded");
      expect(called).toContain("limit=1");
    });
  });

  it("says when a restored sweep was computed", async () => {
    stubJobs([STORED_JOB]);

    renderPage();

    // Without this, a sweep from three months ago is indistinguishable from one that
    // finished while you watched, and only one of them describes the current history.
    expect(await screen.findByText(/Showing the last completed sweep/i)).toBeInTheDocument();
    expect(screen.getByText(/Run it again to include trades imported since/i)).toBeInTheDocument();
  });

  it("still shows the empty state when nothing has ever been run", async () => {
    stubJobs([]);

    renderPage();

    expect(await screen.findByText(/No sweep has been run yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/No rule change was established/i)).not.toBeInTheDocument();
  });
});
