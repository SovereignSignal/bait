// ESLint flat config. Next 16 removed `next lint`, so `npm run lint` runs the ESLint CLI directly with the rules
// Next ships (core-web-vitals + typescript). Narrow exceptions live next to the code they cover, with a reason.
import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([".next/**", ".next-fresh/**", "out/**", "build/**", "next-env.d.ts", "test-results/**", "playwright-report/**"]),
]);
