import type { Metadata } from "next";

import "./globals.css";
import { Providers } from "./providers";
import { Shell } from "@/components/shell";

export const metadata: Metadata = {
  title: "Ledgerline",
  description:
    "Institutional-grade AI trading journal. Python computes every statistic; the AI layer only interprets them.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <Providers>
          <Shell>{children}</Shell>
        </Providers>
      </body>
    </html>
  );
}
