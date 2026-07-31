"use client";

import { useMutation } from "@tanstack/react-query";

import { Refusal } from "@/components/evidence";
import { PageHeader } from "@/components/shell";
import { request } from "@/lib/api";
import { formatMoney, formatNumber, formatPercent } from "@/lib/format";
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

export default function SimulatorPage() {
  const sweep = useMutation({
    mutationFn: () =>
      request<SweepReport>("/simulator/sweep?quick=true", {
        method: "POST",
        body: {},
      }),
  });

  const report = sweep.data;

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
            onClick={() => sweep.mutate()}
            disabled={sweep.isPending}
            className="rounded border border-slate-700 bg-slate-800 px-4 py-2 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50"
          >
            {sweep.isPending ? "Simulating…" : "Run the standard sweep"}
          </button>
          <p className="max-w-2xl text-[11px] leading-relaxed text-slate-500">
            Nine scenarios, bootstrapped per scenario and corrected together. Every
            scenario added raises the bar for all the others, which is why the standard
            sweep is short.
          </p>
        </div>

        {sweep.isError ? (
          <p className="rounded border border-rose-500/30 bg-rose-500/5 p-4 text-sm text-rose-200">
            The sweep could not be run.
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

        {!report && !sweep.isPending ? (
          <p className="text-sm text-slate-500">
            No sweep has been run yet. It re-prices every trade under each rule using
            the excursions recorded for that trade, so it needs a few seconds.
          </p>
        ) : null}
      </div>
    </>
  );
}
