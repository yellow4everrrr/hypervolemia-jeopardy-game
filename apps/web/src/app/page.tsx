import { Metric } from "@/components/evidence";
import { PageHeader } from "@/components/shell";
import type { Estimate } from "@/types/evidence";

/**
 * The dashboard.
 *
 * Deliberately statistics-first rather than chart-first. A large equity curve is the
 * conventional hero and it is the least informative thing on the page: it goes up or down
 * and a trader already knows which. Expectancy with its interval, and a win rate with its
 * sample size, are what actually answer "is this working".
 */

const PLACEHOLDER: Record<string, Estimate> = {
  expectancy: {
    value: null,
    sample_size: 0,
    reliability: "insufficient",
    undefined_reason:
      "no trades have been imported yet — link a broker account to begin",
  },
};

export default function DashboardPage() {
  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="Every figure carries its sample size and, where one exists, its confidence interval."
      />
      <div className="grid gap-4 p-6 sm:grid-cols-2 xl:grid-cols-4">
        <Metric
          label="Expectancy"
          estimate={PLACEHOLDER["expectancy"] as Estimate}
          unit="currency"
          hint="Average P&L per trade, bootstrapped"
        />
      </div>
    </>
  );
}
