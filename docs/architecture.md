# Architecture

A modular monolith: one Python service for ingestion, statistics and the API; one PostgreSQL database; one Next.js web application. No graph database, message broker, orchestration platform or microservices — the conceptual evidence graph is ordinary relational tables, and nothing in the workload justifies more.

```
 Public sources                        Backend (Python · FastAPI)                         Web (Next.js)
┌──────────────────┐   ┌───────────────────────────────────────────────────────┐   ┌──────────────────────┐
│ ClinicalTrials   │   │ ingestion/   transport → adapters → snapshots → diff  │   │ 15 research pages    │
│ PubMed           │──▶│ curation/    schema → loader (quote anchoring)        │   │ incl. /review,       │
│ openFDA label    │   │ fulltext/    licence gate → JATS / PDF tables → rows  │◀──│ /conflicts, /quality │
│ openFDA FAERS    │   │ review/      identity · lifecycle · checks · ledger   │   │ charts with table    │
│ Europe PMC       │   │ engines/     estimands · findings · comparability ·   │   │ views and provenance │
│ FDA review PDF   │   │              interpretation · validation · stats ·    │   │ /api proxied to the  │
│ sponsor releases │   │              safety · benefit_risk · gaps · design ·  │   │ backend              │
└──────────────────┘   │              conflicts · quality · indirect           │   └──────────────────────┘
        ▲              │ assistant/   retrieval → deterministic answer         │
        │ fixtures     │              (→ optional model draft → claim verifier)│
┌──────────────────┐   │ reports/     cited Markdown                           │
│ backend/fixtures │   │ api/         typed REST endpoints, limits, reviewer   │
└──────────────────┘   └───────────────────────────┬───────────────────────────┘
                                                   ▼
                                    PostgreSQL (SQLite for offline/dev/tests)
```

## Evidence model

Thirty-eight tables (`backend/incretinos/models.py`), migrated with Alembic (one line of revisions, head `cb85e55cbfb4`; `alembic check` in CI fails on drift between models and migrations).

| Group | Entities |
|---|---|
| Provenance | `EvidenceSource`, `SourceSnapshot`, `ChangeEvent`, `ReviewQueueItem`, `ReviewDecision`, `EvidenceVersion`, `AuditEvent`, `IngestionRun` |
| Full text | `FullTextDocument`, `ExtractedTable`, `FullTextExtraction` |
| Drugs | `Drug`, `DrugMechanism`, `Indication`, `Formulation`, `Dose`, `ExposureEquivalence` |
| Studies | `Study`, `StudyDrug`, `StudyVersion`, `StudyArm`, `Population`, `InclusionCriterion`, `ExclusionCriterion` |
| Evidence | `Endpoint`, `Estimand`, `IntercurrentEventStrategy`, `OutcomeResult`, `StatisticalAnalysis`, `AdverseEvent`, `ParticipantFlow` |
| Literature | `Publication`, `StudyPublication` |
| Claims and gaps | `EvidenceClaim`, `EvidenceGap` |
| Safety and index | `LabelRecord`, `SpontaneousReportSeries`, `DiscoveredTrial` |

Conventions that hold everywhere:

- **Missing is NULL.** A value the source did not report is `NULL` and named in `missing_fields`. There is no code path that writes zero for an absent number, and a derived interval is returned separately, flagged as inference.
- **Every evidence row has a source, a tier and a verification status.** Controlled vocabularies live in `enums.py`.
- **Origins are separate.** Registry-derived rows (`REGISTRY`) are rebuilt on each ingestion; curated rows (`CURATED`) are loaded from files; full-text rows (`FULLTEXT`) are rebuilt by the full-text step; none overwrites another, so one arm can carry a registry value, a publication value, a label value and an FDA-review value side by side.
- **Free text from sources is unbounded.** Only keys, enums and dates have length limits (a test enforces that stored values fit, because PostgreSQL rejects what SQLite tolerates).

## Ingestion

`Transport` abstracts access. `LiveTransport` adds per-service rate limiting, retry with exponential backoff and jitter, `Retry-After` handling, and optional fixture recording. `FixtureTransport` serves the recorded responses and never opens a socket. Adapters address resources by a stable fixture key, so live ingestion, fixture recording and the offline demo are the same code path.

For each source: fetch → canonicalize → hash → `record_snapshot`. A new snapshot is written only when the hash changes; a change produces a structured diff (`ctgov_diff.py`: status, enrollment, dates, arms, outcomes, eligibility, linked PMIDs, posted results), a `ChangeEvent` and a review-queue item. The registry's daily data-version stamp is excluded from the hash so it does not cause false changes.

The registry adapter extracts protocol fields and, when results are posted, arm-level outcome values, between-arm analyses, adverse events and participant flow exactly as posted, with a JSON path as the locator. The estimand is classified from the analysis-population text and marked as inferred.

A failure in one source is recorded (`IngestionRun`, review queue, audit log) and the run continues; a failed study yields no rows rather than partial ones.

## Curation and verification

Curated files (`curation/data`) hold what the APIs cannot supply: publication-, label- and release-reported values, mechanisms, regulatory status, formulations. The schema (`curation/schema.py`) rejects a number whose digits do not appear in its own locator quote, an interval with one limit, and an empty result row. The loader resolves each cited source, refuses an "approved" status that does not cite FDA labeling or review, and assigns verification by matching the quote against stored source text (registry, label) or against excerpts verified at fixture-recording time (abstracts, web pages).

## Evidence review lifecycle

`backend/incretinos/review/`. A status is never stored as a fact of its own: it is derived from the row's mechanical status, its decision history, its current fingerprint and the state of its source, and re-derived for every row after every load (`reconcile`, idempotent).

- **Identity and fingerprints** (`identity.py`). Rows are deleted and re-inserted by every load, so a primary key says nothing. Each row gets a `natural_key` (which quantity from which source; never a value, id or position), an `evidence_key` (the natural key, with a recorded tie-breaker only when several rows share it) and a `fingerprint` over the content a reviewer signs off on. Every version ever seen is kept in `evidence_version` with a readable snapshot, which makes "what changed" answerable. Fields derived by the platform's own classifiers are left out of keys so that an improved classifier asks for a recheck instead of orphaning a decision.
- **Decisions** (`lifecycle.py`). APPROVE, REJECT, REQUEST_INFO, FLAG_WRONG_ATTRIBUTION, PROPOSE_CORRECTION — append-only, attached to (type, evidence key, fingerprint) and the source version the reviewer saw, with an attested flag and a rationale that is checked for boilerplate. Only `lifecycle.py` writes HUMAN_VERIFIED or REJECTED; the ORM refuses any other assignment and validation rule V16 re-derives them from the ledger. Approval on an older fingerprint becomes SOURCE_CHANGED_RECHECK_REQUIRED with the field diff; a rejection on an older fingerprint also holds the row out of use (`review_hold`) until a person decides on the current version. Decisions follow a relabelled row through the natural key or a content anchor; a decision that matches nothing is an orphan, listed and raised, never deleted. Every writer takes the review lock. The ledger exports and imports (`incretinos review export | import`).
- **The choke point** (`db.py`). One `do_orm_execute` hook adds loader criteria to every ORM SELECT, relationship loads included: rows that are REJECTED or on review hold, and FULLTEXT rows that no reviewer has approved, are invisible to every engine, route, report and assistant module. Inspection code opts in explicitly (`rejected_visible`, used by the review workbench and the quality page's review-status dimension; `INCLUDE_UNREVIEWED_FULLTEXT`, used only by the automated checks). The review API states the same rule per row (`usable`, `exclusion_reason`).
- **Automated checks** (`checks.py`). Each record gets explained flags with a severity (the quote does not name the arm, the time point, the n, the unit; several candidate values; an estimand contradicted by the quote; for full-text rows an ambiguous column-to-arm match, a post-hoc subgroup, a value read from running text …) and a risk level. Flags inform the reviewer; they never change a status.
- **Workbench and API** (`api/routers/review.py`, `/review`). The queue with filters and true totals; the dossier with value, source, anchoring, full-text block, related rows, conflict class, checks, versions, decisions and audit; decisions by `POST /api/review/evidence/{type}/{id}/decisions` with the reviewer credential and the expected fingerprint (a mismatch is 409).

## Full-text pipeline

`backend/incretinos/fulltext/`, configured by `curation/data/fulltext.yaml` (where to look and how a place maps onto the evidence model — never a value).

1. **Licence gate** (`licence.py`). Only CC BY, CC0 and US-government works may be stored in full; NC, ND, SA or unknown licences keep metadata, the document hash, extracted cells and short quotes. An open-access flag alone is not a permission; a disagreement between licence fields applies the stricter reading. The FDA multi-discipline review mixes FDA- and applicant-authored sections, so the PDF is not stored and only tables whose source line reads "Source: FDA analysis" and sentences under "The FDA's Assessment" are read.
2. **Parsers.** `jats.py` reads JATS tables onto a row/column grid (spans, row groups, panels, footnotes, abbreviations) and locates sentences by anchor phrase; `pdf.py` (pypdf) reads positioned glyph runs into captioned tables with headers, merged wrapped cells (each merge counted), continuation across pages, footnotes and authorship. Each configured place is checked against the document (expected caption, unique row, unique column) before anything is extracted.
3. **Mapping** (`mapping.py`). A column heading maps to a registered arm by drug and dose (table-defined abbreviations expanded; documented exposure equivalences named when used); ambiguous or unmatched headings stay unlinked and are flagged.
4. **Rows** (`loader.py`). Every extracted value becomes an evidence row of origin FULLTEXT, linked to the exact cells (or sentence) it was read from, with the parse method and version and a quality assessment; its mechanical status is always PENDING_HUMAN_REVIEW. Anything not fetched, matched or parsed reliably is recorded on the document as a limitation and raised as a review-queue item; no row is produced for it. The step runs after the curated load and before the full reconcile.

## Engines

All deterministic; no model is involved.

- **findings** — builds the context bundle for a result (study, population, arm, dose, endpoint, estimand, baseline, precision, source), its mandatory qualifier labels and the self-qualifying sentence.
- **comparability** — factor assessment (match / minor / major / blocking / unknown) and classification; reported between-arm analysis lookup; documented-equivalence dose harmonization with no arithmetic fallback.
- **interpretation** — what a result or analysis establishes and does not (for a between-arm analysis also its `reported_effect`: scale, orientation, interval level and sidedness, p-value, estimand, analysed population, multiplicity and hypothesis, with a plain reading that never goes beyond those fields); pharmacologic class derived from receptor actions; `guard_statement` for free text.
- **indirect** — anchored indirect comparison (Bucher HC et al., J Clin Epidemiol 1997;50:683-691) of drug A versus drug B through a common comparator, from two stored between-arm analyses in two trials. Twelve requirements are reported as PASS / FAIL / CANNOT_ASSESS with the stored evidence (network, comparator, estimand, outcome definition, effect scale, follow-up, contrast reliability, variance, population, effect modifiers, transitivity, no open source conflict). Any non-PASS on the first eight, or a FAIL on the last four, refuses the calculation; CANNOT_ASSESS on population, effect modifiers, transitivity or source agreement is shown as an unverified assumption beside the result. An input whose multi-source group the conflict engine classifies as a true conflict fails both "reliable contrast estimates" and "no open source conflict" (refused: which value the contrast is remains a reconciliation question); a group classified insufficient information is CANNOT_ASSESS (shown, not refused). The group's class is read, not the input row's own pairs — the conservative reading. SEs come only from a reported two-sided interval at a stated level (log scale for ratios); d_AB = d_AC − d_BC, SE_AB = √(SE_AC² + SE_BC²). Only MACHINE_EXTRACTED registry results or HUMAN_VERIFIED evidence enter a calculation. Not a network meta-analysis; the estimate is labelled everywhere as indirect, never randomized, never a ranking. Tests check it against a weighted least-squares fit of the two-trial network (numpy and statsmodels) and by Monte Carlo coverage.
- **conflicts** — groups rows from different sources that purport to report one quantity (same study; same randomized group, a documented formulation equivalence joining a label's tablet dose to the capsule arm; same endpoint category, concept, threshold and unit; same time point; for analyses the same arm pair and orientation; for adverse events the same term, measure kind and seriousness table), compares every cross-source pair field by field (estimand, baseline, analysis set, missing-data handling, definition, effect measure, observation period, formulation named) and classifies it TRUE_CONFLICT, INSUFFICIENT_INFORMATION, EXPLAINABLE_DIFFERENCE or CONSISTENT. Agreement is judged at the precision the source printed (its own text), never the float. Evidence tier is listed as context and never read by the classification. Gap rule G12 follows it: true conflicts and unreconciled differences become gaps, explained differences and agreements do not.
- **quality** — counts six separate dimensions (review status, completeness, source authority, reliability signals, precision, freshness) from the stored rows on each request, with no combined score. Rejected, held and unreviewed full-text rows are excluded from analytic counts by the session rule and counted only in the review-status dimension. The conflict dimension is one Item per multi-source group from the conflict engine, by molecule and trial, one metric per class, each opening the same set on `/conflicts`. Every metric lists exactly the records it counted.
- **validation** — 16 rules over the live database (V01–V12 are the mandatory scientific cases; V16 re-derives every human-verified or rejected status from the decision ledger).
- **stats** — Wilson and Newcombe intervals, risk ratio, interval from a standard error, sample size and power for five designs, attrition and delta-adjustment scenarios, seeded simulation. Formulas and references are in the docstrings; tests compare against statsmodels, scipy and published worked values.
- **safety** — trial adverse-event tables by measure kind; FAERS overview; disproportionality; guards that raise on incidence or causality from spontaneous reports and on mixing measure kinds or data families.
- **benefit_risk**, **gaps**, **design** — arm profiles, preference-weighted summaries and scenarios; gap rules with typed reasons and transparent priority; eligibility comparison, coverage matrix and amendment history.

## Assistant

`ask()` guards the question (recommendation and spontaneous-report causality requests are refused), routes it to one of six deterministic composers or to BM25 retrieval over evidence cards, and returns typed blocks: reported evidence, rule-based interpretation, unavailable, hypothesis, refusal. Every evidence block names the stored verification status of each record behind it. With a provider configured, the composed blocks are passed as delimited cards to the model, which may only draft a short summary.

**Claim verifier** (`assistant/claims.py`, `assistant/verifier.py`). The draft is split into sentences; each factual sentence is read into structured claims (drug, dose, arm, comparator, trial, endpoint, number with its role, direction, interval, estimand, time point, hypothesis type, tier, verification wording) and each claim is bound to a cited card. A sentence is SUPPORTED only when every claim binds to the cited evidence; anything contradicted or unverifiable withholds the whole draft (fail closed), and the deterministic answer is shown instead. Non-inferiority is never read as superiority, an internally inconsistent analysis grounds no significance or direction claim, and cross-trial values ground no ranking. The verifier is measured by `assistant/benchmark.py` on four adversarial sets (`backend/benchmarks/claims/`): `dev` and `dev-review` (development), `heldout` (heldout-v1: blind at first measurement, development since) and `heldout-v2` (blind). Provider failures and refusals never affect the deterministic answer. The provider interface is three lines (`LLMProvider`); `AnthropicProvider` is the bundled implementation and defaults to `claude-opus-5-5`; no live call has been made.

## Security

| Concern | Control |
|---|---|
| External input | Pydantic models with bounds and patterns on every endpoint, strict types where a coercion would change meaning; NCT identifiers validated before use; SQLAlchemy parameter binding throughout; a validation error names where the request is wrong, never what was sent |
| Request size and cost | `RequestBodyLimitMiddleware` refuses a body larger than its route can use (413) before parsing, and a JSON body with a lone surrogate; per-client budgets for the simulation, the assistant and the expensive reads (`QUERY_BUDGET_ROUTES`), enforced before routing; simulation size capped before allocation. Every POST route is listed in exactly one body table, and every budgeted route exists (tests enforce both) |
| Client identity for budgets | The socket peer; `X-Forwarded-For` is read only from declared trusted proxies, and every launcher starts uvicorn with proxy headers off |
| Least privilege | Read-only by default: without a reviewer credential no HTTP request changes the database (a sweep of every route checks that no data-changing SQL is sent), and anonymous activity goes to the application log, not the audit trail. Mutations require a reviewer credential compared on bytes in constant time; containers run as unprivileged users |
| Reviewer identity | Personal tokens (`INCRETINOS_REVIEWERS_FILE`: name → SHA-256 of the token): the recorded name is the server's. The shared development token labels its decisions self-declared. Reserved and implausible names are refused |
| Secrets | Read from the environment only; none in the repository; optional keys are never required; the web image ships no `.env` |
| Prompt injection from retrieved text | Cards are sanitized (markup and control characters removed, angle brackets neutralized), wrapped in delimiters, and the system prompt declares card text to be data; instruction-like cards are kept from the model; the output is checked by the claim verifier regardless |
| Document rendering | Source text is rendered as text; no HTML from sources is injected; openFDA table HTML fields are not used |
| XML and PDF parsing | `defusedxml` for PubMed and JATS (entity-expansion attacks are refused; tested); pypdf for the FDA review excerpt, read from committed fixtures offline |
| Copyright and licences | Full text stored only under CC BY, CC0 or US-government authorship; NC/ND/unknown licences and mixed-authorship documents keep hashes, cells and short quotes; an NC-ND full text is never written to the fixtures (tested) |
| Data separation | Every source has a classification; `record_snapshot` refuses anything other than PUBLIC |
| Browser | Same-origin API proxy; a production Content-Security-Policy with no `unsafe-eval` and no websocket source; `nosniff`, frame-deny and referrer-policy headers |
| Dependencies | `backend/constraints.txt` pins exact versions (no hashes) for CI and the image; `npm audit --omit=dev` reports 0 vulnerabilities on Next.js 16.4.6 |

## Deployment

`docker compose up --build` starts PostgreSQL, the API (applies migrations, seeds from fixtures if empty, serves) and the web app. For development the default is a SQLite file and `next dev`. Scheduled live re-ingestion (`INCRETINOS_REFRESH_HOURS`) runs in-process with APScheduler and is off by default.

## If restricted sponsor data were ever integrated

This build must not be used for it. The minimum additional requirements:

- **Validation.** Documented user requirements, risk assessment and installation/operational/performance qualification for every component that touches regulated records (computerized-system validation under GxP).
- **21 CFR Part 11 controls**, where records or signatures are in scope: unique user identities and authentication; role-based authorization; secure, computer-generated, time-stamped, immutable audit trails that capture the previous value; electronic-signature manifestation and linking; record retention and retrieval; system and clock controls.
- **Access and segregation.** Tenant and study-level isolation; restricted data in a separate database and network segment; public demo data physically separate; least-privilege service accounts; encryption in transit and at rest; key management.
- **Privacy.** A lawful basis and data-use agreements for any participant-level data; de-identification assessment; no protected health information in logs, prompts or third-party model calls.
- **Model use.** No restricted data sent to an external model without a contractual and technical basis; evaluation and monitoring of any model-assisted output; human review before use.
- **Change control and traceability.** Versioned releases, controlled migrations, and traceability from requirement to test.

No regulatory compliance is claimed for the current software; none has been assessed.
