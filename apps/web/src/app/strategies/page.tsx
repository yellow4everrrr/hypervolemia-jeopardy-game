"use client";

import { useMutation, useQuery } from "@tanstack/react-query";

import { PageHeader } from "@/components/shell";
import { keys, request } from "@/lib/api";
import { formatMoney, formatPercent } from "@/lib/format";
import type {
  ComplianceReport,
  RuleImpact,
  RuleSeverity,
  RuleSummary,
  StrategyList,
  StrategySummary,
} from "@/types/strategy";

/**
 * Strategies and compliance.
 *
 * The compliance score is the number on this page a trader will fixate on, so the two
 * things that make it meaningful are given more room than the score itself:
 *
 * **What was not scored.** `trades_without_strategy` is rendered even when it is zero.
 * A 94% score over 534 of 1,447 trades is a different claim from 94% over all of them,
 * and a screen that shows only the percentage lets the reader assume the second.
 *
 * **What a rule impact is not.** The engine compares P&L on trades that broke a rule
 * against trades that followed it, and that difference is *association*, not cost. The
 * trades that break a rule differ from the ones that do not in every way that led to the
 * rule being broken — a trader down on the day, late in the session, sizing up trips
 * three rules at once. Rendering "-$196" under a heading like "what this rule costs you"
 * would be the single most misleading sentence in the application, so the wording says
 * "associated with" and the caveat sits directly beneath the table rather than in a
 * tooltip.
 */

const SEVERITY_TONE: Record<RuleSeverity, string> = {
  critical: "border-rose-500/40 text-rose-300",
  major: "border-amber-500/40 text-amber-300",
  minor: "border-slate-600 text-slate-400",
};

function SeverityBadge({ severity }: { severity: RuleSeverity }) {
  return (
    <span
      className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${SEVERITY_TONE[severity]}`}
    >
      {severity}
    </span>
  );
}

/** Render the stored predicate AST as something a person can check. */
function readExpression(node: unknown): string {
  if (node === null || typeof node !== "object") return String(node);
  const value = node as Record<string, unknown>;

  if (Array.isArray(value["all"])) {
    return value["all"].map(readExpression).join(" and ");
  }
  if (Array.isArray(value["any"])) {
    return `(${value["any"].map(readExpression).join(" or ")})`;
  }
  if (typeof value["field"] === "string") {
    const field = value["field"].replace(/_/g, " ");
    const operator =
      {
        eq: "is",
        ne: "is not",
        lt: "is below",
        lte: "is at most",
        gt: "is above",
        gte: "is at least",
      }[String(value["op"])] ?? String(value["op"]);
    return `${field} ${operator} ${String(value["value"])}`;
  }
  return JSON.stringify(node);
}

function Rule({ rule }: { rule: RuleSummary }) {
  return (
    <li className="flex flex-col gap-1 border-b border-slate-800 py-2.5 last:border-b-0">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm text-slate-300">{rule.label}</span>
        <SeverityBadge severity={rule.severity} />
      </div>
      {/* The rule as the engine will evaluate it, not as it was described. A rule a
          trader cannot read is one they cannot dispute. */}
      <code className="font-mono text-[11px] text-slate-500">
        {readExpression(rule.expression)}
      </code>
    </li>
  );
}

function ImpactRow({ impact }: { impact: RuleImpact }) {
  const evaluated = impact.violations + impact.compliant_trades;

  return (
    <tr className="border-b border-slate-800/60">
      <td className="px-3 py-2 text-xs text-slate-300">
        {impact.rule_label}
        <span className="ml-2 font-mono text-[10px] uppercase tracking-wider text-slate-600">
          {impact.severity}
        </span>
      </td>
      <td className="px-3 py-2 text-right font-mono text-xs tabular-nums text-slate-400">
        {impact.violations.toLocaleString("en-US")}
        <span className="text-slate-600">
          {" / "}
          {evaluated.toLocaleString("en-US")}
        </span>
      </td>
      <td className="px-3 py-2 text-right font-mono text-xs tabular-nums text-slate-400">
        {impact.violations === 0 ? (
          <span className="text-slate-600">never broken</span>
        ) : (
          formatPercent(String(impact.violations / Math.max(evaluated, 1)), 1)
        )}
      </td>
      <td
        className={`px-3 py-2 text-right font-mono text-xs tabular-nums ${
          impact.difference === null
            ? "text-slate-600"
            : impact.difference.startsWith("-")
              ? "text-rose-300"
              : "text-emerald-300"
        }`}
      >
        {impact.difference === null ? "—" : formatMoney(impact.difference)}
      </td>
    </tr>
  );
}

function Strategy({ strategy }: { strategy: StrategySummary }) {
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-5">
      <header className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-medium text-slate-200">{strategy.name}</h2>
        <span className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
          version {strategy.version}
        </span>
      </header>
      {strategy.description ? (
        <p className="mt-1 max-w-3xl text-xs leading-relaxed text-slate-400">
          {strategy.description}
        </p>
      ) : null}

      <h3 className="mt-4 text-[11px] uppercase tracking-wider text-slate-500">
        Rules ({strategy.rules.length})
      </h3>
      <ul className="mt-1">
        {strategy.rules.map((rule) => (
          <Rule key={rule.id} rule={rule} />
        ))}
      </ul>

      {/* Revisions rather than edits, surfaced so the reason is visible: editing rules in
          place would rewrite history, and yesterday's 92% would silently become a score
          against rules that did not exist yesterday. */}
      <p className="mt-3 text-[11px] text-slate-600">
        Revising a strategy creates the next version rather than editing this one, so a
        past score always refers to the rules that were in force when it was computed.
      </p>
    </section>
  );
}

export default function StrategiesPage() {
  const strategies = useQuery({
    queryKey: keys.strategies(),
    queryFn: () => request<StrategyList>("/strategies"),
  });

  const compliance = useMutation({
    mutationFn: () =>
      request<ComplianceReport>("/compliance/evaluate", { method: "POST" }),
  });

  const report = compliance.data;

  return (
    <>
      <PageHeader
        title="Strategies"
        subtitle="Rules checked against imported fills — nothing here depends on remembering to tick a box."
      />

      <div className="space-y-6 p-6">
        {strategies.isLoading ? (
          <p className="text-sm text-slate-500">Loading…</p>
        ) : null}

        {strategies.data && strategies.data.items.length === 0 ? (
          <p className="max-w-3xl text-sm text-slate-500">
            No strategies yet. A strategy is a set of machine-checkable rules; the
            starter set covers stops, risk per trade, session limits and re-entry
            timing, all of which are evaluable from imported fills alone.
          </p>
        ) : null}

        {strategies.data?.items.map((strategy) => (
          <Strategy key={strategy.id} strategy={strategy} />
        ))}

        {strategies.data && strategies.data.items.length > 0 ? (
          <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-5">
            <div className="flex items-center gap-4">
              <button
                type="button"
                onClick={() => compliance.mutate()}
                disabled={compliance.isPending}
                className="rounded border border-slate-700 bg-slate-800 px-4 py-2 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50"
              >
                {compliance.isPending ? "Scoring…" : "Score recent sessions"}
              </button>
              <p className="text-[11px] text-slate-500">
                Bounded by trading days rather than trades, because the
                sequence-sensitive rules are defined over a whole session.
              </p>
            </div>

            {report ? (
              <>
                <dl className="mt-4 grid gap-4 sm:grid-cols-4">
                  <div>
                    <dt className="text-[11px] uppercase tracking-wider text-slate-500">
                      Mean score
                    </dt>
                    <dd className="font-mono text-2xl tabular-nums text-slate-100">
                      {report.mean_score === null
                        ? "not available"
                        : formatPercent(report.mean_score, 1)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[11px] uppercase tracking-wider text-slate-500">
                      Trades scored
                    </dt>
                    <dd className="font-mono text-2xl tabular-nums text-slate-300">
                      {report.trades_scored.toLocaleString("en-US")}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[11px] uppercase tracking-wider text-slate-500">
                      Violations
                    </dt>
                    <dd className="font-mono text-2xl tabular-nums text-amber-300">
                      {report.total_violations.toLocaleString("en-US")}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[11px] uppercase tracking-wider text-slate-500">
                      Critical
                    </dt>
                    <dd
                      className={`font-mono text-2xl tabular-nums ${
                        report.critical_violations > 0
                          ? "text-rose-400"
                          : "text-slate-500"
                      }`}
                    >
                      {report.critical_violations.toLocaleString("en-US")}
                    </dd>
                  </div>
                </dl>

                {/* Rendered even at zero. A score over a subset of trades is a different
                    claim from a score over all of them, and only this number tells them
                    apart. */}
                <p className="mt-3 text-[11px] text-slate-500">
                  {report.trades_without_strategy === 0
                    ? "Every trade in the window was attributed to a strategy and scored."
                    : `${report.trades_without_strategy.toLocaleString("en-US")} trades in the window had no strategy attached and were not scored. The score above describes the rest.`}
                </p>

                {report.impacts.length > 0 ? (
                  <>
                    <h3 className="mt-5 text-[11px] uppercase tracking-wider text-slate-500">
                      Rules by how often they are broken
                    </h3>
                    <div className="overflow-x-auto">
                      <table className="mt-1 w-full border-collapse">
                        <thead>
                          <tr className="border-b border-slate-700 text-left">
                            {["Rule", "Broken", "Rate", "Associated difference"].map(
                              (heading, index) => (
                                <th
                                  key={heading}
                                  className={`px-3 py-2 text-[10px] font-medium uppercase tracking-wider text-slate-500 ${
                                    index > 0 ? "text-right" : ""
                                  }`}
                                >
                                  {heading}
                                </th>
                              ),
                            )}
                          </tr>
                        </thead>
                        <tbody>
                          {report.impacts.map((impact) => (
                            <ImpactRow key={impact.rule_code} impact={impact} />
                          ))}
                        </tbody>
                      </table>
                    </div>

                    {/* The caveat is body text under the table, not a tooltip. It is the
                        difference between a true statement and a false one. */}
                    <p className="mt-3 max-w-3xl text-[11px] leading-relaxed text-amber-300/70">
                      &ldquo;Associated difference&rdquo; compares average P&amp;L on
                      trades that broke a rule against trades that followed it. It is not
                      what breaking the rule cost. Trades that break a rule differ from
                      trades that do not in every way that led to the rule being broken —
                      being down on the day, late in the session and sizing up trips three
                      rules at once, and no one of the three differences is that rule&rsquo;s
                      own contribution.
                    </p>
                  </>
                ) : null}
              </>
            ) : null}
          </section>
        ) : null}
      </div>
    </>
  );
}
