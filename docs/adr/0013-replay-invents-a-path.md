# ADR 0013 — A replay reconstructs; it does not record

**Status:** Accepted
**Date:** 2026-07-31
**Extends:** [ADR 0008](./0008-counterfactual-simulation.md), [ADR 0012](./0012-frontend-honesty.md)

## Context

Milestone 6 builds the trade replay: TradingView Lightweight Charts, play/pause/seek/speed,
entry and exit markers, a risk box.

Replay is the feature that sells a trading journal. It is also the one where the interface
makes a claim the data cannot support, and unlike every previous milestone the claim is not
in a number — it is in the *motion*.

The chart has bars. A bar is an open, a high, a low and a close over an interval. The path
price took **within** that interval was never recorded, and it is not derivable from the
four numbers that summarise it. That is not a gap in this implementation; it is what a bar
is.

A conventional replay draws a smooth line and animates a cursor along it, and it does so
with the visual authority of a recording. A trader watching it will believe they are seeing
what happened, and for everything between two closes they are seeing an interpolation
somebody's charting library invented.

## Decision 1 — step bar by bar, never interpolate

The playback state machine advances one bar per tick and the chart renders candles. There
is no tweening, no smoothed line, no sub-bar cursor.

Candles rather than a line chart, for the same reason: a line of closes discards the high
and the low, which is the *only* information the data has about intra-bar movement. Drawing
through the closes hides the range and asserts the path in one gesture.

This is the same constraint ADR 0008 already applied upstream. Excursions record that both
extremes were reached without recording which came first, which is why the what-if simulator
resolves a stop before a target rather than assuming an order. A replay that animated a
smooth path would be asserting exactly the ordering that ADR refuses to assume — and doing
it visually, where nobody thinks to ask for the evidence.

The cost is that a three-minute scalp replayed at one-minute resolution is three candles and
looks unimpressive. It *is* three observations. The alternative is a beautiful animation of
data that does not exist.

## Decision 2 — the chart shows only what the playhead has reached

`revealed()` slices the series at the current index and the chart receives only that. A
marker for a moment the replay has not reached is filtered out.

The obvious implementation renders the whole series and moves a cursor across it, which is
simpler and has already told the viewer how the trade ends before the replay starts. The
entire point of replaying a trade is to sit in the decision without knowing the outcome.

`index = -1` is a real state — "not started" — rather than an off-by-one to be avoided.
Beginning at bar zero is already showing something.

## Decision 3 — a gap is not a flat market

The backend surfaces `gaps` explicitly, with a comment saying why: *so the chart can break
the line rather than drawing a straight segment across a session close as though price
moved through it.*

Any chart that connects the points it has renders "no data here" and "price did not move
here" identically. On a futures chart the overnight break is the largest gap in the series
and the most misleading thing to draw through.

So the transport surfaces the gap in words when the playhead is about to cross one: *the
next bar is not the next moment*. `crossesGap` is pure and tested, including against
unparseable timestamps, because a malformed gap must not throw inside a render.

## Decision 4 — only recorded prices get a line

A stop line is drawn when a stop was recorded, and not otherwise. Same for the target.

Rendering a dashed line at a plausible level would invent the trader's plan — the
display-layer version of the imputation the ML layer refuses in `r_label`, and of the zero
the blotter refuses to show for a trade with no R multiple. It would also be nearly
impossible to notice, because a chart with a stop line looks *more* complete than one
without.

## Decision 5 — the playback engine is pure and lives outside the chart

`app/web/src/lib/playback.ts` is a reducer over an array with no reference to the charting
library. Everything worth asserting — clamping at both ends, pausing on step, restarting
from the end, surviving an empty series, refusing to reveal an unreached bar — is
arithmetic, and driving those assertions through a canvas renderer would test almost none of
them.

21 tests cover it. The empty-series case is explicit: a trade whose instrument was never
backfilled must produce a quiet "no replay data" rather than an index of −2.

## Consequences

* **The replay will look less impressive than a competitor's.** Theirs animates smoothly
  because it is drawing a line nobody measured. This is the same trade-off ADR 0012 named
  for the dashboard, in its most visible form, and it is worth stating that the honest
  version demos worse.
* A trade with no backfilled bars gets an explanation rather than an empty chart. Bar
  coverage is uneven by construction — the backfill reaches liquid instruments during the
  hours it ran — and `POST /replay/excursions` already reports coverage for the same reason.
* Speed caps at 16×, beyond which a bar is on screen for roughly one frame and the replay
  has stopped being one.
* The bar count and timeframe are always visible. A viewer who does not know they are
  looking at three data points will read the shape between them as something that happened.
* Milestone 4's marker and risk-box computation is consumed unchanged. The frontend adds no
  price arithmetic of its own — consistent with ADR 0012's rule that decimals stay strings
  to the point of render.
