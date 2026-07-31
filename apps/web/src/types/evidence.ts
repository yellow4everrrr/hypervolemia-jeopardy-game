/**
 * The shapes the backend returns, transcribed exactly.
 *
 * Every one of these carries a qualification alongside its number: a sample size, a
 * reliability grade, a confidence interval, a significance verdict, or a reason the value
 * is missing. Twelve backend milestones exist to attach those qualifications and refuse to
 * state anything without them.
 *
 * **The frontend is where that discipline dies.** A `MetricChange` becomes a green arrow.
 * An `Estimate` becomes a big number in a card. A refusal becomes an empty state that
 * reads as "nothing to report" rather than "we could not establish this". None of those
 * is a bug anybody files — the page looks finished.
 *
 * So the types are written to make the lossy version awkward. `Estimate["value"]` is
 * `string | null`, not `number`, which forces a caller to handle the null; and the
 * components in `@/components/evidence` take these objects rather than the primitives
 * inside them, so there is no way to render the number without its qualification in scope.
 *
 * Decimals arrive as **strings**, deliberately, all the way from the Python engine. Money
 * and probabilities are `Decimal` server-side precisely so they do not drift, and parsing
 * them into JavaScript numbers at the boundary would discard that on arrival. They are
 * formatted from the string.
 */

/** How much weight a result can bear. Mirrors `app.analytics.types.Reliability`. */
export type Reliability = "insufficient" | "provisional" | "reliable";

export interface Interval {
  low: string;
  high: string;
  confidence: string;
  method: string;
  excludes_zero: boolean;
}

/**
 * A point estimate with its uncertainty and sample size.
 *
 * `value` is null when the statistic is undefined for the sample — profit factor with no
 * losing trades, expectancy in R with no recorded stops. `undefined_reason` says which,
 * in language meant for a trader.
 */
export interface Estimate {
  value: string | null;
  sample_size: number;
  reliability: Reliability;
  interval?: Interval | null;
  undefined_reason?: string | null;
}

/**
 * One metric compared across periods.
 *
 * `direction` distinguishes `flat` from `unchanged`, and the difference is the whole
 * point: `flat` means the measured values matched, `unchanged` means the measurement could
 * not tell them apart. A UI that renders both as a grey dash reports a settled fact where
 * there is an open question.
 */
export interface MetricChange {
  key: string;
  label: string;
  current: string | null;
  previous: string | null;
  difference: string | null;
  current_sample: number;
  previous_sample: number;
  is_established: boolean;
  direction: "up" | "down" | "flat" | "unchanged";
  narrative: string;
  p_value?: string | null;
  adjusted_p_value?: string | null;
  effect_size?: string | null;
}

/** A behavioural or cluster pattern. Never a finding until `is_actionable`. */
export interface PatternFinding {
  kind: string;
  label: string;
  description: string;
  polarity: "leak" | "edge";
  affected: number;
  unaffected: number;
  unit: "currency" | "r_multiple" | "seconds";
  difference: string | null;
  estimated_cost: string | null;
  reliability: Reliability;
  is_actionable: boolean;
  adjusted_p_value?: string | null;
}

/** Deduplicated leak cost. `naive_sum` is carried so the overlap stays visible. */
export interface LeakSummary {
  total_cost: string | null;
  naive_sum: string | null;
  overstatement_factor: string | null;
  trades_touched: number;
  trades_considered: number;
  coverage: string | null;
  leaks: LeakAttribution[];
  undefined_reason: string | null;
  interpretation: string;
}

export interface LeakAttribution {
  kind: string;
  label: string;
  affected: number;
  estimated_cost: string | null;
  exclusive_trades: number;
  exclusive_cost: string | null;
  shared_trades: number;
  is_wholly_shared: boolean;
}

/**
 * A trained model and whether it may be served.
 *
 * `refusal` is populated exactly when `is_deployable` is false — the database enforces it.
 * A UI showing an empty probability card instead of the refusal has thrown away the most
 * useful thing the model produced.
 */
export interface ModelReport {
  head: "win_probability" | "expected_r";
  version: number;
  is_deployable: boolean;
  refusal?: string | null;
  folds: number;
  out_of_sample_predictions: number;
  skill: SkillEvidence | null;
  calibration?: CalibrationReport | null;
}

export interface SkillEvidence {
  value: string | null;
  interval: [string, string] | null;
  sessions: number;
  predictions: number;
  established: boolean;
  caveat: string;
}

export interface CalibrationReport {
  sample_size: number;
  base_rate: string | null;
  brier: string | null;
  skill: string | null;
  expected_calibration_error: string | null;
  calibration_p_value: string | null;
  beats_baseline: boolean;
  is_calibrated: boolean;
  is_reliable: boolean;
  interpretation: string;
}

/** One block of a periodic report, which may be withheld with a reason. */
export interface ReportSection {
  key: string;
  title: string;
  is_present: boolean;
  withheld_reason: string | null;
  data?: Record<string, unknown>;
}

export interface PeriodicReport {
  period: { type: string; start: string; end: string; label: string };
  version: number;
  generated_at: string;
  sessions: number;
  trades: number;
  headline: string;
  /** False when the period is too thin to interpret. Sections still render. */
  draws_conclusions: boolean;
  sections: ReportSection[];
  notes: string[];
}

export interface TradeSummary {
  id: string;
  opened_at: string;
  closed_at: string | null;
  direction: "long" | "short";
  instrument_symbol: string | null;
  quantity: string;
  net_pnl: string;
  r_multiple: string | null;
  duration_seconds: number | null;
  session_date: string | null;
  strategy: string | null;
  setup: string | null;
  compliance_score: string | null;
}

export interface Job {
  id: string;
  kind: string;
  state: "pending" | "running" | "succeeded" | "failed" | "dead" | "cancelled";
  attempts: number;
  max_attempts: number;
  created_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  result: Record<string, unknown>;
  last_error: string | null;
}

/** The API's error envelope. Every failure has this shape, including 429s. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}
