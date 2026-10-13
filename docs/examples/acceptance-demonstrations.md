# Acceptance demonstrations

Generated from the offline-seeded evidence base (retrieved 2026-10-13) by `python scripts/build_docs.py`. Each demonstration is also covered by automated tests (`backend/tests`, `frontend/e2e`).

## A. Retatrutide versus tirzepatide — no head-to-head result is invented

**Classification: DESCRIPTIVE CROSS-STUDY COMPARISON** — Side-by-side description only.

- Findings come from different trials: there is no randomization between these groups, so differences may reflect populations, sites, era, conduct or analysis rather than the treatments.
- Indication / defining comorbidity: The trials enrolled populations defined by different comorbidities.
- Precision: At least one finding has no reported measure of uncertainty, so the precision of any difference cannot be judged.
- Assessment time point (minor): Assessment times differ by 8 weeks. Small difference in follow-up.
- Endpoint hierarchy and multiplicity (minor): The endpoint has a different position in each trial's hierarchy.

What may not be said:
- Do not state or imply that retatrutide 12 mg is superior (or inferior) to tirzepatide 15 mg on the basis of these two findings.
- Do not describe the difference between these values as statistically significant; no valid test supports it.
- Do not rank the treatments by these values as if the ranking were a demonstrated treatment effect.

Assistant answer (deterministic, no language model):

- *Verified Evidence* — Retatrutide — pharmacologic class: Triple GIP, GLP-1 and glucagon receptor agonist (derived from sourced receptor actions). Regulatory status: investigational as of 2026-10-13. Investigational. Listed in Phase 3 on Lilly's pipeline page (data as of 2026-08-05); no FDA-approved labeling exists in the openFDA label database as of the last check. Not approved for any indication.
- *Verified Evidence* — In TRIUMPH-1 (NCT05929066; Phase 3; adults without type 2 diabetes — registered conditions: Obesity, Overweight, Osteoarthritis, Knee), the retatrutide 12 mg group had a reported percent change in body weight of -25% from baseline (week 0, pre-treatment; source says "from baseline") at week 80 (treatment-regimen estimand; confidence interval not reported in this source; n not reported; source: peer-reviewed publication).
- *Verified Evidence* — Tirzepatide — pharmacologic class: Dual GIP and GLP-1 receptor agonist (derived from sourced receptor actions). Regulatory status: approved as of 2026-10-13. FDA-approved as Zepbound (chronic weight management; moderate to severe obstructive sleep apnea in adults with obesity) and as Mounjaro (glycemic control in type 2 diabetes from age 10; reduction of major adverse cardiovascular events in adults with type 2 diabetes at high risk).
- *Verified Evidence* — In SURMOUNT-1 (NCT04184622; Phase 3; adults without type 2 diabetes — registered conditions: Overweight, Obesity), the tirzepatide 15 mg group had a reported percent change in body weight of -20.9% from baseline (randomization, week 0) at week 72 (treatment-regimen estimand; 95% CI -21.8 to -19.9; n not reported; source: peer-reviewed publication).
- *Verified Evidence* — A registered trial includes both agents: TRIUMPH-5 (NCT06662383), PHASE3, status ACTIVE_NOT_RECRUITING, primary completion 2026-11 (estimated). No results are posted or published.
- *Unavailable* — No randomized head-to-head result comparing Retatrutide with Tirzepatide is publicly available in the stored sources.
- *Interpretation* — Comparability of the two results above: DESCRIPTIVE CROSS-STUDY COMPARISON — Side-by-side description only. Findings come from different trials: there is no randomization between these groups, so differences may reflect populations, sites, era, conduct or analysis rather than the treatments. Indication / defining comorbidity: The trials enrolled populations defined by different comorbidities. Precision: At least one finding has no reported measure of uncertainty, so the precision of any difference cannot be judged. Assessment time point (minor): Assessment times differ by 8 weeks. Small difference in follow-up.
- *Interpretation* — What may not be concluded: Do not state or imply that retatrutide 12 mg is superior (or inferior) to tirzepatide 15 mg on the basis of these two findings. Do not describe the difference between these values as statistically significant; no valid test supports it. Do not rank the treatments by these values as if the ranking were a demonstrated treatment effect.
- *Interpretation* — What a rigorous comparison would require: A randomized head-to-head trial with a pre-specified estimand is the only design that removes cross-trial confounding. A registered trial includes both agents: TRIUMPH-5 (NCT06662383), status ACTIVE_NOT_RECRUITING, primary completion 2026-11 (estimated); no results are posted in the registry. Without a head-to-head trial: individual participant data from at least one trial for a matching-adjusted or simulated-treatment comparison, with all effect modifiers measured in both.
- *Hypothesis* — The agents differ in receptor targets (GCGR). Whether that difference translates into different clinical outcomes is a hypothesis until a randomized comparison reports.

Full cited report: [report-retatrutide-vs-tirzepatide.md](report-retatrutide-vs-tirzepatide.md)

## B. Foundayo — tablet/capsule bioequivalence and efficacy versus treatment-regimen estimands

| Capsule dose queried | Result |
|---|---|
| 36 mg | 17.2 mg tablet — documented bioequivalence, quote anchored (pubmed:41994902) |
| 12 mg | 9 mg tablet — documented bioequivalence, quote anchored (pubmed:41994902) |
| 6 mg | 5.5 mg tablet — documented bioequivalence, quote anchored (pubmed:41994902) |
| 20 mg | **No conversion offered.** No 20 mg dose of formulation 'orforglipron-capsule' is on record; no conversion is offered. |

ATTAIN-1, 36 mg capsule arm, week 72: **-12.35%** under the efficacy estimand (Registry-posted results (sponsor-submitted); label inferred from the registry's analysis-population text) and **-11.2%** under the treatment-regimen estimand (Peer-reviewed publication; stated by the publication).

Comparing the two: **NOT SUITABLE FOR EFFICACY COMPARISON**. Estimand: One finding uses the efficacy estimand (hypothetical: had participants stayed on treatment) and the other the treatment-regimen estimand (regardless of discontinuation). They answer different questions and are systematically different in magnitude. Handling of discontinuation / rescue therapy (minor): Documented intercurrent-event strategies differ in at least one respect.

Full report: [report-attain1-estimands.md](report-attain1-estimands.md)

## C. EloraTZP Phase 2b — efficacy and adverse-event discontinuation, correctly qualified

| Arm | % weight change, week 48 (efficacy estimand; press release) | Treatment discontinuation due to adverse events |
|---|---|---|
| Placebo | -3 | 16.7% |
| eloralintide 3 mg | -8.2 | not reported per arm |
| eloralintide 6 mg | -12.3 | not reported per arm |
| eloralintide 9 mg | -11.1 | not reported per arm |
| eloralintide 3 mg + tirzepatide 5 mg | -13.2 | not reported per arm |
| eloralintide 6 mg + tirzepatide 10 mg | -19.9 | not reported per arm |
| eloralintide 6 mg + tirzepatide 5 mg | -19.4 | not reported per arm |
| eloralintide 9 mg + tirzepatide 15 mg | -23.3 | not reported per arm |
| tirzepatide 15 mg | -14.8 | 2.9% |

Range only — EloraTZP combination arms (range across arms; arm-level values not disclosed): 10.8% to 27% (treatment discontinuation due to ae); not attributable to any single arm.

Range only — eloralintide monotherapy arms (range across arms; arm-level values not disclosed): 0% to 10.8% (treatment discontinuation due to ae); not attributable to any single arm.

Context that travels with the headline number:

> In EloraTZP Phase 2b (NCT06603571; Phase 2; adults with type 2 diabetes — registered conditions: Obesity, Overweight), the eloralintide 9 mg + tirzepatide 15 mg group had a reported percent change in body weight of -23.3% from baseline at week 48 (efficacy estimand; confidence interval not reported in this source; n not reported; source: sponsor press release (topline)). Phase 2 dose-finding data. Topline press-release disclosure, not peer reviewed.

## D. ACHIEVE-4 — non-inferiority, hazard ratios, intervals and multiplicity-uncontrolled findings

**Time to first occurrence of MACE-4 (cardiovascular death, non-fatal myocardial infarction, non-fatal stroke, or hospitalisation for unstable angina)** — Orforglipron versus Insulin Glargine — Hazard ratio: 0.84 (95% CI 0.59 to 1.2); p <0.0001 for non-inferiority.  
Qualifiers: Non-inferiority test; Multiplicity status not stated. Source tier: Peer-reviewed publication; quote anchored.
- Establishes: Non-inferiority on this endpoint: the treatment is not worse than the comparator by more than the pre-specified margin (upper confidence limit 1.2 is below the pre-specified margin of 1.8).
- Does not establish: Superiority of the treatment over the comparator: the confidence interval includes 1; the stored source reports a non-inferiority conclusion and does not report a superiority test. Non-inferiority must not be read as superiority.
- Does not establish: A reduction in risk: a point estimate below 1 with an interval spanning 0.59 to 1.2 is compatible with benefit, no difference, or harm up to the upper limit.
- Does not establish: Effects in populations, doses or durations other than those studied.

**Time to first occurrence of MACE-3 (myocardial infarction, stroke, or cardiovascular death)** — Orforglipron versus Insulin Glargine — Hazard ratio: 0.77 (95% CI 0.52 to 1.13); p 0.181 (log-rank).  
Qualifiers: Test type not stated; Multiplicity status not stated; Press release · topline. Source tier: Sponsor press release (topline); quote anchored.
- Does not establish: A between-arm difference: the confidence interval includes no difference.
- Does not establish: Peer-reviewed confirmation: this figure comes from a sponsor press release.
- Does not establish: Effects in populations, doses or durations other than those studied.

**All-cause death** — Orforglipron versus Insulin Glargine — Hazard ratio: 0.43 (95% CI 0.25 to 0.75); p 0.002 (nominal; not controlled for multiplicity).  
Qualifiers: Test type not stated; Not multiplicity-controlled; Exploratory; Press release · topline. Source tier: Sponsor press release (topline); curated pending review.
- Does not establish: A confirmatory conclusion: the interval excludes no difference, but the test type and its place in the testing hierarchy are not on record.
- Does not establish: Any confirmatory claim: the endpoint is exploratory. The result is hypothesis-generating.
- Does not establish: Statistical significance in the confirmatory sense: the result was not controlled for multiplicity, so its p-value is nominal.
- Does not establish: Peer-reviewed confirmation: this figure comes from a sponsor press release.

**Cardiovascular death** — Orforglipron versus Insulin Glargine — Hazard ratio: 0.47 (95% CI 0.25 to 0.9); p 0.019 (nominal; not controlled for multiplicity).  
Qualifiers: Test type not stated; Not multiplicity-controlled; Exploratory; Press release · topline. Source tier: Sponsor press release (topline); quote anchored.
- Does not establish: A confirmatory conclusion: the interval excludes no difference, but the test type and its place in the testing hierarchy are not on record.
- Does not establish: Any confirmatory claim: the endpoint is exploratory. The result is hypothesis-generating.
- Does not establish: Statistical significance in the confirmatory sense: the result was not controlled for multiplicity, so its p-value is nominal.
- Does not establish: Peer-reviewed confirmation: this figure comes from a sponsor press release.

Full study report: [report-achieve-4.md](report-achieve-4.md)

## E. Eloralintide — Phase 3 trials and the questions they seek to answer

| Trial | NCT | Population (registry) | Status | Planned N | Primary completion | Registered primary endpoint(s) |
|---|---|---|---|---|---|---|
| ENLIGHTEN-1 | NCT07321886 | A Study of Eloralintide (LY3841136) in Participants With Obesity, or Overweight Without Type 2 Diabetes | RECRUITING | 1980 | 2028-03 (estimated) | Percent Change from Baseline in Body Weight [Baseline, Week 64] |
| ENLIGHTEN-2 | NCT07282600 | A Study of Eloralintide (LY3841136) in Participants With Obesity or Overweight, and Type 2 Diabetes | RECRUITING | 1035 | 2028-01 (estimated) | Percent Change from Baseline in Body Weight [Baseline, Week 64] |
| ENLIGHTEN-3 | NCT07369011 | A Study of Eloralintide (LY3841136) in Participants With Obstructive Sleep Apnea and Obesity or Overweight | RECRUITING | 800 | 2028-03 (estimated) | Percent Change from Baseline in Body Weight [Baseline, Week 64]; Change from Baseline in Apnea-Hypopnea Index (AHI) [Baseline, Week 64] |
| ENLIGHTEN-4 | NCT07353931 | Efficacy and Safety of Eloralintide (LY3841136) in Participants With Osteoarthritis Knee Pain and Obesity or Overweight | RECRUITING | 900 | 2028-03 (estimated) | Percent Change from Baseline in Body Weight [Baseline, Week 64]; Change from Baseline in the Western Ontario and McMaster Universities Osteoarthritis Index (WOMAC) Pain Subscale Score [Baseline, Week 64] |
| ENLIGHTEN-6 | NCT07392190 | A Study of Eloralintide (LY3841136) in Participants With Persistent Obesity Who Are Treated With a Weekly Incretin | RECRUITING | 900 | 2028-06 (estimated) | Percent Change from Baseline in Body Weight [Baseline, Week 64] |

Open questions recorded for eloralintide:

- Are safety and efficacy results beyond two years available for Eloralintide? — *not known to be studied*
- Are cardiovascular outcomes established for Eloralintide in a prospective outcomes trial? — *not known to be studied*
- Is the evidence for Eloralintide based primarily on intermediate endpoints? — *methodological*
- Are physical-function outcomes reported for Eloralintide? — *evidence in generation*
- What is known about body composition — and about muscle specifically — during weight reduction with Eloralintide? — *evidence in generation*
- What happens to body weight after Eloralintide is stopped, and is maintenance studied? — *not known to be studied*
- Which clinical populations are under-represented in the reported evidence for Eloralintide? — *evidence in generation*
- Have the EloraTZP Phase 2b (NCT06603571) topline results been confirmed in a peer-reviewed publication or registry posting? — *public reporting gap*
- A tracked source changed (ClinicalTrials.gov NCT07321886: A Study of Eloralintide (LY3841136) in Participants With Obesity, or Overweight Without ): does it alter any stored finding or conclusion? — *record change*

## F. Safety — why spontaneous reports cannot establish incidence

FAERS (via openFDA, data through 2026-07-30) holds **160,663 reports** that mention tirzepatide, of which 17,092 mention nausea. Dividing one by the other is not an incidence: the platform's only code path for that calculation raises

> Incidence cannot be calculated from spontaneous reports: the number of exposed patients is unknown and reporting is incomplete. Use adverse-event data from a trial arm (events / participants at risk) for incidence.

- FAERS contains voluntary reports. The number of people exposed to the drug is unknown, so report counts cannot be converted into incidence or risk.
- A report does not establish that the drug caused the event; reports reflect suspicion, coincidence, underlying disease and co-medication.
- Reporting is incomplete and uneven: it rises with publicity, litigation, time on market and prescription volume, and duplicate reports exist.
- The openFDA query matches the drug in any role (suspect or concomitant) and counts a report once per reaction term it lists.
- Counts must not be compared with, added to, or plotted on the same scale as adverse-event percentages from clinical trials.

What is offered instead is a disproportionality screen — for nausea, PRR 2.87 (95% CI 2.83 to 2.91) — labelled as a measure of reporting disproportion, not risk, alongside within-trial incidence by arm (for example SURMOUNT-1 registry adverse-event tables, events over participants at risk).

## G. Source change — an updated registry record becomes a reviewable change summary

**ClinicalTrials.gov NCT07321886: A Study of Eloralintide (LY3841136) in Participants With Obesity, or Overweight Without Type 2 Diabetes** — NCT07321886: 4 tracked change(s) — overall recruitment status, enrollment, secondary outcome added, exclusion criterion added. Significance: HIGH. Review status: OPEN.

> **Synthetic demonstration.** The public registry API exposes only the current record, so the “before” version here was generated from the real record by labelled edits (`make_synthetic_prior_version`). The detection, diffing and review-queue code paths are the production ones; running `incretinos check-updates --live` applies them to real changes.

| Field | Before | After | Significance | Why it matters |
|---|---|---|---|---|
| Overall recruitment status | NOT_YET_RECRUITING | RECRUITING | HIGH | Status changes affect when evidence can be expected and whether the trial was stopped. |
| Enrollment | 1800 | 1980 | MEDIUM | Sample-size changes alter precision and may indicate a protocol amendment. |
| Last update posted | 2026-01-15 | 2026-09-22 | LOW | Registry revision marker. |
| Secondary outcome added | — | PK: Area Under the Concentration Versus Time Curve for One Dosing Interval at Steady State (AUC(0-ᴛ)ss) \| Baseline through Week 64 | MEDIUM | Outcome changes after study start can indicate outcome switching; compare against the dated registry history. |
| Exclusion criterion added | — | Have taken medications or alternative remedies intended for weight loss within 90 days of screening | MEDIUM | Changes the studied population and generalizability. |
