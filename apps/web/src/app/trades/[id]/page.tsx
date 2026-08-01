"use client";

import { useQuery } from "@tanstack/react-query";
import { use } from "react";

import { ReplayChart, type ChartMarker, type PriceLine } from "@/components/replay/chart";
import { ReplayControls, usePlayback } from "@/components/replay/controls";
import { Screenshots } from "@/components/replay/screenshots";
import { PageHeader } from "@/components/shell";
import { request } from "@/lib/api";
import { formatMoney, formatPrice } from "@/lib/format";
import { revealed, type Bar, type Gap } from "@/lib/playback";

interface ReplayPayload {
  /** The resolution the window *chose*, from the trade's duration. */
  primary_timeframe: string;
  /**
   * The resolution the returned bars are actually at.
   *
   * Usually the same as `primary_timeframe`, but not when a caller coarsens it — and
   * labelling the chart from the wrong one puts an interval on the axis that the candles
   * do not have.
   */
  served_timeframe: string;
  bars: Bar[];
  gaps: Gap[];
  bar_count: number;
  markers: ChartMarker[];
  risk_box: { stop_price: string | null; entry_price: string | null } | null;
  trade: {
    id: string;
    direction: "long" | "short";
    net_pnl: string;
    avg_entry_price: string | null;
    avg_exit_price: string | null;
  };
}

/**
 * Trade replay.
 *
 * The chart only ever shows bars the playhead has reached, so the outcome is not visible
 * before the replay arrives at it — a chart that renders the whole series and animates a
 * cursor across it has already told the viewer how it ends.
 */
export default function TradeReplayPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { data, isLoading, error } = useQuery({
    queryKey: ["replay", id],
    queryFn: () => request<ReplayPayload>(`/replay/trades/${id}`),
    retry: false,
  });

  const bars = data?.bars ?? [];
  const { state, dispatch } = usePlayback(bars);
  const shown = revealed(bars, state);

  const priceLines: PriceLine[] = data
    ? [
        {
          price: data.trade.avg_entry_price,
          label: "entry",
          color: "#38bdf8",
        },
        {
          price: data.trade.avg_exit_price,
          label: "exit",
          color: "#fbbf24",
        },
        // Drawn only when a stop was actually recorded. A line at a plausible level
        // would invent the trader's plan.
        {
          price: data.risk_box?.stop_price ?? null,
          label: "stop",
          color: "#f43f5e",
          dashed: true,
        },
      ]
    : [];

  return (
    <>
      <PageHeader
        title="Trade replay"
        subtitle="Stepped bar by bar. Nothing is drawn between two bars, because nothing was recorded there."
      />

      <div className="space-y-4 p-6">
        {isLoading ? <p className="text-sm text-slate-500">Loading…</p> : null}
        {error ? (
          <p className="text-sm text-rose-300">
            No replay data for this trade. Bars may not have been backfilled for its
            instrument and session.
          </p>
        ) : null}

        {data ? (
          <>
            <div className="flex flex-wrap gap-6 rounded-lg border border-slate-800 bg-slate-900/60 px-4 py-3 font-mono text-xs">
              <span className="text-slate-400">
                {data.trade.direction === "long" ? "LONG" : "SHORT"}
              </span>
              <span
                className={
                  Number(data.trade.net_pnl) >= 0
                    ? "text-emerald-400"
                    : "text-rose-400"
                }
              >
                {formatMoney(data.trade.net_pnl)}
              </span>
              <span className="text-slate-500">
                entry {formatPrice(data.trade.avg_entry_price)}
              </span>
              <span className="ml-auto text-slate-600">
                {data.bar_count} bars at {data.served_timeframe}
              </span>
            </div>

            <ReplayChart
              bars={shown}
              markers={data.markers}
              priceLines={priceLines}
            />

            <ReplayControls
              bars={bars}
              gaps={data.gaps}
              timeframe={data.served_timeframe}
              state={state}
              dispatch={dispatch}
            />

            {/* The replay is live — it re-reads bars on every visit. These are what
                survives a re-fetch, a correction, or an aged-out bar history. */}
            <Screenshots tradeId={id} />

            <p className="text-[11px] leading-relaxed text-slate-600">
              This is a reconstruction at {data.served_timeframe} resolution, not a
              recording. Each candle summarises an interval; the path price took inside
              one was never recorded, and nothing here interpolates between them.
            </p>
          </>
        ) : null}
      </div>
    </>
  );
}
