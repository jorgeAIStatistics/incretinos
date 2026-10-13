# Build roadmap

Status as of 2026-10-13. "Verified" means demonstrated by an automated test or a recorded run in this repository — not asserted.

## Delivered

| Stage | Delivered | Verified by |
|---|---|---|
| 0 Discovery and specification | Source-grounded research on every core and adjacent compound; six specification documents | `research/web-notes`, `docs/scientific-research.md` (generated from the database) |
| 1 Evidence ingestion | ClinicalTrials.gov, PubMed, openFDA label and FAERS adapters; retry/backoff, rate limiting, hashing, versioning, duplicate and change detection, identifier reconciliation, review queue, audit trail, fixture recording; 33-table evidence model | `tests/test_ingestion.py`; live and offline seeds produce identical databases; Alembic migration creates all tables; PostgreSQL build in Docker matches SQLite row-for-row |
| 2 Comparability engine | Factor-by-factor assessment (about twenty dimensions), five classifications, reasons, permitted/prohibited statements, requirements, head-to-head trial lookup | `tests/test_scientific_validation.py` |
| 3 Interpretation engine | Deterministic readings, language guard, 15 validation rules covering the 12 mandatory cases | Each case has a positive and a negative test; `incretinos validate` passes 15/15 on SQLite and PostgreSQL |
| 4 Efficacy and tolerability | Arm profiles, risk difference/ratio, preference-weighted summary, hypothetical dose scenarios | `tests/test_statistics.py` |
| 5 Evidence gaps | 13 rule families, gap-nature typing, configurable priority, source-conflict detection across documented-equivalent doses | `tests/test_platform.py` |
| 6 Trial design | Eligibility comparison, coverage matrix, complexity counts, sample size and power (five designs), sensitivity scenarios, seeded simulation, amendment history | Statistics checked against statsmodels, scipy and published worked values |
| 7 Safety intelligence | Trial adverse-event tables by measure kind, label warnings and change detection, FAERS trends, disproportionality with caveats, hard guards against incidence/causality inference | `tests/test_scientific_validation.py`, `tests/test_statistics.py` |
| 8 Grounded assistant | Retrieval, six question types, refusals, typed answer blocks, pluggable provider, citation-fidelity check, prompt-injection hardening | `tests/test_platform.py` with stand-in providers |
| 9 Interface | Eleven pages, custom interval charts with table views and provenance footers | 11 Playwright tests; screenshots in `docs/screenshots` |
| 10 Engineering | FastAPI, SQLAlchemy, Alembic, Next.js, Docker Compose with PostgreSQL, CI workflow, reviewer-token permission boundary | API and web images built; Compose stack started and checked |
| 11 Verification | 159 backend tests and 11 browser tests, all offline | Test runs recorded in this session |

## Defects found by the verification work (and fixed)

- Number formatting stripped trailing zeros of whole numbers, so "week 80" rendered as "week 8". Found when generating the acceptance document; fixed in both backend and frontend with regression tests over every stored time point.
- PostgreSQL rejected source text longer than a `VARCHAR` limit that SQLite silently accepted, so curated evidence failed to load in Docker. Source-derived text columns are now unbounded and a test enforces declared lengths.
- A registry outcome ("participants who maintain ≥80% of weight lost") was categorised as percent weight change; caught by validation rule V10.
- Source-conflict detection grouped SURMOUNT-4 values measured from different baselines, and missed the ATTAIN-1 label-versus-publication discrepancy because the label names doses by tablet strength; both fixed.
- The language guard did not recognise a negation placed after the flagged phrase ("superiority was not demonstrated").

## Not done, or only partly done

| Item | State | Why it matters |
|---|---|---|
| Human review of curated evidence | Not started (queue populated) | Pending rows should not be cited as verified |
| Live language-model call | Not exercised (no key) | Provider code follows the documented SDK but is untested end to end |
| CI workflow | Written, not run on a hosted runner | First run may need runner-specific fixes |
| Scheduled ingestion | Implemented, disabled by default, not soak-tested | Long-running behaviour unobserved |
| Real registry change detection | Diff logic tested; the seeded example is synthetic | Needs repeated live runs over time |
| Full-text extraction | Not attempted | Per-arm intervals and hierarchies remain missing |
| pgvector retrieval | Not used; BM25 over evidence cards is sufficient at this size | Revisit if the corpus grows by orders of magnitude |
| Object storage for snapshots | Not needed; snapshots live in the database | Revisit for PDF sources |
| Authentication beyond a shared reviewer token | Not built | Required before any multi-user or restricted-data use |
| Mobile layout | Responsive but only spot-checked | Dense tables scroll horizontally |

## Next steps, by scientific value

1. **Reviewer pass** over the 104 pending result rows and the press-release claims, starting with TRIUMPH-3/-4 and EloraTZP.
2. **Re-extract on publication**: TRIUMPH-3, TRIUMPH-4 and EloraTZP are press-release only; TRIUMPH-5 (retatrutide vs tirzepatide) reaches primary completion in 2026-11 by the registry's estimate.
3. **Supplement extraction** for per-arm confidence intervals and testing hierarchies (ATTAIN-1/-2, TRIUMPH-1/-2, SURMOUNT-5), with the same quote-anchoring rule.
4. **Anchored indirect comparison module** (Bucher) for trials sharing a placebo control, restricted to pairs the comparability engine classes as partially comparable, with the consistency assumptions displayed.
5. **Structured eligibility** (BMI, HbA1c, age thresholds as numbers) to make population coverage quantitative.
6. **Broader classes**: semaglutide, cagrilintide/CagriSema, survodutide and other amylin and incretin agents as first-class drugs, so comparators are tracked to the same standard.
7. **Role-based access and immutable audit storage** as the precondition for any restricted-data deployment.

## Technical risks

- **Public API drift.** ClinicalTrials.gov v2, E-utilities and openFDA can change fields or limits (openFDA already rejects `limit=1000` on count queries). Fixtures pin behaviour for tests; live runs fail loudly into the review queue.
- **Curation scale.** Every curated number needs a quote. That is deliberate friction; it will not scale to hundreds of trials without reviewer tooling.
- **Heuristic classification.** Estimand, population and endpoint categorisation are rule-based; each rule is tested on the tracked trials and will need extension for new programmes.
- **Press-release instability.** Web pages move or change; the stored hash detects change but cannot recover the earlier text.
