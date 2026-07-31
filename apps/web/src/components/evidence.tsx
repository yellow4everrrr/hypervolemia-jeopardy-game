/**
 * Components that will not render a number without what qualifies it.
 *
 * This file is the frontend's version of the placeholder contract in
 * `app/ai/contract.py`, and it exists for the same reason. The coach cannot write a
 * number because it only ever writes `{{metric.key}}` and Python substitutes. A component
 * here cannot render a bare figure because it takes an `Estimate` — value, sample size,
 * reliability, interval, and the reason it might be missing — rather than a `number`.
 *
 * The failure this prevents is not hypothetical, and it is not a bug anybody files. The
 * backend refuses to call an unproven difference a change; the dashboard renders a green
 * arrow anyway, because the designer asked for one and `difference` was right there in the
 * payload. The page looks finished. Twelve milestones of statistical discipline are gone,
 * and no test fails.
 *
 * Three rules run through everything below:
 *
 * 1. **An undefined value renders its reason, never a zero or a dash.** "not available"
 *    with an explanation is information; "0" is a lie and "—" is a shrug.
 * 2. **`unchanged` is visually distinct from `flat`.** One means the values matched; the
 *    other means the measurement could not tell them apart.
 * 3. **A refusal is content, not an empty state.** When a model will not be served, the
 *    reason is the most useful thing on the screen.
 */

import type { ReactNode } from "react";

import {
  NOT_AVAILABLE,
  describeSample,
  formatMoney,
  formatNumber,
  formatPercent,
} from "@/lib/format";
import type {
  Estimate,
  MetricChange,
  ModelReport,
  PatternFinding,
  Reliability,
} from "@/types/evidence";

type Unit = "currency" | "ratio" | "percent" | "r";

function renderValue(value: string, unit: Unit): string {
  switch (unit) {
    case "currency":
      return formatMoney(value);
    case "percent":
      return formatPercent(value);
    case "r":
      return `${formatNumber(value)}R`;
    default:
      return formatNumber(value);
  }
}

const RELIABILITY_COPY: Record<Reliability, string> = {
  insufficient: "too few trades to rely on",
  provisional: "provisional — small sample",
  reliable: "reliable sample",
};

const RELIABILITY_TONE: Record<Reliability, string> = {
  insufficient: "text-amber-400 border-amber-500/30 bg-amber-500/10",
  provisional: "text-sky-300 border-sky-500/30 bg-sky-500/10",
  reliable: "text-emerald-300 border-emerald-500/30 bg-emerald-500/10",
};

export function ReliabilityBadge({ reliability }: { reliability: Reliability }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${RELIABILITY_TONE[reliability]}`}
      title={RELIABILITY_COPY[reliability]}
    >
      {reliability}
    </span>
  );
}

/**
 * A statistic, with its sample size and interval attached.
 *
 * Takes an `Estimate`, not a number — the single most important signature in the app.
 * Passing `estimate.value` here is a type error, so the qualification is always in scope
 * at the call site.
 */
export function Metric({
  label,
  estimate,
  unit = "ratio",
  hint,
}: {
  label: string;
  estimate: Estimate;
  unit?: Unit;
  hint?: string;
}) {
  const defined = estimate.value !== null;

  return (
    <div className="flex flex-col gap-1 rounded-lg border border-slate-800 bg-slate-900/60 p-4">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium uppercase tracking-wider text-slate-400">
          {label}
        </span>
        <ReliabilityBadge reliability={estimate.reliability} />
      </div>

      <span
        className={`font-mono text-2xl tabular-nums ${
          defined ? "text-slate-100" : "text-slate-500 text-base"
        }`}
      >
        {defined ? renderValue(estimate.value as string, unit) : NOT_AVAILABLE}
      </span>

      {/* The reason a statistic is undefined is more useful than the gap it leaves. */}
      {!defined && estimate.undefined_reason ? (
        <p className="text-xs leading-relaxed text-amber-300/80">
          {estimate.undefined_reason}
        </p>
      ) : null}

      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span>{describeSample(estimate.sample_size)}</span>
        {estimate.interval ? (
          <span className="font-mono" title={`${estimate.interval.method}, ${formatPercent(estimate.interval.confidence, 0)} confidence`}>
            {renderValue(estimate.interval.low, unit)} to{" "}
            {renderValue(estimate.interval.high, unit)}
          </span>
        ) : null}
        {estimate.interval && !estimate.interval.excludes_zero ? (
          <span className="text-amber-400/80">spans zero</span>
        ) : null}
      </div>

      {hint ? <p className="text-[11px] text-slate-600">{hint}</p> : null}
    </div>
  );
}

const DIRECTION_TONE: Record<MetricChange["direction"], string> = {
  up: "text-emerald-400",
  down: "text-rose-400",
  flat: "text-slate-400",
  // Deliberately not grey-and-quiet like `flat`: "we cannot tell" is a different
  // statement from "it did not move", and the eye must not read them as the same.
  unchanged: "text-slate-500 italic",
};

const DIRECTION_GLYPH: Record<MetricChange["direction"], string> = {
  up: "▲",
  down: "▼",
  flat: "=",
  unchanged: "?",
};

/**
 * A period-over-period change.
 *
 * Renders an arrow **only when the change was established**. An unestablished difference
 * gets a question mark and the backend's own sentence explaining that chance produces a
 * gap that size often enough — which on a typical monthly report is almost every metric,
 * and is the honest result rather than a missing feature.
 */
export function Change({ change }: { change: MetricChange }) {
  const established = change.is_established;

  return (
    <div className="flex flex-col gap-1 border-b border-slate-800 py-3 last:border-b-0">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm text-slate-300">{change.label}</span>
        <span
          className={`font-mono text-sm tabular-nums ${DIRECTION_TONE[change.direction]}`}
        >
          <span aria-hidden className="mr-1">
            {DIRECTION_GLYPH[change.direction]}
          </span>
          {change.current ?? NOT_AVAILABLE}
        </span>
      </div>

      <p
        className={`text-[11px] leading-relaxed ${
          established ? "text-slate-400" : "text-slate-500"
        }`}
      >
        {change.narrative}
      </p>

      {!established ? (
        <span className="sr-only">
          This difference was not established as a change.
        </span>
      ) : null}
    </div>
  );
}

/**
 * A detected pattern.
 *
 * An unestablished pattern is shown — the backend stores every test it ran, and hiding the
 * failures would make recurrence unfalsifiable — but it is labelled *examined*, never
 * *found*, and never carries a cost figure.
 */
export function Finding({ finding }: { finding: PatternFinding }) {
  const established = finding.is_actionable;

  return (
    <article
      className={`rounded-lg border p-4 ${
        established
          ? finding.polarity === "leak"
            ? "border-rose-500/30 bg-rose-500/5"
            : "border-emerald-500/30 bg-emerald-500/5"
          : "border-slate-800 bg-slate-900/40"
      }`}
    >
      <header className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-medium text-slate-200">{finding.label}</h3>
        <span
          className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
            established
              ? "border-slate-600 bg-slate-800 text-slate-200"
              : "border-slate-700 text-slate-500"
          }`}
        >
          {established ? finding.polarity : "examined"}
        </span>
      </header>

      <p className="mt-1 text-xs leading-relaxed text-slate-400">
        {finding.description}
      </p>

      <dl className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
        <div className="flex gap-1">
          <dt>affected</dt>
          <dd className="font-mono text-slate-400">{finding.affected}</dd>
        </div>
        {/* A cost is attached only to an established leak. Showing one for a pattern that
            failed its test would put the loudest coincidence in the scan on the screen
            beside a dollar figure. */}
        {established && finding.unit === "currency" && finding.estimated_cost ? (
          <div className="flex gap-1">
            <dt>associated with</dt>
            <dd className="font-mono text-rose-300">
              {formatMoney(finding.estimated_cost)}
            </dd>
          </div>
        ) : null}
        {finding.adjusted_p_value ? (
          <div className="flex gap-1">
            <dt>adjusted p</dt>
            <dd className="font-mono text-slate-400">
              {formatNumber(finding.adjusted_p_value, 3)}
            </dd>
          </div>
        ) : null}
      </dl>

      {!established ? (
        <p className="mt-2 text-[11px] italic text-slate-600">
          Examined and not established. This is not something you do — it is
          something that was tested for and did not survive correction.
        </p>
      ) : null}
    </article>
  );
}

/**
 * A refusal, rendered as content.
 *
 * The temptation is an empty state — a grey card saying "no data yet". That reads as
 * "nothing happened", when what actually happened is that the platform tested something
 * and declined to claim it. The reason is the useful part.
 */
export function Refusal({
  title,
  reason,
  children,
}: {
  title: string;
  reason: string;
  children?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-amber-500/25 bg-amber-500/5 p-4">
      <h3 className="text-sm font-medium text-amber-200">{title}</h3>
      <p className="mt-1 text-xs leading-relaxed text-amber-100/70">{reason}</p>
      {children ? <div className="mt-3">{children}</div> : null}
    </div>
  );
}

/**
 * A model's prediction, or the reason there is not one.
 *
 * Most traders will see the refusal, and that is the designed outcome rather than a
 * shortfall: a few hundred trades is genuinely not enough to establish that a nine-feature
 * model beats a single number.
 */
export function ModelCard({
  report,
  value,
  label,
}: {
  report: ModelReport;
  value: string | null;
  label: string;
}) {
  if (!report.is_deployable) {
    return (
      <Refusal
        title={`${label} — not served`}
        reason={report.refusal ?? "this model did not meet the bar to be served"}
      >
        {report.skill?.interval ? (
          <p className="font-mono text-[11px] text-amber-100/50">
            skill {formatNumber(report.skill.value, 3)} (
            {formatNumber(report.skill.interval[0], 3)} to{" "}
            {formatNumber(report.skill.interval[1], 3)})
          </p>
        ) : null}
      </Refusal>
    );
  }

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-4">
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-slate-400">
          {label}
        </span>
        <span className="rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 font-mono text-[10px] uppercase text-emerald-300">
          served
        </span>
      </div>
      <span className="font-mono text-2xl tabular-nums text-slate-100">
        {value ?? NOT_AVAILABLE}
      </span>
      <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
        Out of sample across {report.folds} folds and{" "}
        {report.out_of_sample_predictions} predictions.
        {report.calibration?.is_calibrated
          ? " Stated probabilities match observed frequencies."
          : null}
      </p>
    </div>
  );
}
