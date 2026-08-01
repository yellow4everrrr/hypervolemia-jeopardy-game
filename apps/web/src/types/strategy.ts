/**
 * Strategies, rules and compliance, transcribed from the strategies and compliance routers.
 *
 * `RuleImpact` is the type to be careful with. It reports what trades that broke a rule
 * did against trades that followed it, and the temptation is to render that difference as
 * "breaking this rule costs you $196 a trade". The backend says otherwise in its own
 * docstring, and it is right: trades that broke a rule differ from trades that did not in
 * every way that led to the rule being broken. A trader who is down on the day, late in
 * the session and sizing up breaks three rules at once, and none of the three differences
 * is that rule's contribution.
 *
 * So `difference` is an association. The UI names it as one.
 */

export type RuleSeverity = "critical" | "major" | "minor";

export interface RuleSummary {
  id: string;
  code: string;
  label: string;
  description: string | null;
  rule_type: string;
  severity: RuleSeverity;
  weight: string;
  /** The declarative predicate AST. Stored, never executed. */
  expression: Record<string, unknown>;
  is_active: boolean;
}

export interface StrategySummary {
  id: string;
  name: string;
  description: string | null;
  version: number;
  supersedes_id: string | null;
  is_active: boolean;
  checklist: Record<string, unknown>;
  created_at: string;
  rules: RuleSummary[];
}

export interface StrategyList {
  items: StrategySummary[];
}

export interface RuleImpact {
  rule_code: string;
  rule_label: string;
  severity: RuleSeverity;
  violations: number;
  compliant_trades: number;
  mean_pnl_when_violated: string | null;
  mean_pnl_when_followed: string | null;
  /** Associated difference in mean P&L. **Not** the cost of breaking the rule. */
  difference: string | null;
  total_cost: string | null;
  is_costly: boolean;
}

export interface ComplianceReport {
  trades_evaluated: number;
  trades_scored: number;
  total_violations: number;
  critical_violations: number;
  /** Trades with no strategy attached — unscored, and reported rather than hidden. */
  trades_without_strategy: number;
  trades_without_rules: number;
  mean_score: string | null;
  impacts: RuleImpact[];
}
