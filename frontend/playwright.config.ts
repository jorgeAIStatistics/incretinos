import { defineConfig, devices } from "@playwright/test";

const API_PORT = process.env.INCRETINOS_API_PORT ?? "8010";
const WEB_PORT = process.env.INCRETINOS_WEB_PORT ?? "3000";

// End-to-end tests run against the offline-seeded stack (no network, no API keys).
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
      command: `cd ../backend && .venv/bin/uvicorn incretinos.api.main:app --host 127.0.0.1 --port ${API_PORT}`,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      reuseExistingServer: true,
      timeout: 60_000,
    },
    {
      command: `INCRETINOS_API_URL=http://127.0.0.1:${API_PORT} npx next dev -p ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});
