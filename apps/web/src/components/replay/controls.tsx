"use client";

import { useEffect, useMemo, useReducer, useRef } from "react";

import {
  type Bar,
  type Gap,
  type PlaybackState,
  type Speed,
  SPEEDS,
  crossesGap,
  entryIndex,
  intervalFor,
  progress,
  reduce,
} from "@/lib/playback";
import { formatTime } from "@/lib/format";

/**
 * Playback transport.
 *
 * The bar counter is always visible, and the timeframe is stated next to it, because the
 * granularity is the single most important caveat about a replay: a one-minute replay of
 * a three-minute scalp has three data points, and a viewer who does not know that will
 * read the shape between them as something that happened.
 */
export function usePlayback(bars: Bar[], tradeStart?: string | null) {
  const origin = useMemo(() => entryIndex(bars, tradeStart), [bars, tradeStart]);

  const [state, dispatch] = useReducer(
    (current: PlaybackState, action: Parameters<typeof reduce>[1]) =>
      reduce(current, action, bars.length, origin),
    { index: -1, playing: false, speed: 1 as Speed },
  );

  // Seek to the entry once, when the bars arrive.
  //
  // The reducer's initial state cannot do this: the component renders before the query
  // resolves, so at mount `bars` is empty and there is no entry to seek to. Without this
  // the chart stays at -1 for the whole session and the fix does nothing — the state is
  // correct in the reducer and never reached in the browser.
  //
  // Guarded by a ref rather than by comparing indices, so it fires exactly once per
  // loaded series. Re-seeking whenever `origin` changed would drag the playhead back to
  // the entry underneath a trader who had scrubbed away from it.
  const seeded = useRef(false);
  useEffect(() => {
    if (seeded.current || bars.length === 0) return;
    seeded.current = true;
    if (origin >= 0) dispatch({ type: "seek", index: origin });
  }, [bars.length, origin]);

  useEffect(() => {
    if (!state.playing) return;
    const timer = window.setInterval(
      () => dispatch({ type: "tick" }),
      intervalFor(state.speed),
    );
    return () => window.clearInterval(timer);
  }, [state.playing, state.speed]);

  return { state, dispatch };
}

export function ReplayControls({
  bars,
  gaps,
  timeframe,
  state,
  dispatch,
}: {
  bars: Bar[];
  gaps: Gap[];
  timeframe: string;
  state: PlaybackState;
  dispatch: (action: Parameters<typeof reduce>[1]) => void;
}) {
  const total = bars.length;
  const current = state.index >= 0 ? bars[state.index] : undefined;
  const gapAhead = crossesGap(bars, state.index, gaps);

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-slate-800 bg-slate-900/60 p-3">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => dispatch({ type: "step", by: -1 })}
          className="rounded border border-slate-700 px-2 py-1 font-mono text-xs text-slate-300 hover:bg-slate-800"
          aria-label="Step back one bar"
        >
          ◀
        </button>
        <button
          type="button"
          onClick={() => dispatch({ type: "toggle" })}
          className="rounded border border-slate-600 bg-slate-800 px-3 py-1 font-mono text-xs text-slate-100 hover:bg-slate-700"
          aria-label={state.playing ? "Pause" : "Play"}
        >
          {state.playing ? "❚❚" : "▶"}
        </button>
        <button
          type="button"
          onClick={() => dispatch({ type: "step", by: 1 })}
          className="rounded border border-slate-700 px-2 py-1 font-mono text-xs text-slate-300 hover:bg-slate-800"
          aria-label="Step forward one bar"
        >
          ▶
        </button>
        <button
          type="button"
          onClick={() => dispatch({ type: "reset" })}
          className="rounded border border-slate-700 px-2 py-1 font-mono text-[10px] uppercase text-slate-400 hover:bg-slate-800"
        >
          Reset
        </button>

        <div className="ml-2 flex gap-1">
          {SPEEDS.map((speed) => (
            <button
              key={speed}
              type="button"
              onClick={() => dispatch({ type: "speed", speed })}
              aria-pressed={state.speed === speed}
              className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
                state.speed === speed
                  ? "bg-slate-700 text-slate-100"
                  : "text-slate-500 hover:text-slate-300"
              }`}
            >
              {speed}×
            </button>
          ))}
        </div>

        <span className="ml-auto font-mono text-[11px] tabular-nums text-slate-500">
          bar {Math.max(0, state.index + 1)} / {total}
          <span className="ml-2 text-slate-600">{timeframe}</span>
        </span>
      </div>

      <input
        type="range"
        min={-1}
        max={Math.max(-1, total - 1)}
        value={state.index}
        onChange={(event) =>
          dispatch({ type: "seek", index: Number(event.target.value) })
        }
        aria-label="Seek"
        className="w-full accent-slate-500"
      />

      <div className="flex items-center justify-between font-mono text-[10px] text-slate-600">
        <span>{current ? formatTime(current.ts) : "not started"}</span>
        <span>{(progress(state, total) * 100).toFixed(0)}%</span>
      </div>

      {/* A gap is not a flat market. Saying so is the difference between "price did not
          move" and "we have no data", which look identical on any chart that connects
          the points it has. */}
      {gapAhead ? (
        <p className="rounded border border-amber-500/25 bg-amber-500/5 px-2 py-1 text-[10px] text-amber-200/80">
          No data between {formatTime(gapAhead.from)} and {formatTime(gapAhead.to)} —
          a session break or a hole in the backfill. The next bar is not the next
          moment.
        </p>
      ) : null}
    </div>
  );
}
