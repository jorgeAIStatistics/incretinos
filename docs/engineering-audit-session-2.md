# Engineering audit — start of Session 2

Audited at commit `576030d` on 2026-10-13 by nine independent read-only reviews (one per subsystem), each required to cite code lines or query output and to try to refute a suspected weakness before confirming it. Findings carry the reviewers' ids (for example `RL-01`). The status column is updated as fixes land; see [build-roadmap.md](build-roadmap.md) for what was verified afterwards.

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

| Id | Finding | Severity |
|---|---|---|
| `RL-01` | A review decision never changes the evidence record. Approve and reject are both no-ops; no code can set "human-verified" or "rejected" | Critical |
| `CUR-01` | "Quote-anchored" proves that a number and a quote exist in the source, not that the number belongs to the stated arm, endpoint, estimand or population. Ten deliberately mis-attributed rows all loaded as quote-anchored | Critical |
| `CUR-04` / `ING-01` | A key collision in the notes converter stored 68 of 259 curated results and 6 analyses under another endpoint's title (≥10/15/20% responders shown as "≥5%"; fat mass as lean mass). 24 label rows are contradicted by their own quote | Critical |
| `B2` | The model-draft citation check accepted 30 of 32 deliberately wrong drafts (reversed direction, swapped drug, arm or trial, wrong unit, non-inferiority as superiority) and rejected faithful prose on formatting | High |
| `C1`–`C3` | Inside one trial, "direct randomized comparison" is returned for contrasts that were not randomized against each other; endpoints are matched by category only; the quoted between-arm analysis can belong to a different estimand or threshold | High |
| `ING-02`, `ING-03` | Registry contrasts ignore the stated direction (BELIEVE stored reversed); two distinct arms merged; a placebo arm attributed to the drug; mg/kg stored as mg | High |
| `CUR-05` | 15 adverse-event rows carry a measure kind their own quote contradicts | High |
| `XS-06`, `XS-07`, `XS-13` | Gap rules state that ACHIEVE-2 and ATTAIN-MAINTAIN have no public results although results are stored, and can never clear a missing-superiority gap | High |
| `WEB-01`–`WEB-05` | The trial hero chart mixes estimands, tiers and sources under one caption; units are missing beside 39% of results; a kg value can be labelled "%"; active semaglutide arms are coloured "Placebo" | High |
| `A1` | The assistant's internal block kind is `VERIFIED_EVIDENCE` although no evidence is human-verified (the visible label is "Reported evidence") | Medium |

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
