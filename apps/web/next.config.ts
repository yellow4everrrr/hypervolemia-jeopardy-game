import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  // Emits a self-contained server bundle with only the modules actually imported, so the
  // runtime image copies a few megabytes instead of the whole `node_modules` tree.
  output: "standalone",
};

export default config;
