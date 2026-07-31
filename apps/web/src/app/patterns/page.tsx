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

  const established = data?.items.filter((item) => item.is_significant) ?? [];
  const examined = data?.items.filter((item) => !item.is_significant) ?? [];

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
                    <Finding key={finding.id} finding={finding} />
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
                  <Finding key={finding.id} finding={finding} />
                ))}
              </div>
            </section>

            {/* The backend's own explanation of why the failures are on the screen.
                Rendered rather than paraphrased, so the reason a page full of "examined
                and not established" is the correct output comes from the engine that
                decided it. */}
            {data.items.length > 0 ? (
              <p className="max-w-3xl text-[11px] leading-relaxed text-slate-600">
                {data.note}
              </p>
            ) : (
              <p className="text-sm text-slate-500">
                No scan has been run yet. A scan is an explicit action rather than
                something a page triggers on render — it runs a permutation test per
                detector and a null-reference battery for the clustering.
              </p>
            )}
          </>
        ) : null}
      </div>
    </>
  );
}
