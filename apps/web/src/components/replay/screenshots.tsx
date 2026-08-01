"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, request, requestObjectUrl } from "@/lib/api";

/**
 * The captured chart images for a trade.
 *
 * Six frames, rendered server-side from the same bars the replay draws, at the six
 * moments the schema has named since milestone 1. They exist because the replay is
 * *live* — it re-reads bars each time — and a stored image is what you still have when
 * the bar history is re-fetched, corrected, or aged out.
 *
 * **The before-entry frame is the one to be careful with.** It shows the chart as it was
 * at the moment of the decision, with no exit marker on it. That is enforced in
 * `frames_for` and asserted in `test_rendering.py`, and it is the whole reason the frame
 * is worth storing: a review image that already knows the outcome is not a review.
 *
 * Images stream through the API rather than from a presigned bucket URL. A presigned URL
 * would be cheaper and would also be a capability that outlives the session, travels in a
 * referrer header, and cannot be revoked — for pictures of a trader's positions, the extra
 * hop is worth it. The cost of that choice is that the endpoint needs an auth header and
 * an `<img src>` cannot send one, so each frame is fetched through the API client and
 * held as an object URL. See `AuthedImage`.
 */

interface Screenshot {
  id: string;
  kind: string;
  timeframe: string | null;
  content_type: string;
  width: number | null;
  height: number | null;
  byte_size: number | null;
  captured_at: string;
  /** `auto` for pipeline captures, `manual` for trader uploads. */
  source: string;
}

interface CaptureOutcome {
  trade_id: string;
  captured: { kind: string; timeframe: string; bytes: number; bars: number }[];
  /** Populated when nothing was captured, with the reason. */
  skipped: string | null;
}

/** Reading order, not enum order: a gallery should read chronologically. */
const ORDER = [
  "before_entry",
  "entry",
  "exit",
  "after_exit",
  "execution_timeframe",
  "higher_timeframe",
];

const LABELS: Record<string, string> = {
  before_entry: "Before entry",
  entry: "At entry",
  exit: "At exit",
  after_exit: "After exit",
  execution_timeframe: "Execution timeframe",
  higher_timeframe: "Higher timeframe",
};

const HINTS: Record<string, string> = {
  before_entry: "What you were looking at. The exit is deliberately not drawn.",
  entry: "Entry through exit, on the timeframe the trade was taken on.",
  exit: "The exit, with what followed it.",
  after_exit: "What the instrument did once you were flat.",
  execution_timeframe: "The whole window at the trade's own resolution.",
  higher_timeframe: "The same window, aggregated, for context.",
};

/**
 * One chart image, fetched with credentials and shown from an object URL.
 *
 * A plain `<img src={endpoint}>` does not work here: the browser makes that request on
 * its own and there is no way to attach the bearer token, so the API answers 401 and the
 * tag renders broken. Worse, cross-origin Opaque Response Blocking discards the JSON
 * error before `onerror` sees anything useful, so the failure reads as a mysterious
 * `ERR_BLOCKED_BY_ORB` in the console and nothing at all in the UI.
 *
 * The object URL is revoked on unmount and whenever the id changes. Skipping that leaks
 * the decoded image for the life of the document, which for a gallery a trader scrolls
 * through means every chart they have ever looked at stays in memory.
 */
function AuthedImage({
  screenshotId,
  alt,
  width,
  height,
}: {
  screenshotId: string;
  alt: string;
  width: number | null;
  height: number | null;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let created: string | null = null;

    requestObjectUrl(`/replay/screenshots/${screenshotId}/image`, {
      signal: controller.signal,
    })
      .then((objectUrl) => {
        created = objectUrl;
        setUrl(objectUrl);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setFailed(error instanceof ApiError ? error.message : "could not be loaded");
      });

    return () => {
      controller.abort();
      if (created) URL.revokeObjectURL(created);
    };
  }, [screenshotId]);

  if (failed) {
    return (
      <div className="flex aspect-video items-center justify-center rounded border border-slate-800 text-[11px] text-amber-400/80">
        {failed}
      </div>
    );
  }

  if (!url) {
    return (
      <div className="aspect-video animate-pulse rounded border border-slate-800 bg-slate-900" />
    );
  }

  return (
    // A plain `<img>`, not `next/image`: the source is a blob URL that exists only in
    // this tab, so there is nothing for the optimiser to fetch, resize or cache.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={url}
      alt={alt}
      width={width ?? undefined}
      height={height ?? undefined}
      className="w-full rounded border border-slate-800"
    />
  );
}

export function Screenshots({ tradeId }: { tradeId: string }) {
  const client = useQueryClient();

  const shots = useQuery({
    queryKey: ["screenshots", tradeId],
    queryFn: () =>
      request<{ items: Screenshot[] }>(`/replay/trades/${tradeId}/screenshots`),
  });

  const capture = useMutation({
    mutationFn: () =>
      request<CaptureOutcome>(`/replay/trades/${tradeId}/screenshots`, {
        method: "POST",
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["screenshots", tradeId] });
    },
  });

  const items = [...(shots.data?.items ?? [])].sort(
    (a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind),
  );

  return (
    <section className="mt-6">
      <div className="flex items-center gap-4">
        <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">
          Captured charts ({items.length})
        </h2>
        <button
          type="button"
          onClick={() => capture.mutate()}
          disabled={capture.isPending}
          className="rounded border border-slate-700 bg-slate-800 px-3 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
        >
          {capture.isPending ? "Capturing…" : items.length ? "Re-capture" : "Capture"}
        </button>
      </div>

      {/* The reason nothing was captured, rather than an empty grid. A trade whose bars
          were never backfilled produces no frames on purpose — six blank images would sit
          here looking like six charts of a quiet market. */}
      {capture.data?.skipped ? (
        <p className="mt-2 text-[11px] text-amber-400/80">
          Nothing captured: {capture.data.skipped}.
        </p>
      ) : null}

      {items.length === 0 && !capture.data?.skipped ? (
        <p className="mt-2 max-w-3xl text-[11px] leading-relaxed text-slate-500">
          None yet. Capture runs automatically after a broker sync; this button is for a
          trade whose bars arrived later than it did.
        </p>
      ) : null}

      <div className="mt-3 grid gap-4 lg:grid-cols-2">
        {items.map((shot) => (
          <figure key={shot.id} className="flex flex-col gap-1">
            <AuthedImage
              screenshotId={shot.id}
              alt={`${LABELS[shot.kind] ?? shot.kind} chart for this trade`}
              width={shot.width}
              height={shot.height}
            />
            <figcaption className="flex flex-wrap items-baseline gap-x-3 text-[11px]">
              <span className="text-slate-300">{LABELS[shot.kind] ?? shot.kind}</span>
              <span className="font-mono text-slate-500">{shot.timeframe}</span>
              {shot.source !== "auto" ? (
                <span className="font-mono uppercase tracking-wider text-sky-400/80">
                  {shot.source}
                </span>
              ) : null}
              <span className="text-slate-600">{HINTS[shot.kind]}</span>
            </figcaption>
          </figure>
        ))}
      </div>
    </section>
  );
}
