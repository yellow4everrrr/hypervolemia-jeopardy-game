/**
 * The performance payload, transcribed from `GET /analytics/performance`.
 *
 * Partial by design: the endpoint returns considerably more than the dashboard reads —
 * every segmentation, the full distribution, the Monte Carlo paths — and declaring the
 * parts nobody renders would make this file a second, drifting copy of the engine's
 * output schema. The rule enforced by `tests/api/test_wire_contracts.py` runs one way:
 * what is declared here must exist, not the reverse.
 *
 * Ratios arrive as bare strings rather than as `Estimate`s. That is the engine being
 * accurate about itself: a bootstrap interval for expectancy is meaningful, and one for
 * "profit factor" is a much more delicate object than a percentile of resamples. Where a
 * figure has no interval, this app shows it with its sample size and no interval — it
 * does not manufacture one to make the card look uniform.
 */

import type { Estimate, Reliability } from "@/types/evidence";

export interface PerformanceReport {
  engine_version: number;
  computed_at: string;
  period: { start: string | null; end: string | null };
  sample: {
    trades: number;
    reliability: Reliability;
    winners: number;
    losers: number;
    scratches: number;
  };
  pnl: {
    net: string;
    gross: string;
    commission: string;
    fees: string;
    gross_profit: string;
    gross_loss: string;
    cost_ratio: string | null;
    largest_win: string | null;
    largest_loss: string | null;
    average_win: string | null;
    average_loss: string | null;
  };
  core: {
    win_rate: string | null;
    profit_factor: string | null;
    payoff_ratio: string | null;
    expectancy: Estimate;
    expectancy_r: Estimate;
    sqn: Estimate;
    average_hold_seconds: Estimate;
  };
  drawdown: {
    max: string | null;
    max_pct: string | null;
    current: string | null;
    time_underwater_pct: string | null;
    longest_drawdown_trades: number | null;
    recovery_factor: string | null;
  };
  streaks: {
    longest_wins: number;
    longest_losses: number;
    current: number;
    current_is_wins: boolean;
  };
  /**
   * `is_comparable` is the field that matters here, not `value`.
   *
   * A Sharpe computed per *trade* rather than per unit time, annualised by a
   * trade-count convention, is not the Sharpe anyone quotes — this sample produces 12.2,
   * which would be a world-historical result if it meant what the name implies. The
   * engine flags it rather than hiding it, and the dashboard must repeat the flag beside
   * the number instead of printing a headline figure that is off by an order of
   * magnitude in the reader's units.
   */
  risk_adjusted: {
    sharpe: RiskAdjusted;
    sortino: RiskAdjusted;
  };
  /** The one test that asks whether there is an edge at all. */
  edge_test: {
    expectancy: string | null;
    p_value: string | null;
    interval_low: string | null;
    interval_high: string | null;
    has_demonstrable_edge: boolean;
    undefined_reason: string | null;
  };
  position_sizing: {
    kelly_full: string | null;
    kelly_half: string | null;
    kelly_warnings: string[];
    kelly_undefined_reason: string | null;
  };
}

export interface RiskAdjusted {
  value: string | null;
  basis: string;
  observations: number;
  periods_per_year: number;
  is_comparable: boolean;
  undefined_reason: string | null;
}
