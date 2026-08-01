"use client";

import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

import type { Bar } from "@/lib/playback";

/**
 * The replay chart.
 *
 * **Candles, not a line.** A line chart of closes hides the range each bar covered, which
 * is the only information the data has about intra-bar movement. Drawing a smooth line
 * through the closes is the visual claim that price travelled that way, and it did not —
 * nothing recorded says what happened between two closes.
 *
 * **Price lines are drawn only for prices that were recorded.** A trade with no stop gets
 * no stop line. Rendering one at a plausible level would invent the trader's plan, which
 * is the display-layer version of the imputation the ML layer refuses upstream.
 */

export interface PriceLine {
  price: string | null;
  label: string;
  color: string;
  dashed?: boolean;
}

export interface ChartMarker {
  ts: string;
  price: string;
  kind: string;
  label: string;
}

function toTime(iso: string): UTCTimestamp {
  return (Date.parse(iso) / 1000) as UTCTimestamp;
}

export function ReplayChart({
  bars,
  markers = [],
  priceLines = [],
  height = 420,
}: {
  bars: Bar[];
  markers?: ChartMarker[];
  priceLines?: PriceLine[];
  height?: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);

  useEffect(() => {
    if (!container.current) return;

    const created = createChart(container.current, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: "#0b1017" },
        textColor: "#94a3b8",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#16202e" },
        horzLines: { color: "#16202e" },
      },
      rightPriceScale: { borderColor: "#1a2332" },
      timeScale: { borderColor: "#1a2332", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
    });

    const candles = created.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#f43f5e",
      wickUpColor: "#10b981",
      wickDownColor: "#f43f5e",
      borderVisible: false,
    });

    chart.current = created;
    series.current = candles;

    const resize = () => {
      if (container.current) {
        created.applyOptions({ width: container.current.clientWidth });
      }
    };
    resize();
    window.addEventListener("resize", resize);

    return () => {
      window.removeEventListener("resize", resize);
      created.remove();
      chart.current = null;
      series.current = null;
    };
  }, [height]);

  useEffect(() => {
    if (!series.current) return;

    series.current.setData(
      bars.map((bar) => ({
        time: toTime(bar.ts),
        open: Number(bar.open),
        high: Number(bar.high),
        low: Number(bar.low),
        close: Number(bar.close),
      })),
    );
  }, [bars]);

  useEffect(() => {
    if (!series.current) return;

    const visible = new Set(bars.map((bar) => bar.ts));
    // A marker for a moment the replay has not reached yet would show the exit before
    // the trade got there — the chart would be spoiling its own playback.
    const shown = markers.filter((marker) => visible.has(marker.ts));

    createSeriesMarkers(
      series.current,
      shown.map((marker) => ({
        time: toTime(marker.ts),
        position: marker.kind.includes("entry") ? "belowBar" : "aboveBar",
        color: marker.kind.includes("entry") ? "#38bdf8" : "#fbbf24",
        shape: marker.kind.includes("entry") ? "arrowUp" : "arrowDown",
        text: marker.label,
      })),
    );
  }, [bars, markers]);

  useEffect(() => {
    const current = series.current;
    if (!current) return;

    const created = priceLines
      // Nothing is drawn for a price that was never recorded.
      .filter((line) => line.price !== null)
      .map((line) =>
        current.createPriceLine({
          price: Number(line.price),
          color: line.color,
          lineWidth: 1,
          lineStyle: line.dashed ? LineStyle.Dashed : LineStyle.Solid,
          axisLabelVisible: true,
          title: line.label,
        }),
      );

    return () => {
      for (const line of created) current.removePriceLine(line);
    };
  }, [priceLines]);

  return <div ref={container} className="w-full" />;
}
