"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "@/lib/api";

/**
 * Query defaults chosen for this data rather than copied from a template.
 *
 * Trades are immutable once reconstructed and analytics are recomputed on demand, so
 * nothing here benefits from aggressive refetching — and the expensive endpoints are rate
 * limited, so a window-focus refetch storm would spend a trader's whole allowance on
 * screens they are not looking at.
 *
 * A 429 is never retried automatically. Retrying into a rate limit is how a client turns
 * a small overage into a sustained one.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60_000,
            refetchOnWindowFocus: false,
            retry: (failureCount, error) => {
              if (error instanceof ApiError && !error.isTransient) return false;
              if (error instanceof ApiError && error.status === 429) return false;
              return failureCount < 2;
            },
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
