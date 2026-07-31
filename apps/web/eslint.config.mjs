// eslint-config-next ships flat configs directly on its subpath exports, so no
// FlatCompat shim is needed — and on ESLint 9.39 the shim throws on a circular
// reference inside the React plugin, so avoiding it is required rather than tidier.
import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...coreWebVitals,
  ...typescript,
];

export default config;
