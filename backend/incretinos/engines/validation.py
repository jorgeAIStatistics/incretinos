"""Scientific validation rules.

Each rule inspects the live database (and the engines' behaviour on it) for one way the
platform could mislead a reader. The twelve mandatory cases from the product specification
are V01–V12; V13–V16 are additional integrity checks. Rules return violations rather than
raising, so the UI, the CLI and the test-suite can all report them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..curation.loader import AUTHORITATIVE_APPROVAL_TIERS
from ..db import INCLUDE_REJECTED
from ..enums import (
    DISCONTINUATION_KINDS,
    EndpointRole,
    EquivalenceBasis,
    EstimandLabel,
    RegulatoryStatus,
    TriState,
)
from ..models import (
    REVIEWABLE_MODELS,
    AdverseEvent,
    Drug,
    EvidenceSource,
    EvidenceVersion,
    ExposureEquivalence,
    LabelRecord,
    OutcomeResult,
    ReviewDecision,
    SourceSnapshot,
    StatisticalAnalysis,
    Study,
)
from ..review.policy import HUMAN_DECISION_STATUSES, MECHANICAL_STATUSES, STORED_STATUSES
from .comparability import compare_findings, harmonize_dose
from .findings import analysis_dto, build_finding
from .interpretation import classify_drug, guard_statement, interpret_analysis
from .safety import (
    InvalidInferenceError,
    assert_single_data_family,
    assert_single_measure_kind,
    incidence_from_spontaneous_reports,
    spontaneous_overview,
)


@dataclass
class RuleResult:
    rule_id: str
    title: str
    passed: bool
    checked: int = 0
    violations: list[str] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict:
        return {"rule_id": self.rule_id, "title": self.title, "passed": self.passed, "checked": self.checked, "violations": self.violations, "detail": self.detail}


def _result(rule_id: str, title: str, violations: list[str], checked: int, detail: str = "") -> RuleResult:
    return RuleResult(rule_id, title, not violations, checked, violations[:25], detail)


def _drug(session: Session, slug: str) -> Drug | None:
    return session.scalar(select(Drug).where(Drug.slug == slug))


def _study(session: Session, nct: str) -> Study | None:
    return session.scalar(select(Study).where(Study.nct_id == nct))


def _mechs(drug: Drug) -> list[tuple[str, str]]:
    return [(m.target, m.action) for m in drug.mechanisms]


def v01_approval_requires_regulatory_source(session: Session) -> RuleResult:
    violations: list[str] = []
    drugs = list(session.scalars(select(Drug)))
    for drug in drugs:
        if drug.regulatory_status != RegulatoryStatus.APPROVED.value:
            continue
        tier = drug.regulatory_source.tier if drug.regulatory_source else None
        if tier not in AUTHORITATIVE_APPROVAL_TIERS:
            violations.append(f"{drug.name} is marked APPROVED but its status source is tier {tier}, not FDA labeling or review.")
        if session.scalar(select(LabelRecord).where(LabelRecord.drug_id == drug.id)) is None:
            violations.append(f"{drug.name} is marked APPROVED but no FDA label record has been ingested for it.")
    reta = _drug(session, "retatrutide")
    if reta is None:
        violations.append("Retatrutide is missing from the drug registry.")
    else:
        if reta.regulatory_status != RegulatoryStatus.INVESTIGATIONAL.value:
            violations.append(f"Retatrutide status is {reta.regulatory_status}; it must remain INVESTIGATIONAL absent FDA labeling.")
        watch = session.scalar(select(EvidenceSource).where(EvidenceSource.key == "openfda-label-search:retatrutide"))
        if watch is None:
            violations.append("No dated check of the FDA label database is on record for retatrutide.")
    return _result("V01", "Approval status requires an authoritative regulatory source (retatrutide remains investigational)", violations, len(drugs),
                   "A drug is APPROVED only when FDA labeling is ingested; investigational drugs carry a dated label-database check.")


def v02_mechanism_classification(session: Session) -> RuleResult:
    violations: list[str] = []
    expectations = {
        "eloralintide": ("Selective amylin receptor agonist", False),
        "macupatide": ("GIP receptor agonist", False),
        "tirzepatide": ("Dual GIP and GLP-1 receptor agonist", True),
        "retatrutide": ("Triple GIP, GLP-1 and glucagon receptor agonist", True),
        "orforglipron": ("GLP-1 receptor agonist", True),
        "dulaglutide": ("GLP-1 receptor agonist", True),
    }
    for slug, (label, is_glp1) in expectations.items():
        drug = _drug(session, slug)
        if drug is None:
            violations.append(f"{slug} missing from drug registry.")
            continue
        cls = classify_drug(_mechs(drug))
        if cls["class_label"] != label:
            violations.append(f"{drug.name} is classified '{cls['class_label']}', expected '{label}'.")
        if cls["is_glp1_receptor_agonist"] != is_glp1:
            violations.append(f"{drug.name}: is_glp1_receptor_agonist={cls['is_glp1_receptor_agonist']}, expected {is_glp1}.")
        if any(m.source_id is None for m in drug.mechanisms):
            violations.append(f"{drug.name} has a mechanism entry without a source.")
    return _result("V02", "Pharmacologic class derives from sourced receptor actions (eloralintide is a selective amylin receptor agonist)", violations, len(expectations))


def v03_formulation_harmonization(session: Session) -> RuleResult:
    violations: list[str] = []
    rows = list(session.scalars(select(ExposureEquivalence)))
    for row in rows:
        if row.source_id is None or not row.quote:
            violations.append(f"Exposure equivalence {row.dose_a.key} ↔ {row.dose_b.key} has no cited source and quote.")
        if row.basis == EquivalenceBasis.NOT_DOCUMENTED.value:
            violations.append(f"Exposure equivalence {row.dose_a.key} ↔ {row.dose_b.key} is stored without a documented basis.")
    top = harmonize_dose(session, "orforglipron-capsule", 36, "foundayo-tablet")
    if top["status"] != "DOCUMENTED" or (top["equivalent"] or {}).get("amount") != 17.2:
        violations.append(f"36 mg capsule should map to the documented 17.2 mg tablet; got {top}.")
    for undocumented in (18.0, 30.0, 48.0):
        guess = harmonize_dose(session, "orforglipron-capsule", undocumented, "foundayo-tablet")
        if guess["status"] != "NOT_DOCUMENTED" or guess["equivalent"] is not None:
            violations.append(f"An undocumented capsule dose ({undocumented} mg) was converted: {guess}.")
    ratios = {round(r.dose_b.amount / r.dose_a.amount, 3) for r in rows if r.dose_a.amount}
    detail = f"Documented pairs have {len(ratios)} distinct tablet:capsule ratios ({sorted(ratios)}), confirming that no single conversion factor exists."
    return _result("V03", "Foundayo capsule/tablet doses are harmonized only through documented bioequivalence pairs", violations, len(rows), detail)


def v04_attain1_estimands_distinct(session: Session) -> RuleResult:
    violations: list[str] = []
    study = _study(session, "NCT05869903")
    if study is None:
        return _result("V04", "ATTAIN-1 efficacy and treatment-regimen estimands remain distinct", ["ATTAIN-1 not ingested."], 0)
    rows = [r for r in study.results if r.endpoint.category == "WEIGHT_PCT_CHANGE" and r.timepoint_weeks == 72 and "36" in r.arm_label and r.estimand is not None]
    efficacy = [r for r in rows if r.estimand.label == EstimandLabel.EFFICACY.value]
    regimen = [r for r in rows if r.estimand.label == EstimandLabel.TREATMENT_REGIMEN.value]
    if not efficacy or not regimen:
        violations.append("Both estimands must be on record for the 36 mg arm at week 72.")
    else:
        if {round(r.value, 1) for r in efficacy if r.value is not None} & {round(r.value, 1) for r in regimen if r.value is not None}:
            violations.append("An efficacy-estimand value equals a treatment-regimen value for the same arm — check for a merged row.")
        comparison = compare_findings(session, build_finding(session, efficacy[0]), build_finding(session, regimen[0]))
        if not comparison.classification.startswith("NOT SUITABLE"):
            violations.append(f"Comparing the two estimands was classified '{comparison.classification}' instead of NOT SUITABLE.")
        if comparison.descriptive_difference is not None:
            violations.append("A difference was computed between efficacy- and treatment-regimen-estimand values.")
    return _result("V04", "ATTAIN-1 efficacy and treatment-regimen estimands remain distinct", violations, len(rows))


def v05_noninferiority_not_superiority(session: Session) -> RuleResult:
    violations: list[str] = []
    analyses = list(session.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.hypothesis_type == "NON_INFERIORITY")))
    for analysis in analyses:
        dto = analysis_dto(analysis)
        reading = interpret_analysis(dto)
        if not any(label["code"] == "NON_INFERIORITY" for label in dto["labels"]):
            violations.append(f"Analysis {analysis.id} lacks the non-inferiority label.")
        if analysis.superiority_met != TriState.YES.value:
            if not any("superiority" in s.lower() for s in reading["does_not_establish"]):
                violations.append(f"Analysis {analysis.id}: interpretation does not state that superiority is not established.")
            leaked = guard_statement(" ".join(reading["establishes"]), hypothesis_type="NON_INFERIORITY", superiority_met=analysis.superiority_met)
            if leaked:
                violations.append(f"Analysis {analysis.id}: 'establishes' text implies superiority: {leaked[0]['sentence']}")
    achieve4 = _study(session, "NCT05803421")
    mace4 = [a for a in (achieve4.analyses if achieve4 else []) if a.endpoint.category == "MACE" and a.hypothesis_type == "NON_INFERIORITY"]
    if not mace4:
        violations.append("ACHIEVE-4 MACE-4 non-inferiority analysis is not on record.")
    return _result("V05", "Non-inferiority results (ACHIEVE-4 MACE-4) are never presented as superiority", violations, len(analyses))


def v06_exploratory_and_multiplicity_labels(session: Session) -> RuleResult:
    violations: list[str] = []
    checked = 0
    for analysis in session.scalars(select(StatisticalAnalysis)):
        codes = {label["code"] for label in analysis_dto(analysis)["labels"]}
        checked += 1
        if analysis.endpoint.role in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value) and "EXPLORATORY" not in codes:
            violations.append(f"Analysis {analysis.id} ({analysis.study.display_name}) is exploratory but unlabelled.")
        if analysis.multiplicity_controlled == TriState.NO.value and "MULTIPLICITY_UNCONTROLLED" not in codes:
            violations.append(f"Analysis {analysis.id} is multiplicity-uncontrolled but unlabelled.")
        if analysis.multiplicity_controlled == TriState.NOT_STATED.value and "MULTIPLICITY_NOT_STATED" not in codes:
            violations.append(f"Analysis {analysis.id} has unstated multiplicity status but no label saying so.")
    results = session.scalars(select(OutcomeResult).join(OutcomeResult.endpoint)).all()
    for result in results:
        endpoint = result.endpoint
        if endpoint.role not in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value) and endpoint.multiplicity_controlled != TriState.NO.value:
            continue
        checked += 1
        codes = {label["code"] for label in build_finding(session, result).labels}
        if endpoint.role in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value) and "EXPLORATORY" not in codes:
            violations.append(f"Result {result.id} is exploratory but unlabelled.")
        if endpoint.multiplicity_controlled == TriState.NO.value and "MULTIPLICITY_UNCONTROLLED" not in codes:
            violations.append(f"Result {result.id} is multiplicity-uncontrolled but unlabelled.")
    return _result("V06", "Exploratory and multiplicity-uncontrolled results are explicitly labelled", violations, checked)


def v07_eloratzp_context(session: Session) -> RuleResult:
    violations: list[str] = []
    study = _study(session, "NCT06603571")
    rows = [r for r in (study.results if study else []) if r.endpoint.category == "WEIGHT_PCT_CHANGE"]
    if not rows:
        violations.append("No EloraTZP Phase 2b weight results on record.")
    for row in rows:
        statement = build_finding(session, row).statement.lower()
        for needle, name in (("type 2 diabetes", "population"), ("week 48", "treatment duration"), ("phase 2", "study phase"), ("press", "evidence tier"), ("estimand", "estimand")):
            if needle not in statement:
                violations.append(f"Result {row.id} ({row.arm_label}): statement omits the {name}.")
        if row.arm_label.lower() not in statement:
            violations.append(f"Result {row.id}: statement omits the arm and dose.")
    return _result("V07", "EloraTZP efficacy is always stated with population, duration, dose and limitations", violations, len(rows))


def v08_discontinuation_is_not_incidence(session: Session) -> RuleResult:
    violations: list[str] = []
    rows = session.execute(select(AdverseEvent.id, AdverseEvent.term, AdverseEvent.measure_kind, AdverseEvent.category)).all()
    for ae_id, term, kind, category in rows:
        low = term.lower()
        if ("discontinu" in low or "did not complete" in low) and kind == "PARTICIPANTS_WITH_EVENT":
            violations.append(f"Adverse-event row {ae_id} ('{term}') is a discontinuation stored as event incidence.")
        if kind in DISCONTINUATION_KINDS and category != "DISCONTINUATION":
            violations.append(f"Adverse-event row {ae_id} has a discontinuation measure but category {category}.")
    try:
        assert_single_measure_kind({"PARTICIPANTS_WITH_EVENT", "TREATMENT_DISCONTINUATION_DUE_TO_AE"})
        violations.append("Mixing discontinuation and incidence measures did not raise an error.")
    except InvalidInferenceError:
        pass
    return _result("V08", "Discontinuation due to adverse events is never confused with adverse-event incidence", violations, len(rows))


def v09_spontaneous_reports_are_not_incidence(session: Session) -> RuleResult:
    violations: list[str] = []
    try:
        incidence_from_spontaneous_reports(100, 1000)
        violations.append("incidence_from_spontaneous_reports returned a value.")
    except InvalidInferenceError:
        pass
    try:
        assert_single_data_family({"TRIAL", "SPONTANEOUS"})
        violations.append("Combining trial and spontaneous data did not raise an error.")
    except InvalidInferenceError:
        pass
    checked = 0
    for drug in session.scalars(select(Drug)):
        overview = spontaneous_overview(session, drug)
        checked += 1
        if overview.get("available") and (overview.get("is_incidence") is not False or overview.get("supports_causality") is not False or not overview.get("caveats")):
            violations.append(f"{drug.name}: spontaneous-report overview lacks the non-incidence / non-causality flags or caveats.")
    return _result("V09", "Spontaneous FDA reports are never used for incidence or causality", violations, checked)


def v10_randomized_withdrawal_baseline(session: Session) -> RuleResult:
    violations: list[str] = []
    checked = 0
    for study in session.scalars(select(Study)):
        if not (study.design_features or {}).get("randomized_withdrawal"):
            continue
        for row in study.results:
            if row.endpoint.category not in ("WEIGHT_PCT_CHANGE", "WEIGHT_ABS_CHANGE"):
                continue
            checked += 1
            if not row.baseline_definition:
                violations.append(f"{study.display_name} result {row.id} ({row.arm_label}) has no baseline definition.")
                continue
            finding = build_finding(session, row)
            if finding.baseline_class == "RANDOMIZATION_AFTER_LEAD_IN" and "randomi" not in finding.statement.lower():
                violations.append(f"{study.display_name} result {row.id}: statement does not name the randomization baseline.")
    sm4, sm1 = _study(session, "NCT04660643"), _study(session, "NCT04184622")
    if sm4 and sm1:
        withdrawal = next((r for r in sm4.results if r.endpoint.category == "WEIGHT_PCT_CHANGE" and r.baseline_definition and "andomization" in r.baseline_definition and r.timepoint_weeks == 88), None)
        entry = next((r for r in sm1.results if r.endpoint.category == "WEIGHT_PCT_CHANGE" and r.timepoint_weeks == 72), None)
        if withdrawal is None:
            violations.append("SURMOUNT-4 week 36→88 result is not on record.")
        elif entry is not None:
            comparison = compare_findings(session, build_finding(session, withdrawal), build_finding(session, entry))
            if not comparison.classification.startswith("NOT SUITABLE"):
                violations.append(f"SURMOUNT-4 (from randomization) vs SURMOUNT-1 (from baseline) classified '{comparison.classification}'.")
    return _result("V10", "SURMOUNT-4 weight change is reported from the randomized-withdrawal baseline", violations, checked)


def v11_lean_mass_is_not_muscle(session: Session) -> RuleResult:
    violations: list[str] = []
    rows = session.scalars(select(OutcomeResult).join(OutcomeResult.endpoint)).all()
    checked = 0
    for row in rows:
        if row.endpoint.category != "BODY_COMPOSITION":
            continue
        finding = build_finding(session, row)
        if not finding.is_body_composition_lean:
            continue
        checked += 1
        if not any(label["code"] == "LEAN_MASS" for label in finding.labels):
            violations.append(f"Result {row.id} (lean mass) lacks the lean-mass qualification.")
        if "not a direct measurement of skeletal muscle" not in finding.statement:
            violations.append(f"Result {row.id}: statement lacks the lean-mass qualification.")
    if not guard_statement("Participants lost muscle mass on treatment.", lean_mass=True):
        violations.append("The language guard did not flag 'lost muscle' for a lean-mass result.")
    return _result("V11", "Lean / fat-free mass changes are not described as skeletal muscle loss", violations, checked)


def v12_missing_values_stay_missing(session: Session) -> RuleResult:
    violations: list[str] = []
    rows = session.scalars(select(OutcomeResult)).all()
    for row in rows:
        missing = set(row.missing_fields or [])
        if (row.ci_low is None) != (row.ci_high is None):
            violations.append(f"Result {row.id} has only one confidence limit.")
        if row.ci_low is None and "confidence_interval" not in missing:
            violations.append(f"Result {row.id} has no CI but does not list it as missing.")
        if row.ci_low is not None and "confidence_interval" in missing:
            violations.append(f"Result {row.id} lists the CI as missing but has values.")
        if "value" in missing and row.value is not None:
            violations.append(f"Result {row.id} lists the value as missing but stores {row.value}.")
        if "n_analyzed" in missing and row.n_analyzed is not None:
            violations.append(f"Result {row.id} lists n as missing but stores {row.n_analyzed}.")
    analyses = session.scalars(select(StatisticalAnalysis)).all()
    for analysis in analyses:
        missing = set(analysis.missing_fields or [])
        if analysis.ci_low is None and "confidence_interval" not in missing:
            violations.append(f"Analysis {analysis.id} has no CI but does not list it as missing.")
        if "estimate" in missing and analysis.estimate is not None:
            violations.append(f"Analysis {analysis.id} lists the estimate as missing but stores a value.")
    sample = next((r for r in rows if r.ci_low is None), None)
    if sample is not None and not any(label["code"] == "CI_NOT_REPORTED" for label in build_finding(session, sample).labels):
        violations.append("A result without a CI is not labelled 'CI not reported'.")
    return _result("V12", "An absent confidence interval is marked unavailable, never estimated silently or zero-filled", violations, len(rows) + len(analyses))


def v13_every_result_has_provenance(session: Session) -> RuleResult:
    violations: list[str] = []
    rows = session.execute(
        select(OutcomeResult.id, OutcomeResult.source_id, OutcomeResult.tier, OutcomeResult.verification_status, OutcomeResult.origin, OutcomeResult.locator_quote, OutcomeResult.source_location)
        .execution_options(**{INCLUDE_REJECTED: True})  # provenance is checked for every stored row, whatever a reviewer decided
    ).all()
    for rid, source_id, tier, status, origin, quote, location in rows:
        if source_id is None or not tier or not status:
            violations.append(f"Result {rid} lacks a source, tier or verification status.")
        elif status not in STORED_STATUSES:
            violations.append(f"Result {rid} carries '{status}', which is not one of the six verification statuses.")
        if origin == "CURATED" and not quote:
            violations.append(f"Curated result {rid} has no locator quote.")
        if origin == "REGISTRY" and not location:
            violations.append(f"Registry result {rid} has no source location.")
    # Whether a human-verified or rejected status is legitimate is rule V16 (it needs the decision ledger).
    return _result("V13", "Every result carries a source, evidence tier, locator and verification status", violations, len(rows))


def v16_human_statuses_rest_on_attested_decisions(session: Session) -> RuleResult:
    """HUMAN_VERIFIED and REJECTED exist only as the outcome of an attested human decision on the current fingerprint.

    For every row that carries one of the two statuses, and for every row a recorded decision applies
    to, the status is derived again from the decision ledger with the same pure function the review
    lifecycle uses. Any difference is a violation: a human status nothing supports, an approval that
    should have lapsed, or (the dangerous direction) a rejected row stored as usable.
    """
    from ..review.lifecycle import effective_status
    from ..review.queries import decisions_for

    opt_in = {INCLUDE_REJECTED: True}
    violations: list[str] = []
    scanned = 0
    candidates: set[tuple[str, str]] = set()
    for evidence_type, model in REVIEWABLE_MODELS.items():
        scanned += session.scalar(select(func.count()).select_from(model).execution_options(**opt_in)) or 0
        for row_id, key, status in session.execute(
            select(model.id, model.evidence_key, model.verification_status).where(model.verification_status.in_(sorted(HUMAN_DECISION_STATUSES))).execution_options(**opt_in)
        ):
            if key is None:
                violations.append(f"{evidence_type} {row_id} is {status} but has no evidence key: no recorded decision can apply to it.")
            else:
                candidates.add((evidence_type, key))
    decided = session.execute(
        select(ReviewDecision.evidence_type, ReviewDecision.evidence_key, ReviewDecision.natural_key).where(ReviewDecision.evidence_type.is_not(None)).distinct()
    ).all()
    naturals = {(t, n) for t, _, n in decided if n}
    for evidence_type, key, natural in session.execute(
        select(EvidenceVersion.evidence_type, EvidenceVersion.evidence_key, EvidenceVersion.natural_key).where(EvidenceVersion.is_current.is_(True))
        .where(EvidenceVersion.natural_key.in_(sorted({n for _, n in naturals}) or [""]))
    ):
        if (evidence_type, natural) in naturals:
            candidates.add((evidence_type, key))
    candidates.update((t, k) for t, k, _ in decided if k)

    for evidence_type, key in sorted(candidates):
        model = REVIEWABLE_MODELS.get(evidence_type)
        if model is None:
            continue
        row = session.execute(
            select(model.id, model.fingerprint, model.verification_status, model.base_verification_status, model.source_snapshot_id, *( [model.origin] if hasattr(model, "origin") else [] ))
            .where(model.evidence_key == key).execution_options(**opt_in)
        ).first()
        if row is None:
            continue  # the decision's evidence no longer exists: an orphan, reported by the review summary
        natural = session.scalar(
            select(EvidenceVersion.natural_key).where(EvidenceVersion.evidence_type == evidence_type, EvidenceVersion.evidence_key == key, EvidenceVersion.fingerprint == row.fingerprint)
        )
        facts = decisions_for(session, evidence_type, key, natural)
        mechanical = row.base_verification_status if row.base_verification_status in MECHANICAL_STATUSES else "PENDING_HUMAN_REVIEW"
        source_hash = session.scalar(select(SourceSnapshot.content_hash).where(SourceSnapshot.id == row.source_snapshot_id)) if row.source_snapshot_id else None
        outcome = effective_status(mechanical=mechanical, decisions=facts, fingerprint=row.fingerprint or "", structured=getattr(row, "origin", None) == "REGISTRY", source_content_hash=source_hash)
        if row.verification_status in HUMAN_DECISION_STATUSES and outcome.decision is None:
            violations.append(f"{evidence_type} {row.id} is {row.verification_status} without an attested human decision on its current fingerprint.")
        elif outcome.status != row.verification_status:
            violations.append(f"{evidence_type} {row.id} is stored as {row.verification_status} but its decision history yields {outcome.status} ({outcome.reason}).")
    return _result("V16", "Human-verified and rejected statuses rest on an attested human decision for the exact evidence version", violations, scanned)


def v14_press_release_results_are_labelled(session: Session) -> RuleResult:
    violations: list[str] = []
    rows = session.scalars(select(OutcomeResult).where(OutcomeResult.tier == "PRESS_RELEASE")).all()
    for row in rows:
        if not any(label["code"] == "PRESS_RELEASE" for label in build_finding(session, row).labels):
            violations.append(f"Result {row.id} is from a press release but is not labelled as topline.")
    return _result("V14", "Press-release topline results are labelled and never shown as peer-reviewed", violations, len(rows))


def v15_cross_trial_never_superiority(session: Session) -> RuleResult:
    violations: list[str] = []
    sm1, tr1 = _study(session, "NCT04184622"), _study(session, "NCT05929066")
    checked = 0
    if sm1 and tr1:
        def pick(study: Study, needle: str) -> OutcomeResult | None:
            return next((r for r in study.results if r.endpoint.category == "WEIGHT_PCT_CHANGE" and needle in r.arm_label.lower() and r.estimand is not None and r.estimand.label == "TREATMENT_REGIMEN" and r.scope == "ARM" and "minus" not in r.arm_label), None)

        a, b = pick(tr1, "12 mg"), pick(sm1, "15 mg")
        if a is not None and b is not None:
            checked = 1
            comparison = compare_findings(session, build_finding(session, a), build_finding(session, b))
            if comparison.classification.startswith("DIRECT"):
                violations.append("A cross-trial retatrutide-vs-tirzepatide comparison was classified as a direct randomized comparison.")
            if not any("superior" in s.lower() for s in comparison.prohibited_statements):
                violations.append("The cross-trial comparison does not prohibit superiority language.")
            if comparison.descriptive_difference and comparison.descriptive_difference.get("ci") is not None:
                violations.append("A confidence interval was attached to a cross-trial arithmetic difference.")
            if guard_statement(" ".join(comparison.permitted_statements), cross_trial=True):
                violations.append("Permitted statements for a cross-trial comparison contain superiority or significance language.")
    return _result("V15", "Cross-trial comparisons never yield superiority or significance claims", violations, checked)


RULES: list[Callable[[Session], RuleResult]] = [
    v01_approval_requires_regulatory_source,
    v02_mechanism_classification,
    v03_formulation_harmonization,
    v04_attain1_estimands_distinct,
    v05_noninferiority_not_superiority,
    v06_exploratory_and_multiplicity_labels,
    v07_eloratzp_context,
    v08_discontinuation_is_not_incidence,
    v09_spontaneous_reports_are_not_incidence,
    v10_randomized_withdrawal_baseline,
    v11_lean_mass_is_not_muscle,
    v12_missing_values_stay_missing,
    v13_every_result_has_provenance,
    v14_press_release_results_are_labelled,
    v15_cross_trial_never_superiority,
    v16_human_statuses_rest_on_attested_decisions,
]


def run_all(session: Session) -> list[RuleResult]:
    return [rule(session) for rule in RULES]
