# Engineering audit — start of Session 2

Audited at commit `576030d` on 2026-10-13 by nine independent read-only reviews (one per subsystem), each required to cite code lines or query output and to try to refute a suspected weakness before confirming it. Findings carry the reviewers' ids (for example `RL-01`). Sections 1–7 are the audit as written at `576030d`; the last section gives the **status after Session 2** of every finding id, at the merge of the last Session 2 wave (branch `s2/integration3`), each with the test or recorded check that shows it. See [build-roadmap.md](build-roadmap.md) for what was verified afterwards.

## Reported facts, checked

| Claim | Result |
|---|---|
| 33 database tables | True: models, migration and seeded database all have 33 |
| 45 trials seeded in depth | True (plus 245 in the registry index) |
| 159 backend tests | True: 159 collected, all passing |
| 11 browser tests, 15 validation rules, 11 research pages | True (12 page files: the 11 pages plus trial detail) |
| Hosted CI | The run on `576030d` succeeded. Earlier documents that call CI "written, not run" were out of date |

## 1. Implemented and working

- Ingestion with hashed, append-only snapshots; registry numbers are stored exactly as posted (all 1,638 registry values equal their posted text; "NA" is NULL).
- Statistical formulas (Wilson, Newcombe, Katz, sample size, non-inferiority, Schoenfeld) reproduce statsmodels, scipy or hand values.
- No cross-trial pair is ever classed as a direct randomized comparison (0 of 26,612 pairs), and a descriptive difference never gets an interval.
- Estimand and baseline differences are not reported as source conflicts; the documented capsule/tablet equivalence unifies Foundayo label doses correctly.
- The deterministic assistant is independent of any language model; refusals work.
- The reviewer-token boundary on the two intended mutations works; no SQL injection, SSRF, path traversal or committed secret was found.
- Charts start at zero, have table equivalents, and pair icon with text for status.

## 2. Partially implemented

- **Review.** Decisions are recorded and audited but act on nothing (see §3).
- **Change detection.** Detects protocol-field changes; does not see result values (`ING-05`). The seeded example is synthetic.
- **Conflict detection.** One rule, arm-level results in two endpoint categories only; no classes of conflict; analyses, adverse events and responder endpoints are never compared (`XS-03`).
- **Language-model summary.** Off by default, never exercised with a live key, and its check is weak (see §3).
- **Responsive layout.** No mobile navigation; 7 of 12 routes overflow at 375 px (`WEB-13`).

## 3. Scientific correctness risks

| Id | Finding | Severity | Status after Session 2 |
|---|---|---|---|
| `RL-01` | A review decision never changes the evidence record. Approve and reject are both no-ops; no code can set "human-verified" or "rejected" | Critical | Fixed |
| `CUR-01` | "Quote-anchored" proves that a number and a quote exist in the source, not that the number belongs to the stated arm, endpoint, estimand or population. Ten deliberately mis-attributed rows all loaded as quote-anchored | Critical | Partly fixed (flagged, not resolved) |
| `CUR-04` / `ING-01` | A key collision in the notes converter stored 68 of 259 curated results and 6 analyses under another endpoint's title (≥10/15/20% responders shown as "≥5%"; fat mass as lean mass). 24 label rows are contradicted by their own quote | Critical | Fixed |
| `B2` | The model-draft citation check accepted 30 of 32 deliberately wrong drafts (reversed direction, swapped drug, arm or trial, wrong unit, non-inferiority as superiority) and rejected faithful prose on formatting | High | Fixed |
| `C1`–`C3` | Inside one trial, "direct randomized comparison" is returned for contrasts that were not randomized against each other; endpoints are matched by category only; the quoted between-arm analysis can belong to a different estimand or threshold | High | Fixed (C1, C2, C3) |
| `ING-02`, `ING-03` | Registry contrasts ignore the stated direction (BELIEVE stored reversed); two distinct arms merged; a placebo arm attributed to the drug; mg/kg stored as mg | High | Fixed (ING-02, ING-03) |
| `CUR-05` | 15 adverse-event rows carry a measure kind their own quote contradicts | High | Fixed |
| `XS-06`, `XS-07`, `XS-13` | Gap rules state that ACHIEVE-2 and ATTAIN-MAINTAIN have no public results although results are stored, and can never clear a missing-superiority gap | High | Open (XS-06, XS-07, XS-13) |
| `WEB-01`–`WEB-05` | The trial hero chart mixes estimands, tiers and sources under one caption; units are missing beside 39% of results; a kg value can be labelled "%"; active semaglutide arms are coloured "Placebo" | High | Fixed (WEB-01 to WEB-05) |
| `A1` | The assistant's internal block kind is `VERIFIED_EVIDENCE` although no evidence is human-verified (the visible label is "Reported evidence") | Medium | Fixed |

## 4. Data reliability and provenance risks

- `RL-02`: all 202 curated review items point at one of 27 sources; none of the 192 pending results, analyses, adverse events or claims is referenced.
- `RL-03`: every ingest deletes and re-inserts evidence rows, so primary keys change; `seed --reset` drops all tables including decisions and the audit log.
- `RL-05`: a resolved review item suppresses re-review of that position forever.
- `ING-04`: an evidence row records its source but not the snapshot version it came from.
- `ING-06`: curated rows are not rechecked when their source changes.
- `CUR-03`: 143 curated rows (83 quote-anchored) rest on quotes listing several candidate values; only 13 of 297 anchored rows pass every attribution check prototyped.
- Open data: none of the seven primary publications of the priority programs is open access. The registry adapter drops most posted serious adverse-event terms (125 of 146 for ATTAIN-1).

## 5. UX and workflow limitations

- The review screen shows 120 of 203 items, without the value under review or its source (`RL-09`).
- A pending or failed request can render a confident empty state, for example "No FDA labeling is on record" (`WEB-06`).
- Long lists are silently truncated (`WEB-07`, `F08`); every route shares one document title; several text tokens miss WCAG AA contrast (`WEB-14`, `WEB-15`).

## 6. Security and maintainability

- `F01`: one unauthenticated request to the simulation endpoint can allocate 13–19 GiB.
- `F03`: "read-only" mode still writes audit rows from three unauthenticated routes.
- `F06`: one shared reviewer token with a self-declared name; no decision state machine.
- `F13`, `F14`: no lockfile for backend dependencies; CI has no PostgreSQL job and no migration-drift check.
- `WEB-17`: no shared API types (121 explicit `any`); the lint script cannot run.
- `F19`: `api/main.py` is one 989-line module.

## 7. Highest-value improvements, in order

1. An evidence-level review lifecycle: stable evidence identity and fingerprint, decisions bound to the exact record and version, statuses that survive rebuilds, and invalidation when the record or its source changes.
2. Repair the mis-filed curated data and the converter and adapter defects that caused it.
3. Replace digit matching with structured claim verification, measured on an adversarial benchmark.
4. Explained attribution checks on every record, shown to the reviewer.
5. Fix the comparison engine's within-trial errors before adding any indirect comparison. Exactly one cross-drug pair currently has what an anchored comparison needs (SURMOUNT-1 and ATTAIN-1: placebo-controlled, week 72, no diabetes, 95% intervals on both contrasts).
6. Conflict classification (true conflict, explainable difference, insufficient information) and an evidence-quality view.
7. Full-text extraction where the licence allows: the FDA multi-discipline review of NDA 220934 and CC BY secondary analyses.

## Status after Session 2

Status at the merge of the last Session 2 wave. *Fixed*: the defect cannot recur without a named test failing. *Partly fixed*: the risk is now detected and shown, or reduced, but not removed. *Open*: unchanged. Test names are in `backend/tests/` unless they are browser specs (`frontend/e2e/`).

| Id | Status after Session 2 | What is true now | Verified by |
|---|---|---|---|
| `RL-01` | Fixed | Approve sets HUMAN_VERIFIED on that one record and version only; reject removes the row from every analytic surface (one choke point in `db.py`). | `test_review_lifecycle.py::test_approving_one_row_verifies_only_that_row`, `::test_a_rejected_result_leaves_every_analytic_surface_and_stays_visible_for_inspection` |
| `CUR-01` | Partly fixed | All ten audit mis-attributions are flagged by the automated checks, but a flag changes no status: 594 rows carry a quote-based flag and 206 a HIGH-risk flag on the fresh seed. Only a reviewer can resolve them. | `test_review_checks.py::test_audit_misattributions_are_flagged`; count on the seeded database |
| `CUR-04` / `ING-01` | Fixed | The converter refuses a key collision; responder rows sit under their own threshold, label responder values too. | `test_data_integrity.py::test_converter_raises_on_a_key_collision`, `::test_no_curated_responder_row_names_another_threshold_than_its_endpoint`, `::test_fda_label_responder_values_sit_under_their_own_threshold` |
| `B2` | Fixed | Every wrong draft from the audit is withheld; faithful drafts are kept. Measured since on four benchmark sets (heldout-v2, blind: detection 99.3%, false rejection 19.2%). | `test_claim_verification.py::test_deliberately_wrong_drafts_are_withheld`, `::test_faithful_drafts_are_kept`; `incretinos benchmark-claims` |
| `C1` | Fixed | "Direct randomized comparison" only for distinct arms of one randomization measuring one quantity. | `test_scientific_validation.py::test_c1_sweep_direct_only_for_distinct_arms_of_one_randomization_and_one_quantity` |
| `C2` | Fixed | Endpoints are matched on measurement, threshold and unit, not on category. | `test_scientific_validation.py::test_c2_unrelated_endpoints_of_one_category_are_never_differenced` |
| `C3` | Fixed | A between-arm analysis is quoted only when estimand, baseline, threshold and arm pair match. | `test_scientific_validation.py::test_c3_no_analysis_is_quoted_when_none_matches_the_estimand`, `::test_c3_analysis_matches_baseline_and_responder_threshold` |
| `ING-02` | Fixed | Registry contrasts follow the stated direction (BELIEVE); every analysis records how its direction is known. | `test_data_integrity.py::test_believe_contrasts_follow_the_registrys_stated_direction`, `::test_every_registry_analysis_records_how_its_direction_is_known` |
| `ING-03` | Fixed | Distinct arms stay distinct, no placebo arm is attributed to a drug, mg/kg is kept. | `test_data_integrity.py::test_nct06643728_arms_are_distinct`, `::test_no_placebo_arm_is_attributed_to_a_drug`, `::test_weight_based_doses_keep_their_unit` |
| `CUR-05` | Fixed | Each curated adverse-event row's measure kind agrees with its term and quote. | `test_data_integrity.py::test_curated_adverse_event_rows_carry_the_kind_their_term_describes`; `test_review_checks.py::test_adverse_event_kind_contradicted_by_term_and_quote` |
| `XS-06` | Fixed | A completed study with a linked publication but no results in analytic use (ACHIEVE-2, ATTAIN-MAINTAIN) is now a *not yet extracted* gap naming the PMID, never "no public results". | `test_gap_statements.py::test_a_completed_study_with_a_linked_publication_is_not_called_unreported`, `::test_withdrawal_gap_names_the_linked_publication` |
| `XS-07` | Fixed | A superiority conclusion is recorded only where the cited quote states it in words (the REWIND label: "significantly reduced the risk"), never from an interval or p-value; the dulaglutide cardiovascular gap then clears. SURPASS-CVOT and ACHIEVE-4 keep the gap: their quotes state non-inferiority, not superiority. | `test_gap_statements.py::test_superiority_is_recorded_only_where_the_quote_states_it_and_then_clears_the_cv_gap` |
| `XS-13` | Fixed | Long-term evidence counts only assessments with a concurrent control arm (TRIUMPH-1's uncontrolled week-104 extension is named, not counted); the comparator gap describes dose-comparison trials instead of "only placebo-controlled". | `test_gap_statements.py::test_long_term_evidence_counts_only_controlled_assessments`, `::test_comparator_gap_describes_dose_comparison_trials_truthfully` |
| `WEB-01` | Fixed | The trial headline chart plots one coherent result set and its caption names it. | `presentation.spec.ts` "WEB-01: no trial page mixes estimands, tiers, sources or time points in its headline chart" |
| `WEB-02` | Fixed | Every reported value carries its unit. | `presentation.spec.ts` "WEB-02: …" |
| `WEB-03` | Fixed | Values in different units never share an axis; a kg value never gets "%". | `presentation.spec.ts` "WEB-03: …" |
| `WEB-04` | Fixed | Chart labels are complete at desktop and phone widths (the audit text does not describe WEB-04 separately; this is the closest test). | `presentation.spec.ts` "WEB-04: …" |
| `WEB-05` | Fixed | Arm roles are stored and read from data; semaglutide arms are active comparators, never "Placebo". | `presentation.spec.ts` "WEB-05: …"; `test_safety_presentation.py::test_semaglutide_arms_named_placebo_plus_are_active_comparators` |
| `A1` | Fixed | The assistant's block kind is REPORTED_EVIDENCE; VERIFIED_EVIDENCE no longer exists in the code. | `test_integration_wave1.py::test_the_assistant_labels_evidence_with_the_real_statuses_and_never_as_human_verified` |
| `RL-02` | Fixed | Every evidence row is reviewable in /review; source-level curated queue items are refused. | `test_review_lifecycle.py::test_queue_items_follow_a_state_machine_and_legacy_curated_items_are_refused`, `::test_review_list_filters_sorts_paginates_and_counts` |
| `RL-03` | Fixed (row ids still change) | Decisions bind to a content-derived evidence key and fingerprint, so they survive a reseed; `seed --reset` keeps decisions, versions and audit; wiping them needs its own flag. Registry rows are still deleted and re-inserted, so primary keys change. | `test_review_lifecycle.py::test_evidence_keys_do_not_depend_on_primary_keys_values_or_positions`, `::test_statuses_and_history_survive_a_reseed_a_reset_and_reopening_the_database`, `::test_reset_keeps_review_history_and_wiping_it_needs_its_own_flag` |
| `RL-05` | Fixed | A resolved queue item no longer suppresses a later occurrence. | `test_review_lifecycle.py::test_a_resolved_queue_item_does_not_suppress_a_later_occurrence` |
| `ING-04` | Fixed | Every evidence row records the snapshot version it came from. | `test_review_lifecycle.py::test_a_decision_is_attached_to_the_exact_evidence_version_and_audited` |
| `ING-05` | Fixed | A revised registry value is a HIGH change event with before and after values, and forces a recheck of an approval. | `test_review_lifecycle.py::test_a_revised_registry_value_forces_a_recheck_and_leaves_unaffected_approvals_verified` |
| `ING-06` | Fixed | A changed curated value, or a quote that left its source, forces a recheck (shown in demonstration A). | `test_review_lifecycle.py::test_a_changed_curated_value_forces_a_recheck`, `::test_a_quote_that_left_the_source_forces_a_recheck` |
| `CUR-03` | Partly fixed | Quotes listing several candidate values are flagged (QUOTE_MULTIPLE_CANDIDATES, 171 flags on the fresh seed), not resolved. | `test_review_checks.py::test_a_list_of_values_next_to_one_arm_name_is_unresolved_but_the_named_arm_is_not` |
| `XS-03` | Fixed | The conflict engine classifies results, between-arm analyses and adverse events as true conflict, insufficient information, explainable difference or consistent; claims are not compared. | `test_conflicts.py::test_between_arm_analyses_are_grouped_by_pair_and_orientation`, `::test_adverse_event_counts_and_percentages_are_compared_as_reported` and the ATTAIN-1 cases |
| `RL-09` | Fixed | The /review workbench pages through every row and shows value, source, quote, checks and history. | `test_review_lifecycle.py::test_the_dossier_gives_a_reviewer_everything_needed_to_decide`; `review.spec.ts` |
| `WEB-06` | Fixed | A failed request shows an error with a retry, never a confident empty state. | `foundation.spec.ts` "a failed request shows an error with a retry and never an empty state" |
| `WEB-07` / `F08` | Fixed | Lists state their true total and every row can be reached; list endpoints page through everything with bounded limits. | `foundation.spec.ts` "lists state how many rows exist and every row can be reached"; `test_hardening.py::test_list_endpoint_reports_the_true_total_and_pages_through_everything` |
| `WEB-13` | Fixed | No route scrolls sideways at 320, 390 or 834 px; phone navigation exists. 375 px itself is not a tested width. | `foundation.spec.ts` "no route scrolls sideways at … width, in any tab" |
| `WEB-14`, `WEB-15` | Fixed, no automated test | Text and status tokens were changed to meet WCAG AA contrast (see design-system.md); recomputed by hand, not tested. | Manual computation only |
| `F01` | Fixed | Simulation size is capped before any allocation; the route is rate-limited. | `test_statistics.py::test_f01_simulation_size_is_capped_before_any_memory_is_allocated` |
| `F03` | Fixed | A read-only deployment sends no data-changing SQL from any route. | `test_hardening.py::test_read_only_deployment_writes_nothing` |
| `F06` | Partly fixed | Personal reviewer tokens (`INCRETINOS_REVIEWERS_FILE`) bind the name to the token; a decision state machine exists. The shared development token is still accepted, labelled self-declared, and docker-compose passes only the shared token. | `test_review_lifecycle_fixes.py::test_f8_with_personal_tokens_the_reviewer_name_comes_from_the_token`, `::test_f8_shared_token_decisions_are_labelled_self_declared` |
| `F13` | Fixed (pins, no hashes) | `backend/constraints.txt` pins exact versions; CI and the image install with it. No hash pinning. | `test_hardening.py::test_constraints_pin_final_releases_and_runtime_needs_no_test_only_stack` |
| `F14` | Fixed | CI has a PostgreSQL job (migrate, `alembic check`, seed, validate, full test suite). | `test_hardening.py::test_models_and_migrations_describe_the_same_schema`; `.github/workflows/ci.yml` |
| `F19` | Open | `api/main.py` grew to 1,552 lines; only the Session 2 features went into routers (review, conflicts, quality, indirect). | `wc -l` |
| `WEB-17` | Partly fixed | Lint runs in CI and shared API types exist, but explicit `any` was not reduced (lint warnings, not errors). | `npm run lint` (0 errors) |
| Open-access primary publications | Partly fixed | Still closed; a licence-gated full-text step reads the FDA review and two CC BY secondary analyses instead. | `test_fulltext.py::test_nc_nd_full_text_is_never_stored_but_its_cells_are` |
| Serious adverse-event terms dropped (ATTAIN-1) | Fixed | All 146 posted serious terms of ATTAIN-1 are stored. | `test_data_integrity.py::test_every_posted_serious_adverse_event_term_is_stored` |
