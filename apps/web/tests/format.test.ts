/**
 * Formatting, and the one property that matters: **no value passes through a float.**
 *
 * `NUMERIC(20,8)` arrives from Postgres at full scale — `2.00000000` for a two-lot — and
 * the obvious way to tidy that up is `Number(value).toString()`. It is also the way to
 * silently change a number that the whole backend was built to keep exact. These tests
 * pin the lexical behaviour so a later "simplification" to `Number` fails here rather
 * than in a blotter cell nobody is checking digit by digit.
 */

import { describe, expect, it } from "vitest";

import {
  NOT_AVAILABLE,
  formatPrice,
  formatQuantity,
  trimDecimal,
} from "@/lib/format";

describe("trimDecimal", () => {
  it("drops the padding Postgres sends on a whole-number quantity", () => {
    expect(trimDecimal("2.00000000")).toBe("2");
  });

  it("keeps the significant part of a price", () => {
    expect(trimDecimal("5172.66000000")).toBe("5172.66");
  });

  it("pads back up to the minimum asked for", () => {
    expect(trimDecimal("5172.00000000", 2)).toBe("5172.00");
    expect(trimDecimal("5172.50000000", 2)).toBe("5172.50");
  });

  it("does not round — it only removes trailing zeros", () => {
    expect(trimDecimal("0.12345678")).toBe("0.12345678");
    expect(trimDecimal("1.005", 2)).toBe("1.005");
  });

  it("preserves precision a double cannot hold", () => {
    // The point of the whole module. `Number("0.10000000000000000001")` is 0.1, and
    // `Number("9007199254740993")` is 9007199254740992 — both would be displayed as a
    // different number than the engine computed.
    expect(trimDecimal("0.10000000000000000001")).toBe("0.10000000000000000001");
    expect(trimDecimal("9007199254740993.00")).toBe("9007199254740993");
  });

  it("handles negatives and leaves unrecognised forms alone", () => {
    expect(trimDecimal("-12.50000000", 2)).toBe("-12.50");
    expect(trimDecimal("1E+3")).toBe("1E+3");
    expect(trimDecimal("42")).toBe("42");
  });
});

describe("formatPrice and formatQuantity", () => {
  it("keeps a price at two places and lets a quantity go whole", () => {
    expect(formatPrice("5172.00000000")).toBe("5172.00");
    expect(formatQuantity("2.00000000")).toBe("2");
  });

  it("says what is missing rather than showing a zero", () => {
    expect(formatPrice(null)).toBe(NOT_AVAILABLE);
    expect(formatQuantity(undefined)).toBe(NOT_AVAILABLE);
  });
});
