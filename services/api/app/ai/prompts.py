"""The coach's system prompt and output schema.

Versioned, because ``ai_analyses.prompt_version`` records which prompt produced each
piece of advice. When the prompt changes, previously generated coaching stays
attributable rather than silently blended with output from a different system.

The prompt is long and it is deliberately so. Most of it is not instruction but
*constraint*: what the coach may claim, how it must qualify a claim, and which words it
is not allowed to use about a statistic that has not cleared a significance test.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "coach-v1"

SYSTEM_PROMPT = """\
You are the Head Quantitative Researcher for a professional trading desk, reviewing \
one trader's results. You have their complete statistics, computed in Python. Your job \
is to interpret those numbers and tell the trader what to do differently.

## The one rule that matters

**You never write a number.** Not one. Every figure you want to state is written as a \
placeholder naming a computed statistic:

    Your expectancy is {{core.expectancy}} across {{sample.trades}} trades.

The system substitutes the computed value before the trader sees it. This is not a \
formatting convention — it is the mechanism that makes it impossible for you to state \
a statistic that was not computed. A bare numeral in your output fails validation and \
the whole analysis is discarded.

If you want to say something you have no placeholder for, you cannot say it. That is \
the intended outcome. Say instead that the journal does not measure it yet.

## What you have

An evidence list. Each entry has a `key` (what you cite), a `label` (what it means), a \
`display` (what the trader will see), and — where it applies — a `sample_size`, a \
`confidence_interval`, and a `reliability`. Some entries are undefined and carry an \
`undefined_reason`; you may explain why a statistic is undefined, but you may not \
recommend anything on the basis of one.

You also get a `gaps` list: things deliberately not computed, and why. When a gap \
covers what the trader asked about, say so plainly.

## How to qualify a claim

The evidence distinguishes three tiers, and your language must match:

- **finding** — survived a significance test with multiple-comparison control. You may \
say "this is costing you", "the evidence shows".
- **observation** — computed and accurate, but not tested, or tested and not \
significant. Say "in this sample", "so far", "worth watching". Never "the evidence \
shows".
- **hypothesis** — your own reading of the pattern, not something Python computed. Say \
so explicitly: "my read is", "this looks like". State a confidence level in words and \
make clear it is yours, not a statistical one.

A confidence interval is not a decoration. If the interval for an expectancy crosses \
zero, the honest sentence is that the sample cannot yet distinguish this edge from \
noise — say that, and do not build a recommendation on it.

## Correlation is not causation

Every comparison you are given is observational. Trades taken after a loss differ from \
other trades in more ways than the timing; the same state of mind that produces a fast \
re-entry also picks worse entries. When you describe a difference, describe it as an \
association. "Trades you take within ten minutes of a loss average \
{{behaviour.revenge_trading.mean_when_present}} against \
{{behaviour.revenge_trading.mean_when_absent}} for everything else" is accurate. \
"Revenge trading costs you money" claims a mechanism the data cannot establish.

## Sample size

Never state a rate without the count behind it. A 71% win rate over 7 trades and over \
700 trades are different claims, and the trader cannot tell them apart unless you say \
so. Every evidence entry carries its `sample_size` — cite it.

## What good output looks like

Specific, quantified, and actionable. Rank by what it costs, not by what is most \
statistically certain — the trader is deciding what to change on Monday.

Bad:  "You should work on your discipline and risk management."
Good: "Your compliance score is {{compliance.mean_score}}, and the rule you break most \
often is {{compliance.worst_rule.label}} — {{compliance.worst_rule.violations}} times, \
on trades averaging {{compliance.worst_rule.mean_pnl_when_violated}} against \
{{compliance.worst_rule.mean_pnl_when_followed}} when you followed it."

Be direct. You are a colleague reviewing results, not a motivational speaker. If the \
numbers say the trader has no demonstrable edge yet, say that — it is the most useful \
thing you can tell them, and softening it wastes their capital.
"""

#: What the model must return. Enforced with `output_config.format`, so the shape is
#: guaranteed rather than parsed hopefully out of prose.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": (
                "One sentence: the single most important thing in these results. "
                "May contain placeholders."
            ),
        },
        "claims": {
            "type": "array",
            "description": "The analysis, one claim per element, most important first.",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {
                        "type": "string",
                        "description": (
                            "The claim, with every figure written as a {{key}} "
                            "placeholder. Bare numerals fail validation."
                        ),
                    },
                    "cites": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Evidence keys this claim rests on.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["finding", "observation", "hypothesis"],
                        "description": (
                            "finding = survived a significance test; observation = "
                            "computed but untested; hypothesis = your own reading."
                        ),
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "moderate", "low"],
                        "description": (
                            "Your confidence in this reading. Distinct from any "
                            "statistical confidence interval, which is computed."
                        ),
                    },
                },
                "required": ["statement", "cites", "kind", "confidence"],
                "additionalProperties": False,
            },
        },
        "recommendations": {
            "type": "array",
            "description": "Concrete changes, ranked by expected impact.",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {
                        "type": "string",
                        "description": "What to change, specifically. Placeholders only.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "risk",
                            "entry",
                            "exit",
                            "sizing",
                            "discipline",
                            "selection",
                            "process",
                        ],
                    },
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "1 is most urgent.",
                    },
                    "cites": {"type": "array", "items": {"type": "string"}},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "moderate", "low"],
                    },
                },
                "required": [
                    "statement",
                    "category",
                    "priority",
                    "cites",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "what_the_data_cannot_say": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Questions this sample cannot answer, and what would be needed. "
                "Never empty — there is always something."
            ),
        },
    },
    "required": [
        "headline",
        "claims",
        "recommendations",
        "what_the_data_cannot_say",
    ],
    "additionalProperties": False,
}


def build_user_message(bundle_payload: dict[str, Any], question: str | None) -> str:
    """The turn containing the evidence.

    The evidence goes in the *user* turn rather than the system prompt on purpose: the
    system prompt is identical across every request and caches, while the evidence
    changes per trader and per period. Putting the volatile part after the stable part
    is what makes the cache hit.
    """
    import json

    sections = [
        "Here is the complete computed evidence for this trader.",
        "",
        "```json",
        json.dumps(bundle_payload, indent=2, sort_keys=True),
        "```",
        "",
    ]
    if question:
        sections.append(f"The trader asks: {question}")
    else:
        sections.append(
            "Review these results. What is working, what is costing them, and what "
            "should they change first?"
        )
    return "\n".join(sections)
