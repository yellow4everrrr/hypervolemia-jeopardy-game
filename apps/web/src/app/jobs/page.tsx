"use client";

import { PageHeader } from "@/components/shell";
import { formatDuration } from "@/lib/format";
import { useJobs, useQueueHealth } from "@/lib/hooks";

/**
 * Background work.
 *
 * The two numbers worth watching are surfaced above the list: `dead` is work that will
 * never run without intervention, and `pending` climbing while `running` stays at zero
 * means no worker is consuming the queue — a depth figure alone cannot tell that apart
 * from "busy".
 */

const STATE_TONE: Record<string, string> = {
  pending: "text-slate-400",
  running: "text-sky-400",
  succeeded: "text-emerald-400",
  failed: "text-amber-400",
  dead: "text-rose-400",
  cancelled: "text-slate-600",
};

export default function JobsPage() {
  const { data: health } = useQueueHealth();
  const { data } = useJobs();

  return (
    <>
      <PageHeader
        title="Jobs"
        subtitle="Pattern scans, model training and report generation run here rather than in a request."
      />

      <div className="space-y-6 p-6">
        {health ? (
          <div className="flex gap-4">
            {(
              [
                ["Pending", health.pending, "text-slate-300"],
                ["Running", health.running, "text-sky-300"],
                ["Dead", health.dead, health.dead > 0 ? "text-rose-300" : "text-slate-500"],
              ] as const
            ).map(([label, value, tone]) => (
              <div
                key={label}
                className="rounded-lg border border-slate-800 bg-slate-900/60 px-4 py-3"
              >
                <p className="text-[10px] uppercase tracking-wider text-slate-500">
                  {label}
                </p>
                <p className={`font-mono text-xl tabular-nums ${tone}`}>{value}</p>
              </div>
            ))}
          </div>
        ) : null}

        {health && health.dead > 0 ? (
          <p className="rounded border border-rose-500/30 bg-rose-500/5 p-3 text-xs text-rose-200">
            {health.dead} job(s) have exhausted their attempts and will not run again
            without intervention. Each carries the error that stopped it.
          </p>
        ) : null}

        {data && data.jobs.length > 0 ? (
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-slate-700 text-left">
                {["Kind", "State", "Attempts", "Duration", "Error"].map((heading) => (
                  <th
                    key={heading}
                    className="px-3 py-2 text-[10px] font-medium uppercase tracking-wider text-slate-500"
                  >
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.jobs.map((job) => (
                <tr key={job.id} className="border-b border-slate-800/60">
                  <td className="px-3 py-1.5 font-mono text-xs text-slate-300">
                    {job.kind}
                  </td>
                  <td
                    className={`px-3 py-1.5 font-mono text-xs ${
                      STATE_TONE[job.state] ?? "text-slate-400"
                    }`}
                  >
                    {job.state}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-xs tabular-nums text-slate-500">
                    {job.attempts}/{job.max_attempts}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-xs tabular-nums text-slate-500">
                    {job.duration_ms === null
                      ? "—"
                      : formatDuration(Math.round(job.duration_ms / 1000))}
                  </td>
                  {/* Surfaced, not hidden behind a detail view: a dead job whose cause is
                      one click away is a job nobody diagnoses. */}
                  <td className="max-w-md truncate px-3 py-1.5 font-mono text-[11px] text-rose-300/70">
                    {job.last_error ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-sm text-slate-500">No jobs have been queued.</p>
        )}
      </div>
    </>
  );
}
