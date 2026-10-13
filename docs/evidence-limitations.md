# Evidence limitations

What IncretinOS cannot tell you, and where the current build is weak. Read this before relying on any output.

## Limits of the evidence itself

1. **Aggregate public data only.** There are no individual participant data. The platform cannot estimate individual dose–response, adjust for covariates, re-run a sponsor's analysis under another estimand, or perform population-adjusted indirect comparisons. Where such an analysis would be needed, the product says so and lists the data required.
2. **Cross-trial comparisons are not treatment comparisons.** Arms of different trials were not randomized against each other. The comparability engine classifies and explains; it never produces a comparative effect estimate, interval or p-value across trials.
3. **Press releases are topline.** Retatrutide TRIUMPH-3 and TRIUMPH-4, the EloraTZP Phase 2b trial and several efficacy-estimand figures are known only from sponsor releases: no confidence intervals, incomplete populations, selective adverse-event reporting, and wording that can change on publication. These rows are labelled and many are pending review.
4. **Registry results are sponsor-submitted.** ClinicalTrials.gov results pass NLM quality control but not peer review, and usually present one analysis per outcome. The estimand is rarely named; IncretinOS infers it from the analysis-population text and labels that inference.
5. **Abstract-level extraction.** Publication values were taken from PubMed abstracts and, for some trials, FDA labeling. Full texts and supplements were not accessible, so most per-arm confidence intervals, multiplicity hierarchies and complete safety tables are absent — and are shown as "not reported in this source", not as non-existent.
6. **Spontaneous reports.** FAERS counts have no exposure denominator, contain duplicates and stimulated reporting, and match the drug in any role. They support signal screening only.
7. **Surrogate endpoints dominate.** Most primary endpoints are weight or HbA1c change. Cardiovascular outcome evidence exists for dulaglutide (REWIND) and tirzepatide (SURPASS-CVOT); for orforglipron ACHIEVE-4 reports cardiovascular *non-inferiority*; for retatrutide and eloralintide outcomes evidence is in generation or absent.
8. **Body composition.** DXA lean mass is not a direct measure of skeletal muscle. No stored source provides muscle-specific imaging paired with function for these agents.
9. **A reporting gap is not an absence of data.** Completed trials without public results are sponsor-held data, and the gap tracker labels them as such.

## Limits of this build

1. **No human verification has taken place.** Statuses are mechanical. 104 of 1,897 result rows, and many non-numeric claims, are *pending human review*; press-release quotes for pages that refused automated retrieval (lilly.com pipeline and explainer pages) and all FDA review PDFs are in that state. The review workflow exists; reviewers do not.
2. **Curated evidence was drafted with AI assistance.** Retrieval notes were produced by language-model research agents reading primary sources, then converted mechanically. Safeguards: every number must appear in its own locator quote, registry values are extracted independently by code, and quotes are anchored to source text where the text can be fetched. A quote can still be accurate while the surrounding interpretation (arm label, estimand attribution, endpoint role) is wrong. Treat pending rows accordingly.
3. **Estimand inference is rule-based.** The classifier reads phrases such as "excluding data after discontinuation of study drug". It will miss unusual wording (classified *not stated*) and cannot see protocol detail. Inferred labels are flagged; they are not the sponsor's statement.
4. **Population classification is heuristic.** Diabetes status is inferred from registry titles, conditions and eligibility text, with the basis shown. Mixed-population trials and unusual phrasing can be misclassified; TRIUMPH-3 is correctly left as *not stated*.
5. **Arm matching is label-based.** Registry, publication and label describe the same arm with different labels (and, for orforglipron, different dose numbers). Arms are unified by drug and dose, and across formulations only through the documented bioequivalence pairs. Unusual labels produce separate arm rows rather than a wrong merge.
6. **Adverse-event coverage is partial.** All registry non-serious events above the posting threshold are stored; serious events are stored when frequent or of class interest. Registry participant-flow "adverse event" counts are study non-completion, which is not the same as treatment discontinuation reported in publications — the two are separate measure kinds.
7. **Baseline characteristics.** Only what the registry baseline module or FDA labeling reports is available; BMI is often absent from the registry, so that comparability factor is frequently "not assessable".
8. **The change-detection demonstration is synthetic.** The public registry API exposes only the current record, so the offline build seeds one labelled, synthetic prior version of ENLIGHTEN-1 to exercise change review. Real changes are detected when `check-updates --live` is run over time.
9. **Gap rules are generic.** They detect classes of missing evidence from structure (status, design features, endpoint categories). They do not know the clinical literature beyond the tracked trials, and "no study found" refers only to tracked registry records.
10. **Design calculations are textbook approximations.** Normal-approximation sample sizes, Schoenfeld event counts and a two-sample t-test simulation. No group-sequential designs, covariate adjustment, repeated measures or multiplicity. They are not a forecast of trial success.
11. **Language-model layer untested against a live provider.** The Anthropic provider is implemented against the documented SDK and the citation check is tested with stand-in providers; no API key was available, so no live model call was made.
12. **Regulatory coverage is U.S.-centred.** Status and indications come from FDA labeling. Ex-U.S. approvals found during research are recorded as claims but not modelled.

## Not claimed

No GxP validation, no 21 CFR Part 11 compliance, no regulatory-grade audit trail, no clinical decision support. See `architecture.md` for what a restricted-data deployment would require.
