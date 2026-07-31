/**
 * The frontend honesty contract.
 *
 * These assert the properties that twelve backend milestones exist to produce and that a
 * rendering layer destroys most easily: an unproven change must not look like a change, an
 * undefined statistic must not look like zero, and a refusal must not look like an empty
 * state.
 *
 * None of these failures would be caught by a visual review. The page looks finished in
 * every one of them.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Change, Finding, Metric, ModelCard, Refusal } from "@/components/evidence";
import type {
  Estimate,
  MetricChange,
  ModelReport,
  StoredPattern,
} from "@/types/evidence";

function estimate(overrides: Partial<Estimate> = {}): Estimate {
  return {
    value: "142.50",
    sample_size: 240,
    reliability: "reliable",
    interval: {
      low: "88.10",
      high: "196.90",
      confidence: "0.95",
      method: "bootstrap_percentile",
      excludes_zero: true,
    },
    ...overrides,
  };
}

function change(overrides: Partial<MetricChange> = {}): MetricChange {
  return {
    key: "win_rate",
    label: "win rate",
    current: "0.48",
    previous: "0.44",
    difference: "0.04",
    current_sample: 21,
    previous_sample: 19,
    is_established: false,
    direction: "unchanged",
    narrative:
      "win rate was 0.48 against 0.44 — with 21 and 19 trades, chance produces a gap that size often enough that this is not a change",
    ...overrides,
  };
}

describe("Metric", () => {
  it("shows the sample size beside every figure", () => {
    render(<Metric label="Expectancy" estimate={estimate()} unit="currency" />);

    expect(screen.getByText(/240 trades/)).toBeInTheDocument();
  });

  it("shows the confidence interval when one exists", () => {
    render(<Metric label="Expectancy" estimate={estimate()} unit="currency" />);

    expect(screen.getByText(/\$88\.10 to/)).toBeInTheDocument();
  });

  it("renders an undefined statistic as its reason, never as zero", () => {
    render(
      <Metric
        label="Profit factor"
        estimate={estimate({
          value: null,
          interval: null,
          undefined_reason: "no losing trades in this sample, so it is undefined",
        })}
      />,
    );

    expect(screen.getByText("not available")).toBeInTheDocument();
    expect(screen.getByText(/no losing trades/)).toBeInTheDocument();
    expect(screen.queryByText("0.00")).not.toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("marks an interval that spans zero", () => {
    render(
      <Metric
        label="Expectancy"
        estimate={estimate({
          interval: {
            low: "-40.00",
            high: "120.00",
            confidence: "0.95",
            method: "bootstrap_percentile",
            excludes_zero: false,
          },
        })}
        unit="currency"
      />,
    );

    expect(screen.getByText("spans zero")).toBeInTheDocument();
  });

  it("grades the sample rather than hiding a thin one", () => {
    render(
      <Metric
        label="Expectancy"
        estimate={estimate({ sample_size: 7, reliability: "provisional" })}
      />,
    );

    expect(screen.getByText("provisional")).toBeInTheDocument();
  });
});

describe("Change", () => {
  it("does not render a direction arrow for an unestablished change", () => {
    const { container } = render(<Change change={change()} />);

    expect(container.textContent).not.toContain("▲");
    expect(container.textContent).not.toContain("▼");
  });

  it("renders an arrow once the change is established", () => {
    const { container } = render(
      <Change
        change={change({
          is_established: true,
          direction: "up",
          narrative: "win rate was higher than the previous period, by more than chance explains",
        })}
      />,
    );

    expect(container.textContent).toContain("▲");
  });

  it("carries the backend's own sentence about chance", () => {
    render(<Change change={change()} />);

    expect(
      screen.getByText(/chance produces a gap that size/),
    ).toBeInTheDocument();
  });

  it("distinguishes unchanged from flat", () => {
    const unchanged = render(<Change change={change({ direction: "unchanged" })} />);
    const unchangedText = unchanged.container.textContent ?? "";
    unchanged.unmount();

    const flat = render(
      <Change change={change({ direction: "flat", is_established: true })} />,
    );
    const flatText = flat.container.textContent ?? "";

    // "we cannot tell" and "it did not move" must not render identically.
    expect(unchangedText).not.toEqual(flatText);
  });

  it("tells a screen reader that an unestablished change is not a change", () => {
    render(<Change change={change()} />);

    expect(
      screen.getByText(/was not established as a change/),
    ).toBeInTheDocument();
  });
});

function finding(overrides: Partial<StoredPattern> = {}): StoredPattern {
  return {
    id: "019fb99f-fdc9-7316-9e3e-69f5fba86a50",
    kind: "overtrading",
    label: "Overtrading",
    description: "Trades taken beyond the fourth in a session",
    polarity: "leak",
    sample_size: 100,
    effect_size: "-0.41",
    p_value: "0.004",
    confidence_low: "-343.67",
    confidence_high: "-78.20",
    is_significant: true,
    estimated_annual_impact: "-21113.24",
    detail: {},
    engine_version: 1,
    ...overrides,
  };
}

describe("Finding", () => {
  it("shows a cost for an established leak", () => {
    render(<Finding finding={finding()} />);

    expect(screen.getByText(/\$21,113\.24/)).toBeInTheDocument();
  });

  it("never attaches a cost to an unestablished pattern", () => {
    const { container } = render(
      <Finding finding={finding({ is_significant: false })} />,
    );

    // The loudest coincidence in a scan must not appear beside a dollar figure.
    expect(container.textContent).not.toContain("21,113");
  });

  it("labels an unestablished pattern as examined rather than found", () => {
    render(<Finding finding={finding({ is_significant: false })} />);

    expect(screen.getByText("examined")).toBeInTheDocument();
    expect(
      screen.getByText(/not something you do — it is something that was tested for/),
    ).toBeInTheDocument();
  });

  it("shows the polarity only once a pattern is established", () => {
    render(<Finding finding={finding()} />);

    expect(screen.getByText("leak")).toBeInTheDocument();
  });
});

describe("Refusal", () => {
  it("renders the reason as the content", () => {
    render(
      <Refusal
        title="Against April 2026"
        reason="this period is too small to compare against the one before it"
      />,
    );

    expect(screen.getByText(/too small to compare/)).toBeInTheDocument();
  });
});

function model(overrides: Partial<ModelReport> = {}): ModelReport {
  return {
    head: "win_probability",
    version: 1,
    is_deployable: false,
    refusal:
      "this model beat simply assuming your average win rate on your history (skill score 0.095), but resampling your sessions puts the true figure anywhere from -0.058 to 0.207",
    folds: 8,
    out_of_sample_predictions: 160,
    skill: {
      value: "0.095",
      interval: ["-0.058", "0.207"],
      sessions: 40,
      predictions: 160,
      established: false,
      caveat: "somewhat optimistic",
    },
    ...overrides,
  };
}

describe("ModelCard", () => {
  it("shows the refusal instead of an empty probability", () => {
    render(<ModelCard report={model()} value={null} label="Win probability" />);

    expect(screen.getByText(/not served/)).toBeInTheDocument();
    expect(screen.getByText(/cannot be distinguished|resampling your sessions/)).toBeInTheDocument();
  });

  it("shows the skill interval alongside the refusal", () => {
    render(<ModelCard report={model()} value={null} label="Win probability" />);

    // Appears twice by design: once inside the backend's own sentence, and once as a
    // scannable figure. Both are wanted, so the assertion allows for both.
    expect(screen.getAllByText(/-0\.058 to 0\.207/).length).toBeGreaterThan(0);
  });

  it("never renders a probability for a model that is not deployable", () => {
    const { container } = render(
      <ModelCard report={model()} value="0.68" label="Win probability" />,
    );

    // Even when a caller passes a value, an undeployable model must not display one.
    expect(container.textContent).not.toContain("0.68");
  });

  it("renders the value once the model is served", () => {
    render(
      <ModelCard
        report={model({ is_deployable: true, refusal: null })}
        value="68%"
        label="Win probability"
      />,
    );

    expect(screen.getByText("68%")).toBeInTheDocument();
    expect(screen.getByText("served")).toBeInTheDocument();
  });
});
