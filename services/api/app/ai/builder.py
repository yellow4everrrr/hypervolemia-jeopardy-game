"""Assembling the evidence bundle from the engines that computed it.

This module decides what the coach is allowed to know. Every key added here widens the
coach's vocabulary; every key left out is a sentence it cannot write.

Two rules govern what goes in:

**Nothing enters without its uncertainty.** An expectancy arrives with its interval and
its sample size, a segment with its reliability, a pattern with its adjusted p-value.
Adding a bare number would let the model quote it with a confidence the number does not
have.

**What was deliberately not computed becomes a gap.** A statistic that is undefined,
a scan that found nothing, a period too short to annualise — these are recorded as
explicit gaps rather than omitted. A model shown an absence guesses; a model shown
"this is absent because the sample is too small" says so.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.ai.evidence import (
    COUNT,
    CURRENCY,
    PERCENT,
    R_MULTIPLE,
    RATIO,
    SECONDS,
    EvidenceBundle,
)
from app.analytics.behaviours import BehaviourFinding
from app.analytics.discovery import ClusterFinding, PatternReport
from app.analytics.engine import AnalyticsReport
from app.analytics.types import Estimate
from app.domain.rules.engine import RuleImpact


def build_bundle(
    *,
    analytics: AnalyticsReport | None = None,
    patterns: PatternReport | None = None,
    compliance: dict[str, Any] | None = None,
    rule_impacts: list[RuleImpact] | None = None,
) -> EvidenceBundle:
    """Fold every engine's output into one keyed evidence set."""
    bundle = EvidenceBundle()

    if analytics is not None:
        _add_analytics(bundle, analytics)
    if patterns is not None:
        _add_patterns(bundle, patterns)
    if compliance is not None:
        _add_compliance(bundle, compliance)
    if rule_impacts:
        _add_rule_impacts(bundle, rule_impacts)

    if bundle.is_empty():
        bundle.gaps.append(
            "No statistics could be computed — there are no closed trades in this "
            "period."
        )
    return bundle


def _estimate(
    bundle: EvidenceBundle,
    key: str,
    estimate: Estimate,
    *,
    unit: str,
    label: str,
) -> None:
    """Add a statistic together with everything that qualifies it."""
    bundle.put(
        key,
        estimate.value,
        unit=unit,
        label=label,
        sample_size=estimate.sample_size,
        interval=(
            (estimate.interval.low, estimate.interval.high)
            if estimate.interval is not None
            else None
        ),
        reliability=estimate.reliability.value,
        undefined_reason=estimate.undefined_reason,
    )
    if estimate.undefined_reason:
        bundle.gaps.append(f"{label}: {estimate.undefined_reason}")


def _add_analytics(bundle: EvidenceBundle, report: AnalyticsReport) -> None:
    core = report.core
    bundle.put(
        "sample.trades",
        report.sample_size,
        unit=COUNT,
        label="closed trades analysed",
    )
    bundle.put(
        "core.win_rate",
        core.counts.win_rate,
        unit=PERCENT,
        label="win rate",
        sample_size=core.counts.total,
    )
    bundle.put("core.wins", core.counts.winners, unit=COUNT, label="winning trades")
    bundle.put("core.losses", core.counts.losers, unit=COUNT, label="losing trades")
    bundle.put(
        "core.scratches",
        core.counts.scratches,
        unit=COUNT,
        label="break-even trades (excluded from the win-rate denominator)",
    )
    bundle.put(
        "core.net_pnl",
        core.pnl.net_pnl,
        unit=CURRENCY,
        label="net profit and loss",
        sample_size=core.counts.total,
    )
    bundle.put(
        "core.profit_factor",
        core.pnl.profit_factor,
        unit=RATIO,
        label="profit factor",
        sample_size=core.counts.total,
        undefined_reason=(
            None
            if core.pnl.profit_factor is not None
            else "undefined — this sample has no losing trades to divide by"
        ),
    )
    bundle.put(
        "core.average_win",
        core.pnl.average_win,
        unit=CURRENCY,
        label="average winning trade",
        sample_size=core.counts.winners,
    )
    bundle.put(
        "core.average_loss",
        core.pnl.average_loss,
        unit=CURRENCY,
        label="average losing trade",
        sample_size=core.counts.losers,
    )
    bundle.put(
        "core.costs",
        core.pnl.total_costs,
        unit=CURRENCY,
        label="total commissions and fees",
    )

    _estimate(
        bundle,
        "core.expectancy",
        report.expectancy,
        unit=CURRENCY,
        label="expectancy per trade",
    )
    _estimate(
        bundle,
        "core.expectancy_r",
        core.expectancy_r,
        unit=R_MULTIPLE,
        label="expectancy in R",
    )

    drawdown = report.drawdown
    bundle.put(
        "risk.max_drawdown",
        drawdown.max_drawdown,
        unit=CURRENCY,
        label="maximum drawdown",
    )
    bundle.put(
        "risk.current_drawdown",
        drawdown.current_drawdown,
        unit=CURRENCY,
        label="current drawdown from peak",
    )
    bundle.put(
        "risk.recovery_factor",
        drawdown.recovery_factor,
        unit=RATIO,
        label="recovery factor (net profit per unit of worst drawdown)",
        undefined_reason=(
            None
            if drawdown.recovery_factor is not None
            else "undefined — there has been no drawdown to recover from"
        ),
    )
    _estimate(
        bundle,
        "risk.risk_of_ruin",
        report.risk_of_ruin,
        unit=PERCENT,
        label="simulated risk of ruin",
    )
    bundle.put(
        "risk.kelly_half",
        report.kelly.half,
        unit=PERCENT,
        label="half-Kelly position fraction",
        undefined_reason=(
            None if report.kelly.half is not None else "undefined for this sample"
        ),
    )
    if report.kelly.warnings:
        bundle.gaps.extend(f"Kelly sizing caveat: {note}" for note in report.kelly.warnings)

    for dimension, analysis in report.segments.items():
        actionable = [segment for segment in analysis.segments if segment.is_actionable]
        bundle.put(
            f"segments.{dimension}.actionable_count",
            len(actionable),
            unit=COUNT,
            label=f"segments of {dimension.replace('_', ' ')} that survived the scan",
        )
        if not actionable:
            bundle.gaps.append(
                f"No {dimension.replace('_', ' ')} segment survived significance "
                "testing — the differences between them are within what chance "
                "produces at this sample size."
            )
            continue
        for segment in actionable[:3]:
            prefix = f"segments.{dimension}.{segment.key}"
            bundle.context[f"{prefix}.label"] = segment.key.replace("_", " ")
            _estimate(
                bundle,
                f"{prefix}.expectancy",
                segment.expectancy,
                unit=CURRENCY,
                label=f"expectancy for {dimension.replace('_', ' ')} = {segment.key}",
            )
            if segment.comparison and segment.comparison.adjusted_p_value is not None:
                bundle.put(
                    f"{prefix}.adjusted_p_value",
                    segment.comparison.adjusted_p_value,
                    unit=RATIO,
                    label=f"FDR-adjusted p-value for {segment.key}",
                    sample_size=segment.counts.total,
                )


def _add_patterns(bundle: EvidenceBundle, report: PatternReport) -> None:
    bundle.put(
        "patterns.tests_performed",
        report.tests_performed,
        unit=COUNT,
        label="behavioural and cluster tests performed in the scan",
    )
    bundle.put(
        "patterns.findings",
        len(report.findings),
        unit=COUNT,
        label="patterns that survived multiple-comparison control",
    )

    if report.tests_performed and not report.findings:
        bundle.gaps.append(
            "The pattern scan tested every behavioural detector and found nothing that "
            "survived correction. That is a result: no measurable leak at this sample "
            "size, not an absence of analysis."
        )

    for finding in report.behaviours:
        if not isinstance(finding, BehaviourFinding) or finding.comparison is None:
            continue
        prefix = f"behaviour.{finding.kind}"
        bundle.context[f"{prefix}.label"] = finding.label
        bundle.context[f"{prefix}.tier"] = (
            "finding" if finding.is_actionable else "observation"
        )
        bundle.put(
            f"{prefix}.affected",
            finding.affected,
            unit=COUNT,
            label=f"trades exhibiting: {finding.label}",
        )
        unit = CURRENCY if finding.unit.value == "currency" else SECONDS
        bundle.put(
            f"{prefix}.mean_when_present",
            finding.mean_when_present,
            unit=unit,
            label=f"average when present: {finding.label}",
            sample_size=finding.affected,
        )
        bundle.put(
            f"{prefix}.mean_when_absent",
            finding.mean_when_absent,
            unit=unit,
            label=f"average otherwise: {finding.label}",
            sample_size=finding.unaffected,
        )
        if finding.estimated_cost is not None:
            bundle.put(
                f"{prefix}.estimated_cost",
                finding.estimated_cost,
                unit=CURRENCY,
                label=f"total associated with: {finding.label}",
                sample_size=finding.affected,
            )
        if finding.comparison.adjusted_p_value is not None:
            bundle.put(
                f"{prefix}.adjusted_p_value",
                finding.comparison.adjusted_p_value,
                unit=RATIO,
                label=f"FDR-adjusted p-value: {finding.label}",
            )

    for cluster in report.clusters:
        if not isinstance(cluster, ClusterFinding) or cluster.comparison is None:
            continue
        prefix = f"cluster.{cluster.cluster}"
        bundle.context[f"{prefix}.label"] = cluster.label
        bundle.context[f"{prefix}.tier"] = (
            "finding" if cluster.is_actionable else "observation"
        )
        bundle.put(
            f"{prefix}.size", cluster.size, unit=COUNT, label=f"trades in {cluster.label}"
        )
        _estimate(
            bundle,
            f"{prefix}.expectancy",
            cluster.expectancy,
            unit=CURRENCY,
            label=f"expectancy for {cluster.label}",
        )

    if report.capture is not None and report.capture.is_computable:
        _estimate(
            bundle,
            "exits.capture_ratio",
            report.capture.mean_capture_ratio,
            unit=RATIO,
            label="fraction of the available move captured on winning trades",
        )
        bundle.put(
            "exits.forgone_r",
            report.capture.forgone_r,
            unit=R_MULTIPLE,
            label="R left on the table per winning trade, on average",
            sample_size=report.capture.winners,
        )
        bundle.gaps.append(
            "Excursion figures are measured with hindsight — the best price a trade "
            "reached was not knowable at the exit. They bound what was available, not "
            "what was achievable."
        )


def _add_compliance(bundle: EvidenceBundle, compliance: dict[str, Any]) -> None:
    evaluated = int(compliance.get("trades_evaluated") or 0)
    bundle.put(
        "compliance.trades_evaluated",
        evaluated,
        unit=COUNT,
        label="trades scored against their strategy's rules",
    )
    mean_score = compliance.get("mean_score")
    bundle.put(
        "compliance.mean_score",
        Decimal(mean_score) if mean_score is not None else None,
        unit=PERCENT,
        label="average compliance score",
        sample_size=evaluated,
        undefined_reason=(
            None
            if mean_score is not None
            else "no trade had enough recorded data for any rule to be checked"
        ),
    )
    bundle.put(
        "compliance.violations",
        int(compliance.get("total_violations") or 0),
        unit=COUNT,
        label="rule violations",
    )
    bundle.put(
        "compliance.critical_violations",
        int(compliance.get("critical_violations") or 0),
        unit=COUNT,
        label="trades breaching a critical rule",
    )

    unlabelled = int(compliance.get("trades_without_strategy") or 0)
    if unlabelled:
        bundle.put(
            "compliance.unlabelled_trades",
            unlabelled,
            unit=COUNT,
            label="trades with no strategy attached, so not scored",
        )
        bundle.gaps.append(
            "Some trades carry no strategy and were not scored. Compliance figures "
            "describe only the labelled ones."
        )


def _add_rule_impacts(bundle: EvidenceBundle, impacts: list[RuleImpact]) -> None:
    costly = [impact for impact in impacts if impact.is_costly and impact.violations > 0]
    if not costly:
        return
    worst = min(costly, key=lambda impact: impact.total_cost or Decimal(0))

    bundle.context["compliance.worst_rule.label"] = worst.rule_label
    bundle.context["compliance.worst_rule.severity"] = worst.severity.value
    bundle.put(
        "compliance.worst_rule.violations",
        worst.violations,
        unit=COUNT,
        label=f"times broken: {worst.rule_label}",
    )
    bundle.put(
        "compliance.worst_rule.mean_pnl_when_violated",
        worst.mean_pnl_when_violated,
        unit=CURRENCY,
        label=f"average result when breaking: {worst.rule_label}",
        sample_size=worst.violations,
    )
    bundle.put(
        "compliance.worst_rule.mean_pnl_when_followed",
        worst.mean_pnl_when_followed,
        unit=CURRENCY,
        label=f"average result when following: {worst.rule_label}",
        sample_size=worst.compliant_trades,
    )
    bundle.put(
        "compliance.worst_rule.total_cost",
        worst.total_cost,
        unit=CURRENCY,
        label=f"total associated with breaking: {worst.rule_label}",
    )
    bundle.gaps.append(
        "Rule-impact comparisons are observational. Trades that broke a rule differ "
        "from trades that did not in ways beyond the rule itself."
    )
