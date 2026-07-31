"use client";

import { useQuery } from "@tanstack/react-query";

import { keys, request } from "@/lib/api";
import type {
  Job,
  ModelReport,
  PatternFinding,
  PeriodicReport,
  TradeSummary,
} from "@/types/evidence";

/**
 * Data hooks.
 *
 * Thin on purpose. Each maps one endpoint to one query key, and none of them reshapes the
 * payload: the qualifications the backend attached — sample sizes, intervals, refusals —
 * travel to the component intact. A hook that returned `{winRate: 0.42}` instead of the
 * `Estimate` would strip exactly what the evidence components exist to render.
 */

export function useTrades(params: { limit?: number; accountId?: string } = {}) {
  const search = new URLSearchParams();
  if (params.limit) search.set("limit", String(params.limit));
  if (params.accountId) search.set("account_id", params.accountId);

  return useQuery({
    queryKey: keys.trades(params),
    queryFn: () =>
      request<{ trades: TradeSummary[]; count: number }>(
        `/trades?${search.toString()}`,
      ),
  });
}

export function usePatterns(accountId?: string) {
  return useQuery({
    queryKey: keys.patterns(accountId),
    queryFn: () =>
      request<{ behaviours: PatternFinding[]; notes: string[] }>(
        `/patterns${accountId ? `?account_id=${accountId}` : ""}`,
      ),
  });
}

export function useLatestReport(type: string) {
  return useQuery({
    queryKey: keys.reports(type),
    queryFn: () =>
      request<{ report: PeriodicReport }>(`/reports/latest/${type}`),
    // A missing report is an ordinary state, not an error worth retrying: it means the
    // period has not been generated yet.
    retry: false,
  });
}

export function useModel(head: "win_probability" | "expected_r") {
  return useQuery({
    queryKey: keys.models(head),
    queryFn: () => request<ModelReport>(`/predictions/models/${head}`),
    retry: false,
  });
}

export function useJobs(state?: string) {
  return useQuery({
    queryKey: keys.jobs(state),
    queryFn: () =>
      request<{ jobs: Job[]; count: number }>(
        `/jobs${state ? `?state=${state}` : ""}`,
      ),
    // The only place polling is justified: a queued job's state changes without the user
    // doing anything, and this endpoint is cheap.
    refetchInterval: 5_000,
  });
}

export function useQueueHealth() {
  return useQuery({
    queryKey: keys.queue(),
    queryFn: () =>
      request<{
        counts: Record<string, number>;
        dead: number;
        pending: number;
        running: number;
      }>("/jobs/queue"),
    refetchInterval: 10_000,
  });
}
