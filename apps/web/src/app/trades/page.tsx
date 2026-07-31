"use client";

import { PageHeader } from "@/components/shell";
import { ApiError } from "@/lib/api";
import {
  NOT_AVAILABLE,
  formatDate,
  formatDuration,
  formatMoney,
  formatR,
  formatTime,
  pnlTone,
} from "@/lib/format";
import { useTrades } from "@/lib/hooks";
import type { TradeSummary } from "@/types/evidence";

/**
 * The trade blotter.
 *
 * The screen a trader spends the most time in, so it is optimised for scanning rather
 * than for looking impressive: monospaced tabular figures, right-aligned numerics, one
 * row per round turn, no card chrome between the eye and the data.
 *
 * A trade with no recorded stop shows **no R multiple**, not a zero. Filling that gap
 * would be the display-layer version of imputing a missing value, and the whole engine
 * upstream refuses to do it.
 */

const TONE = {
  up: "text-emerald-400",
  down: "text-rose-400",
  flat: "text-slate-400",
} as const;

function Row({ trade }: { trade: TradeSummary }) {
  const tone = TONE[pnlTone(trade.net_pnl)];

  return (
    <tr className="border-b border-slate-800/60 hover:bg-slate-800/30">
      <td className="px-3 py-1.5 font-mono text-xs text-slate-500">
        {formatDate(trade.session_date ?? trade.opened_at)}
      </td>
      <td className="px-3 py-1.5 font-mono text-xs text-slate-500">
        {formatTime(trade.opened_at)}
      </td>
      <td className="px-3 py-1.5 font-mono text-xs text-slate-300">
        {trade.instrument_symbol ?? NOT_AVAILABLE}
      </td>
      <td className="px-3 py-1.5">
        <span
          className={`font-mono text-[10px] uppercase tracking-wider ${
            trade.direction === "long" ? "text-sky-400" : "text-orange-400"
          }`}
        >
          {trade.direction}
        </span>
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-xs tabular-nums text-slate-400">
        {trade.quantity}
      </td>
      <td className={`px-3 py-1.5 text-right font-mono text-xs tabular-nums ${tone}`}>
        {formatMoney(trade.net_pnl)}
      </td>
      {/* No stop recorded means no R multiple. A zero here would invent a break-even
          trade that never happened. */}
      <td className="px-3 py-1.5 text-right font-mono text-xs tabular-nums text-slate-400">
        {trade.r_multiple === null ? (
          <span className="text-slate-600" title="no stop was recorded for this trade">
            —
          </span>
        ) : (
          formatR(trade.r_multiple)
        )}
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-xs tabular-nums text-slate-500">
        {formatDuration(trade.duration_seconds)}
      </td>
      <td className="px-3 py-1.5 font-mono text-xs text-slate-500">
        {trade.setup ?? trade.strategy ?? ""}
      </td>
    </tr>
  );
}

export default function TradesPage() {
  const { data, isLoading, error } = useTrades({ limit: 200 });

  return (
    <>
      <PageHeader
        title="Trades"
        subtitle="Reconstructed from raw fills. Nothing here was entered by hand."
      />

      <div className="p-6">
        {error ? (
          <p className="rounded border border-rose-500/30 bg-rose-500/5 p-4 text-sm text-rose-200">
            {error instanceof ApiError ? error.message : "Could not load trades."}
          </p>
        ) : null}

        {isLoading ? (
          <p className="text-sm text-slate-500">Loading…</p>
        ) : null}

        {data && data.trades.length === 0 ? (
          <p className="text-sm text-slate-500">
            No trades yet. Link a broker account and they will be imported and
            reconstructed automatically.
          </p>
        ) : null}

        {data && data.trades.length > 0 ? (
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-slate-700 text-left">
                {[
                  "Session",
                  "Opened",
                  "Symbol",
                  "Side",
                  "Qty",
                  "Net P&L",
                  "R",
                  "Held",
                  "Setup",
                ].map((heading, index) => (
                  <th
                    key={heading}
                    className={`px-3 py-2 text-[10px] font-medium uppercase tracking-wider text-slate-500 ${
                      index >= 4 && index <= 7 ? "text-right" : ""
                    }`}
                  >
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.trades.map((trade) => (
                <Row key={trade.id} trade={trade} />
              ))}
            </tbody>
          </table>
        ) : null}
      </div>
    </>
  );
}
