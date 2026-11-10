# IncretinOS

Clinical evidence and trial intelligence for incretin and amylin therapeutics, built entirely on public data.

IncretinOS helps clinical scientists and biostatisticians answer three questions about a body of trial evidence: **what do public sources actually report, can two results be compared, and what is still unknown?** The initial domain is Eli Lilly's obesity and diabetes portfolio — dulaglutide, tirzepatide, orforglipron (Foundayo), retatrutide, eloralintide, the eloralintide + tirzepatide combination, and adjacent pipeline compounds.

> Research software for qualified researchers. It is not a patient treatment tool, does not give medical advice or dosing recommendations, and is not a regulatory submission system. It is independent of Eli Lilly and uses no proprietary or patient-level data. No regulatory compliance (GxP, 21 CFR Part 11) is claimed.

![Comparison laboratory](docs/screenshots/04-comparison-laboratory.png)

## What it does

| Area | What you get |
|---|---|
| **Evidence ingestion** | Adapters for ClinicalTrials.gov API v2, PubMed E-utilities and openFDA (labels, FAERS) with retry/backoff, rate limiting, content hashing, versioned snapshots, change detection and a review queue. |
| **Normalized evidence model** | Drug → study → arm → endpoint → estimand → result → source. A value the source did not report is `NULL` and listed as missing — never zero. |
| **Comparability engine** | Classifies any two results as *direct randomized comparison*, *partially comparable*, *descriptive cross-study comparison*, *not suitable for efficacy comparison* or *insufficient evidence*, with a factor-by-factor explanation and the statements the evidence does and does not permit. |
| **Interpretation and validation** | Deterministic readings of each result and analysis, a language guard, and 16 scientific validation rules (non-inferiority is not superiority, estimands stay separate, missing intervals stay missing, …). |
| **Workbenches** | Benefit–risk profiles, preference-weighted summaries, hypothetical dose scenarios, safety intelligence, evidence-gap tracking, protocol-design calculations and reproducible synthetic simulations. |
| **Grounded assistant** | Answers composed from stored evidence with citations. Works with no language model; an optional model-drafted summary is kept only if every number traces to a cited evidence card. |
| **Provenance** | Every number carries its source, evidence tier, retrieval date, content hash, locator and verification status. Reports export as cited Markdown. |
| **Evidence review** | Every reviewable row has a stable evidence key and a fingerprint. Decisions (approve, reject, request information, flag wrong attribution, propose a correction) bind to the exact version reviewed, are audited, survive rebuilds, and are invalidated when the row or its source changes (status *changed since review — recheck required*, with the field diff). The `/review` workbench shows the value, its source, quote, location, automated attribution checks, conflict class and history. Nothing is human-verified without an attested decision; **no human review has taken place yet** (0 human-verified rows). |
| **Full-text evidence** | A licence-gated step reads the FDA multi-discipline review of NDA 220934 (FDA-authored tables only; the PDF is not stored) and CC BY Europe PMC articles, keeping the table, the exact cells used, footnotes, page or paragraph, parse method and a quality assessment. Every full-text row is *pending human review* and stays out of every analysis until a reviewer approves it. |
| **Source conflicts** | `/conflicts` classifies every group of rows from several sources that report the same quantity as a true conflict, insufficient information, an explainable difference (estimand, baseline, analysis set, follow-up, definition) or consistent, at the precision each source printed. Evidence tier is shown as context and never decides. |
| **Evidence quality** | `/quality` reports review status, completeness, source authority, reliability signals, precision and freshness as separate dimensions with true totals; every count opens exactly the records it counts. There is no combined score. |
| **Anchored indirect comparison** | Bucher's method from two stored randomized contrasts that share a comparator, only when twelve requirements pass (estimand, outcome, scale, follow-up, variance, reliable inputs, no open source conflict, population, …); otherwise a refusal with reasons. Labelled everywhere as not randomized and not a ranking. |
| **Claim verification** | Each sentence of an optional model draft is verified against the cited evidence (numbers, arms, drugs, direction, interval, estimand, hypothesis type, tier, …) and the draft is withheld unless every factual sentence is supported. Measured on four adversarial benchmark sets (below). |

## Quick start (offline, no API keys)

Requires Python ≥ 3.11 and Node ≥ 20.

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
cd backend && .venv/bin/incretinos seed --reset
```

```bash
cd backend && .venv/bin/incretinos serve --port 8000
```

In a second terminal:

```bash
cd frontend && npm install && INCRETINOS_API_URL=http://127.0.0.1:8000 npm run dev
```

Open <http://localhost:3000>. The seed step ingests 45 trials, their linked publications, four FDA labels, FAERS counts and the full-text fixtures (FDA review excerpt, Europe PMC articles) from committed fixtures in about 10 seconds; nothing touches the network. The web app runs on Next.js 16.4.6.

### Docker

```bash
docker compose up --build
```

PostgreSQL, the API (migrates, then seeds from fixtures) and the web app start together; the app is at <http://localhost:3000>. Set `INCRETINOS_API_PORT` / `INCRETINOS_WEB_PORT` if 8000 or 3000 are taken.

## Working with live data

```bash
cd backend && .venv/bin/incretinos seed --live            # query the public APIs
```

```bash
cd backend && .venv/bin/incretinos check-updates --live   # re-fetch tracked records, report changes
```

```bash
cd backend && .venv/bin/incretinos seed --live --record   # refresh the committed fixtures
```

```bash
cd backend && .venv/bin/incretinos anchor-web             # re-verify curated press-release quotes
```

Other commands: `incretinos validate` (run the 16 scientific validation rules), `incretinos report A B -o file.md` (export a comparison), `incretinos review status | reconcile [--check] | export | import` (the decision ledger), `incretinos benchmark-claims --set dev|dev-review|heldout|heldout-v2`. Configuration is by environment variable — see [.env.example](.env.example).

### Reviewer credentials

Without a credential the API is read-only: no HTTP request can change the database.

- **Personal tokens (use these for real review):** set `INCRETINOS_REVIEWERS_FILE` to a JSON file mapping each reviewer's name to the SHA-256 hex digest of that reviewer's own token, for example `{"Dr. Jane Doe": "<sha256 hex>"}`. Only those tokens can write, the shared token is then not accepted, and the reviewer name recorded with a decision is the one bound to the token. [.env.example](.env.example) shows how to create a token and its digest.
- **Shared development token:** `INCRETINOS_REVIEWER_TOKEN`. Whoever holds it types a reviewer name, and every decision is labelled *self-declared under the shared token*.

The token travels in the `X-Reviewer-Token` header, so a shared deployment needs TLS in front of it. `docker-compose.yml` passes only the shared token.

## Tests

```bash
cd backend && .venv/bin/pytest -q
```

```bash
cd frontend && npx playwright install chromium && npx playwright test
```

- **Backend: 1,161 tests**, all offline, passing on SQLite and on PostgreSQL 17 (`INCRETINOS_TEST_DATABASE_URL` points the suite at a PostgreSQL database, which it drops and rebuilds). They cover ingestion, the review lifecycle and the decision ledger, the automated attribution checks, data integrity, comparability and the mandatory scientific cases (each with a negative test), statistics against statsmodels/scipy, the claim verifier, full-text parsing and licence gating, conflict classification, evidence quality, the anchored indirect comparison, request limits and the read-only boundary.
- **Frontend: 89 browser tests in 10 specs** (workflows, design foundation, scientific presentation, review fixes, evidence review workbench, full-text evidence, source conflicts, evidence quality, anchored indirect comparison, user journey), each failing on any browser-console error, run against `next dev` and against the production build (`E2E_WEB=production`). Set `INCRETINOS_API_PORT` / `INCRETINOS_WEB_PORT` to move the test servers off 8011/3001. With `CAPTURE_DOC_SCREENSHOTS=1` the workflow spec rewrites the screenshots in [docs/screenshots](docs/screenshots); otherwise captures go to the git-ignored `frontend/test-results/workflow-screenshots` and a test run leaves the worktree unchanged.
- CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs lint, the tests on SQLite and on PostgreSQL, the migrations and a drift check, a fresh seed, the validation rules, the claim benchmarks, the container images, and every browser spec in both modes. The hosted run on `576030d` succeeded; the merged Session 2 code has not yet run on the hosted runner (see [docs/build-roadmap.md](docs/build-roadmap.md)).

### Claim-verification benchmarks

```bash
cd backend && .venv/bin/incretinos benchmark-claims --set heldout-v2
```

| Set | Role | Detection of unsupported sentences | False rejection of faithful sentences |
|---|---|---|---|
| `dev` (249 cases) | development | 100% (177/177) | 0.0% (0/72) |
| `dev-review` (70) | development: an independent reviewer's attack, added afterwards | 100% (45/45) | 4.0% (1/25) |
| `heldout` (163, heldout-v1) | blind when first measured (98.3% / 20.8%); development since its misses were studied | 100% (115/115) | 0.0% (0/48) |
| `heldout-v2` (221) | **blind**: written without sight of the verifier, reported as measured, not tuned | **99.3% (147/148)** | **19.2% (14/73)** |

A rejected faithful sentence is withheld, never passed; the deterministic answer is shown instead.

## How evidence is kept honest

1. **Tiers are never blended.** FDA labeling, peer-reviewed publications, registry-posted results and press releases are separate rows with separate badges, even for the same arm and endpoint. Where they disagree, the discrepancy is itself surfaced.
2. **Verification is mechanical and visible.** *Machine-extracted* means parsed from a structured registry payload whose hash is stored. *Quote-anchored* means a curated value's locator quote was matched verbatim against the source text — which shows the number is in the quote, not that it belongs to the stated arm or estimand; automated checks flag where the quote does not show that. Everything else, including every full-text extraction, is *pending human review*. *Human-verified* exists only as the result of an attested decision on that exact version, and becomes *changed since review — recheck required* when the row or its source changes. Today no row is human-verified.
3. **A number cannot be curated without its supporting text.** The curation schema rejects any value whose digits do not appear in its own locator quote.
4. **Text that may be copyrighted is not redistributed.** Registry records, FDA labels and FAERS counts are stored in full, as are CC BY full texts. For abstracts, press releases, NC/ND-licensed articles and the mixed-authorship FDA review only a hash, the retrieval date, extracted cells and the short quotes that anchor values are kept.
5. **Calculations are deterministic and labelled.** Derived intervals, risk contrasts, utility scores, scenarios and simulations are produced outside any language model and are marked as inference, hypothetical or synthetic wherever they appear.

## Documentation

| Document | Contents |
|---|---|
| [docs/scientific-research.md](docs/scientific-research.md) | Drug-by-drug evidence with sources, retrieval dates and regulatory status (generated from the database) |
| [docs/product-requirements.md](docs/product-requirements.md) | Users, scope, requirements and acceptance criteria |
| [docs/data-source-inventory.md](docs/data-source-inventory.md) | Each source, what is taken from it, licence and storage policy |
| [docs/evidence-limitations.md](docs/evidence-limitations.md) | What the platform cannot tell you, and known weaknesses |
| [docs/architecture.md](docs/architecture.md) | System design, data model, engines, security, future restricted-data requirements |
| [docs/build-roadmap.md](docs/build-roadmap.md) | What is built, what was verified, and what remains |
| [docs/examples/acceptance-demonstrations.md](docs/examples/acceptance-demonstrations.md) | The seven acceptance demonstrations with real output |
| [docs/examples/session-2-demonstrations.md](docs/examples/session-2-demonstrations.md) | Session 2 demonstrations A–F (review lifecycle, claim verification, conflicts, full-text extraction, statistical integrity, user journey), produced by `backend/scripts/session2_demos.py` on a scratch database |
| [docs/engineering-audit-session-2.md](docs/engineering-audit-session-2.md) | The audit at the start of Session 2 and the status of every finding after it |
| [docs/design-system.md](docs/design-system.md) | Interface tokens, components and rules |

## Repository layout

```
backend/
  incretinos/
    ingestion/     transports, snapshots, ClinicalTrials.gov / PubMed / openFDA adapters, pipeline, scheduler
    curation/      schema, loader and data files for publication-, label- and release-reported evidence
    fulltext/      licence gating, JATS and FDA-review PDF parsers, column-to-arm mapping, loader
    review/        evidence identity and fingerprints, lifecycle and decision ledger, automated checks, queries
    engines/       estimands, findings, comparability, interpretation, validation, stats, safety,
                   benefit_risk, gaps, design, conflicts, quality, indirect
    assistant/     retrieval, deterministic answers, optional model provider, claim verifier, benchmark runner
    reports/       cited Markdown export
    api/           FastAPI application
  fixtures/        recorded public-API responses for offline use
  benchmarks/      claim-verification benchmark sets
  tests/           unit and integration tests
  scripts/         research-notes converter, document generator, Session 2 demonstrations
frontend/          Next.js application and Playwright tests
research/          retrieval notes (with source URLs and locator quotes) and conversion report
docs/              specification, limitations, examples, screenshots
```

## Ownership and permissions

Maintained by **Jorge Rodriguez** ([@jorgeAIStatistics](https://github.com/jorgeAIStatistics)).
See [COPYRIGHT.md](COPYRIGHT.md) for ownership and third-party attribution scope,
and [LICENSE](LICENSE) for permissions. Source-visible for portfolio review; reuse rights are reserved.
