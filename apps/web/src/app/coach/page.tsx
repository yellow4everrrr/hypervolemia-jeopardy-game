"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Refusal } from "@/components/evidence";
import { PageHeader } from "@/components/shell";
import { ApiError, request } from "@/lib/api";
import { formatDate } from "@/lib/format";
import type {
  CoachAnalysis,
  CoachClaim,
  CoachRecommendation,
  OpenRecommendationList,
  StoredAnalysisList,
} from "@/types/coach";

/**
 * The AI coach.
 *
 * Twelve milestones exist to compute statistics that can be trusted, and one milestone
 * exists to let a language model talk about them without being able to invent any. This
 * screen is where that guarantee is either visible or quietly discarded, so three things
 * are non-negotiable in the markup below.
 *
 * **A withheld analysis shows its reasons, never its claims.** `is_publishable: false` is
 * a successful request whose output failed validation — the model cited evidence it was
 * not given, or wrote a number nobody computed. The obvious UI is to render the claims
 * with a warning badge. That is exactly how a fabricated statistic reaches a trading
 * decision: nobody reads the badge, everybody reads the sentence. So the claims are not
 * rendered at all, and `validation.reasons` takes their place.
 *
 * **Every claim shows what it cites.** The model never writes a number — it writes
 * `{{metric.key}}` and Python substitutes the computed value. Displaying the citations
 * makes that checkable by the reader instead of only by the validator, and makes a claim
 * resting on nothing visibly different from one resting on a sample of 1,400 trades.
 *
 * **A missing API key is a refusal, not an error page.** The endpoint returns a 502 with
 * a reason when the deployment has no model configured. That is a true statement about
 * the deployment and it belongs on the screen — an unconfigured coach is a deployment
 * fact, not a trader with no advice.
 */

/** The API's shape for "this deployment has no model configured". */
const NOT_CONFIGURED = "not configured";

function Claim({ claim }: { claim: CoachClaim }) {
  return (
    <li className="flex flex-col gap-1 border-b border-slate-800 py-3 last:border-b-0">
      <p className="text-sm leading-relaxed text-slate-300">{claim.statement}</p>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span className="font-mono uppercase tracking-wider">{claim.kind}</span>
        <span>confidence {claim.confidence}</span>
        {/* An empty citation list is shown as such rather than omitted. A claim with
            nothing under it should look different from one with evidence, and hiding the
            row would make them identical. */}
        {claim.cites.length > 0 ? (
          <span className="font-mono text-slate-600">
            cites {claim.cites.join(", ")}
          </span>
        ) : (
          <span className="text-amber-400/80">cites nothing</span>
        )}
      </div>
    </li>
  );
}

function Recommendation({ item }: { item: CoachRecommendation }) {
  return (
    <li className="flex flex-col gap-1 border-b border-slate-800 py-3 last:border-b-0">
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-sm leading-relaxed text-slate-300">{item.statement}</p>
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-slate-500">
          {item.category}
        </span>
      </div>
      <div className="flex flex-wrap gap-x-3 text-[11px] text-slate-500">
        <span>priority {item.priority}</span>
        <span>confidence {item.confidence}</span>
        {item.cites.length > 0 ? (
          <span className="font-mono text-slate-600">cites {item.cites.join(", ")}</span>
        ) : null}
      </div>
    </li>
  );
}

/**
 * Exported for `tests/coach.test.tsx`.
 *
 * The withheld path is the one carrying the whole guarantee and it cannot be reached
 * without an API key and a model that actually misbehaves — so it is asserted against a
 * fixture rather than left to be verified the day it first happens in production.
 */
export function Analysis({ analysis }: { analysis: CoachAnalysis }) {
  return (
    <div className="space-y-5">
      <section
        className={`rounded-lg border p-5 ${
          analysis.is_publishable
            ? "border-slate-800 bg-slate-900/40"
            : "border-amber-500/25 bg-amber-500/5"
        }`}
      >
        <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">
          {analysis.is_publishable ? "Analysis" : "Withheld"}
        </h2>
        <p
          className={`mt-1 text-lg leading-relaxed ${
            analysis.is_publishable ? "text-slate-200" : "text-amber-200"
          }`}
        >
          {analysis.is_publishable
            ? analysis.headline
            : "This analysis did not survive validation, so its claims are not shown."}
        </p>
        <p className="mt-2 font-mono text-[11px] text-slate-600">
          {analysis.model} · prompt {analysis.prompt_version}
        </p>
      </section>

      {/* The whole contract, in one conditional. Claims render only when the validator
          passed them; otherwise the reasons render in their place. There is deliberately
          no third branch showing claims with a caveat. */}
      {analysis.is_publishable ? (
        <>
          {analysis.claims.length > 0 ? (
            <section>
              <h3 className="mb-1 text-xs font-medium uppercase tracking-wider text-slate-400">
                What the numbers say ({analysis.claims.length})
              </h3>
              <ul>
                {analysis.claims.map((claim) => (
                  <Claim key={claim.statement} claim={claim} />
                ))}
              </ul>
            </section>
          ) : null}

          {analysis.recommendations.length > 0 ? (
            <section>
              <h3 className="mb-1 text-xs font-medium uppercase tracking-wider text-slate-400">
                Recommendations ({analysis.recommendations.length})
              </h3>
              <ul>
                {analysis.recommendations.map((item) => (
                  <Recommendation key={item.statement} item={item} />
                ))}
              </ul>
            </section>
          ) : null}
        </>
      ) : (
        <section className="rounded-lg border border-rose-500/25 bg-rose-500/5 p-4">
          <h3 className="text-xs font-medium uppercase tracking-wider text-rose-200">
            Why it was withheld
          </h3>
          <ul className="mt-2 space-y-1">
            {analysis.validation.reasons.map((reason) => (
              <li key={reason} className="text-xs leading-relaxed text-rose-100/80">
                {reason}
              </li>
            ))}
          </ul>
          {analysis.validation.uncited_numbers.length > 0 ? (
            <p className="mt-2 font-mono text-[11px] text-rose-200/70">
              numbers written rather than computed:{" "}
              {analysis.validation.uncited_numbers.join(", ")}
            </p>
          ) : null}
          {analysis.validation.unknown_keys.length > 0 ? (
            <p className="mt-1 font-mono text-[11px] text-rose-200/70">
              cited evidence that does not exist:{" "}
              {analysis.validation.unknown_keys.join(", ")}
            </p>
          ) : null}
        </section>
      )}

      {/* Shown whether or not the analysis published. These are the limits of the sample,
          and they are true either way. */}
      {analysis.what_the_data_cannot_say.length > 0 ? (
        <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
          <h3 className="text-xs font-medium uppercase tracking-wider text-slate-400">
            What this data cannot say
          </h3>
          <ul className="mt-2 space-y-1">
            {analysis.what_the_data_cannot_say.map((item) => (
              <li key={item} className="text-xs leading-relaxed text-slate-400">
                {item}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {analysis.evidence_gaps.length > 0 ? (
        <p className="max-w-3xl text-[11px] leading-relaxed text-slate-600">
          Statistics the engine could not supply for this period, and which the coach was
          therefore never able to cite: {analysis.evidence_gaps.join(", ")}.
        </p>
      ) : null}
    </div>
  );
}

export default function CoachPage() {
  const [question, setQuestion] = useState("");

  const history = useQuery({
    queryKey: ["coach", "analyses"],
    queryFn: () => request<StoredAnalysisList>("/coach/analyses?limit=5"),
  });

  const open = useQuery({
    queryKey: ["coach", "recommendations"],
    queryFn: () => request<OpenRecommendationList>("/coach/recommendations"),
  });

  const analyse = useMutation({
    mutationFn: () =>
      request<CoachAnalysis>("/coach/analyse", {
        method: "POST",
        body: question.trim() ? { question: question.trim() } : {},
      }),
  });

  const unconfigured =
    analyse.error instanceof ApiError &&
    analyse.error.message.includes(NOT_CONFIGURED);

  return (
    <>
      <PageHeader
        title="Coach"
        subtitle="Reads the statistics Python computed. It cannot write a number of its own."
      />

      <div className="space-y-6 p-6">
        <section className="max-w-3xl rounded-lg border border-slate-800 bg-slate-900/40 p-5">
          <label className="flex flex-col gap-1">
            <span className="text-[11px] uppercase tracking-wider text-slate-400">
              Ask something specific, or leave blank for a review of the period
            </span>
            <input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Why do my afternoon trades do worse?"
              className="rounded border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:border-slate-500 focus:outline-none"
            />
          </label>

          <div className="mt-4 flex items-center gap-4">
            <button
              type="button"
              onClick={() => analyse.mutate()}
              disabled={analyse.isPending}
              className="rounded border border-slate-700 bg-slate-800 px-4 py-2 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50"
            >
              {analyse.isPending ? "Analysing…" : "Ask the coach"}
            </button>
            <p className="text-[11px] leading-relaxed text-slate-500">
              The evidence bundle is computed first and the model only sees that. Every
              claim it makes is checked back against it before anything is shown.
            </p>
          </div>
        </section>

        {/* A deployment without a model is a fact worth stating plainly, and it is the
            state this screen is in until an API key is configured. Rendering it as a
            generic error would suggest something broke. */}
        {unconfigured ? (
          <Refusal
            title="No coaching model is configured"
            reason="This deployment has no Anthropic API key, so the coach cannot be asked. Everything else on this screen — the evidence bundle, the validation contract, the stored history — works without one; only the model call is unavailable."
          />
        ) : analyse.isError ? (
          <p className="rounded border border-rose-500/30 bg-rose-500/5 p-4 text-sm text-rose-200">
            {analyse.error instanceof ApiError
              ? analyse.error.message
              : "The analysis could not be generated."}
          </p>
        ) : null}

        {analyse.data ? <Analysis analysis={analyse.data} /> : null}

        {open.data && open.data.items.length > 0 ? (
          <section>
            <h2 className="mb-1 text-xs font-medium uppercase tracking-wider text-slate-400">
              Open recommendations ({open.data.items.length})
            </h2>
            <ul>
              {open.data.items.map((item) => (
                <li
                  key={item.id}
                  className="flex flex-col gap-1 border-b border-slate-800 py-3 last:border-b-0"
                >
                  <p className="text-sm text-slate-300">{item.statement}</p>
                  <div className="flex flex-wrap gap-x-3 text-[11px] text-slate-500">
                    <span className="font-mono uppercase tracking-wider">
                      {item.category}
                    </span>
                    <span>priority {item.priority}</span>
                    {/* "not simulated" rather than a blank or a zero. The improvement is
                        computed by the what-if engine, and an empty object means the
                        counterfactual has not been run — not that there is nothing to
                        gain. */}
                    <span
                      className={
                        Object.keys(item.expected_improvement).length === 0
                          ? "text-amber-400/70"
                          : "text-slate-400"
                      }
                    >
                      {Object.keys(item.expected_improvement).length === 0
                        ? "expected improvement not simulated"
                        : "expected improvement simulated"}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
            <p className="mt-2 max-w-3xl text-[11px] leading-relaxed text-slate-600">
              {open.data.note}
            </p>
          </section>
        ) : null}

        <section>
          <h2 className="mb-1 text-xs font-medium uppercase tracking-wider text-slate-400">
            Previous analyses ({history.data?.items.length ?? 0})
          </h2>
          {history.data && history.data.items.length === 0 ? (
            <p className="max-w-3xl text-sm text-slate-500">
              None yet. Each analysis is stored with the evidence it was given and whether
              it passed validation, so a claim can be audited after the fact rather than
              taken on trust at the time.
            </p>
          ) : null}
          <ul>
            {history.data?.items.map((item) => (
              <li
                key={item.id}
                className="flex items-baseline justify-between gap-3 border-b border-slate-800 py-2.5 last:border-b-0"
              >
                <span className="text-sm text-slate-300">
                  {item.summary ?? "(withheld)"}
                </span>
                <span className="shrink-0 font-mono text-[11px] text-slate-500">
                  {formatDate(item.created_at)}
                  {item.evidence_validated ? "" : " · withheld"}
                </span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </>
  );
}
