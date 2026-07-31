/**
 * The what-if sweep, transcribed from `POST /simulator/sweep`.
 *
 * One field on this type carries the whole design of the feature. `best` is the largest
 * **established** improvement, not the largest improvement — and it is `null` far more
 * often than a product manager would like, including on a profitable trader with a year
 * of history.
 *
 * That is the feature working. Sweep nine rules against a fixed history and one of them
 * wins by construction; returning that one is how a journal becomes a curve-fitting tool
 * that tells every trader their edge was two settings away. The backend corrects the nine
 * as a single FDR family and reports `best: null` when none of them survives, so the UI
 * must never fall back to sorting `results` by `difference` and calling the top row a
 * recommendation.
 */

import type { Estimate } from "@/types/evidence";

export interface Scenario {
  label: string;
  stop_r: string | null;
  target_r: string | null;
  max_trades_per_session: number | null;
  skip_after_consecutive_losses: number | null;
  only_hours: number[] | null;
}

export interface ScenarioResult {
  scenario: Scenario;
  baseline_pnl: string;
  simulated_pnl: string;
  difference: string;
  is_improvement: boolean;
  baseline_expectancy: Estimate;
  simulated_expectancy: Estimate;
  trades: {
    total: number;
    repriced: number;
    skipped: number;
    inapplicable: number;
    evaluated: number;
  };
  coverage: string | null;
  reliability: "insufficient" | "provisional" | "reliable";
  /** Survived FDR correction *and* the coverage and interval gates. */
  is_actionable: boolean;
  delta_interval: [string, string] | null;
  p_value: string | null;
  adjusted_p_value: string | null;
  effect_size: string | null;
}

export interface SweepReport {
  engine_version: number;
  sample_size: number;
  scenarios_tested: number;
  actionable: number;
  /** Null when nothing survived correction — the common and correct outcome. */
  best: ScenarioResult | null;
  results: ScenarioResult[];
  notes: string[];
  /** The engine's own caveat about hindsight. Rendered, never paraphrased. */
  interpretation: string;
}
