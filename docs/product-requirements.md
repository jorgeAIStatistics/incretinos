# Product requirements

## Purpose

IncretinOS is a research tool for reading clinical evidence precisely. It answers, for a therapeutic programme: what do public sources report, under which estimand and in which population; can two results be compared; and what remains unknown. The first domain is Eli Lilly's incretin and amylin portfolio.

It is **not** a patient treatment app, a prescribing or dosing system, an FDA submission system, a drug-discovery model, or a general trial-management platform. Established products (Medidata, Veeva, Citeline) cover trial operations and broad pipeline intelligence; IncretinOS is differentiated by depth of scientific interpretation: estimands, comparability, provenance and gap analysis.

## Users

| User | Needs |
|---|---|
| Clinical biostatistician | The exact estimand, analysis population, interval and multiplicity status behind a number; validated calculations; no silent imputation |
| Clinical scientist / trial designer | Side-by-side protocols, eligibility and endpoints; what current trials will and will not answer |
| R&D strategist | A defensible reading of cross-trial evidence and a prioritised list of open questions |
| Safety scientist | Trial incidence and spontaneous reports kept apart, with label changes tracked |
| Regulatory scientist | Tier of every claim (label, review, publication, registry, press release) and an audit trail |

## Scope constraints

- Public data only: ClinicalTrials.gov, PubMed, FDA labeling and FAERS via openFDA, sponsor disclosures. No proprietary Lilly systems, clinical study reports, patient-level data or commercial databases.
- No protected health information is ingested or stored.
- The product must be fully functional offline, with no API keys, and with the language-model assistant disabled.

## Functional requirements

| # | Requirement | Where implemented |
|---|---|---|
| F1 | Ingest registry records, publications, labels and spontaneous-report counts through structured APIs with retries, rate limiting, hashing, versioning, duplicate and change detection, and offline fixtures | `backend/incretinos/ingestion` |
| F2 | Reconcile NCT, PMID and DOI identifiers and record the basis for every study–publication link | `ingestion/pubmed.py` (`reconcile`) |
| F3 | Store results with arm, sample size, analysis population, follow-up, interval, p-value, endpoint role, multiplicity status, source location, verification status and missing fields; never zero-fill | `models.py`, `ingestion/ctgov.py`, `curation/` |
| F4 | Classify comparability of any two results into five classes with specific reasons | `engines/comparability.py` |
| F5 | For every result state what was observed, in whom, on which endpoint and analysis, what it does and does not establish, what is missing, and its source | `engines/interpretation.py` |
| F6 | Enforce the twelve mandatory scientific validation cases, with automated tests | `engines/validation.py`, `tests/test_scientific_validation.py` |
| F7 | Arm-level efficacy and tolerability views, risk differences and ratios from valid counts, preference-weighted summaries and hypothetical dose scenarios — each labelled for what it is | `engines/benefit_risk.py`, `engines/stats.py` |
| F8 | Detect and track evidence gaps with transparent, configurable prioritisation; distinguish reporting gaps from absence of data | `engines/gaps.py` |
| F9 | Trial-design workbench: eligibility comparison, coverage matrix, complexity indicators, sample size/power, sensitivity scenarios, seeded synthetic simulation, amendment tracking | `engines/design.py`, `engines/stats.py` |
| F10 | Safety dashboard separating trial adverse events, label warnings and spontaneous reports; disproportionality only with caveats | `engines/safety.py` |
| F11 | Grounded assistant: retrieve first, cite everything, name what is unavailable, mark hypotheses, refuse recommendations and unsupported causal claims; optional pluggable model with a citation check | `assistant/` |
| F12 | Eleven research pages with charts that link to study, endpoint and source | `frontend/src/app` |
| F13 | Source and audit explorer, review queue with a permission boundary, cited report export | `api/main.py`, `reports/export.py` |

## Non-functional requirements

- **Epistemic separation.** Documented evidence, inference, hypothesis, simulation and unsupported claims are distinct types in data and distinct treatments in the interface.
- **Reproducibility.** The same fixtures always produce the same database; simulations record their seed.
- **Security.** Typed validation of all inputs; secrets only from the environment; retrieved text is treated as untrusted and cannot inject instructions into the model prompt; no HTML from sources is rendered; mutations require a reviewer token; public and restricted data classifications are separated and restricted data is refused.
- **Accessibility.** Keyboard focus states, a table view for every chart, status never encoded by colour alone, reduced-motion support.
- **Maintainability.** A modular monolith on PostgreSQL relations; no graph database, orchestration platform or microservices.

## Acceptance demonstrations

Each is implemented, tested and reproduced with real output in [examples/acceptance-demonstrations.md](examples/acceptance-demonstrations.md).

| | Demonstration | Automated coverage |
|---|---|---|
| A | Retatrutide vs tirzepatide without inventing head-to-head results | `test_retatrutide_vs_tirzepatide_is_not_head_to_head`, browser test "vertical slice" |
| B | Foundayo tablet/capsule bioequivalence; efficacy vs treatment-regimen estimands | `test_case03_*`, `test_case04_*`, browser tests "molecule explorer", "estimand inspector" |
| C | EloraTZP efficacy and adverse-event discontinuation, correctly qualified | `test_case07_*`, browser test "benefit–risk workbench" |
| D | ACHIEVE-4 non-inferiority, hazard ratios, intervals, multiplicity-uncontrolled findings | `test_case05_*`, `test_case06_*` |
| E | Eloralintide Phase 3 trials and the questions they address | `test_assistant_lists_all_registered_enlighten_trials` |
| F | Why spontaneous reports cannot establish incidence | `test_case09_*`, browser test "safety intelligence" |
| G | Detect an updated registry record and produce a reviewable change summary | `test_updated_record_produces_reviewable_change_summary`, browser test "source and audit explorer" |
