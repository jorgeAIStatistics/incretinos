import { defineConfig, devices } from "@playwright/test";

const API_PORT = process.env.INCRETINOS_API_PORT ?? "8011";
const WEB_PORT = process.env.INCRETINOS_WEB_PORT ?? "3001";
// Browser tests use a dedicated database and reviewer token. Review decisions recorded by a test must never
// reach the working database, and the working database must never contain test "human verifications".
export const E2E_REVIEWER_TOKEN = "e2e-reviewer-token";
const E2E_ENV = `INCRETINOS_DATABASE_URL=sqlite:///./var/e2e.db INCRETINOS_REVIEWER_TOKEN=${E2E_REVIEWER_TOKEN} INCRETINOS_OFFLINE=true INCRETINOS_LLM_PROVIDER=none`;

// E2E_WEB=production runs the same tests against the production build, served by the standalone server the web
// image ships (node server.js), so the production Content-Security-Policy, the baked /api rewrite and the
// standalone output are what the browser sees. The default is `next dev`, which also reports React warnings.
const PRODUCTION = process.env.E2E_WEB === "production";
const PROD_DIST = ".next-e2e-prod";
const WEB_HOST = PRODUCTION ? "127.0.0.1" : "localhost";
const API_FOR_WEB = `INCRETINOS_API_URL=http://127.0.0.1:${API_PORT}`;
const webCommand = PRODUCTION
  ? [
      `rm -rf ${PROD_DIST}`,
      `NEXT_DIST_DIR=${PROD_DIST} ${API_FOR_WEB} npx next build`,
      // The same layout as the web image (frontend/Dockerfile): static assets and public files next to server.js.
      `cp -R ${PROD_DIST}/static ${PROD_DIST}/standalone/${PROD_DIST}/static`,
      `cp -R public ${PROD_DIST}/standalone/public`,
      `NODE_ENV=production PORT=${WEB_PORT} HOSTNAME=${WEB_HOST} ${API_FOR_WEB} node ${PROD_DIST}/standalone/server.js`,
    ].join(" && ")
  : `NEXT_DIST_DIR=.next-e2e ${API_FOR_WEB} npx next dev -p ${WEB_PORT}`;

// End-to-end tests run against a freshly seeded offline stack on its own ports (no network, no API keys).
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: { baseURL: `http://${WEB_HOST}:${WEB_PORT}`, viewport: { width: 1440, height: 900 }, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      command: `cd ../backend && rm -f var/e2e.db && ${E2E_ENV} .venv/bin/incretinos seed > var/e2e-seed.log 2>&1 && ${E2E_ENV} .venv/bin/uvicorn incretinos.api.main:app --host 127.0.0.1 --port ${API_PORT} --no-proxy-headers`,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 180_000,
    },
    {
      command: webCommand,
      url: `http://${WEB_HOST}:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: PRODUCTION ? 300_000 : 120_000,
    },
  ],
});
