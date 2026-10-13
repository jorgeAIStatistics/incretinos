"""Evidence Gap Engine.

Rules scan the evidence base for questions the public record does not yet answer. Every
gap states what is available, what is missing, and *why* it is missing — and the reason is
never collapsed: a completed trial with no public results is a reporting gap (the sponsor
holds data), an ongoing trial is evidence in generation, and "no registered study found
among tracked records" is a statement about this platform's coverage, not proof of absence.

Priority is a transparent weighted sum whose components are returned with the score.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import GapNature, ReviewStatus
from ..models import ChangeEvent, Drug, EvidenceGap, ExposureEquivalence, SpontaneousReportSeries, Study, StudyDrug
from .findings import baseline_class

DEFAULT_WEIGHTS = {"clinical_importance": 0.4, "uncertainty": 0.3, "tractability": 0.15, "actionability": 0.15}
_UNCERTAINTY = {"VERY_LOW": 1.0, "LOW": 0.75, "MODERATE": 0.5, "HIGH": 0.25}
_ACTIONABILITY = {
    GapNature.PUBLIC_REPORTING_GAP.value: 1.0,
    GapNature.SOURCE_CONFLICT.value: 1.0,
    GapNature.RECORD_CHANGE.value: 1.0,
    GapNature.NOT_KNOWN_TO_BE_STUDIED.value: 0.7,
    GapNature.METHODOLOGICAL.value: 0.6,
    GapNature.EVIDENCE_IN_GENERATION.value: 0.4,
}
NATURE_LABEL = {
    GapNature.PUBLIC_REPORTING_GAP.value: "Public reporting gap — study completed; results not (fully) public. Sponsor-held data may exist.",
    GapNature.EVIDENCE_IN_GENERATION.value: "Evidence in generation — a registered study addressing this is ongoing.",
    GapNature.NOT_KNOWN_TO_BE_STUDIED.value: "No study found among tracked registry records — not proof that none exists.",
    GapNature.METHODOLOGICAL.value: "Methodological — data exist but cannot answer the question as analysed.",
    GapNature.SOURCE_CONFLICT.value: "Source conflict — public sources disagree.",
    GapNature.RECORD_CHANGE.value: "Record change — a tracked source changed and awaits review.",
}


@dataclass
class GapDraft:
    key: str
    rule_id: str
    drug_id: int | None
    program: str | None
    category: str
    question: str
    available_evidence: str
    missing_evidence: str
    gap_nature: str
    clinical_importance: int
    evidence_certainty: str
    tractability: int
    analytic_approach: str
    data_required: str
    source_ids: list[int] = field(default_factory=list)
    related_ncts: list[str] = field(default_factory=list)


def _decimals(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def _names(studies: list[Study]) -> str:
    return ", ".join(f"{s.display_name} ({s.nct_id})" for s in studies) or "none"


def _has_any_results(study: Study) -> bool:
    return bool(study.has_results) or any(r.origin == "CURATED" for r in study.results) or any(a.origin == "CURATED" for a in study.analyses)


def _ongoing(study: Study) -> bool:
    return (study.overall_status or "") in ("RECRUITING", "ACTIVE_NOT_RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION")


def _sources(studies: list[Study]) -> list[int]:
    return sorted({s.source_id for s in studies if s.source_id})


def _months_since(iso: str | None, today: date) -> float | None:
    if not iso:
        return None
    try:
        parts = [int(p) for p in iso.split("-")]
        then = date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except (ValueError, IndexError):
        return None
    return (today - then).days / 30.44


def detect_gaps(session: Session, today: date | None = None) -> list[GapDraft]:
    today = today or date.today()
    gaps: list[GapDraft] = []
    drugs = [d for d in session.scalars(select(Drug)) if d.tier_group in ("CORE", "ADJACENT")]

    equivalent_dose: dict[tuple[str | None, float | None], float] = {}
    for eq in session.scalars(select(ExposureEquivalence)):
        equivalent_dose[(eq.dose_b.formulation.key, eq.dose_b.amount)] = eq.dose_a.amount

    for drug in drugs:
        links = session.scalars(select(StudyDrug).where(StudyDrug.drug_id == drug.id)).all()
        studies = [link.study for link in links if link.role != "COMPARATOR"]
        if not studies:
            continue
        name = drug.name
        with_results = [s for s in studies if _has_any_results(s)]
        feats = lambda s: s.design_features or {}  # noqa: E731
        if drug.tier_group != "CORE":
            # Adjacent-pipeline compounds are tracked for context; only reporting status is assessed.
            for study in studies:
                if (study.overall_status or "") == "COMPLETED" and not _has_any_results(study):
                    gaps.append(GapDraft(
                        key=f"{drug.slug}:unreported:{study.nct_id}", rule_id="G11_UNREPORTED", drug_id=drug.id, program=study.program, category="RESULTS_NOT_PUBLIC",
                        question=f"What were the results of {study.display_name} ({study.nct_id})?",
                        available_evidence=f"Registry status COMPLETED; primary completion {study.primary_completion_date or 'not stated'}; no posted registry results and no curated publication or release results.",
                        missing_evidence="Any public results. This is a reporting gap: the sponsor holds the data.",
                        gap_nature=GapNature.PUBLIC_REPORTING_GAP.value, clinical_importance=2, evidence_certainty="VERY_LOW", tractability=4,
                        analytic_approach="Monitor the registry record and PubMed for posting.", data_required="Registry-posted or published results.",
                        source_ids=_sources([study]), related_ncts=[study.nct_id]))
            continue

        # 1 long-term safety / durability of follow-up
        longest: tuple[float, Study] | None = None
        for study in with_results:
            weeks = [r.timepoint_weeks for r in study.results if r.timepoint_weeks]
            if weeks and (longest is None or max(weeks) > longest[0]):
                longest = (max(weeks), study)
        long_ongoing = [s for s in studies if _ongoing(s) and (feats(s).get("outcomes_trial") or feats(s).get("maintenance"))]
        if longest is None or longest[0] < 104:
            gaps.append(GapDraft(
                key=f"{drug.slug}:long-term-safety", rule_id="G01_LONG_TERM", drug_id=drug.id, program=None, category="LONG_TERM_SAFETY",
                question=f"Are safety and efficacy results beyond two years available for {name}?",
                available_evidence=(f"Longest reported assessment among tracked studies: week {longest[0]:.0f} in {longest[1].display_name}." if longest else "No reported results among tracked studies."),
                missing_evidence="Controlled follow-up of two years or more with systematic adverse-event ascertainment.",
                gap_nature=(GapNature.EVIDENCE_IN_GENERATION if long_ongoing else GapNature.NOT_KNOWN_TO_BE_STUDIED).value,
                clinical_importance=5, evidence_certainty="LOW", tractability=2,
                analytic_approach="Exposure-adjusted event rates and time-to-event analyses from long-term extensions and outcomes trials; compare with label warnings as they are updated.",
                data_required="Long-term extension or outcomes-trial datasets with adjudicated events; post-marketing studies with defined denominators.",
                source_ids=_sources(([longest[1]] if longest else []) + long_ongoing), related_ncts=[s.nct_id for s in long_ongoing]))

        # 2 direct comparative evidence
        active = [s for s in studies if feats(s).get("comparator_type") in ("ACTIVE", "PLACEBO_AND_ACTIVE", "ACTIVE_DOSE") and feats(s).get("comparator_type") != "ACTIVE_DOSE"]
        active_reported = [s for s in active if _has_any_results(s)]
        active_pending = [s for s in active if not _has_any_results(s)]
        if not active_reported:
            gaps.append(GapDraft(
                key=f"{drug.slug}:direct-comparative", rule_id="G02_DIRECT_COMPARISON", drug_id=drug.id, program=None, category="COMPARATIVE_EVIDENCE",
                question=f"Is there randomized head-to-head evidence comparing {name} with another active therapy?",
                available_evidence=("Registered active-comparator trials without public results: " + _names(active_pending) + "." if active_pending else "Only placebo-controlled trials among tracked studies."),
                missing_evidence="Results of a randomized active-comparator trial.",
                gap_nature=(GapNature.EVIDENCE_IN_GENERATION if any(_ongoing(s) for s in active_pending) else GapNature.PUBLIC_REPORTING_GAP if active_pending else GapNature.NOT_KNOWN_TO_BE_STUDIED).value,
                clinical_importance=4, evidence_certainty="VERY_LOW", tractability=3,
                analytic_approach="Await the head-to-head trial; meanwhile only anchored indirect comparisons with explicit effect-modifier assessment are defensible.",
                data_required="Head-to-head trial results; or individual participant data for a population-adjusted indirect comparison.",
                source_ids=_sources(active_pending), related_ncts=[s.nct_id for s in active_pending]))

        # 3 cardiovascular outcomes
        cv = [s for s in studies if feats(s).get("outcomes_trial") or feats(s).get("cv_safety_assessment")]
        cv_reported = [s for s in cv if any(a.endpoint.category == "MACE" for a in s.analyses)]
        if not cv_reported:
            gaps.append(GapDraft(
                key=f"{drug.slug}:cv-outcomes", rule_id="G03_CV_OUTCOMES", drug_id=drug.id, program=None, category="CARDIOVASCULAR_OUTCOMES",
                question=f"Are cardiovascular outcomes established for {name} in a prospective outcomes trial?",
                available_evidence=("Registered outcomes trials: " + _names(cv) + "." if cv else "No cardiovascular outcomes trial among tracked studies."),
                missing_evidence="Adjudicated major adverse cardiovascular event results with a pre-specified analysis.",
                gap_nature=(GapNature.EVIDENCE_IN_GENERATION if any(_ongoing(s) for s in cv) else GapNature.PUBLIC_REPORTING_GAP if cv else GapNature.NOT_KNOWN_TO_BE_STUDIED).value,
                clinical_importance=5, evidence_certainty="VERY_LOW", tractability=2,
                analytic_approach="Time-to-first-event analysis under the trial's pre-specified hypothesis (non-inferiority and/or superiority) with multiplicity control.",
                data_required="Outcomes-trial primary results.", source_ids=_sources(cv), related_ncts=[s.nct_id for s in cv]))
        else:
            ni_only = [s for s in cv_reported if all(a.hypothesis_type == "NON_INFERIORITY" or a.superiority_met != "YES" for a in s.analyses if a.endpoint.category == "MACE")]
            if ni_only:
                gaps.append(GapDraft(
                    key=f"{drug.slug}:cv-benefit-vs-safety", rule_id="G03_CV_OUTCOMES", drug_id=drug.id, program=None, category="CARDIOVASCULAR_OUTCOMES",
                    question=f"Do the reported cardiovascular results for {name} establish benefit, or only cardiovascular safety (non-inferiority)?",
                    available_evidence="Cardiovascular analyses on record without a demonstrated superiority conclusion: " + _names(ni_only) + ".",
                    missing_evidence="A pre-specified, multiplicity-controlled superiority result on a cardiovascular endpoint (where not already demonstrated).",
                    gap_nature=GapNature.METHODOLOGICAL.value, clinical_importance=5, evidence_certainty="MODERATE", tractability=2,
                    analytic_approach="Read each analysis against its hypothesis type, margin, confidence interval and position in the testing hierarchy.",
                    data_required="Full publication and statistical analysis plan; where relevant a dedicated superiority outcomes trial.",
                    source_ids=_sources(ni_only), related_ncts=[s.nct_id for s in ni_only]))

        # 4 surrogate endpoints
        primaries = [e for s in studies for e in s.endpoints if e.role == "PRIMARY"]
        clinical = [e for e in primaries if e.category in ("MACE", "MORTALITY", "CV_COMPONENT", "KIDNEY", "DIABETES_ONSET", "PAIN", "PHYSICAL_FUNCTION")]
        if primaries and len(clinical) / len(primaries) < 0.25:
            gaps.append(GapDraft(
                key=f"{drug.slug}:surrogate-endpoints", rule_id="G04_SURROGATE", drug_id=drug.id, program=None, category="SURROGATE_ENDPOINTS",
                question=f"Is the evidence for {name} based primarily on intermediate endpoints?",
                available_evidence=f"{len(primaries) - len(clinical)} of {len(primaries)} registered primary endpoints across tracked studies are weight, glycaemic or other intermediate measures.",
                missing_evidence="Primary results on clinical events, function or symptoms.",
                gap_nature=(GapNature.EVIDENCE_IN_GENERATION if any(_ongoing(s) and feats(s).get("outcomes_trial") for s in studies) else GapNature.METHODOLOGICAL).value,
                clinical_importance=4, evidence_certainty="LOW", tractability=2,
                analytic_approach="Separate surrogate from clinical-outcome evidence in every summary; do not extrapolate event benefit from weight change.",
                data_required="Outcomes-trial results.", source_ids=_sources(studies[:6]), related_ncts=[]))

        # 5 functional outcomes
        functional = [s for s in studies if any(e.category == "PHYSICAL_FUNCTION" for e in s.endpoints)]
        functional_reported = [s for s in functional if any(r.endpoint.category == "PHYSICAL_FUNCTION" for r in s.results)]
        if not functional_reported:
            gaps.append(GapDraft(
                key=f"{drug.slug}:functional-outcomes", rule_id="G05_FUNCTION", drug_id=drug.id, program=None, category="FUNCTIONAL_OUTCOMES",
                question=f"Are physical-function outcomes reported for {name}?",
                available_evidence=("Physical-function endpoints are registered in: " + _names(functional) + ", without results in stored sources." if functional else "No physical-function endpoint found among tracked studies."),
                missing_evidence="Reported physical-function results (patient-reported or performance-based).",
                gap_nature=(GapNature.EVIDENCE_IN_GENERATION if any(_ongoing(s) for s in functional) else GapNature.PUBLIC_REPORTING_GAP if functional else GapNature.NOT_KNOWN_TO_BE_STUDIED).value,
                clinical_importance=3, evidence_certainty="LOW", tractability=3,
                analytic_approach="Report change in function with the instrument, its minimal important difference and its place in the testing hierarchy.",
                data_required="Function endpoint results with responder definitions.", source_ids=_sources(functional), related_ncts=[s.nct_id for s in functional]))

        # 6 body composition
        body = [s for s in studies if any(e.category == "BODY_COMPOSITION" for e in s.endpoints)]
        body_reported = [s for s in body if any(r.endpoint.category == "BODY_COMPOSITION" for r in s.results)]
        gaps.append(GapDraft(
            key=f"{drug.slug}:body-composition", rule_id="G06_BODY_COMPOSITION", drug_id=drug.id, program=None, category="BODY_COMPOSITION",
            question=f"What is known about body composition — and about muscle specifically — during weight reduction with {name}?",
            available_evidence=("Body-composition (DXA) results on record from: " + _names(body_reported) + "." if body_reported else ("Body-composition endpoints registered in: " + _names(body) + ", without results in stored sources." if body else "No body-composition endpoint found among tracked studies.")),
            missing_evidence="Direct measures of skeletal muscle mass, strength and physical performance. DXA lean mass is not a direct measure of muscle.",
            gap_nature=(GapNature.METHODOLOGICAL if body_reported else GapNature.EVIDENCE_IN_GENERATION if any(_ongoing(s) for s in body) else GapNature.PUBLIC_REPORTING_GAP if body else GapNature.NOT_KNOWN_TO_BE_STUDIED).value,
            clinical_importance=4, evidence_certainty="LOW" if body_reported else "VERY_LOW", tractability=3,
            analytic_approach="Report fat and lean mass changes separately, in absolute and relative terms, with the measurement method; pair with strength or function outcomes.",
            data_required="MRI/CT muscle volume or validated muscle-specific measures, strength testing, and functional performance in the same participants.",
            source_ids=_sources(body_reported or body), related_ncts=[s.nct_id for s in (body_reported or body)]))

        # 7 treatment withdrawal / maintenance
        withdrawal = [s for s in studies if feats(s).get("randomized_withdrawal") or feats(s).get("maintenance") or feats(s).get("maintenance_after_incretin")]
        withdrawal_reported = [s for s in withdrawal if _has_any_results(s)]
        if not withdrawal_reported:
            gaps.append(GapDraft(
                key=f"{drug.slug}:treatment-withdrawal", rule_id="G07_WITHDRAWAL", drug_id=drug.id, program=None, category="MAINTENANCE_AFTER_WITHDRAWAL",
                question=f"What happens to body weight after {name} is stopped, and is maintenance studied?",
                available_evidence=("Registered maintenance/withdrawal studies without results in stored sources: " + _names(withdrawal) + "." if withdrawal else "No randomized-withdrawal or maintenance study among tracked studies."),
                missing_evidence="Randomized-withdrawal or maintenance results with weight change measured from the withdrawal baseline.",
                gap_nature=(GapNature.EVIDENCE_IN_GENERATION if any(_ongoing(s) for s in withdrawal) else GapNature.PUBLIC_REPORTING_GAP if withdrawal else GapNature.NOT_KNOWN_TO_BE_STUDIED).value,
                clinical_importance=4, evidence_certainty="VERY_LOW", tractability=3,
                analytic_approach="Randomized-withdrawal design; report change from the randomization baseline separately from change from study entry.",
                data_required="Withdrawal-period weight trajectories by arm.", source_ids=_sources(withdrawal), related_ncts=[s.nct_id for s in withdrawal]))

        # 8 population representativeness
        statuses = {s.population.diabetes_status for s in with_results if s.population}
        missing_pops = []
        if with_results:
            if "T2D" not in statuses and "MIXED" not in statuses:
                missing_pops.append("people with type 2 diabetes")
            if "NO_T2D" not in statuses and "MIXED" not in statuses:
                missing_pops.append("people without type 2 diabetes")
        adult_only = all((s.population.min_age or "").startswith("18") for s in studies if s.population and s.population.min_age)
        if adult_only and studies:
            missing_pops.append("children and adolescents")
        low_female: list[str] = []
        for study in with_results:
            summary = study.population.summary if study.population else {}
            f_n, tot = (summary.get("female_n") or {}).get("value"), (summary.get("sex_total_n") or {}).get("value")
            if f_n is not None and tot:
                pct = 100 * f_n / tot
                if pct < 35 or pct > 70:
                    low_female.append(f"{study.display_name} ({pct:.0f}% female, calculated from registry counts)")
        if missing_pops or low_female:
            gaps.append(GapDraft(
                key=f"{drug.slug}:population-coverage", rule_id="G08_POPULATION", drug_id=drug.id, program=None, category="POPULATION_REPRESENTATIVENESS",
                question=f"Which clinical populations are under-represented in the reported evidence for {name}?",
                available_evidence=f"Reported results cover diabetes status: {', '.join(sorted(statuses)) or 'none'}." + (f" Sex distribution outside 35–70% female in: {'; '.join(low_female)}." if low_female else ""),
                missing_evidence=("Reported results in: " + ", ".join(missing_pops) + ". " if missing_pops else "") + "Race/ethnicity, older-age and comorbidity subgroups should be checked in each trial's baseline table.",
                gap_nature=GapNature.NOT_KNOWN_TO_BE_STUDIED.value if not any(_ongoing(s) for s in studies) else GapNature.EVIDENCE_IN_GENERATION.value,
                clinical_importance=3, evidence_certainty="LOW", tractability=4,
                analytic_approach="Tabulate eligibility criteria and baseline characteristics against the intended-use population; examine pre-specified subgroup consistency.",
                data_required="Baseline tables and subgroup analyses by age, sex, race/ethnicity, BMI class, renal function and diabetes status.",
                source_ids=_sources(with_results[:6]), related_ncts=[]))

        # 9 safety signals without denominators
        faers = session.scalar(select(SpontaneousReportSeries).where(SpontaneousReportSeries.drug_id == drug.id, SpontaneousReportSeries.kind == "TOTAL"))
        if faers is not None and faers.total:
            gaps.append(GapDraft(
                key=f"{drug.slug}:safety-denominators", rule_id="G09_DENOMINATORS", drug_id=drug.id, program=None, category="SAFETY_SIGNAL_SUPPORT",
                question=f"Are post-marketing safety concerns for {name} supported by data with an exposure denominator?",
                available_evidence=f"{faers.total:,} spontaneous FAERS reports mention the drug (report counts; database through {faers.database_last_updated}). Trial adverse-event tables provide within-trial incidence.",
                missing_evidence="Incidence estimates from exposed populations outside trials (cohort or claims studies with defined denominators).",
                gap_nature=GapNature.METHODOLOGICAL.value, clinical_importance=4, evidence_certainty="LOW", tractability=3,
                analytic_approach="Use disproportionality only for signal screening; confirm with cohort designs (active-comparator, new-user) and trial meta-analysis.",
                data_required="Observational cohorts with exposure time, or pooled trial data with adjudicated events.",
                source_ids=[faers.source_id] if faers.source_id else [], related_ncts=[]))

        # 10 results available only as exploratory / uncontrolled
        by_category: dict[str, set[str]] = defaultdict(set)
        for study in with_results:
            for analysis in study.analyses:
                confirm = "CONFIRMATORY_OR_UNSTATED"
                if analysis.endpoint.role in ("EXPLORATORY", "POST_HOC") or analysis.multiplicity_controlled == "NO":
                    confirm = "EXPLORATORY"
                by_category[analysis.endpoint.category].add(confirm)
        only_exploratory = sorted(cat for cat, kinds in by_category.items() if kinds == {"EXPLORATORY"})
        if only_exploratory:
            gaps.append(GapDraft(
                key=f"{drug.slug}:exploratory-only", rule_id="G10_EXPLORATORY_ONLY", drug_id=drug.id, program=None, category="EXPLORATORY_ONLY",
                question=f"Which outcomes for {name} are supported only by exploratory or multiplicity-uncontrolled analyses?",
                available_evidence="Between-arm analyses reported only as exploratory / not multiplicity-controlled for: " + ", ".join(c.replace("_", " ").lower() for c in only_exploratory) + ".",
                missing_evidence="A pre-specified, multiplicity-controlled test of these outcomes.",
                gap_nature=GapNature.METHODOLOGICAL.value, clinical_importance=4, evidence_certainty="LOW", tractability=2,
                analytic_approach="Treat as hypothesis-generating; look for replication in a trial where the outcome is confirmatory.",
                data_required="A trial with the outcome inside the confirmatory testing hierarchy.", source_ids=_sources(with_results[:6]), related_ncts=[]))

        # 11 completed without public results / press-release only
        for study in studies:
            if (study.overall_status or "") == "COMPLETED" and not _has_any_results(study):
                months = _months_since(study.primary_completion_date, today)
                gaps.append(GapDraft(
                    key=f"{drug.slug}:unreported:{study.nct_id}", rule_id="G11_UNREPORTED", drug_id=drug.id, program=study.program, category="RESULTS_NOT_PUBLIC",
                    question=f"What were the results of {study.display_name} ({study.nct_id})?",
                    available_evidence=f"Registry status COMPLETED; primary completion {study.primary_completion_date or 'not stated'}" + (f" ({months:.0f} months ago)" if months is not None else "") + "; no posted registry results and no curated publication or release results.",
                    missing_evidence="Any public results. This is a reporting gap: the sponsor holds the data.",
                    gap_nature=GapNature.PUBLIC_REPORTING_GAP.value, clinical_importance=3, evidence_certainty="VERY_LOW", tractability=4,
                    analytic_approach="Monitor the registry record and PubMed for posting; registry results are generally due within 12 months of primary completion.",
                    data_required="Registry-posted or published results.", source_ids=_sources([study]), related_ncts=[study.nct_id]))
            tiers = {r.tier for r in study.results} | {a.tier for a in study.analyses}
            if tiers and tiers <= {"PRESS_RELEASE", "REGISTRY_PROTOCOL"}:
                gaps.append(GapDraft(
                    key=f"{drug.slug}:press-only:{study.nct_id}", rule_id="G11_UNREPORTED", drug_id=drug.id, program=study.program, category="RESULTS_NOT_PEER_REVIEWED",
                    question=f"Have the {study.display_name} ({study.nct_id}) topline results been confirmed in a peer-reviewed publication or registry posting?",
                    available_evidence="Results on record come only from sponsor press releases.",
                    missing_evidence="Confidence intervals, full analysis populations, complete adverse-event tables and peer review.",
                    gap_nature=GapNature.PUBLIC_REPORTING_GAP.value, clinical_importance=4, evidence_certainty="LOW", tractability=4,
                    analytic_approach="Re-extract on publication and reconcile every topline number against the published value.",
                    data_required="Peer-reviewed article and registry-posted results.", source_ids=sorted({r.source_id for r in study.results if r.source_id}), related_ncts=[study.nct_id]))

        # 12 conflicts between sources
        for study in with_results:
            groups: dict[tuple, list] = defaultdict(list)
            for r in study.results:
                if r.value is None or r.scope != "ARM" or r.arm is None or r.estimand is None or r.estimand.label == "NOT_STATED":
                    continue
                if r.arm.dose_amount is None and not r.arm.is_placebo:
                    continue
                # A documented exposure-equivalent dose identifies the same arm under another formulation's label.
                dose = equivalent_dose.get((r.arm.formulation_key, r.arm.dose_amount), r.arm.dose_amount)
                arm_key = "placebo" if r.arm.is_placebo else f"{r.arm.drug_slug}|{dose:g}"
                if r.endpoint.category not in ("WEIGHT_PCT_CHANGE", "HBA1C_CHANGE") or " minus " in r.arm_label:
                    continue
                groups[(r.endpoint.category, arm_key, r.estimand.label, r.timepoint_weeks, baseline_class(study.design_features, r.baseline_definition))].append(r)
            for (category, arm_key, estimand, weeks, _), rows in groups.items():
                by_source: dict[int, float] = {}
                precision: dict[int, int] = {}
                for r in rows:
                    by_source.setdefault(r.source_id, r.value)
                    precision.setdefault(r.source_id, _decimals(r.value_text if r.value_text is not None else f"{r.value:g}"))
                values = sorted(set(by_source.values()))
                # Values disagree only if they cannot be rounding variants of one another at the coarser reported precision.
                tolerance = 0.5 * 10 ** (-min(precision.values())) + 1e-9 if precision else 0.0
                if len(values) > 1 and (max(values) - min(values)) > tolerance:
                    detail = "; ".join(f"{r.value:g} ({r.source.tier.replace('_', ' ').lower()}, source #{r.source_id})" for r in {r.source_id: r for r in rows}.values())
                    gaps.append(GapDraft(
                        key=f"{drug.slug}:conflict:{study.nct_id}:{category}:{arm_key}:{estimand}:{weeks}", rule_id="G12_SOURCE_CONFLICT", drug_id=drug.id, program=study.program, category="SOURCE_DISCREPANCY",
                        question=f"Why do public sources give different values for {category.replace('_', ' ').lower()} in {study.display_name}, {arm_key.replace('|', ' ')} mg ({estimand.replace('_', ' ').lower()} estimand, week {weeks:g})?" if weeks else f"Why do public sources differ for {study.display_name} {arm_key}?",
                        available_evidence=f"Reported values: {detail}.",
                        missing_evidence="An explanation of the discrepancy (rounding, analysis population, data cut, or model) from the statistical analysis plan or full publication.",
                        gap_nature=GapNature.SOURCE_CONFLICT.value, clinical_importance=2 if (max(values) - min(values)) < 0.5 else 4, evidence_certainty="MODERATE", tractability=5,
                        analytic_approach="Show each value with its source and tier; prefer the regulator-reviewed or peer-reviewed value for citation and note the others.",
                        data_required="Full publication, supplementary appendix or FDA review tables.", source_ids=sorted(by_source), related_ncts=[study.nct_id]))

    # specific head-to-head question: retatrutide vs tirzepatide
    reta, tirz = (session.scalar(select(Drug).where(Drug.slug == s)) for s in ("retatrutide", "tirzepatide"))
    if reta and tirz:
        both = [s for s in session.scalars(select(Study)) if {"retatrutide", "tirzepatide"} <= {link.drug.slug for link in s.drug_links}]
        reported = [s for s in both if _has_any_results(s)]
        if not reported:
            gaps.append(GapDraft(
                key="retatrutide:vs-tirzepatide", rule_id="G02_DIRECT_COMPARISON", drug_id=reta.id, program="TRIUMPH", category="COMPARATIVE_EVIDENCE",
                question="How does retatrutide compare with tirzepatide in a randomized head-to-head trial?",
                available_evidence=("Registered trial(s) including both agents: " + "; ".join(f"{s.display_name} ({s.nct_id}), status {s.overall_status}, primary completion {s.primary_completion_date} ({(s.primary_completion_type or '').lower()})" for s in both) + ". No results are public." if both else "No registered trial including both agents among tracked studies.") + " Separate placebo-controlled trials exist for each agent.",
                missing_evidence="Any randomized comparison of the two agents. Cross-trial differences between TRIUMPH and SURMOUNT results are not head-to-head evidence.",
                gap_nature=(GapNature.EVIDENCE_IN_GENERATION if both else GapNature.NOT_KNOWN_TO_BE_STUDIED).value, clinical_importance=5, evidence_certainty="VERY_LOW", tractability=3,
                analytic_approach="Await the head-to-head trial. Until then present the placebo-controlled trials side by side with their design differences; do not rank.",
                data_required="Head-to-head trial results on a common estimand.", source_ids=_sources(both), related_ncts=[s.nct_id for s in both]))

    # tracked-source changes awaiting review
    for event in session.scalars(select(ChangeEvent).where(ChangeEvent.review_status == ReviewStatus.OPEN.value)):
        study = session.scalar(select(Study).where(Study.source_id == event.source_id))
        drug_id = study.drug_links[0].drug_id if study and study.drug_links else None
        gaps.append(GapDraft(
            key=f"change:{event.id}", rule_id="G13_RECORD_CHANGE", drug_id=drug_id, program=study.program if study else None, category="PROTOCOL_OR_STATUS_CHANGE",
            question=f"A tracked source changed ({event.source.title[:120]}): does it alter any stored finding or conclusion?",
            available_evidence=("[SYNTHETIC DEMONSTRATION] " if event.is_synthetic else "") + event.summary,
            missing_evidence="Reviewer assessment of the change and re-verification of dependent findings.",
            gap_nature=GapNature.RECORD_CHANGE.value, clinical_importance={"HIGH": 4, "MEDIUM": 3}.get(event.significance, 2), evidence_certainty="MODERATE", tractability=5,
            analytic_approach="Review the field-level change summary; re-extract affected results; record a review decision.",
            data_required="None beyond the changed record.", source_ids=[event.source_id], related_ncts=[study.nct_id] if study else []))
    return gaps


def refresh_gaps(session: Session) -> int:
    """Recompute rule-detected gaps, preserving reviewer status and notes on existing ones."""
    drafts = {d.key: d for d in detect_gaps(session)}
    existing = {g.key: g for g in session.scalars(select(EvidenceGap))}
    for key, draft in drafts.items():
        gap = existing.get(key) or EvidenceGap(key=key, rule_id=draft.rule_id, category=draft.category, question=draft.question, available_evidence="", missing_evidence="", gap_nature=draft.gap_nature,
                                              clinical_importance=draft.clinical_importance, evidence_certainty=draft.evidence_certainty, analytic_approach="", data_required="")
        reviewed = key in existing and gap.review_status != ReviewStatus.OPEN.value
        for attr in ("rule_id", "drug_id", "program", "category", "question", "available_evidence", "missing_evidence", "gap_nature", "evidence_certainty", "tractability", "analytic_approach", "data_required", "source_ids", "related_ncts"):
            setattr(gap, attr, getattr(draft, attr))
        if not reviewed:
            gap.clinical_importance = draft.clinical_importance
        session.add(gap)
    for key, gap in existing.items():
        if key not in drafts:
            if gap.review_status == ReviewStatus.OPEN.value:
                session.delete(gap)
            else:
                gap.review_status = ReviewStatus.RESOLVED.value
    session.flush()
    return len(drafts)


def priority(gap: EvidenceGap, weights: dict[str, float] | None = None) -> dict[str, Any]:
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    total = sum(max(0.0, w) for w in weights.values()) or 1.0
    components = {
        "clinical_importance": gap.clinical_importance / 5,
        "uncertainty": _UNCERTAINTY.get(gap.evidence_certainty, 0.5),
        "tractability": gap.tractability / 5,
        "actionability": _ACTIONABILITY.get(gap.gap_nature, 0.5),
    }
    score = sum(components[k] * max(0.0, weights.get(k, 0.0)) for k in components) / total
    return {"score": round(score, 4), "components": components, "weights": {k: max(0.0, weights.get(k, 0.0)) / total for k in components},
            "formula": "Σ weightᵢ × componentᵢ (weights normalised to 1; each component scaled 0–1)"}
