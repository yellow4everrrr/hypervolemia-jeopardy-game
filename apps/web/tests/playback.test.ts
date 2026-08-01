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
  entryIndex,
  hasReached,
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

describe("where the replay opens", () => {
  /**
   * The screen used to open at index -1 — nothing revealed — which renders as an empty
   * pane with axes and reads as a chart whose data failed to load. That reading was not
   * unreasonable: a genuinely blank replay was a real defect (ADR 0016), and a healthy
   * trade sitting at -1 looks identical to it.
   *
   * The fix has to open at the *entry*, not at the end. Revealing the whole series would
   * make the trade's outcome the first thing on screen, before the trader has looked at
   * the setup — the contamination ADR 0018 exists to prevent, on the one screen where
   * reviewing the decision is the entire point.
   */
  const ENTRY = "2026-05-04T14:32:00Z";

  it("opens on the entry bar", () => {
    expect(entryIndex(BARS, ENTRY)).toBe(2);
  });

  it("opens on the last bar at or before the entry when none lands on it exactly", () => {
    expect(entryIndex(BARS, "2026-05-04T14:31:30Z")).toBe(1);
  });

  it("reveals nothing after the entry", () => {
    // The assertion this change is for. An off-by-one here puts a bar the trader had not
    // seen at decision time into the opening view, and it would look completely normal.
    const opening = revealed(BARS, { ...INITIAL, index: entryIndex(BARS, ENTRY) });

    expect(opening).toHaveLength(3);
    for (const shown of opening) {
      expect(Date.parse(shown.ts)).toBeLessThanOrEqual(Date.parse(ENTRY));
    }
  });

  it("falls back to an empty chart when no bar precedes the entry", () => {
    // A trade whose context bars were never backfilled. Better a blank pane than a
    // silently wrong starting point.
    expect(entryIndex(BARS, "2026-05-04T14:00:00Z")).toBe(-1);
    expect(entryIndex([], ENTRY)).toBe(-1);
    expect(entryIndex(BARS, null)).toBe(-1);
    expect(entryIndex(BARS, "not a date")).toBe(-1);
  });

  it("resets to the entry rather than to an empty chart", () => {
    const origin = entryIndex(BARS, ENTRY);
    const scrubbed = reduce(INITIAL, { type: "seek", index: 4 }, TOTAL, origin);

    expect(reduce(scrubbed, { type: "reset" }, TOTAL, origin).index).toBe(origin);
  });

  it("replays from the entry when play is pressed at the end", () => {
    const origin = entryIndex(BARS, ENTRY);
    const finished = reduce(INITIAL, { type: "seek", index: TOTAL - 1 }, TOTAL, origin);

    const restarted = reduce(finished, { type: "play" }, TOTAL, origin);

    expect(restarted.index).toBe(origin);
    expect(restarted.playing).toBe(true);
  });

  it("still starts from nothing when no origin is supplied", () => {
    // The default keeps every existing caller and every test above behaving as before.
    expect(reduce(INITIAL, { type: "reset" }, TOTAL).index).toBe(-1);
  });
});

describe("the exit level", () => {
  /**
   * The exit *marker* was already withheld until playback reached it. The exit *price
   * line* was not — it came straight from the trade record, so the axis carried a
   * labelled `exit 5219.10` from the first frame onward. The chart named the price the
   * trade would close at before showing a single bar of what led there.
   *
   * That was survivable while the replay opened on an empty chart, because there was
   * nothing to read it against. Opening on the entry makes it the most legible thing on
   * screen at exactly the moment the trader is supposed to be judging the setup.
   */
  const EXIT = "2026-05-05T10:00:00Z";

  it("is hidden before playback reaches it", () => {
    const atEntry = revealed(BARS, { ...INITIAL, index: 2 });

    expect(hasReached(atEntry, EXIT)).toBe(false);
  });

  it("appears once playback arrives", () => {
    expect(hasReached(revealed(BARS, { ...INITIAL, index: 3 }), EXIT)).toBe(true);
    expect(hasReached(revealed(BARS, { ...INITIAL, index: 4 }), EXIT)).toBe(true);
  });

  it("is hidden on an empty chart and for a trade with no recorded exit", () => {
    expect(hasReached([], EXIT)).toBe(false);
    expect(hasReached(BARS, null)).toBe(false);
    expect(hasReached(BARS, "not a date")).toBe(false);
  });
});
