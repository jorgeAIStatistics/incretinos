import { defineConfig, devices } from "@playwright/test";

const API_PORT = process.env.INCRETINOS_API_PORT ?? "8011";
const WEB_PORT = process.env.INCRETINOS_WEB_PORT ?? "3001";
// Browser tests use a dedicated database and reviewer token. Review decisions recorded by a test must never
// reach the working database, and the working database must never contain test "human verifications".
export const E2E_REVIEWER_TOKEN = "e2e-reviewer-token";
const E2E_ENV = `INCRETINOS_DATABASE_URL=sqlite:///./var/e2e.db INCRETINOS_REVIEWER_TOKEN=${E2E_REVIEWER_TOKEN} INCRETINOS_OFFLINE=true INCRETINOS_LLM_PROVIDER=none`;

// End-to-end tests run against a freshly seeded offline stack on its own ports (no network, no API keys).
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: { baseURL: `http://localhost:${WEB_PORT}`, viewport: { width: 1440, height: 900 }, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      command: `cd ../backend && rm -f var/e2e.db && ${E2E_ENV} .venv/bin/incretinos seed > var/e2e-seed.log 2>&1 && ${E2E_ENV} .venv/bin/uvicorn incretinos.api.main:app --host 127.0.0.1 --port ${API_PORT}`,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 180_000,
    },
    {
      command: `NEXT_DIST_DIR=.next-e2e INCRETINOS_API_URL=http://127.0.0.1:${API_PORT} npx next dev -p ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
