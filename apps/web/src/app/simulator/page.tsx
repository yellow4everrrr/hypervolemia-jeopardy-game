"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { Refusal } from "@/components/evidence";
import { PageHeader } from "@/components/shell";
import { request } from "@/lib/api";
import { formatMoney, formatNumber, formatPercent } from "@/lib/format";
import type { EnqueuedJob, Job } from "@/types/evidence";
import type { ScenarioResult, SweepReport } from "@/types/simulation";

/**
 * The what-if simulator.
 *
 * Every trading journal that ships this feature ships it as a leaderboard: sweep some
 * rules, sort by improvement, put the winner at the top. That design cannot fail to find
 * something. Nine scenarios against one fixed history always produce a best one, and
 * presenting it is how a journal tells every trader their edge was two settings away.
 *
 * So this screen is built around a value the backend returns and a leaderboard has no
 * place for: **`best` is null unless something survived correction**, and on this demo
 * history — a genuinely profitable trader, 1,447 trades, a year of sessions — it is null.
 * The honest headline is "no rule change was established", and it is the *first* thing on
 * the page rather than an absence at the bottom of a sorted table.
 *
 * The results are still all shown, in the order the engine returned them, never sorted by
 * `difference`. Sorting by outcome would rebuild the leaderboard out of the honest data:
 * whatever landed on top would read as the recommendation regardless of the caption
 * underneath it.
 *
 * The run is an explicit button. A sweep costs seconds of bootstrapping per scenario, and
 * a screen that re-runs it on mount invites the one behaviour this whole module exists to
 * prevent — running it repeatedly until something clears the threshold.
 *
 * It is also **queued rather than awaited**. Nine scenarios across a year of trades takes
 * about a minute, which is past most proxy timeouts and far past the point where a person
 * concludes the page has hung; the first version of this screen held a `POST` open for all
 * of it. The button now enqueues a job and this component polls it, so the wait is visible
 * and survives a reload. The result payload lands in `jobs.result`, which is why no new
 * endpoint was needed to read it back.
 */

function verdictTone(result: ScenarioResult): string {
  if (!result.is_actionable) return "border-slate-800 bg-slate-900/40";
  return result.is_improvement
    ? "border-emerald-500/30 bg-emerald-500/5"
    : "border-rose-500/30 bg-rose-500/5";
}

function ScenarioRow({ result }: { result: ScenarioResult }) {
  const { scenario, trades } = result;
  const touched = trades.repriced + trades.skipped;

  return (
    <article className={`rounded-lg border p-4 ${verdictTone(result)}`}>
      <header className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-medium text-slate-200">{scenario.label}</h3>
        <span
          className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
            result.is_actionable
              ? result.is_improvement
                ? "border-emerald-500/40 text-emerald-300"
                : "border-rose-500/40 text-rose-300"
              : "border-slate-700 text-slate-500"
          }`}
        >
          {result.is_actionable
            ? result.is_improvement
              ? "established gain"
              : "established loss"
            : "not established"}
        </span>
      </header>

      {/* Zero is its own case, not a gain. `difference` arrives as an exact Decimal
          string — "0E-8" for a scenario that repriced nothing — so `startsWith("-")` is
          false and a two-branch conditional paints it the same green as a $200,000
          improvement. Comparing the number rather than the string keeps every exponent
          form of zero in the neutral branch. */}
      <p
        className={`mt-2 font-mono text-lg tabular-nums ${
          Number(result.difference) === 0
            ? "text-slate-400"
            : result.difference.startsWith("-")
              ? "text-rose-300"
              : "text-emerald-300"
        }`}
      >
        {formatMoney(result.difference)}
      </p>

      <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
        {/* A rule that touched no trades is not a weak result, it is a non-event, and
            saying so is clearer than showing it a p-value of 1 and letting the reader
            work out why. This is the case that used to report p = 0.0001 — see the note
            in `_test_difference`. */}
        {touched === 0 ? (
          <div className="flex gap-1">
            <dt className="text-amber-400/80">
              changed nothing — no trade in this history met the rule
            </dt>
          </div>
        ) : (
          <>
            <div className="flex gap-1">
              <dt>trades affected</dt>
              <dd className="font-mono text-slate-400">
                {touched.toLocaleString("en-US")} of{" "}
                {trades.total.toLocaleString("en-US")}
              </dd>
            </div>
            {result.adjusted_p_value ? (
              <div className="flex gap-1">
                <dt>adjusted p</dt>
                <dd className="font-mono text-slate-400">
                  {formatNumber(result.adjusted_p_value, 4)}
                </dd>
              </div>
            ) : null}
            {result.delta_interval ? (
              <div className="flex gap-1">
                <dt>per trade</dt>
                <dd className="font-mono text-slate-400">
                  {formatMoney(result.delta_interval[0])} to{" "}
                  {formatMoney(result.delta_interval[1])}
                </dd>
              </div>
            ) : null}
            {result.coverage ? (
              <div className="flex gap-1">
                <dt>coverage</dt>
                <dd className="font-mono text-slate-400">
                  {formatPercent(result.coverage, 0)}
                </dd>
              </div>
            ) : null}
          </>
        )}
      </dl>
    </article>
  );
}

/** States in which a job will never produce a result, so polling should stop. */
const SETTLED = ["succeeded", "failed", "dead", "cancelled"] as const;

export default function SimulatorPage() {
  const [jobId, setJobId] = useState<string | null>(null);

  const enqueue = useMutation({
    mutationFn: () =>
      request<EnqueuedJob>("/jobs", {
        method: "POST",
        body: { kind: "run_simulation" },
      }),
    // `job_id`, not `id`: the enqueue response is not a Job. See `EnqueuedJob`.
    onSuccess: (enqueued) => setJobId(enqueued.job_id),
  });

  const client = useQueryClient();

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => request<Job>(`/jobs/${jobId}`),
    enabled: jobId !== null,
    // Polling stops the moment the job settles rather than running forever behind an
    // open tab. `false` is how TanStack Query is told to stand down.
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state && SETTLED.includes(state as (typeof SETTLED)[number])
        ? false
        : 2_000;
    },
  });

  // A finished sweep replaces the stored one, so the "computed at" line does not keep
  // citing an older run than the results beside it.
  useEffect(() => {
    if (job.data?.state === "succeeded") {
      void client.invalidateQueries({ queryKey: ["jobs", "latest", "run_simulation"] });
    }
  }, [job.data?.state, client]);

  // The most recent sweep this user has ever completed, whoever started it.
  //
  // Without this the page only knew about a job *this component instance* enqueued, so a
  // reload — or arriving from another screen, or coming back tomorrow — showed "No sweep
  // has been run yet" while finished sweeps sat in the database with their full results.
  // The natural response to that screen is to run the sweep again, which is the one
  // behaviour this module exists to discourage: re-running until something clears the
  // threshold. It cost a minute of compute per accidental re-run and quietly undermined
  // the point of the feature.
  const latest = useQuery({
    queryKey: ["jobs", "latest", "run_simulation"],
    queryFn: () =>
      request<{ jobs: Job[] }>("/jobs?kind=run_simulation&state=succeeded&limit=1"),
    select: (data) => data.jobs[0],
  });

  const state = job.data?.state;
  const running = jobId !== null && !!state && !SETTLED.includes(state as (typeof SETTLED)[number]);
  const pending = enqueue.isPending || running;

  // The handler returns the sweep payload whole, so the job's result *is* the report.
  // A sweep run in this session wins; otherwise the last stored one is shown.
  const shown = state === "succeeded" ? job.data : latest.data;
  const report = shown?.result as unknown as SweepReport | undefined;

  // Whether what is on screen came from storage rather than from a run just watched. It
  // is labelled, because a sweep from three months ago and one from thirty seconds ago
  // render identically and only one of them describes the current history.
  const restored = state !== "succeeded" && !!latest.data;

  return (
    <>
      <PageHeader
        title="What-if simulator"
        subtitle="Counterfactual rules re-priced against your real fills, corrected as one family."
      />

      <div className="space-y-6 p-6">
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={() => enqueue.mutate()}
            disabled={pending}
            className="rounded border border-slate-700 bg-slate-800 px-4 py-2 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50"
          >
            {pending ? "Simulating…" : "Run the standard sweep"}
          </button>
          <p className="max-w-2xl text-[11px] leading-relaxed text-slate-500">
            Nine scenarios, bootstrapped per scenario and corrected together. Every
            scenario added raises the bar for all the others, which is why the standard
            sweep is short. It runs as a background job — about a minute — so closing
            this page does not cancel it.
          </p>
        </div>

        {enqueue.isError ? (
          <p className="rounded border border-rose-500/30 bg-rose-500/5 p-4 text-sm text-rose-200">
            The sweep could not be queued.
          </p>
        ) : null}

        {/* A failed job's reason is the most useful thing it produced, so it is rendered
            rather than collapsed into "something went wrong". */}
        {state === "failed" || state === "dead" ? (
          <p className="rounded border border-rose-500/30 bg-rose-500/5 p-4 text-sm text-rose-200">
            The sweep {state === "dead" ? "failed repeatedly and was given up on" : "failed"}
            {job.data?.last_error ? `: ${job.data.last_error}` : "."}
          </p>
        ) : null}

        {running ? (
          <p className="text-sm text-slate-500">
            Queued as a background job — nine scenarios re-priced across your whole
            history takes about a minute. This page updates when it finishes, and the
            work continues if you navigate away.
          </p>
        ) : null}

        {/* Where these numbers came from. A restored sweep and one just watched render
            identically, and only one of them was computed against the current history —
            a trade imported since then is not in it. */}
        {restored && shown?.finished_at ? (
          <p className="text-[11px] text-slate-500">
            Showing the last completed sweep, computed{" "}
            {new Date(shown.finished_at).toLocaleString()}. Run it again to include
            trades imported since.
          </p>
        ) : null}

        {report ? (
          <>
            {/* The headline. Null `best` is the expected outcome and gets the same
                visual weight a recommendation would, because "nothing was established"
                is the finding — not the absence of one. */}
            {report.best ? (
              <section className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-5">
                <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">
                  Largest established improvement
                </h2>
                <p className="mt-1 text-lg text-emerald-200">
                  {report.best.scenario.label} —{" "}
                  {formatMoney(report.best.difference)} across{" "}
                  {report.sample_size.toLocaleString("en-US")} trades
                </p>
                <p className="mt-2 max-w-3xl text-xs leading-relaxed text-slate-400">
                  This is the largest improvement that survived correction across all{" "}
                  {report.scenarios_tested} scenarios — not the largest improvement,
                  which is always something and is usually noise.
                </p>
              </section>
            ) : (
              <Refusal
                title="No rule change was established"
                reason={`All ${report.scenarios_tested} scenarios were simulated against ${report.sample_size.toLocaleString(
                  "en-US",
                )} trades and none produced an improvement that survived correction across the family. On a history with a real edge this is the ordinary result: the rule you are already following is hard to beat by adjusting one parameter, and the scenario that looks best in any sweep is the one that got luckiest.`}
              />
            )}

            <section>
              <h2 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">
                Every scenario tested ({report.results.length})
              </h2>
              {/* Rendered in the engine's order. Sorting by `difference` would rebuild
                  the leaderboard this page exists to avoid — whatever sat on top would
                  read as advice no matter what the caption said. */}
              <div className="grid gap-3 lg:grid-cols-2">
                {report.results.map((result) => (
                  <ScenarioRow key={result.scenario.label} result={result} />
                ))}
              </div>
            </section>

            <p className="max-w-3xl text-[11px] leading-relaxed text-slate-600">
              {report.interpretation}
            </p>
            {report.notes.map((note) => (
              <p key={note} className="max-w-3xl text-[11px] text-slate-600">
                {note}
              </p>
            ))}
          </>
        ) : null}

        {!report && !pending ? (
          <p className="text-sm text-slate-500">
            No sweep has been run yet. It re-prices every trade under each rule using
            the excursions recorded for that trade, so it needs a few seconds.
          </p>
        ) : null}
      </div>
    </>
  );
}
