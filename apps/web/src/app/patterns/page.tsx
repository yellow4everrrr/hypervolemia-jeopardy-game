"use client";

import { Finding } from "@/components/evidence";
import { PageHeader } from "@/components/shell";
import { usePatterns } from "@/lib/hooks";

/**
 * Detected patterns.
 *
 * Established findings first, then everything else that was tested. The failures are shown
 * rather than filtered because the backend stores every test it ran: hiding them would
 * make recurrence unfalsifiable — "this leak has appeared four months running" is only
 * evidence if the months it did not appear were also recorded.
 */
export default function PatternsPage() {
  const { data, isLoading, error } = usePatterns();

  const established = data?.behaviours.filter((item) => item.is_actionable) ?? [];
  const examined = data?.behaviours.filter((item) => !item.is_actionable) ?? [];

  return (
    <>
      <PageHeader
        title="Patterns"
        subtitle="Nothing is called a finding until it beats its own null, corrected across the whole scan."
      />

      <div className="space-y-8 p-6">
        {isLoading ? <p className="text-sm text-slate-500">Loading…</p> : null}
        {error ? (
          <p className="text-sm text-rose-300">Could not load the pattern scan.</p>
        ) : null}

        {data ? (
          <>
            <section>
              <h2 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">
                Established ({established.length})
              </h2>
              {established.length === 0 ? (
                <p className="text-sm text-slate-500">
                  Nothing survived correction across this scan. On a normal history
                  that is the expected result, not a missing feature — it means no
                  behaviour differed from the rest by more than chance explains.
                </p>
              ) : (
                <div className="grid gap-3 lg:grid-cols-2">
                  {established.map((finding) => (
                    <Finding key={finding.kind} finding={finding} />
                  ))}
                </div>
              )}
            </section>

            <section>
              <h2 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500">
                Examined and not established ({examined.length})
              </h2>
              <div className="grid gap-3 lg:grid-cols-2">
                {examined.map((finding) => (
                  <Finding key={finding.kind} finding={finding} />
                ))}
              </div>
            </section>
          </>
        ) : null}
      </div>
    </>
  );
}
