/**
 * The playback state machine.
 *
 * Every property here is arithmetic, which is why it lives outside the chart component —
 * driving these assertions through a canvas library would test almost none of them.
 *
 * The two that matter beyond ordinary correctness: the replay never reveals a bar the
 * playhead has not reached (a chart that renders the whole series has already told the
 * viewer how the trade ends), and a gap in the data is never silently stepped over.
 */

import { describe, expect, it } from "vitest";

import {
  BASE_INTERVAL_MS,
  INITIAL,
  type Bar,
  type Gap,
  crossesGap,
  intervalFor,
  progress,
  reduce,
  revealed,
} from "@/lib/playback";

const TOTAL = 5;

function bar(iso: string): Bar {
  return { ts: iso, open: "1", high: "2", low: "0.5", close: "1.5", volume: null };
}

const BARS: Bar[] = [
  bar("2026-05-04T14:30:00Z"),
  bar("2026-05-04T14:31:00Z"),
  bar("2026-05-04T14:32:00Z"),
  // A break: the next bar is 20 hours later, across a session close.
  bar("2026-05-05T10:00:00Z"),
  bar("2026-05-05T10:01:00Z"),
];

const GAPS: Gap[] = [
  { from: "2026-05-04T14:33:00Z", to: "2026-05-05T09:59:00Z" },
];

describe("transport", () => {
  it("starts before the first bar rather than on it", () => {
    // -1 is a real state: the replay has not begun, and showing bar zero would already
    // be showing something.
    expect(INITIAL.index).toBe(-1);
    expect(revealed(BARS, INITIAL)).toEqual([]);
  });

  it("advances one bar per tick while playing", () => {
    let state = reduce(INITIAL, { type: "play" }, TOTAL);
    state = reduce(state, { type: "tick" }, TOTAL);
    state = reduce(state, { type: "tick" }, TOTAL);

    expect(state.index).toBe(1);
  });

  it("ignores ticks while paused", () => {
    const state = reduce(
      { index: 2, playing: false, speed: 1 },
      { type: "tick" },
      TOTAL,
    );

    expect(state.index).toBe(2);
  });

  it("stops at the last bar instead of running past it", () => {
    const state = reduce(
      { index: TOTAL - 1, playing: true, speed: 1 },
      { type: "tick" },
      TOTAL,
    );

    expect(state.index).toBe(TOTAL - 1);
    expect(state.playing).toBe(false);
  });

  it("restarts when play is pressed at the end", () => {
    const state = reduce(
      { index: TOTAL - 1, playing: false, speed: 1 },
      { type: "play" },
      TOTAL,
    );

    expect(state.index).toBe(-1);
    expect(state.playing).toBe(true);
  });

  it("pauses when the user steps", () => {
    // Continuing to advance underneath someone who asked to look at one bar is the most
    // irritating possible response.
    const state = reduce(
      { index: 1, playing: true, speed: 1 },
      { type: "step", by: 1 },
      TOTAL,
    );

    expect(state.index).toBe(2);
    expect(state.playing).toBe(false);
  });

  it("clamps stepping at both ends", () => {
    const start = reduce(
      { index: -1, playing: false, speed: 1 },
      { type: "step", by: -5 },
      TOTAL,
    );
    const end = reduce(
      { index: TOTAL - 1, playing: false, speed: 1 },
      { type: "step", by: 99 },
      TOTAL,
    );

    expect(start.index).toBe(-1);
    expect(end.index).toBe(TOTAL - 1);
  });

  it("clamps seeking to the series", () => {
    expect(reduce(INITIAL, { type: "seek", index: 999 }, TOTAL).index).toBe(4);
    expect(reduce(INITIAL, { type: "seek", index: -50 }, TOTAL).index).toBe(-1);
  });

  it("survives an empty series", () => {
    // A trade with no backfilled bars must not produce an index of -2 or a crash.
    const state = reduce(INITIAL, { type: "seek", index: 3 }, 0);

    expect(state.index).toBe(-1);
    expect(revealed([], state)).toEqual([]);
  });

  it("keeps the chosen speed across a reset", () => {
    const state = reduce(
      { index: 3, playing: true, speed: 4 },
      { type: "reset" },
      TOTAL,
    );

    expect(state.index).toBe(-1);
    expect(state.speed).toBe(4);
  });

  it("toggles between playing and paused", () => {
    const playing = reduce(INITIAL, { type: "toggle" }, TOTAL);
    const paused = reduce(playing, { type: "toggle" }, TOTAL);

    expect(playing.playing).toBe(true);
    expect(paused.playing).toBe(false);
  });
});

describe("speed", () => {
  it("shortens the interval as the multiplier rises", () => {
    expect(intervalFor(1)).toBe(BASE_INTERVAL_MS);
    expect(intervalFor(4)).toBe(BASE_INTERVAL_MS / 4);
    expect(intervalFor(0.5)).toBeGreaterThan(BASE_INTERVAL_MS);
  });
});

describe("revealed bars", () => {
  it("never returns a bar the playhead has not reached", () => {
    // The property that stops the chart spoiling its own playback.
    const shown = revealed(BARS, { index: 1, playing: false, speed: 1 });

    expect(shown).toHaveLength(2);
    expect(shown.at(-1)?.ts).toBe(BARS[1]?.ts);
  });

  it("returns everything at the end", () => {
    expect(
      revealed(BARS, { index: TOTAL - 1, playing: false, speed: 1 }),
    ).toHaveLength(TOTAL);
  });
});

describe("progress", () => {
  it("is zero before the start and one at the end", () => {
    expect(progress(INITIAL, TOTAL)).toBe(0);
    expect(progress({ index: TOTAL - 1, playing: false, speed: 1 }, TOTAL)).toBe(1);
  });

  it("is zero for an empty series rather than NaN", () => {
    expect(progress(INITIAL, 0)).toBe(0);
  });
});

describe("gaps", () => {
  it("detects a break between two consecutive bars", () => {
    // Bars 2 and 3 straddle a session close: the next bar is not the next moment.
    expect(crossesGap(BARS, 2, GAPS)).not.toBeNull();
  });

  it("reports no gap where the series is continuous", () => {
    expect(crossesGap(BARS, 0, GAPS)).toBeNull();
  });

  it("reports no gap at the end of the series", () => {
    expect(crossesGap(BARS, TOTAL - 1, GAPS)).toBeNull();
  });

  it("reports no gap before the replay has started", () => {
    expect(crossesGap(BARS, -1, GAPS)).toBeNull();
  });

  it("ignores an unparseable gap rather than throwing", () => {
    expect(
      crossesGap(BARS, 2, [{ from: "not-a-date", to: "also-not" }]),
    ).toBeNull();
  });
});
