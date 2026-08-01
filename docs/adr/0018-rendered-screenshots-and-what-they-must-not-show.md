# 0018 — Rendered screenshots, and what they must not show

**Status:** accepted
**Date:** 2026-08-01
**Relates to:** [0013 — replay invents a path](0013-replay-invents-a-path.md),
[0015 — outcome leakage in clustering](0015-outcome-leakage-in-clustering.md)

## Context

The `screenshots` table, the `ScreenshotKind` enum and the object store have existed since
milestone 1. The schema docstring says "capture is automatic at six moments per trade."
No code ever wrote a row. The feature was fully described and entirely absent.

Three decisions had to be made to build it, and each had an obvious answer that was wrong.

## Decision 1 — Render the charts, do not photograph them

The obvious reading of "automatic screenshot" is a headless browser pointed at the replay
page. Rejected:

- It puts a Chromium in the API image and seconds of latency in every capture.
- It produces a picture whose contents no test can assert on. "Did this image draw the
  candles?" becomes a human judgement.
- It photographs whatever the trader's indicators happened to be that day, which makes
  the image a record of the UI rather than of the market.

Instead `app/rendering/` draws the same bars the replay endpoint already serves, using
PNG encoding built on stdlib `zlib`. This is deterministic, runs in milliseconds, and can
be asserted on pixel by pixel — which matters for the next decision.

The cost is real: a drawing the trader made by hand cannot be reproduced. Manual upload
stays supported, and `Screenshot.source` distinguishes `auto` from `manual`.

Because the render is a pure function of the bars and the storage key is a hash of the
image bytes, re-capture is idempotent by construction — which the at-least-once job queue
requires, since a worker that dies after storing the object but before committing the row
will redo both.

## Decision 2 — The before-entry frame must not draw the exit

This is the same failure as ADR 0015, in picture form.

A frame captioned "what did this look like before I took it?" with the exit marker on it
is a chart that knows the answer. Every judgement a trader forms from it is contaminated
by the outcome, and **nothing about the image looks wrong**: the marker is correctly
placed, the candles are real, no error is raised anywhere.

`frames_for` filters markers to those inside the rendered slice, and
`test_rendering.py::test_the_before_entry_frame_does_not_draw_the_exit` asserts the
before-entry PNG is byte-identical to what the renderer produces from that slice with the
exit marker absent. Byte equality rather than an inspection, because the whole problem is
that the contaminated image looks fine.

## Decision 3 — The sweep must only offer trades it can actually capture

Capture runs as one swept job after a sync rather than a job per trade: a backfill of six
months would otherwise bury every other job kind behind thousands of captures.

The first version selected every uncaptured closed trade, newest first, capped at 200. On
the demo data — 1,447 closed trades, one day of backfilled bars — two consecutive sweeps
each loaded 200 windows, rendered nothing and finished with the same trades outstanding.
Nothing errored. Every one of those trades genuinely had no bars, each was reported as
`skipped`, and the job succeeded. But a fixed cap over a recency ordering returns *the
same uncapturable page every run*, so a trade below the cap whose bars arrived later would
never be reconsidered. The docstring claiming the sweep self-heals was stating an
intention.

The correction contains a trap worth recording, because the obvious form of it is wrong.
Filtering to trades with a bar inside the trade's own span cut the demo from four captured
trades to two — silently, with the sweep still reporting success. The window a capture
renders is far wider than the trade: 120 bars of setup before entry, 60 after the exit. A
two-minute trade in a quiet stretch can contain no bar at all and still render six good
frames.

So the clause pads by `max_window_padding()`, the widest lead and trail `build_window` can
produce, derived from the same constants. It is a **bound, not a prediction**: it keeps a
superset of the capturable trades, so it can only ever discard trades that would have
skipped anyway, and the in-sweep skip remains the authoritative answer. That property is
what lets it discard thousands of trades without ever discarding a capturable one, and it
is asserted across every timeframe threshold in `test_replay.py`.

## Decision 4 — Stream the images through the API, and accept what that costs

A presigned bucket URL is cheaper. It is also a capability that outlives the session,
travels in a referrer header and cannot be revoked. For pictures of a trader's open
positions, the extra hop is worth it.

The consequence was not obvious and cost a full round trip to find: **an `<img src>`
cannot carry an `Authorization` header.** The browser issues that request itself, so a
private endpoint answers 401 and the tag renders broken — and because the response is
cross-origin with a JSON body, Opaque Response Blocking discards it before `onerror` sees
anything. The page rendered its heading, its count and six captions around six invisible
broken images. The only trace was `ERR_BLOCKED_BY_ORB` in the console and six unremarkable
401s in the API log.

It would also have rendered perfectly in any environment with auth disabled, which is
every local setup — so the first place it could fail was a deployment with real users.

`requestObjectUrl` fetches the bytes through the API client, which attaches credentials,
and the gallery renders an object URL, revoked on unmount. The smoke suite asserts
`naturalWidth > 0` on all six images in a real browser, because every text assertion on
that page passes either way.

## Consequences

- Screenshots are re-renderable from bars, so losing the object store loses nothing
  permanent. This is why the local store is an in-memory dict and why that is acceptable
  here but not for secrets.
- Locally the API and the worker each own a separate in-memory store, so bytes written by
  the sweep are unreadable by the API and the gallery 404s while the rows look healthy.
  Deployed, both point at the same bucket. Documented at `get_object_store`, because the
  only environment it appears in is the one where it will be mistaken for a capture bug.
- Manual uploads remain the escape hatch for anything the renderer cannot reproduce.
