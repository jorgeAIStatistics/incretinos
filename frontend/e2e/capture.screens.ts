import { appendFileSync, mkdirSync, rmSync } from "node:fs";
import { test } from "@playwright/test";

// Run through playwright.capture.config.ts (see the comment there). CAPTURE_LABEL names the output folder.
const LABEL = (process.env.CAPTURE_LABEL ?? "after").replace(/[^a-z0-9-]/gi, "");
const OUT = `../docs/design/${LABEL}`;
const METRICS = `${OUT}/metrics.jsonl`;

const ROUTES: [name: string, path: string][] = [
  ["home", "/"],
  ["molecules", "/molecules"],
  ["trials", "/trials"],
  ["trial-detail", "/trials/NCT05869903"],
  ["compare", "/compare"],
  ["estimands", "/estimands"],
  ["benefit-risk", "/benefit-risk"],
  ["safety", "/safety"],
  ["gaps", "/gaps"],
  ["design", "/design"],
  ["assistant", "/assistant"],
  ["sources", "/sources"],
];
// Routes that exist only after the Session 2 foundation; captured when CAPTURE_EXTRA=1.
const EXTRA: [name: string, path: string][] = [
  ["review", "/review"],
  ["quality", "/quality"],
  ["conflicts", "/conflicts"],
];
// Tab and selection states that are not the default view of a route. Each is captured and measured like a route.
// A tab name selects that tab; "button:<name>" presses that button and waits for the network to settle (the
// research assistant answering one of its example questions).
const STATES: [name: string, path: string, tab: string][] = [
  ["assistant-answer", "/assistant", "button:What evidence differentiates retatrutide from tirzepatide?"],
  ["design-simulation", "/design", "Synthetic simulation and sensitivity"],
  ["design-coverage", "/design", "Population coverage"],
  ["design-eligibility", "/design", "Eligibility and amendments"],
  ["sources-changes", "/sources", "Changes and review queue"],
  ["sources-validation", "/sources", "Validation rules"],
];
const VIEWPORTS = [
  { name: "1440x900", width: 1440, height: 900 },
  { name: "834x1112", width: 834, height: 1112 },
  { name: "390x844", width: 390, height: 844 },
];

test.beforeAll(() => {
  mkdirSync(OUT, { recursive: true });
});

// CAPTURE_ONLY=home,assistant,assistant-answer restricts the run to the named captures (the priority-page reviews).
const ONLY = (process.env.CAPTURE_ONLY ?? "").split(",").map((s) => s.trim()).filter(Boolean);
const routes: [name: string, path: string, tab?: string][] = [...(process.env.CAPTURE_EXTRA ? [...ROUTES, ...EXTRA] : ROUTES), ...STATES].filter(([name]) => ONLY.length === 0 || ONLY.includes(name));
let first = true;

for (const viewport of VIEWPORTS) {
  for (const [name, path, tab] of routes) {
    test(`${name} at ${viewport.name}`, async ({ page }, testInfo) => {
      if (first && testInfo.retry === 0 && viewport === VIEWPORTS[0] && name === routes[0][0]) rmSync(METRICS, { force: true });
      first = false;
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.goto(path, { waitUntil: "networkidle" });
      await page.locator("h1").first().waitFor({ state: "visible", timeout: 60_000 });
      // Wait until no loading placeholder is left, then let late requests settle.
      if (tab?.startsWith("button:")) await page.getByRole("button", { name: tab.slice("button:".length), exact: true }).first().click();
      else if (tab) await page.getByRole("tab", { name: tab, exact: true }).click();
      await page.waitForLoadState("networkidle");
      await page.waitForFunction(() => !document.querySelector('[role="status"][aria-label^="Loading"], .skeleton'), null, { timeout: 60_000 }).catch(() => undefined);
      await page.waitForLoadState("networkidle");
      await page.evaluate(() => window.scrollTo(0, 0));
      const metrics = await page.evaluate(() => {
        const root = document.documentElement;
        return {
          title: document.title,
          overflowX: root.scrollWidth - root.clientWidth,
          scrollHeight: root.scrollHeight,
          h1: document.querySelectorAll("h1").length,
          headings: Array.from(document.querySelectorAll("h1,h2,h3,h4")).map((h) => `${h.tagName.toLowerCase()}:${(h.textContent ?? "").trim().slice(0, 60)}`),
        };
      });
      appendFileSync(METRICS, `${JSON.stringify({ route: path, ...(tab ? { tab } : {}), name, viewport: viewport.name, ...metrics })}\n`);
      await page.screenshot({ path: `${OUT}/${name}-${viewport.name}.png`, fullPage: true, animations: "disabled", style: "nextjs-portal { display: none !important; }" });
    });
  }
}
