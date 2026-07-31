/**
 * Formatting decimal strings without turning them back into floats.
 *
 * Every money and probability value arrives as a string because the Python engine keeps it
 * as a `Decimal` end to end. `parseFloat` at the boundary would throw that away in the last
 * step, which is the same class of mistake as the AI layer restating a computed number:
 * the care upstream is wasted by one convenience downstream.
 *
 * `Intl.NumberFormat` needs a number, so there is no avoiding the conversion for display —
 * but it happens *here*, once, at the point of rendering, and never on a value that is
 * subsequently compared, summed or stored. Nothing in this app does arithmetic on a
 * formatted figure.
 */

const MONEY = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const COMPACT_MONEY = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

/** Rendered wherever a value is genuinely undefined, never "0" or "—". */
export const NOT_AVAILABLE = "not available";

export function formatMoney(value: string | null | undefined, compact = false): string {
  if (value === null || value === undefined) return NOT_AVAILABLE;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return NOT_AVAILABLE;
  return compact ? COMPACT_MONEY.format(parsed) : MONEY.format(parsed);
}

export function formatR(value: string | null | undefined): string {
  if (value === null || value === undefined) return NOT_AVAILABLE;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return NOT_AVAILABLE;
  return `${parsed >= 0 ? "+" : ""}${parsed.toFixed(2)}R`;
}

export function formatPercent(
  value: string | null | undefined,
  digits = 1,
): string {
  if (value === null || value === undefined) return NOT_AVAILABLE;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return NOT_AVAILABLE;
  return `${(parsed * 100).toFixed(digits)}%`;
}

export function formatNumber(
  value: string | null | undefined,
  digits = 2,
): string {
  if (value === null || value === undefined) return NOT_AVAILABLE;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return NOT_AVAILABLE;
  return parsed.toFixed(digits);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return NOT_AVAILABLE;
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  const hours = Math.floor(seconds / 3600);
  return `${hours}h ${Math.floor((seconds % 3600) / 60)}m`;
}

/** Signed classes for P&L. Scratch is its own case: it is not a small win. */
export function pnlTone(value: string | null | undefined): "up" | "down" | "flat" {
  if (value === null || value === undefined) return "flat";
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed === 0) return "flat";
  return parsed > 0 ? "up" : "down";
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return NOT_AVAILABLE;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return NOT_AVAILABLE;
  return date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "2-digit",
  });
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return NOT_AVAILABLE;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return NOT_AVAILABLE;
  return date.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/** Sample-size wording, used wherever a figure is shown beside its evidence. */
export function describeSample(size: number): string {
  return size === 1 ? "1 trade" : `${size.toLocaleString("en-US")} trades`;
}
