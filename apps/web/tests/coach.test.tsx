/**
 * The one guarantee the coach screen exists to keep.
 *
 * Milestone 9's whole claim is that a language model can discuss this trader's statistics
 * without being able to invent one. Python computes every number, the model writes only
 * `{{metric.key}}` placeholders, and a validator checks every claim back against the
 * evidence bundle before anything is published.
 *
 * All of that is undone by one plausible line of JSX. The natural way to render a failed
 * analysis is to show the claims with a warning badge — it feels more transparent, it
 * shows the user "what the model said". It is also precisely how a fabricated statistic
 * reaches a trading decision, because nobody reads the badge and everybody reads the
 * sentence.
 *
 * These tests exist because that path **cannot be exercised live**: reaching it requires
 * an API key and a model that actually misbehaves. Waiting to find out whether the UI
 * handles it correctly until the day it first happens in production is not a plan.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Analysis } from "@/app/coach/page";
import type { CoachAnalysis } from "@/types/coach";

/** A statement no reader should ever see from a withheld analysis. */
const FABRICATED = "Your win rate improved to 71% this month";

function analysis(overrides: Partial<CoachAnalysis> = {}): CoachAnalysis {
  return {
    headline: "Expectancy held up across a larger sample than last month.",
    is_publishable: true,
    claims: [
      {
        statement: "Expectancy was $185.71 per trade over 1,447 trades.",
        kind: "observation",
        confidence: "high",
        cites: ["core.expectancy", "sample.trades"],
      },
    ],
    recommendations: [
      {
        statement: "Stop taking trades after the sixth of a session.",
        category: "discipline",
        priority: 1,
        confidence: "medium",
        cites: ["patterns.overtrading"],
      },
    ],
    what_the_data_cannot_say: [
      "Whether the leak is caused by fatigue or by the market regime late in the session.",
    ],
    evidence_gaps: [],
    validation: {
      is_valid: true,
      unknown_keys: [],
      undefined_keys: [],
      uncited_numbers: [],
      uncited_claims: [],
      reasons: [],
    },
    model: "claude-opus-5",
    prompt_version: "v3",
    error: null,
    ...overrides,
  };
}

function withheld(): CoachAnalysis {
  return analysis({
    is_publishable: false,
    headline: FABRICATED,
    claims: [
      {
        statement: FABRICATED,
        kind: "observation",
        confidence: "high",
        cites: [],
      },
    ],
    validation: {
      is_valid: false,
      unknown_keys: ["core.win_rate_delta"],
      undefined_keys: [],
      uncited_numbers: ["71"],
      uncited_claims: [FABRICATED],
      reasons: [
        "a claim stated a number that was not computed from the evidence bundle",
        "a claim cited core.win_rate_delta, which the bundle does not contain",
      ],
    },
  });
}

describe("a withheld analysis", () => {
  it("does not render the claims", () => {
    const { container } = render(<Analysis analysis={withheld()} />);

    // The assertion the whole file is for. Not "renders them with a badge" — not at all.
    expect(container.textContent).not.toContain(FABRICATED);
  });

  it("does not render the headline either", () => {
    // The headline is model-authored too, and an unvalidated headline is exactly as
    // capable of stating a fabricated number as an unvalidated claim.
    render(<Analysis analysis={withheld()} />);

    expect(screen.getByText(/did not survive validation/)).toBeInTheDocument();
  });

  it("renders why it was withheld", () => {
    render(<Analysis analysis={withheld()} />);

    expect(
      screen.getByText(/stated a number that was not computed/),
    ).toBeInTheDocument();
    expect(screen.getByText(/which the bundle does not contain/)).toBeInTheDocument();
  });

  it("names the specific numbers and keys that failed", () => {
    const { container } = render(<Analysis analysis={withheld()} />);

    expect(container.textContent).toContain("core.win_rate_delta");
    expect(container.textContent).toMatch(/numbers written rather than computed/);
  });

  it("is labelled withheld rather than shown as an error", () => {
    // A failed validation is a *successful* request. Rendering it as a crash would teach
    // a trader that the coach is broken, when what happened is that it worked.
    render(<Analysis analysis={withheld()} />);

    expect(screen.getByText("Withheld")).toBeInTheDocument();
  });
});

describe("a published analysis", () => {
  it("renders the headline and claims", () => {
    render(<Analysis analysis={analysis()} />);

    expect(screen.getByText(/Expectancy held up/)).toBeInTheDocument();
    expect(screen.getByText(/\$185\.71 per trade/)).toBeInTheDocument();
  });

  it("shows what each claim cites", () => {
    // The citations are what make the guarantee checkable by a reader rather than only by
    // the validator. A claim with evidence must look different from one without.
    const { container } = render(<Analysis analysis={analysis()} />);

    expect(container.textContent).toContain("core.expectancy");
    expect(container.textContent).toContain("sample.trades");
  });

  it("marks a claim that cites nothing", () => {
    const uncited = analysis({
      claims: [
        {
          statement: "You are trading well.",
          kind: "observation",
          confidence: "low",
          cites: [],
        },
      ],
    });

    render(<Analysis analysis={uncited} />);

    expect(screen.getByText("cites nothing")).toBeInTheDocument();
  });

  it("renders the limits of the sample alongside the conclusions", () => {
    render(<Analysis analysis={analysis()} />);

    expect(screen.getByText(/What this data cannot say/i)).toBeInTheDocument();
    expect(screen.getByText(/fatigue or by the market regime/)).toBeInTheDocument();
  });

  it("still shows the limits when the analysis was withheld", () => {
    // What the data cannot support is true regardless of whether the model's prose
    // survived validation.
    render(<Analysis analysis={withheld()} />);

    expect(screen.getByText(/What this data cannot say/i)).toBeInTheDocument();
  });

  it("names statistics the engine could not supply", () => {
    const gapped = analysis({
      evidence_gaps: ["core.expectancy_r", "risk.risk_of_ruin"],
    });

    const { container } = render(<Analysis analysis={gapped} />);

    expect(container.textContent).toContain("core.expectancy_r");
    expect(container.textContent).toMatch(/never able to cite/);
  });
});
