import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Design-review captures: full-page screenshots of every route at three viewports, written to docs/design/<label>/.
// This is NOT part of `npm run test:e2e`: the default configuration only collects *.spec.ts, and this file is
// e2e/capture.screens.ts. It reuses the default configuration's isolated, freshly seeded stack and ports.
//
//   INCRETINOS_API_PORT=8025 INCRETINOS_WEB_PORT=3025 CAPTURE_LABEL=after npm run capture
export default defineConfig({
  ...base,
  testDir: "./e2e",
  testMatch: /capture\.screens\.ts$/,
  timeout: 180_000,
  retries: 1,
  reporter: [["line"]],
});
