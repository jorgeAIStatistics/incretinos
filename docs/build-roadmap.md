# Build roadmap

Status as of 2026-11-10, at the merge of the last Session 2 wave (branch `s2/integration3`). "Verified" means demonstrated by an automated test or a recorded run in this repository — not asserted. Where a number is given, it is from a run on that code.

## Session 2 delivered

| Area | Delivered | Verified by |
|---|---|---|
| Evidence review lifecycle | Every reviewable row has a content-derived evidence key and a fingerprint; a decision binds to the exact version it was made on; approve sets HUMAN_VERIFIED on that version only; reject removes the row from every analytic surface through one session-level choke point (`db.py`); a later change to the row or its source turns an approval into SOURCE_CHANGED_RECHECK_REQUIRED with the field diff, and a rejected row that changes is held out of use until decided again; decisions survive a reseed and `seed --reset`; the decision ledger exports and imports; reconcile is idempotent | `test_review_lifecycle.py`, `test_review_lifecycle_fixes.py`; demonstration A (`docs/examples/session-2-demonstrations.md`); `incretinos review reconcile` run twice on a fresh seed with 0 changes, `reconcile --check` clean |
| Reviewer identity | Personal reviewer tokens (`INCRETINOS_REVIEWERS_FILE`, SHA-256 digests): the reviewer name is the server's; the shared development token remains and labels its decisions self-declared | `test_review_lifecycle_fixes.py::test_f8_with_personal_tokens_the_reviewer_name_comes_from_the_token`, `::test_f8_shared_token_decisions_are_labelled_self_declared`; demonstration A |
| Evidence Review workbench (`/review`) | Queue with filters, true totals and paging; the dossier with value, source, quote, location, checks, conflict class, full-text block, versions, decisions and audit; decisions through the API | `review.spec.ts` (6 browser tests), `test_review_lifecycle.py::test_the_dossier_gives_a_reviewer_everything_needed_to_decide` |
| Automated attribution checks | Explained flags on every record (quote, arm, unit, time point, n, estimand, full-text reading) with a risk level; flags never change a status | `test_review_checks.py` |
| Data repairs | Converter key collisions, responder thresholds, registry contrast direction, arm merging, mg/kg, adverse-event measure kinds, every posted serious adverse-event term, stored arm roles | `test_data_integrity.py`, `test_safety_presentation.py` |
| Claim verifier | Structured, fail-closed verification of each sentence of a model draft against the cited cards; four benchmark sets | `test_claim_verification.py`; `incretinos benchmark-claims` (results below) |
| Comparability fixes | "Direct randomized comparison" only within one randomization and one quantity; endpoints matched on measurement, threshold and unit; analyses matched on estimand, baseline, threshold and arm pair; internally inconsistent analyses ground no claim | `test_scientific_validation.py`, `test_integration_wave2.py` |
| Hardening | Request-body limits per route, per-client query budget and rate limits, input-free 422s, a read-only deployment that writes nothing, trusted proxies, pinned constraints, a PostgreSQL CI job with a migration-drift check | `test_hardening.py` |
| Interface foundation and scientific presentation | Design tokens and components, phone navigation, no sideways scrolling, true totals, error states with retry, coherent headline charts with units and arm roles | `foundation.spec.ts` (17), `presentation.spec.ts` (23), `review-fixes.spec.ts` (16) |
| Full-text evidence | Licence-gated reading of the FDA multi-discipline review of NDA 220934 (FDA-authored tables only) and Europe PMC CC BY articles; tables, cells, footnotes, locators and a quality assessment for every extraction; 65 FULLTEXT rows, all PENDING_HUMAN_REVIEW and excluded from every analytic surface until approved | `test_fulltext.py`, `fulltext.spec.ts` (2); `test_integration_wave3.py`; demonstration D |
| Source conflicts (`/conflicts`) | Every group of rows from several sources reporting the same quantity is classified true conflict, insufficient information, explainable difference or consistent, field by field, at the precision printed; evidence tier is context only; the gap tracker's G12 follows the classification | `test_conflicts.py`, `conflicts.spec.ts` (3); demonstration C |
| Evidence Quality (`/quality`, `/quality/records`) | Six separate dimensions with true totals and exact drill-downs, no combined score; the conflict dimension counts the conflict engine's groups class by class and opens them on `/conflicts` | `test_quality.py`, `quality.spec.ts` (7) |
| Anchored indirect comparison | Bucher method from two stored contrasts with a feasibility checklist (twelve requirements, including no open source conflict); refusals with reasons; the comparison laboratory section; the direct comparison's reported effect | `test_indirect.py` (checked against a weighted least-squares fit, statsmodels and Monte Carlo coverage), `indirect.spec.ts` (3); demonstration E |
| Integration | The wave-by-wave guarantees between branches; the end-to-end user journey | `test_integration_wave1.py`, `test_integration_wave2.py`, `test_integration_wave3.py`; `journey.spec.ts` (demonstration F) |
| Validation rules | 16 rules (V01–V12 the mandatory scientific cases; V16: human-verified and rejected statuses rest on an attested decision for the exact version) | `incretinos validate`: 16/16 PASS on a fresh SQLite seed and on PostgreSQL 17 |

### Recorded runs at the final merge

| Check | Result |
|---|---|
| `ruff check incretinos tests scripts` | clean |
| Backend tests | 1,156 passed on SQLite; 1,156 passed on PostgreSQL 17 (`INCRETINOS_TEST_DATABASE_URL`, throwaway container) |
| Alembic | one head (`cb85e55cbfb4`); upgrade, `alembic check` (no drift), downgrade to base and upgrade again clean on SQLite and PostgreSQL; downgrade of `cb85e55cbfb4` on a seeded database removes the FULLTEXT rows |
| Fresh seed | 45 trials, 27,760 reviewable rows: 26,999 machine-extracted, 494 quote-anchored, 267 pending human review, 0 human-verified, 0 decisions |
| Claim benchmarks | dev 100% detection / 0.0% false rejection; dev-review 100% / 4.0%; heldout (v1) 100% / 0.0%; **heldout-v2 (blind) 99.3% (147/148) / 19.2% (14/73)** |
| Frontend | `tsc` (application and browser tests) clean; lint 0 errors (282 warnings); `next build` (Next.js 16.4.6) succeeds; `npm audit --omit=dev` 0 vulnerabilities |
| Browser tests | 89 tests in 10 specs, passing against `next dev` and against the production build |

Benchmark protocol: `dev` and `dev-review` are development data. `heldout` (heldout-v1) was measured once, blind, on the merged Session 2 code (98.3% detection, 20.8% false rejection) and has been development data since its misses were studied. `heldout-v2` was written without sight of the verifier and is reported as measured, without tuning.

### Continuous integration

The hosted CI run on `576030d` succeeded (earlier documents that called CI "written, not run" were out of date). The workflow now runs the backend on SQLite and on PostgreSQL, the claim benchmarks, the container images, and every browser spec in both development and production modes (a test checks that the workflow names every spec). The merged Session 2 code has not yet run on the hosted runner, because nothing has been pushed from this session; every step except the container-image job was run locally with the results above.

## Partially implemented

| Item | State |
|---|---|
| Attribution of quote-anchored values | Flagged, not resolved: 594 rows carry a quote-based check flag, 206 a HIGH-risk flag. Only a reviewer can resolve them |
| Reviewer authentication | Personal tokens exist; the shared development token is still accepted (labelled self-declared) and `docker-compose.yml` passes only the shared token |
| Full-text extraction | Four documents; pooled safety tables, image-only tables and several configured-out tables are recorded as limitations, not extracted |
| Conflict engine | Results, analyses and adverse events; claims are not compared |
| Explicit `any` in the frontend | Lint runs and shared API types exist, but the count was not reduced |
| Colour contrast | Tokens changed to meet WCAG AA; checked by hand, no automated test |
| Language-model layer | Implemented and guarded by the claim verifier; no live provider call has been made |

## Not done

| Item | Why it matters |
|---|---|
| Human review | 0 human-verified rows. Nothing in the product may be cited as verified by a person |
| Gap rules `XS-06`, `XS-07`, `XS-13` | ACHIEVE-2 and ATTAIN-MAINTAIN are reported as having no results although results are stored; a missing-superiority gap cannot clear |
| Splitting `api/main.py` (`F19`) | 1,552 lines; only Session 2 features are in routers |
| Hash-pinned dependencies | `constraints.txt` pins versions, not hashes |
| Scheduled ingestion soak and real registry change detection over time | The seeded change example is synthetic |
| Restricted-data controls | See architecture.md: none of them exists |

## Next milestone

1. **A real reviewer pass** with personal tokens, in this order: the 24 true conflicts (each is a reconciliation question), the 65 full-text rows (they enter no analysis until approved), the 206 HIGH-risk flagged rows.
2. **Fix the three gap rules** (`XS-06`, `XS-07`, `XS-13`) with tests on ACHIEVE-2, ATTAIN-MAINTAIN and SURPASS-CVOT.
3. **Reduce the verifier's false rejections** without loosening detection, measured on a new blind set (heldout-v3); heldout-v2 becomes development data once its misses are studied.
4. **Extend full-text extraction** to the FDA review's configured-out key secondary tables (25, 32, 33).
5. **Run the merged code on the hosted CI**, including the container-image job.

## Session 1 (for reference)

| Stage | Delivered | Verified by |
|---|---|---|
| 0 Discovery and specification | Source-grounded research on every core and adjacent compound | `research/web-notes`, `docs/scientific-research.md` (generated from the database) |
| 1 Evidence ingestion | ClinicalTrials.gov, PubMed, openFDA label and FAERS adapters with hashing, versioning, change detection and a review queue | `tests/test_ingestion.py` |
| 2–3 Comparability and interpretation | Factor-by-factor classification, deterministic readings, language guard, validation rules | `tests/test_scientific_validation.py` |
| 4–7 Workbenches | Benefit–risk, gaps, trial design, safety intelligence | `tests/test_statistics.py`, `tests/test_platform.py` |
| 8 Grounded assistant | Retrieval, refusals, typed answer blocks, pluggable provider | `tests/test_platform.py` |
| 9–10 Interface and engineering | Research pages, charts with table views, FastAPI, Alembic, Next.js, Docker Compose | Browser tests; the hosted CI run on `576030d` |

## Technical risks

- **Public API drift.** ClinicalTrials.gov v2, E-utilities, openFDA and Europe PMC can change fields or limits. Fixtures pin behaviour for tests; live runs fail loudly into the review queue.
- **Curation and review scale.** Every curated number needs a quote, and every flagged or full-text row needs a reviewer. That is deliberate friction.
- **Heuristic classification.** Estimand, population, endpoint and arm matching are rule-based and tested on the tracked trials; new programmes will need extensions.
- **PDF layout drift.** The FDA table parser reads positioned text; a re-typeset review would be re-parsed and its rows rechecked through their fingerprints, but a layout it cannot read produces a recorded limitation and no rows.
