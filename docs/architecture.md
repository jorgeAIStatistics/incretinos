# Architecture

A modular monolith: one Python service for ingestion, statistics and the API; one PostgreSQL database; one Next.js web application. No graph database, message broker, orchestration platform or microservices — the conceptual evidence graph is ordinary relational tables, and nothing in the workload justifies more.

```
 Public sources                        Backend (Python · FastAPI)                         Web (Next.js)
┌──────────────────┐   ┌───────────────────────────────────────────────────────┐   ┌──────────────────────┐
│ ClinicalTrials   │   │ ingestion/   transport → adapters → snapshots → diff  │   │ 11 research pages    │
│ PubMed           │──▶│ curation/    schema → loader (quote anchoring)        │   │ charts with table    │
│ openFDA label    │   │ engines/     estimands · findings · comparability ·   │◀──│ views and provenance │
│ openFDA FAERS    │   │              interpretation · validation · stats ·    │   │ /api proxied to the  │
│ sponsor releases │   │              safety · benefit_risk · gaps · design    │   │ backend              │
└──────────────────┘   │ assistant/   retrieval → deterministic answer         │   └──────────────────────┘
        ▲              │              (→ optional model summary → citation     │
        │ fixtures     │               check)                                  │
┌──────────────────┐   │ reports/     cited Markdown                           │
│ backend/fixtures │   │ api/         typed REST endpoints, reviewer boundary  │
└──────────────────┘   └───────────────────────────┬───────────────────────────┘
                                                   ▼
                                    PostgreSQL (SQLite for offline/dev/tests)
```

## Evidence model

Thirty-three tables (`backend/incretinos/models.py`), migrated with Alembic.

| Group | Entities |
|---|---|
| Provenance | `EvidenceSource`, `SourceSnapshot`, `ChangeEvent`, `ReviewQueueItem`, `ReviewDecision`, `AuditEvent`, `IngestionRun` |
| Drugs | `Drug`, `DrugMechanism`, `Indication`, `Formulation`, `Dose`, `ExposureEquivalence` |
| Studies | `Study`, `StudyDrug`, `StudyVersion`, `StudyArm`, `Population`, `InclusionCriterion`, `ExclusionCriterion` |
| Evidence | `Endpoint`, `Estimand`, `IntercurrentEventStrategy`, `OutcomeResult`, `StatisticalAnalysis`, `AdverseEvent` |
| Literature | `Publication`, `StudyPublication` |
| Claims and gaps | `EvidenceClaim`, `EvidenceGap` |
| Safety and index | `LabelRecord`, `SpontaneousReportSeries`, `DiscoveredTrial` |

Conventions that hold everywhere:

- **Missing is NULL.** A value the source did not report is `NULL` and named in `missing_fields`. There is no code path that writes zero for an absent number, and a derived interval is returned separately, flagged as inference.
- **Every evidence row has a source, a tier and a verification status.** Controlled vocabularies live in `enums.py`.
- **Origins are separate.** Registry-derived rows are rebuilt on each ingestion; curated rows are loaded from files; neither overwrites the other, so one arm can carry a registry value, a publication value and a label value side by side.
- **Free text from sources is unbounded.** Only keys, enums and dates have length limits (a test enforces that stored values fit, because PostgreSQL rejects what SQLite tolerates).

## Ingestion

`Transport` abstracts access. `LiveTransport` adds per-service rate limiting, retry with exponential backoff and jitter, `Retry-After` handling, and optional fixture recording. `FixtureTransport` serves the recorded responses and never opens a socket. Adapters address resources by a stable fixture key, so live ingestion, fixture recording and the offline demo are the same code path.

For each source: fetch → canonicalize → hash → `record_snapshot`. A new snapshot is written only when the hash changes; a change produces a structured diff (`ctgov_diff.py`: status, enrollment, dates, arms, outcomes, eligibility, linked PMIDs, posted results), a `ChangeEvent` and a review-queue item. The registry's daily data-version stamp is excluded from the hash so it does not cause false changes.

The registry adapter extracts protocol fields and, when results are posted, arm-level outcome values, between-arm analyses, adverse events and participant flow exactly as posted, with a JSON path as the locator. The estimand is classified from the analysis-population text and marked as inferred.

A failure in one source is recorded (`IngestionRun`, review queue, audit log) and the run continues; a failed study yields no rows rather than partial ones.

## Curation and verification

Curated files (`curation/data`) hold what the APIs cannot supply: publication-, label- and release-reported values, mechanisms, regulatory status, formulations. The schema (`curation/schema.py`) rejects a number whose digits do not appear in its own locator quote, an interval with one limit, and an empty result row. The loader resolves each cited source, refuses an "approved" status that does not cite FDA labeling or review, and assigns verification by matching the quote against stored source text (registry, label) or against excerpts verified at fixture-recording time (abstracts, web pages).

## Engines

All deterministic; no model is involved.

- **findings** — builds the context bundle for a result (study, population, arm, dose, endpoint, estimand, baseline, precision, source), its mandatory qualifier labels and the self-qualifying sentence.
- **comparability** — factor assessment (match / minor / major / blocking / unknown) and classification; reported between-arm analysis lookup; documented-equivalence dose harmonization with no arithmetic fallback.
- **interpretation** — what a result or analysis establishes and does not (for a between-arm analysis also its `reported_effect`: scale, orientation, interval level and sidedness, p-value, estimand, analysed population, multiplicity and hypothesis, with a plain reading that never goes beyond those fields); pharmacologic class derived from receptor actions; `guard_statement` for free text.
- **indirect** — anchored indirect comparison (Bucher HC et al., J Clin Epidemiol 1997;50:683-691) of drug A versus drug B through a common comparator, from two stored between-arm analyses in two trials. Eleven requirements are reported as PASS / FAIL / CANNOT_ASSESS with the stored evidence (network, comparator, estimand, outcome definition, effect scale, follow-up, contrast reliability, variance, population, effect modifiers, transitivity). Any non-PASS on the first eight, or a FAIL on the last three, refuses the calculation; CANNOT_ASSESS on population, effect modifiers or transitivity is shown as an unverified assumption. SEs come only from a reported two-sided interval at a stated level (log scale for ratios); d_AB = d_AC − d_BC, SE_AB = √(SE_AC² + SE_BC²). Only MACHINE_EXTRACTED registry results or HUMAN_VERIFIED evidence enter a calculation. Not a network meta-analysis; the estimate is labelled everywhere as indirect, never randomized, never a ranking. Tests check it against a weighted least-squares fit of the two-trial network (numpy and statsmodels) and by Monte Carlo coverage.
- **validation** — 15 rules over the live database (V01–V12 are the mandatory scientific cases).
- **stats** — Wilson and Newcombe intervals, risk ratio, interval from a standard error, sample size and power for five designs, attrition and delta-adjustment scenarios, seeded simulation. Formulas and references are in the docstrings; tests compare against statsmodels, scipy and published worked values.
- **safety** — trial adverse-event tables by measure kind; FAERS overview; disproportionality; guards that raise on incidence or causality from spontaneous reports and on mixing measure kinds or data families.
- **benefit_risk**, **gaps**, **design** — arm profiles, preference-weighted summaries and scenarios; gap rules with typed reasons and transparent priority; eligibility comparison, coverage matrix and amendment history.

## Assistant

`ask()` guards the question (recommendation and spontaneous-report causality requests are refused), routes it to one of six deterministic composers or to BM25 retrieval over evidence cards, and returns typed blocks: reported evidence, rule-based interpretation, unavailable, hypothesis, refusal. With a provider configured, the composed blocks are passed as delimited cards to the model, which may only draft a short summary; `check_citation_fidelity` discards the draft unless every factual sentence cites a known card, every number appears in a cited card, and the language guard finds no superiority, causal or recommendation claim. Provider failures and refusals never affect the deterministic answer. The provider interface is three lines (`LLMProvider`); `AnthropicProvider` is the bundled implementation and defaults to `claude-opus-5-5`.

## Security

| Concern | Control |
|---|---|
| External input | Pydantic models with bounds and patterns on every endpoint; NCT identifiers validated before use; SQLAlchemy parameter binding throughout |
| Secrets | Read from the environment only; none in the repository; optional keys are never required |
| Prompt injection from retrieved text | Cards are sanitized (markup and control characters removed, angle brackets neutralized), wrapped in delimiters, and the system prompt declares card text to be data; the output is checked mechanically regardless |
| Document rendering | Source text is rendered as text; no HTML from sources is injected; openFDA table HTML fields are not used |
| XML parsing | `defusedxml` for PubMed responses (entity-expansion attacks are refused; tested) |
| Least privilege | Read-only API by default; mutations require a reviewer token compared in constant time; containers run as unprivileged users |
| Data separation | Every source has a classification; `record_snapshot` refuses anything other than PUBLIC |
| Browser | Same-origin API proxy; `nosniff`, frame-deny and referrer-policy headers |

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
