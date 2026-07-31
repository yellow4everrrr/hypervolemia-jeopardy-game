/**
 * Replay playback: a pure state machine over an array of bars.
 *
 * Separated from the chart deliberately. Every interesting property here — clamping at
 * the ends, not stepping across a data gap, what "position" means when the series is
 * sparse — is arithmetic, and testing it through a canvas-rendering chart library would
 * mean testing almost none of it.
 *
 * **The replay steps bar by bar and never interpolates.** That is the milestone's
 * central constraint rather than a simplification. A bar is an open, high, low and close
 * over an interval; the path price took *within* that interval was not recorded and is
 * not knowable from it. Animating smoothly between two closes draws a straight line
 * through territory nobody observed, and it does so with the visual authority of a
 * recording.
 *
 * The same reasoning already appears upstream: ADR 0008 notes that excursions record
 * both extremes without recording which came first, which is why the what-if simulator
 * resolves a stop before a target. A replay that animated a smooth path would be
 * asserting the ordering that ADR refuses to assume.
 */

export interface Bar {
  ts: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string | null;
}

export interface Gap {
  from: string;
  to: string;
}

/** Playback rates. Capped at 16x — beyond that a bar is on screen for one frame. */
export const SPEEDS = [0.5, 1, 2, 4, 8, 16] as const;
export type Speed = (typeof SPEEDS)[number];

/** Milliseconds a bar is displayed at 1x. */
export const BASE_INTERVAL_MS = 400;

export interface PlaybackState {
  /** Index of the last bar currently revealed. -1 means nothing shown yet. */
  index: number;
  playing: boolean;
  speed: Speed;
}

export const INITIAL: PlaybackState = { index: -1, playing: false, speed: 1 };

export type PlaybackAction =
  | { type: "play" }
  | { type: "pause" }
  | { type: "toggle" }
  | { type: "step"; by: number }
  | { type: "seek"; index: number }
  | { type: "speed"; speed: Speed }
  | { type: "reset" }
  | { type: "tick" };

export function reduce(
  state: PlaybackState,
  action: PlaybackAction,
  total: number,
): PlaybackState {
  const last = total - 1;

  switch (action.type) {
    case "play":
      // Playing from the end restarts rather than doing nothing, which is what a user
      // pressing play on a finished replay means.
      return state.index >= last
        ? { ...state, index: -1, playing: true }
        : { ...state, playing: true };

    case "pause":
      return { ...state, playing: false };

    case "toggle":
      return reduce(state, { type: state.playing ? "pause" : "play" }, total);

    case "step":
      // Stepping pauses. A user who steps wants to look at that bar, and continuing to
      // advance underneath them is the most irritating possible response.
      return {
        ...state,
        playing: false,
        index: clamp(state.index + action.by, -1, last),
      };

    case "seek":
      return { ...state, index: clamp(action.index, -1, last) };

    case "speed":
      return { ...state, speed: action.speed };

    case "reset":
      return { ...INITIAL, speed: state.speed };

    case "tick": {
      if (!state.playing) return state;
      if (state.index >= last) return { ...state, index: last, playing: false };
      return { ...state, index: state.index + 1 };
    }
  }
}

function clamp(value: number, low: number, high: number): number {
  if (high < low) return low;
  return Math.min(high, Math.max(low, value));
}

export function intervalFor(speed: Speed): number {
  return BASE_INTERVAL_MS / speed;
}

/** Fraction of the replay elapsed, for a progress bar. */
export function progress(state: PlaybackState, total: number): number {
  if (total <= 0) return 0;
  return Math.max(0, Math.min(1, (state.index + 1) / total));
}

/**
 * Whether the next step crosses a recorded gap in the data.
 *
 * A gap is a session close, a halt, or a hole in the backfill. Playing straight through
 * one implies continuous trading across it. The chart breaks its line at these points and
 * the controls surface them, so a trader can tell "price did not move here" apart from
 * "we have no data here" — which look identical on a chart that just connects the dots.
 */
export function crossesGap(bars: Bar[], index: number, gaps: Gap[]): Gap | null {
  const current = bars[index];
  const next = bars[index + 1];
  if (!current || !next) return null;

  const from = Date.parse(current.ts);
  const to = Date.parse(next.ts);
  if (Number.isNaN(from) || Number.isNaN(to)) return null;

  for (const gap of gaps) {
    const start = Date.parse(gap.from);
    const end = Date.parse(gap.to);
    if (Number.isNaN(start) || Number.isNaN(end)) continue;
    if (start >= from && end <= to) return gap;
  }
  return null;
}

/** Bars revealed so far. An empty slice at index -1 is a valid state, not an error. */
export function revealed(bars: Bar[], state: PlaybackState): Bar[] {
  return state.index < 0 ? [] : bars.slice(0, state.index + 1);
}
