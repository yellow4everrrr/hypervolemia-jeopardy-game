"use client";

import { Change, Refusal } from "@/components/evidence";
import { PageHeader } from "@/components/shell";
import { formatMoney } from "@/lib/format";
import { useLatestReport } from "@/lib/hooks";
import type { LeakSummary, MetricChange } from "@/types/evidence";

/**
 * The latest monthly report.
 *
 * Two things are rendered that a conventional report screen would drop.
 *
 * A section the backend **withheld** shows its reason instead of vanishing. A month too
 * thin to compare is a fact about the month, and silently omitting the comparison would
 * make a sparse report look like a complete one.
 *
 * The leak total shows the deduplicated figure with the naive sum beside it. Detectors
 * overlap, so adding their estimates bills the same money several times over; keeping both
 * on screen is what stops the larger number quietly becoming the headline again.
 */
export default function ReportsPage() {
  const { data, isLoading, error } = useLatestReport("monthly");
  const report = data?.report;

  const comparison = report?.sections.find((section) => section.key === "comparison");
  const leaks = report?.sections.find((section) => section.key === "leaks");
  const leakData = leaks?.data as unknown as LeakSummary | undefined;
  const changes = (comparison?.data?.["changes"] ?? []) as MetricChange[];

  return (
    <>
      <PageHeader
        title="Reports"
        subtitle="Computed from the period's own trades — never aggregated from shorter reports."
      />

      <div className="space-y-6 p-6">
        {isLoading ? <p className="text-sm text-slate-500">Loading…</p> : null}
        {error ? (
          <p className="text-sm text-slate-500">
            No monthly report has been generated yet.
          </p>
        ) : null}

        {report ? (
          <>
            <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-4">
              <h2 className="text-sm font-medium text-slate-200">
                {report.period.label}
              </h2>
              <p className="mt-1 text-sm text-slate-400">{report.headline}</p>
              {!report.draws_conclusions ? (
                <p className="mt-2 text-xs text-amber-300/80">
                  This period is too small to interpret. The figures below are shown
                  without conclusions.
                </p>
              ) : null}
              {report.notes.map((note) => (
                <p key={note} className="mt-2 text-[11px] leading-relaxed text-slate-500">
                  {note}
                </p>
              ))}
            </div>

            {comparison && !comparison.is_present ? (
              <Refusal
                title={comparison.title}
                reason={comparison.withheld_reason ?? "this section was withheld"}
              />
            ) : null}

            {changes.length > 0 ? (
              <section className="rounded-lg border border-slate-800 bg-slate-900/60 p-4">
                <h2 className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-400">
                  {comparison?.title}
                </h2>
                {changes.map((change) => (
                  <Change key={change.key} change={change} />
                ))}
              </section>
            ) : null}

            {leakData ? (
              <section className="rounded-lg border border-slate-800 bg-slate-900/60 p-4">
                <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">
                  What your leaks cost
                </h2>
                {leakData.total_cost === null ? (
                  <p className="mt-2 text-sm text-slate-500">
                    {leakData.undefined_reason}
                  </p>
                ) : (
                  <>
                    <p className="mt-1 font-mono text-2xl tabular-nums text-rose-300">
                      {formatMoney(leakData.total_cost)}
                    </p>
                    {/* Kept on screen deliberately: the gap between these two numbers is
                        overlap between detectors, not disagreement.

                        Shown only when there *is* a gap. With a single established leak
                        the deduplicated total and the naive sum are the same number, and
                        the sentence then read "adding them would have given -$7,850.04 —
                        the same money billed more than once" beside a headline of
                        -$7,850.04. Printing a figure and calling it inflated relative to
                        itself does not just look wrong, it undermines the deduplication
                        it was written to explain.

                        Compared **as rendered**, not as raw strings. The two arrive from
                        a Decimal division and differ at the 25th decimal place —
                        "-7850.042372881355932203389830" against "...823" — so a string
                        comparison calls them different and shows the contradiction
                        anyway. The rule that always holds: never tell a reader two
                        numbers differ when the screen is showing them the same number. */}
                    {leakData.naive_sum &&
                    formatMoney(leakData.naive_sum) !==
                      formatMoney(leakData.total_cost) ? (
                      <p className="mt-1 text-[11px] text-slate-500">
                        Counting each trade once. Adding the detectors&rsquo; individual
                        estimates would have given {formatMoney(leakData.naive_sum)} —
                        the same money billed more than once.
                      </p>
                    ) : (
                      <p className="mt-1 text-[11px] text-slate-500">
                        Counting each trade once. Where established leaks overlap — the
                        same trade caught by more than one detector — the money is
                        counted a single time rather than summed per detector.
                      </p>
                    )}
                  </>
                )}
              </section>
            ) : null}
          </>
        ) : null}
      </div>
    </>
  );
}
