# 0019 — Where the replay opens, and what is on the axis when it does

**Status:** accepted
**Date:** 2026-08-01
**Relates to:** [0013 — replay invents a path](0013-replay-invents-a-path.md),
[0018 — rendered screenshots](0018-rendered-screenshots-and-what-they-must-not-show.md)

## Context

The replay screen opened at playback index -1: nothing revealed, an empty pane with axes
and a `bar 0 / 91` counter. That is a legal state of the state machine and it was the
intended one — the replay steps bar by bar, so before the first step there is nothing to
draw.

It is also the first thing anyone sees of the feature, and it is indistinguishable from a
chart whose data failed to load. That reading was not paranoid: a genuinely blank replay
had already shipped once (ADR 0016, the 1m-to-2m aggregation returning an empty series),
and a healthy trade sitting at index -1 renders pixel-for-pixel the same as that defect.

## Decision — open on the entry bar

Not on the last bar. Revealing the whole series would put the trade's outcome on screen
before the trader has looked at the setup, which is the contamination ADR 0018 exists to
prevent, on the one screen where reviewing the decision is the entire point.

Opening at the entry shows the context that was on the chart when the decision was made
and not one bar more — the same slice the before-entry screenshot captures. `entryIndex`
returns the last bar at or before `trade_start`, or -1 when no bar precedes the entry, so
a trade whose context was never backfilled keeps the old empty-pane behaviour rather than
starting somewhere arbitrary.

`reset` returns to the entry rather than to -1, and pressing play at the end restarts
there, because "start this replay over" means the state it started in.

The seek happens in an effect rather than in the reducer's initial state. The component
mounts before the query resolves, so at mount there are no bars to seek within; a fix
that only changed the initial state would be correct in the reducer and never reach the
canvas. It is guarded by a ref so it fires once per loaded series — re-seeking whenever
the computed origin changed would drag the playhead back to the entry underneath a trader
who had scrubbed away from it.

## Decision — withhold the exit price line until playback reaches it

Found while making the change above, and coupled to it.

The exit *marker* was already hidden until the replay arrived at it. The exit *price
line* was not: it came straight from the trade record, so the price axis carried a
labelled `exit 5219.10` in its own colour from the first frame onward. The chart named
the price the trade would close at before revealing a single bar of what led there. Two
mechanisms for one rule, and only one of them was applied.

This was survivable while the replay opened on an empty chart — there was nothing to read
the level against. Opening on the entry makes it the most legible thing on screen at
exactly the moment the trader is supposed to be judging the setup, so fixing the blank
chart without this would have moved a latent leak into the default view.

`hasReached` lives in `playback.ts` rather than the chart component: it is a comparison
of two timestamps, and asserting it through a canvas-rendering library would test almost
none of it.

## Consequences

- The stop line is still drawn from the first frame. That is correct — a stop is part of
  the plan at entry, known before the outcome, and hiding it would misrepresent what the
  trader was looking at. The exit is the opposite: it is the outcome.
- The smoke suite samples canvas pixels rather than reading the bar counter, because the
  counter read `bar 0 / 91` and looked entirely plausible while the pane was blank. A
  test that asserts on the counter passes in both worlds.
