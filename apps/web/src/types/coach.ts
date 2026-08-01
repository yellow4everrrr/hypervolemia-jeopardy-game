/**
 * The coaching layer, transcribed from `app.application.use_cases.generate_coaching`.
 *
 * Two fields here carry the entire point of milestone 9, and a UI that treats either as
 * decoration undoes it.
 *
 * **`is_publishable`.** An analysis that failed validation is a *successful* request whose
 * claims are withheld. The model said something; the validator caught it citing a
 * statistic it was never given, or stating a number nobody computed, and the claims do not
 * travel. `validation.reasons` says which. Rendering the claims anyway with a warning
 * beside them is precisely how a fabricated statistic reaches a trading decision, so
 * `claims` must not be displayed when this is false.
 *
 * **`cites`.** Every claim names the evidence keys it rests on. The model never writes a
 * number — it writes `{{metric.key}}` and Python substitutes — so a claim with an empty
 * `cites` is a claim with nothing under it. Showing the citations is what makes that
 * checkable by the reader rather than only by the validator.
 *
 * `expected_improvement` on a recommendation is filled by the what-if simulator, never by
 * the model. An empty object means the counterfactual has not been run — which is a
 * different statement from "no improvement expected", and the UI keeps them apart.
 */

export interface CoachClaim {
  /** Placeholders already substituted server-side; the raw form never leaves Python. */
  statement: string;
  kind: string;
  confidence: string;
  /** Evidence keys this rests on. Empty means nothing supports it. */
  cites: string[];
}

export interface CoachRecommendation {
  statement: string;
  category: string;
  /** Lower sorts first; the backend has already ordered them. */
  priority: number;
  confidence: string;
  cites: string[];
}

/** Why an analysis was withheld. Every list here names a specific failure. */
export interface CoachValidation {
  is_valid: boolean;
  /** Placeholders referring to evidence that does not exist. */
  unknown_keys: string[];
  /** Placeholders whose statistic came back undefined for this sample. */
  undefined_keys: string[];
  /** Literal numbers written by the model rather than substituted. */
  uncited_numbers: string[];
  uncited_claims: string[];
  reasons: string[];
}

export interface CoachAnalysis {
  headline: string;
  is_publishable: boolean;
  claims: CoachClaim[];
  recommendations: CoachRecommendation[];
  /** The model's own account of what the sample cannot support. */
  what_the_data_cannot_say: string[];
  /** Statistics the evidence bundle could not supply, from the engine not the model. */
  evidence_gaps: string[];
  validation: CoachValidation;
  model: string;
  prompt_version: string;
  error: string | null;
}

export interface StoredAnalysis {
  id: string;
  created_at: string;
  subject_type: string;
  summary: string | null;
  model: string | null;
  prompt_version: string | null;
  evidence_validated: boolean;
  confidence: string | null;
  cost_usd: string | null;
  output: Record<string, unknown>;
}

export interface StoredAnalysisList {
  items: StoredAnalysis[];
}

export interface OpenRecommendation {
  id: string;
  priority: number;
  category: string;
  statement: string;
  evidence: Record<string, unknown>;
  /** From the simulator. `{}` means the counterfactual has not been run. */
  expected_improvement: Record<string, unknown>;
  status: string;
}

export interface OpenRecommendationList {
  items: OpenRecommendation[];
  note: string;
}
