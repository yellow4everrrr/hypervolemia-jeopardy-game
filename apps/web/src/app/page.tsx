"use client";

import { Metric, Refusal } from "@/components/evidence";
import { PageHeader } from "@/components/shell";
import {
  NOT_AVAILABLE,
  describeSample,
  formatDate,
  formatDuration,
  formatMoney,
  formatNumber,
  formatPercent,
} from "@/lib/format";
import { usePerformance } from "@/lib/hooks";
import type { Estimate, Reliability } from "@/types/evidence";
import type { PerformanceReport, RiskAdjusted } from "@/types/performance";

/**
 * The dashboard.
 *
 * Deliberately statistics-first rather than chart-first. A large equity curve is the
 * conventional hero and it is the least informative thing on the page: it goes up or
 * down, and a trader already knows which. Expectancy with its interval, and a win rate
 * with its sample size, are what actually answer "is this working".
 *
 * Two things on this screen are more important than any figure on it.
 *
 * The **edge test** leads, because it is the only element that answers the question the
 * rest of the page assumes. Expectancy of $185 a trade is a number; expectancy of $185
 * whose 95% interval excludes zero is a claim. A dashboard that prints the first and
 * omits the second has told the trader nothing about whether their results are
 * distinguishable from a lucky year.
 *
 * The **Sharpe caveat** is the second. This engine computes Sharpe per trade and
 * annualises by a trade-count convention, and says so with `is_comparable: false`. On
 * this sample that produces 12.2 — a figure that, read as the Sharpe anyone actually
 * quotes, would be among the best ever recorded. Rendering it as a headline number would
 * be the single most misleading thing this app could do, so it renders inside its
 * qualification or not at all.
 */

/**
 * Wrap a bare ratio in the sample it was computed over.
 *
 * Not a fabricated `Estimate`: every field comes from the payload — the value from the
 * engine, the sample size and reliability from `sample`, and `interval: null` because
 * there genuinely is not one. The alternative, letting these render as naked numbers
 * beside intervalled ones, is what the components in `@/components/evidence` exist to
 * prevent. What must never happen is the reverse — inventing an interval so the row of
 * cards looks uniform.
 */
function withSample(
  value: string | null,
  sample: PerformanceReport["sample"],
  undefinedReason: string,
): Estimate {
  return {
    value,
    sample_size: sample.trades,
    reliability: sample.reliability as Reliability,
    interval: null,
    undefined_reason: value === null ? undefinedReason : null,
  };
}

/** The question the rest of the page presumes an answer to. */
function EdgeTest({ report }: { report: PerformanceReport }) {
  const { edge_test: test } = report;

  if (test.undefined_reason) {
    return <Refusal title="Is there an edge?" reason={test.undefined_reason} />;
  }

  const established = test.has_demonstrable_edge;

  return (
    <div
      className={`rounded-lg border p-5 ${
        established
          ? "border-emerald-500/30 bg-emerald-500/5"
          : "border-amber-500/25 bg-amber-500/5"
      }`}
    >
      <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">
        Is there an edge?
      </h2>

      <p
        className={`mt-1 text-lg ${
          established ? "text-emerald-200" : "text-amber-200"
        }`}
      >
        {established
          ? "Yes — expectancy is distinguishable from zero."
          : "Not established. These results are consistent with no edge."}
      </p>

      <p className="mt-2 max-w-3xl text-xs leading-relaxed text-slate-400">
        Expectancy of {formatMoney(test.expectancy)} per trade over{" "}
        {describeSample(report.sample.trades)}, with a 95% interval of{" "}
        <span className="font-mono">
          {formatMoney(test.interval_low)} to {formatMoney(test.interval_high)}
        </span>
        {established
          ? ". The interval excludes zero, so a run of luck this good is not a "
          : ". The interval spans zero, so a run of luck this good is a "}
        sufficient explanation on its own.
      </p>
    </div>
  );
}

/**
 * A risk-adjusted ratio, rendered inside its own comparability warning.
 *
 * When `is_comparable` is false the number is deliberately made subordinate to the
 * caveat rather than the other way round — smaller, greyer, and below the sentence
 * explaining why it is not the quantity its name suggests.
 */
function RiskAdjustedCard({ label, ratio }: { label: string; ratio: RiskAdjusted }) {
  if (ratio.undefined_reason) {
    return <Refusal title={label} reason={ratio.undefined_reason} />;
  }

  if (!ratio.is_comparable) {
    return (
      <div className="rounded-lg border border-amber-500/25 bg-amber-500/5 p-4">
        <span className="text-xs font-medium uppercase tracking-wider text-amber-200/80">
          {label}
        </span>
        <p className="mt-1 text-[11px] leading-relaxed text-amber-100/70">
          Computed per trade over {ratio.observations} periods on a{" "}
          {ratio.basis} basis, not per unit of time. It is usable for comparing this
          account against itself; it is not the {label.toLowerCase()} you would compare
          against a fund.
        </p>
        <span className="mt-2 block font-mono text-sm tabular-nums text-amber-100/60">
          {formatNumber(ratio.value)}
        </span>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-4">
      <span className="text-xs font-medium uppercase tracking-wider text-slate-400">
        {label}
      </span>
      <span className="mt-1 block font-mono text-2xl tabular-nums text-slate-100">
        {formatNumber(ratio.value)}
      </span>
      <p className="mt-1 text-[11px] text-slate-500">
        {ratio.observations} periods, {ratio.basis} basis
      </p>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  tone = "text-slate-200",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="flex flex-col gap-0.5 border-b border-slate-800 py-2.5 last:border-b-0">
      <span className="text-[11px] uppercase tracking-wider text-slate-500">
        {label}
      </span>
      <span className={`font-mono text-sm tabular-nums ${tone}`}>{value}</span>
      {hint ? <span className="text-[11px] text-slate-600">{hint}</span> : null}
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
      <h2 className="mb-1 text-xs font-medium uppercase tracking-wider text-slate-400">
        {title}
      </h2>
      {children}
    </section>
  );
}

export default function DashboardPage() {
  const { data, isLoading, error } = usePerformance();

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="Every figure carries its sample size and, where one exists, its confidence interval."
      />

      <div className="space-y-6 p-6">
        {isLoading ? <p className="text-sm text-slate-500">Computing…</p> : null}

        {error ? (
          <p className="rounded border border-rose-500/30 bg-rose-500/5 p-4 text-sm text-rose-200">
            Could not compute performance.
          </p>
        ) : null}

        {data && data.sample.trades === 0 ? (
          <Metric
            label="Expectancy"
            estimate={{
              value: null,
              sample_size: 0,
              reliability: "insufficient",
              undefined_reason:
                "no trades have been imported yet — link a broker account to begin",
            }}
            unit="currency"
            hint="Average P&L per trade, bootstrapped"
          />
        ) : null}

        {data && data.sample.trades > 0 ? (
          <>
            <EdgeTest report={data} />

            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              <Metric
                label="Expectancy"
                estimate={data.core.expectancy}
                unit="currency"
                hint="Average P&L per trade, bootstrapped"
              />
              <Metric
                label="Expectancy in R"
                estimate={data.core.expectancy_r}
                unit="r"
                hint="Trades with no recorded stop are skipped, not zeroed"
              />
              <Metric
                label="Win rate"
                estimate={withSample(
                  data.core.win_rate,
                  data.sample,
                  "no closed trades",
                )}
                unit="percent"
                hint={`${data.sample.winners} winners, ${data.sample.losers} losers`}
              />
              <Metric
                label="Profit factor"
                estimate={withSample(
                  data.core.profit_factor,
                  data.sample,
                  "undefined without a losing trade to divide by",
                )}
                hint="Gross profit over gross loss"
              />
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              <Panel title="P&L">
                <Stat
                  label="Net"
                  value={formatMoney(data.pnl.net)}
                  tone={
                    data.pnl.net.startsWith("-")
                      ? "text-rose-400"
                      : "text-emerald-400"
                  }
                />
                <Stat label="Gross" value={formatMoney(data.pnl.gross)} />
                <Stat
                  label="Costs"
                  value={formatMoney(
                    String(Number(data.pnl.commission) + Number(data.pnl.fees)),
                  )}
                  hint={
                    data.pnl.cost_ratio
                      ? `${formatPercent(data.pnl.cost_ratio)} of gross profit`
                      : undefined
                  }
                  tone="text-rose-300"
                />
                <Stat
                  label="Average win / loss"
                  value={`${formatMoney(data.pnl.average_win)} / ${formatMoney(
                    data.pnl.average_loss,
                  )}`}
                />
                <Stat
                  label="Largest win / loss"
                  value={`${formatMoney(data.pnl.largest_win)} / ${formatMoney(
                    data.pnl.largest_loss,
                  )}`}
                />
              </Panel>

              <Panel title="Drawdown and streaks">
                <Stat
                  label="Maximum drawdown"
                  value={`${formatMoney(data.drawdown.max)}${
                    data.drawdown.max_pct
                      ? ` (${formatPercent(data.drawdown.max_pct)})`
                      : ""
                  }`}
                  tone="text-rose-300"
                  hint={
                    data.drawdown.longest_drawdown_trades !== null
                      ? `longest run underwater: ${data.drawdown.longest_drawdown_trades} trades`
                      : undefined
                  }
                />
                <Stat
                  label="Currently underwater by"
                  value={formatMoney(data.drawdown.current)}
                />
                <Stat
                  label="Time underwater"
                  value={formatPercent(data.drawdown.time_underwater_pct)}
                  hint="Share of trades taken while below a prior equity peak"
                />
                <Stat
                  label="Longest streaks"
                  value={`${data.streaks.longest_wins} wins / ${data.streaks.longest_losses} losses`}
                />
                <Stat
                  label="Current streak"
                  value={`${data.streaks.current} ${
                    data.streaks.current_is_wins ? "wins" : "losses"
                  }`}
                  tone={
                    data.streaks.current_is_wins
                      ? "text-emerald-400"
                      : "text-rose-400"
                  }
                />
              </Panel>

              <Panel title="Quality of the sample">
                <Stat
                  label="Period"
                  value={`${formatDate(data.period.start)} — ${formatDate(
                    data.period.end,
                  )}`}
                />
                <Stat
                  label="Trades"
                  value={describeSample(data.sample.trades)}
                  hint={`reliability: ${data.sample.reliability}`}
                />
                <Stat
                  label="System quality (SQN)"
                  value={
                    data.core.sqn.value === null
                      ? NOT_AVAILABLE
                      : formatNumber(data.core.sqn.value)
                  }
                  hint="Expectancy over its own standard deviation, scaled by sample size"
                />
                <Stat
                  label="Average hold"
                  value={
                    data.core.average_hold_seconds.value === null
                      ? NOT_AVAILABLE
                      : formatDuration(
                          Math.round(Number(data.core.average_hold_seconds.value)),
                        )
                  }
                />
                <Stat
                  label="Computed"
                  value={new Date(data.computed_at).toLocaleString("en-US")}
                  hint={`engine version ${data.engine_version}`}
                />
              </Panel>
            </div>

            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              <RiskAdjustedCard label="Sharpe" ratio={data.risk_adjusted.sharpe} />
              <RiskAdjustedCard label="Sortino" ratio={data.risk_adjusted.sortino} />

              {/* Kelly is shown with its warnings attached or not at all. A "suggested
                  size" of 28% of capital, rendered as a clean number on a dashboard,
                  is an instruction to blow up an account. */}
              {data.position_sizing.kelly_undefined_reason ? (
                <Refusal
                  title="Kelly fraction"
                  reason={data.position_sizing.kelly_undefined_reason}
                />
              ) : (
                <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-4 sm:col-span-2">
                  <span className="text-xs font-medium uppercase tracking-wider text-slate-400">
                    Kelly fraction
                  </span>
                  <span className="mt-1 block font-mono text-2xl tabular-nums text-slate-100">
                    {formatPercent(data.position_sizing.kelly_full)}
                    <span className="ml-2 text-sm text-slate-500">
                      half-Kelly {formatPercent(data.position_sizing.kelly_half)}
                    </span>
                  </span>
                  {data.position_sizing.kelly_warnings.map((warning) => (
                    <p
                      key={warning}
                      className="mt-2 text-[11px] leading-relaxed text-amber-300/80"
                    >
                      {warning}
                    </p>
                  ))}
                </div>
              )}
            </div>
          </>
        ) : null}
      </div>
    </>
  );
}
