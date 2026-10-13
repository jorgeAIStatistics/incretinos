"""ClinicalTrials.gov API v2 adapter.

Fetches a study record, snapshots it (content-hashed, versioned), normalizes the
protocol section, and — when results are posted — extracts arm-level outcome results,
between-arm statistical analyses, adverse events and participant flow exactly as
reported. Nothing is imputed: a value the registry reports as "NA" stays NULL.

ClinicalTrials.gov content is a U.S. Government work and may be stored in full.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..engines.estimands import classify_estimand_text
from ..enums import (
    AeCategory,
    AeMeasureKind,
    DiabetesStatus,
    EndpointRole,
    EstimandBasis,
    EstimandLabel,
    EvidenceTier,
    HypothesisType,
    LinkBasis,
    Origin,
    TriState,
    VerificationStatus,
)
from ..models import (
    AdverseEvent,
    DiscoveredTrial,
    Endpoint,
    Estimand,
    EvidenceSource,
    ExclusionCriterion,
    InclusionCriterion,
    IntercurrentEventStrategy,
    OutcomeResult,
    Population,
    Publication,
    StatisticalAnalysis,
    Study,
    StudyArm,
    StudyPublication,
    StudyVersion,
)
from ..vocab import (
    COMPARATOR_SLUGS,
    arm_drug_slug,
    categorize_endpoint,
    detect_drugs,
    norm_arm_label,
    parse_single_dose_mg,
    parse_timeframe,
    tag_criterion,
    to_float,
    to_int,
)
from .snapshots import SnapshotOutcome, canonical_json, record_snapshot, sha256_text
from .transport import Transport

CTGOV_LICENSE = "ClinicalTrials.gov record (U.S. National Library of Medicine); U.S. Government work, stored in full."
NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)

# Serious-event terms are numerous; keep those that are frequent or of class interest.
_SERIOUS_TERMS_OF_INTEREST = re.compile(
    r"pancreatitis|cholelithiasis|cholecystitis|gallbladder|biliary|thyroid|hypoglyc|renal|kidney|dehydration|"
    r"ileus|obstruction|gastroparesis|suicid|depress|retinopathy|fracture|arrhythm|fibrillation",
    re.IGNORECASE,
)
_SERIOUS_MIN_AFFECTED = 5


def study_fixture_key(nct_id: str) -> str:
    return f"ctgov/studies/{nct_id.upper()}.json"


def validate_nct(nct_id: str) -> str:
    nct = nct_id.strip().upper()
    if not re.fullmatch(r"NCT\d{8}", nct):
        raise ValueError(f"Not a valid NCT identifier: {nct_id!r}")
    return nct


def canonical_study_hash(raw: dict) -> str:
    """Hash of the record excluding the registry's daily data-version stamp (not study content)."""
    clone = copy.deepcopy(raw)
    clone.get("derivedSection", {}).pop("miscInfoModule", None)
    return sha256_text(canonical_json(clone))


# --------------------------------------------------------------------------- parsing


@dataclass
class ParsedStudy:
    nct_id: str
    acronym: str | None
    brief_title: str
    official_title: str | None
    phase: str | None
    overall_status: str | None
    study_type: str | None
    allocation: str | None
    masking: str | None
    intervention_model: str | None
    primary_purpose: str | None
    enrollment: int | None
    enrollment_type: str | None
    start_date: str | None
    primary_completion_date: str | None
    primary_completion_type: str | None
    completion_date: str | None
    last_update_posted: str | None
    results_first_posted: str | None
    has_results: bool
    lead_sponsor: str | None
    conditions: list[str]
    brief_summary: str | None
    location_count: int | None
    countries: list[str]
    arms: list[dict[str, Any]]
    interventions: list[dict[str, Any]]
    eligibility_text: str
    inclusion: list[str]
    exclusion: list[str]
    sex: str | None
    min_age: str | None
    max_age: str | None
    healthy_volunteers: bool | None
    primary_outcomes: list[dict[str, Any]]
    secondary_outcomes: list[dict[str, Any]]
    other_outcomes: list[dict[str, Any]]
    references: list[dict[str, Any]]
    raw: dict = field(repr=False, default_factory=dict)


def split_eligibility(text: str) -> tuple[list[str], list[str]]:
    """Split registry eligibility free text into inclusion and exclusion bullet lists."""
    inclusion: list[str] = []
    exclusion: list[str] = []
    current: list[str] | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^(key )?inclusion criteria", line, re.IGNORECASE):
            current = inclusion
            continue
        if re.match(r"^(key )?exclusion criteria", line, re.IGNORECASE):
            current = exclusion
            continue
        item = re.sub(r"^[\*\-••]\s*|^\d+[\.\)]\s*", "", line).strip()
        if not item:
            continue
        if current is None:
            current = inclusion
        # Indented continuation bullets belong to the previous criterion.
        if raw_line.startswith((" ", "\t")) and current:
            current[-1] = f"{current[-1]}; {item}"
        else:
            current.append(item)
    return inclusion, exclusion


def parse_study(raw: dict) -> ParsedStudy:
    p = raw.get("protocolSection", {})
    ident = p.get("identificationModule", {})
    status = p.get("statusModule", {})
    design = p.get("designModule", {})
    design_info = design.get("designInfo", {})
    arms_mod = p.get("armsInterventionsModule", {})
    elig = p.get("eligibilityModule", {})
    outcomes = p.get("outcomesModule", {})
    locations = p.get("contactsLocationsModule", {}).get("locations", []) or []
    eligibility_text = elig.get("eligibilityCriteria", "") or ""
    inclusion, exclusion = split_eligibility(eligibility_text)
    phases = design.get("phases") or []
    return ParsedStudy(
        nct_id=ident.get("nctId", ""),
        acronym=ident.get("acronym"),
        brief_title=ident.get("briefTitle", ""),
        official_title=ident.get("officialTitle"),
        phase="/".join(phases) if phases else None,
        overall_status=status.get("overallStatus"),
        study_type=design.get("studyType"),
        allocation=design_info.get("allocation"),
        masking=(design_info.get("maskingInfo") or {}).get("masking"),
        intervention_model=design_info.get("interventionModel"),
        primary_purpose=design_info.get("primaryPurpose"),
        enrollment=(design.get("enrollmentInfo") or {}).get("count"),
        enrollment_type=(design.get("enrollmentInfo") or {}).get("type"),
        start_date=(status.get("startDateStruct") or {}).get("date"),
        primary_completion_date=(status.get("primaryCompletionDateStruct") or {}).get("date"),
        primary_completion_type=(status.get("primaryCompletionDateStruct") or {}).get("type"),
        completion_date=(status.get("completionDateStruct") or {}).get("date"),
        last_update_posted=(status.get("lastUpdatePostDateStruct") or {}).get("date"),
        results_first_posted=(status.get("resultsFirstPostDateStruct") or {}).get("date"),
        has_results=bool(raw.get("hasResults")),
        lead_sponsor=(p.get("sponsorCollaboratorsModule", {}).get("leadSponsor") or {}).get("name"),
        conditions=p.get("conditionsModule", {}).get("conditions", []) or [],
        brief_summary=p.get("descriptionModule", {}).get("briefSummary"),
        location_count=len(locations) if locations else None,
        countries=sorted({loc.get("country") for loc in locations if loc.get("country")}),
        arms=arms_mod.get("armGroups", []) or [],
        interventions=arms_mod.get("interventions", []) or [],
        eligibility_text=eligibility_text,
        inclusion=inclusion,
        exclusion=exclusion,
        sex=elig.get("sex"),
        min_age=elig.get("minimumAge"),
        max_age=elig.get("maximumAge"),
        healthy_volunteers=elig.get("healthyVolunteers"),
        primary_outcomes=outcomes.get("primaryOutcomes", []) or [],
        secondary_outcomes=outcomes.get("secondaryOutcomes", []) or [],
        other_outcomes=outcomes.get("otherOutcomes", []) or [],
        references=p.get("referencesModule", {}).get("references", []) or [],
        raw=raw,
    )


def infer_diabetes_status(parsed: ParsedStudy) -> tuple[DiabetesStatus, str]:
    """Rule-based population classification from registry text; returns (status, basis quote)."""
    titles = " ".join(filter(None, [parsed.brief_title, parsed.official_title]))
    for line in parsed.inclusion:
        if re.search(r"without type 2 diabetes.{0,40}\bor with\b|with or without (type 2 )?diabetes", line, re.IGNORECASE):
            return DiabetesStatus.MIXED, f"Registry inclusion criterion: “{line[:200]}”"
    if re.search(r"without (type 2 )?diabetes|who do not have (type 2 )?diabetes|non-?diabetic", titles, re.IGNORECASE):
        return DiabetesStatus.NO_T2D, f"Registry title: “{titles[:200]}”"
    if re.search(r"(with|and|in) type 2 diabetes|type 2 diabetes mellitus who|\bT2DM?\b", titles, re.IGNORECASE):
        return DiabetesStatus.T2D, f"Registry title: “{titles[:200]}”"
    for line in parsed.exclusion:
        low = line.lower().strip()[:200]
        if not re.search(r"diabet|\bt2dm?\b", low) or low.endswith(":"):
            continue
        if re.search(r"insipidus|retinopathy|maculopathy|family history|nephropathy|neuropathy", low):
            continue
        has_t1 = re.search(r"type 1|\bt1dm?\b", low)
        has_t2 = re.search(r"type 2|\bt2dm?\b", low)
        if has_t2 or not has_t1:
            return DiabetesStatus.NO_T2D, f"Registry exclusion criterion: “{line[:200]}”"
    if any(re.search(r"type 2 diabetes|diabetes mellitus, type 2", c, re.IGNORECASE) for c in parsed.conditions):
        return DiabetesStatus.T2D, f"Registry conditions: {', '.join(parsed.conditions)[:200]}"
    for line in parsed.inclusion:
        if re.search(r"type 2 diabetes|\bt2dm?\b", line, re.IGNORECASE) and len(line) < 220:
            return DiabetesStatus.T2D, f"Registry inclusion criterion: “{line[:200]}”"
    return DiabetesStatus.NOT_STATED, "Registry title, conditions and eligibility text do not state diabetes status."


# --------------------------------------------------------------------------- adapter


@dataclass
class IngestResult:
    study: Study
    outcome: SnapshotOutcome
    parsed: ParsedStudy
    previous_parsed: ParsedStudy | None


class CTGovAdapter:
    def __init__(self, transport: Transport):
        self.transport = transport

    # -- fetch
    def fetch_raw(self, nct_id: str) -> bytes:
        nct = validate_nct(nct_id)
        return self.transport.get("ctgov", f"/studies/{nct}", {"format": "json"}, fixture_key=study_fixture_key(nct))

    def search(self, term: str, *, sponsor: str | None = "Eli Lilly", page_size: int = 200) -> list[dict]:
        """Registry search (intervention term + lead-sponsor filter) returning summary fields only."""
        slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
        params = {
            "query.intr": term,
            "pageSize": str(page_size),
            "format": "json",
            "fields": ",".join(
                [
                    "NCTId", "BriefTitle", "Acronym", "Phase", "OverallStatus", "HasResults", "EnrollmentCount",
                    "StartDate", "PrimaryCompletionDate", "LastUpdatePostDate", "LeadSponsorName", "Condition",
                    "InterventionName",
                ]
            ),
        }
        if sponsor:
            params["query.lead"] = sponsor
        data = json.loads(self.transport.get("ctgov", "/studies", params, fixture_key=f"ctgov/search/{slug}.json"))
        return data.get("studies", [])

    # -- ingest
    def ingest_study(self, session: Session, nct_id: str, *, raw_override: bytes | None = None, is_synthetic: bool = False) -> IngestResult:
        nct = validate_nct(nct_id)
        raw_bytes = raw_override if raw_override is not None else self.fetch_raw(nct)
        raw = json.loads(raw_bytes)
        parsed = parse_study(raw)
        if parsed.nct_id.upper() != nct:
            raise ValueError(f"Registry returned {parsed.nct_id!r} for requested {nct}")

        existing_source = session.scalar(select(EvidenceSource).where(EvidenceSource.key == f"ctgov:{nct}"))
        previous_parsed: ParsedStudy | None = None
        if existing_source and existing_source.snapshots:
            prev_content = existing_source.snapshots[-1].content
            if prev_content:
                previous_parsed = parse_study(json.loads(prev_content))

        outcome = record_snapshot(
            session,
            key=f"ctgov:{nct}",
            source_type="CTGOV",
            tier=EvidenceTier.REGISTRY_RESULTS.value if parsed.has_results else EvidenceTier.REGISTRY_PROTOCOL.value,
            title=f"ClinicalTrials.gov {nct}: {parsed.brief_title}",
            url=f"https://clinicaltrials.gov/study/{nct}",
            publisher="ClinicalTrials.gov (U.S. National Library of Medicine)",
            published_date=parsed.last_update_posted,
            version_label=parsed.last_update_posted,
            content_hash=canonical_study_hash(raw),
            content=canonical_json(raw),
            license_note=CTGOV_LICENSE,
            external_ids={"nct": nct},
            byte_size=len(raw_bytes),
            is_synthetic=is_synthetic,
        )
        study = self._upsert_study(session, parsed, outcome)
        if outcome.is_new_source or outcome.changed or not study.versions:
            session.add(
                StudyVersion(
                    study_id=study.id,
                    snapshot_id=outcome.snapshot.id,
                    version_label=parsed.last_update_posted,
                    content_hash=outcome.snapshot.content_hash,
                    overall_status=parsed.overall_status,
                    has_results=parsed.has_results,
                    retrieved_at=outcome.snapshot.retrieved_at,
                    is_synthetic=is_synthetic,
                )
            )
        session.flush()
        return IngestResult(study, outcome, parsed, previous_parsed if outcome.changed else None)

    # -- normalization
    def _upsert_study(self, session: Session, parsed: ParsedStudy, outcome: SnapshotOutcome) -> Study:
        study = session.scalar(select(Study).where(Study.nct_id == parsed.nct_id))
        if study is None:
            study = Study(nct_id=parsed.nct_id, display_name=parsed.acronym or parsed.nct_id, brief_title=parsed.brief_title)
            session.add(study)
        for attr in (
            "acronym", "brief_title", "official_title", "phase", "overall_status", "study_type", "allocation", "masking",
            "intervention_model", "primary_purpose", "enrollment", "enrollment_type", "start_date",
            "primary_completion_date", "primary_completion_type", "completion_date", "last_update_posted",
            "results_first_posted", "has_results", "lead_sponsor", "conditions", "brief_summary", "location_count", "countries",
        ):
            setattr(study, attr, getattr(parsed, attr))
        if not study.display_name or study.display_name == study.nct_id:
            study.display_name = parsed.acronym or parsed.nct_id
        study.source_id = outcome.source.id
        session.flush()

        source = outcome.source
        self._replace_registry_children(session, study)
        arm_by_norm = self._upsert_arms(session, study, parsed)
        self._upsert_population(session, study, parsed, source)
        self._insert_eligibility(session, study, parsed)
        self._insert_protocol_endpoints(session, study, parsed, source)
        self._link_references(session, study, parsed)
        if parsed.has_results and "resultsSection" in parsed.raw:
            self._extract_results(session, study, parsed, source, arm_by_norm)
        session.flush()
        return study

    @staticmethod
    def _replace_registry_children(session: Session, study: Study) -> None:
        """Registry-derived rows are rebuilt on each ingest; curated rows are untouched."""
        reg = Origin.REGISTRY.value
        session.execute(delete(OutcomeResult).where(OutcomeResult.study_id == study.id, OutcomeResult.origin == reg))
        session.execute(delete(StatisticalAnalysis).where(StatisticalAnalysis.study_id == study.id, StatisticalAnalysis.origin == reg))
        session.execute(delete(AdverseEvent).where(AdverseEvent.study_id == study.id, AdverseEvent.origin == reg))
        session.execute(delete(Endpoint).where(Endpoint.study_id == study.id, Endpoint.origin == reg))
        session.execute(delete(InclusionCriterion).where(InclusionCriterion.study_id == study.id))
        session.execute(delete(ExclusionCriterion).where(ExclusionCriterion.study_id == study.id))
        session.flush()
        session.expire(study)

    def _get_or_create_arm(self, session: Session, study: Study, label: str, arm_by_norm: dict[str, StudyArm], **attrs: Any) -> StudyArm:
        norm = norm_arm_label(label)
        arm = arm_by_norm.get(norm)
        if arm is None:
            arm = session.scalar(select(StudyArm).where(StudyArm.study_id == study.id, StudyArm.norm_label == norm))
        if arm is None:
            drugs = detect_drugs(label)
            if not drugs:  # label names no drug (e.g. "Dose 1"): fall back to the arm's interventions
                drugs = detect_drugs(" ".join(attrs.get("interventions", []) or []))
            non_placebo = [d for d in drugs if d != "placebo"]
            slug_text = label if detect_drugs(label) else " ".join(attrs.get("interventions", []) or [])
            drug_slug, is_placebo = arm_drug_slug(slug_text)
            arm = StudyArm(
                study_id=study.id,
                origin=Origin.REGISTRY.value,
                label=label[:300],
                norm_label=norm,
                drug_slug=drug_slug,
                is_placebo=is_placebo,
                is_pooled=bool(re.search(r"pooled|all (tirzepatide|orforglipron|retatrutide|eloralintide)|overall", label, re.IGNORECASE)),
                dose_amount=parse_single_dose_mg(label) if len(non_placebo) <= 1 else None,
                dose_unit="mg" if parse_single_dose_mg(label) is not None and len(non_placebo) <= 1 else None,
                dose_text="maximum tolerated dose" if re.search(r"\bMTD\b", label) else None,
            )
            session.add(arm)
        for key, value in attrs.items():
            if value is not None and getattr(arm, key, None) in (None, [], ""):
                setattr(arm, key, value)
        arm_by_norm[norm] = arm
        return arm

    def _upsert_arms(self, session: Session, study: Study, parsed: ParsedStudy) -> dict[str, StudyArm]:
        arm_by_norm: dict[str, StudyArm] = {}
        for arm in parsed.arms:
            self._get_or_create_arm(
                session,
                study,
                arm.get("label", "Unnamed arm"),
                arm_by_norm,
                arm_type=arm.get("type"),
                description=arm.get("description"),
                interventions=arm.get("interventionNames", []),
            )
        session.flush()
        return arm_by_norm

    def _upsert_population(self, session: Session, study: Study, parsed: ParsedStudy, source: EvidenceSource) -> None:
        population = study.population or Population(study_id=study.id)
        status, basis = infer_diabetes_status(parsed)
        # A curated classification (with its own basis) takes precedence over the registry inference.
        if not (population.diabetes_basis or "").startswith("Curated"):
            population.diabetes_status = status.value
            population.diabetes_basis = basis
        population.description = "; ".join(parsed.conditions) or None
        population.sex = parsed.sex
        population.min_age = parsed.min_age
        population.max_age = parsed.max_age
        population.healthy_volunteers = parsed.healthy_volunteers
        population.source_id = source.id
        baseline_rows, summary = self._parse_baseline(parsed)
        population.baseline = baseline_rows
        population.summary = {**{k: v for k, v in (population.summary or {}).items() if v.get("origin") == "CURATED"}, **summary}
        session.add(population)

    @staticmethod
    def _parse_baseline(parsed: ParsedStudy) -> tuple[list[dict], dict]:
        module = parsed.raw.get("resultsSection", {}).get("baselineCharacteristicsModule")
        if not module:
            return [], {}
        groups = {g["id"]: g.get("title", g["id"]) for g in module.get("groups", [])}
        total_id = next((gid for gid, title in groups.items() if title.strip().lower() == "total"), None)
        rows: list[dict] = []
        summary: dict[str, dict] = {}
        for m_idx, measure in enumerate(module.get("measures", [])):
            title = measure.get("title", "")
            for cls in measure.get("classes", []):
                for cat in cls.get("categories", []):
                    for meas in cat.get("measurements", []):
                        rows.append(
                            {
                                "measure": title,
                                "class": cls.get("title"),
                                "category": cat.get("title"),
                                "group": groups.get(meas.get("groupId"), meas.get("groupId")),
                                "value": to_float(meas.get("value")),
                                "value_text": meas.get("value"),
                                "spread": to_float(meas.get("spread")),
                                "param": measure.get("paramType"),
                                "dispersion": measure.get("dispersionType"),
                                "unit": measure.get("unitOfMeasure"),
                                "source_location": f"resultsSection.baselineCharacteristicsModule.measures[{m_idx}]",
                            }
                        )
            if total_id is None:
                continue

            def total_value(category: str | None = None, _measure=measure) -> float | None:
                for cls in _measure.get("classes", []):
                    for cat in cls.get("categories", []):
                        if category is not None and (cat.get("title") or "").lower() != category:
                            continue
                        for meas in cat.get("measurements", []):
                            if meas.get("groupId") == total_id:
                                return to_float(meas.get("value"))
                return None

            loc = f"resultsSection.baselineCharacteristicsModule.measures[{m_idx}] (Total group)"
            low = title.lower()
            if low.startswith("age, continuous") and measure.get("paramType") == "MEAN":
                summary["age_mean_years"] = {"value": total_value(), "location": loc, "origin": "REGISTRY"}
            elif low.startswith("sex: female, male"):
                female, male = total_value("female"), total_value("male")
                if female is not None and male is not None and (female + male) > 0:
                    summary["female_n"] = {"value": female, "location": loc, "origin": "REGISTRY"}
                    summary["sex_total_n"] = {"value": female + male, "location": loc, "origin": "REGISTRY"}
            elif "weight" in low and "kilogram" in (measure.get("unitOfMeasure") or "").lower() or low in ("baseline body weight", "body weight"):
                if measure.get("paramType") == "MEAN":
                    summary["weight_mean_kg"] = {"value": total_value(), "location": loc, "origin": "REGISTRY"}
            elif "body mass index" in low or re.search(r"\bbmi\b", low):
                if measure.get("paramType") == "MEAN":
                    summary["bmi_mean"] = {"value": total_value(), "location": loc, "origin": "REGISTRY"}
            elif "hba1c" in low or "hemoglobin a1c" in low:
                if measure.get("paramType") == "MEAN":
                    summary["hba1c_mean_pct"] = {"value": total_value(), "location": loc, "origin": "REGISTRY"}
        return rows, {k: v for k, v in summary.items() if v.get("value") is not None}

    @staticmethod
    def _insert_eligibility(session: Session, study: Study, parsed: ParsedStudy) -> None:
        for i, text in enumerate(parsed.inclusion):
            session.add(InclusionCriterion(study_id=study.id, ordinal=i, text=text, tags=tag_criterion(text)))
        for i, text in enumerate(parsed.exclusion):
            session.add(ExclusionCriterion(study_id=study.id, ordinal=i, text=text, tags=tag_criterion(text)))

    @staticmethod
    def _insert_protocol_endpoints(session: Session, study: Study, parsed: ParsedStudy, source: EvidenceSource) -> None:
        groups = (
            ("primaryOutcomes", EndpointRole.PRIMARY, parsed.primary_outcomes),
            ("secondaryOutcomes", EndpointRole.SECONDARY, parsed.secondary_outcomes),
            ("otherOutcomes", EndpointRole.OTHER_PRESPECIFIED, parsed.other_outcomes),
        )
        for key, role, items in groups:
            for i, item in enumerate(items):
                title = item.get("measure", "")
                category, threshold = categorize_endpoint(title)
                weeks, _ = parse_timeframe(item.get("timeFrame"))
                session.add(
                    Endpoint(
                        study_id=study.id,
                        origin=Origin.REGISTRY.value,
                        kind="PROTOCOL",
                        title=title,
                        role=role.value,
                        category=category.value,
                        threshold=threshold,
                        timeframe_text=item.get("timeFrame"),
                        timepoint_weeks=weeks,
                        description=item.get("description"),
                        multiplicity_controlled=TriState.NOT_STATED.value,
                        source_id=source.id,
                        source_location=f"protocolSection.outcomesModule.{key}[{i}]",
                    )
                )

    @staticmethod
    def _link_references(session: Session, study: Study, parsed: ParsedStudy) -> None:
        for ref in parsed.references:
            pmid = ref.get("pmid")
            if not pmid:
                continue
            publication = session.scalar(select(Publication).where(Publication.pmid == pmid))
            if publication is None:
                publication = Publication(pmid=pmid, title=(ref.get("citation") or f"PMID {pmid}")[:2000])
                session.add(publication)
                session.flush()
            link = session.scalar(
                select(StudyPublication).where(StudyPublication.study_id == study.id, StudyPublication.publication_id == publication.id)
            )
            if link is None:
                link = StudyPublication(study_id=study.id, publication_id=publication.id, bases=[])
                session.add(link)
            if LinkBasis.REGISTRY_REFERENCE.value not in link.bases:
                link.bases = [*link.bases, LinkBasis.REGISTRY_REFERENCE.value]

    # -- results
    def _get_estimand(self, session: Session, study: Study, source: EvidenceSource, cache: dict, *texts: str | None) -> tuple[Estimand, list[str]]:
        cls = classify_estimand_text(*texts)
        key = (cls.label.value, cls.basis.value)
        estimand = cache.get(key)
        if estimand is None:
            estimand = session.scalar(
                select(Estimand).where(Estimand.study_id == study.id, Estimand.label == cls.label.value, Estimand.basis == cls.basis.value)
            )
        if estimand is None:
            estimand = Estimand(
                study_id=study.id,
                label=cls.label.value,
                basis=cls.basis.value,
                description=cls.rationale,
                population_text=next((t for t in texts if t), None),
                source_id=source.id,
                quote=cls.quote,
            )
            session.add(estimand)
            session.flush()
            for finding in cls.strategies:
                session.add(
                    IntercurrentEventStrategy(
                        estimand_id=estimand.id,
                        event=finding.event.value,
                        strategy=finding.strategy.value,
                        basis=cls.basis.value,
                        quote=finding.quote,
                    )
                )
        cache[key] = estimand
        missing = ["estimand_label"] if cls.label == EstimandLabel.NOT_STATED else []
        if cls.basis == EstimandBasis.INFERRED_FROM_ANALYSIS_DESCRIPTION:
            missing.append("estimand_label_stated_by_source")
        return estimand, missing

    def _extract_results(self, session: Session, study: Study, parsed: ParsedStudy, source: EvidenceSource, arm_by_norm: dict[str, StudyArm]) -> None:
        results = parsed.raw["resultsSection"]
        tier = EvidenceTier.REGISTRY_RESULTS.value
        machine = VerificationStatus.MACHINE_EXTRACTED.value
        estimand_cache: dict = {}

        for om_idx, om in enumerate(results.get("outcomeMeasuresModule", {}).get("outcomeMeasures", [])):
            base_loc = f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{om_idx}]"
            title = om.get("title", "")
            unit = om.get("unitOfMeasure")
            category, threshold = categorize_endpoint(title, unit)
            weeks, baseline_def = parse_timeframe(om.get("timeFrame"))
            role = {
                "PRIMARY": EndpointRole.PRIMARY,
                "SECONDARY": EndpointRole.SECONDARY,
                "OTHER_PRE_SPECIFIED": EndpointRole.OTHER_PRESPECIFIED,
                "POST_HOC": EndpointRole.POST_HOC,
            }.get(om.get("type", ""), EndpointRole.NOT_STATED)
            groups = {g["id"]: g for g in om.get("groups", [])}
            endpoint = Endpoint(
                study_id=study.id,
                origin=Origin.REGISTRY.value,
                kind="RESULTS",
                title=title,
                role=role.value,
                category=category.value,
                threshold=threshold,
                unit=unit,
                timeframe_text=om.get("timeFrame"),
                timepoint_weeks=weeks,
                description=om.get("description"),
                multiplicity_controlled=TriState.NOT_STATED.value,
                is_pooled_arms=any(re.search(r"pooled", g.get("title", ""), re.IGNORECASE) for g in groups.values()),
                source_id=source.id,
                source_location=base_loc,
            )
            session.add(endpoint)
            session.flush()
            estimand, estimand_missing = self._get_estimand(
                session, study, source, estimand_cache, om.get("populationDescription"), om.get("description")
            )

            measure_denoms = self._denoms(om.get("denoms"))
            dispersion_type = om.get("dispersionType")
            ci_level = None
            if dispersion_type and "confidence interval" in dispersion_type.lower():
                level = re.search(r"(\d+(?:\.\d+)?)\s*%", dispersion_type)
                ci_level = float(level.group(1)) if level else None

            arm_for_group: dict[str, StudyArm] = {}
            for gid, group in groups.items():
                arm_for_group[gid] = self._get_or_create_arm(
                    session, study, group.get("title", gid), arm_by_norm, description=group.get("description"), arm_type="RESULTS_GROUP"
                )
            session.flush()

            for c_idx, cls in enumerate(om.get("classes", [])):
                class_denoms = self._denoms(cls.get("denoms")) or measure_denoms
                for cat_idx, cat in enumerate(cls.get("categories", [])):
                    class_title = " — ".join(filter(None, [cls.get("title"), cat.get("title")])) or None
                    for meas in cat.get("measurements", []):
                        gid = meas.get("groupId")
                        value = to_float(meas.get("value"))
                        missing = list(estimand_missing)
                        if value is None:
                            missing.append("value")
                        ci_low = ci_high = None
                        if ci_level is not None:
                            ci_low, ci_high = to_float(meas.get("lowerLimit")), to_float(meas.get("upperLimit"))
                        if ci_low is None or ci_high is None:
                            missing.append("confidence_interval")
                        n_analyzed = class_denoms.get(gid)
                        if n_analyzed is None:
                            missing.append("n_analyzed")
                        arm = arm_for_group.get(gid)
                        notes = meas.get("comment")
                        if ci_level is None and (meas.get("lowerLimit") or meas.get("upperLimit")):
                            notes = "; ".join(filter(None, [notes, f"{dispersion_type}: {meas.get('lowerLimit')} to {meas.get('upperLimit')}"]))
                        session.add(
                            OutcomeResult(
                                study_id=study.id,
                                endpoint_id=endpoint.id,
                                arm_id=arm.id if arm else None,
                                estimand_id=estimand.id,
                                origin=Origin.REGISTRY.value,
                                arm_label=groups.get(gid, {}).get("title", gid or "?"),
                                scope="POOLED" if arm and arm.is_pooled else "ARM",
                                class_title=class_title,
                                param_type=om.get("paramType"),
                                value=value,
                                value_text=meas.get("value"),
                                unit=unit,
                                dispersion_type=dispersion_type if ci_level is None else None,
                                dispersion_value=to_float(meas.get("spread")) if ci_level is None else None,
                                ci_low=ci_low,
                                ci_high=ci_high,
                                ci_level=ci_level if (ci_low is not None and ci_high is not None) else None,
                                n_analyzed=n_analyzed,
                                analysis_population=om.get("populationDescription"),
                                analysis_method=om.get("description"),
                                baseline_definition=baseline_def,
                                timepoint_weeks=weeks,
                                timeframe_text=om.get("timeFrame"),
                                source_id=source.id,
                                source_location=f"{base_loc}.classes[{c_idx}].categories[{cat_idx}].measurements[{gid}]",
                                tier=tier,
                                verification_status=machine,
                                missing_fields=missing,
                                notes=notes,
                            )
                        )

            for a_idx, analysis in enumerate(om.get("analyses", [])):
                self._add_analysis(session, study, endpoint, estimand, analysis, groups, arm_for_group, source, f"{base_loc}.analyses[{a_idx}]", weeks)

        self._extract_adverse_events(session, study, results, source, arm_by_norm)
        self._extract_participant_flow(session, study, results, source, arm_by_norm)

    @staticmethod
    def _denoms(denoms: list | None) -> dict[str, int]:
        out: dict[str, int] = {}
        for denom in denoms or []:
            if (denom.get("units") or "").lower().startswith("participant"):
                for count in denom.get("counts", []):
                    value = to_int(count.get("value"))
                    if value is not None:
                        out[count.get("groupId")] = value
        return out

    def _add_analysis(self, session, study, endpoint, estimand, analysis, groups, arm_for_group, source, location, weeks) -> None:
        gids = analysis.get("groupIds", [])
        labels = [groups.get(g, {}).get("title", g) for g in gids]
        treatment_id = comparator_id = None
        treatment_label = comparator_label = None
        note = None
        if len(gids) == 2:
            arms = [arm_for_group.get(g) for g in gids]

            def is_comparator(arm: StudyArm | None) -> int:
                if arm is None:
                    return 0
                if arm.is_placebo:
                    return 2
                return 1 if (arm.drug_slug in COMPARATOR_SLUGS) else 0

            scores = [is_comparator(a) for a in arms]
            if scores[0] != scores[1]:
                comp_idx = 0 if scores[0] > scores[1] else 1
                trt_idx = 1 - comp_idx
            else:
                trt_idx, comp_idx = 0, 1
                note = "Registry does not state which group is the reference; groups listed in registry order."
            treatment_label, comparator_label = labels[trt_idx], labels[comp_idx]
            treatment_id = arms[trt_idx].id if arms[trt_idx] else None
            comparator_id = arms[comp_idx].id if arms[comp_idx] else None
        else:
            treatment_label = " vs ".join(labels)
            note = f"Analysis spans {len(gids)} groups."

        hypothesis = {
            "SUPERIORITY": HypothesisType.SUPERIORITY,
            "NON_INFERIORITY": HypothesisType.NON_INFERIORITY,
            "EQUIVALENCE": HypothesisType.EQUIVALENCE,
            "NON_INFERIORITY_OR_EQUIVALENCE": HypothesisType.NON_INFERIORITY,
            "OTHER": HypothesisType.OTHER,
            "SUPERIORITY_OR_OTHER": HypothesisType.OTHER,
        }.get(analysis.get("nonInferiorityType", ""), HypothesisType.NOT_STATED)
        comments = " ".join(
            filter(
                None,
                [
                    analysis.get("nonInferiorityComment"),
                    analysis.get("pValueComment"),
                    analysis.get("statisticalComment"),
                    analysis.get("estimateComment"),
                    analysis.get("otherAnalysisDescription"),
                ],
            )
        )
        multiplicity = TriState.NOT_STATED
        if re.search(r"not (adjusted|controlled) for multiplicity|no (adjustment|control) for multipl|nominal p", comments, re.IGNORECASE):
            multiplicity = TriState.NO
        elif re.search(r"multiplicity|graphical (approach|testing)|type 1 error|type I error|familywise|gatekeep", comments, re.IGNORECASE):
            multiplicity = TriState.YES

        estimate = to_float(analysis.get("paramValue"))
        ci_low, ci_high = to_float(analysis.get("ciLowerLimit")), to_float(analysis.get("ciUpperLimit"))
        missing: list[str] = []
        if estimate is None:
            missing.append("estimate")
        if ci_low is None or ci_high is None:
            missing.append("confidence_interval")
        if not analysis.get("pValue"):
            missing.append("p_value")
        if multiplicity == TriState.NOT_STATED:
            missing.append("multiplicity_status")
        session.add(
            StatisticalAnalysis(
                study_id=study.id,
                endpoint_id=endpoint.id,
                estimand_id=estimand.id,
                origin=Origin.REGISTRY.value,
                treatment_arm_id=treatment_id,
                comparator_arm_id=comparator_id,
                treatment_label=treatment_label,
                comparator_label=comparator_label,
                hypothesis_type=hypothesis.value,
                param_type=analysis.get("paramType"),
                estimate=estimate,
                estimate_text=analysis.get("paramValue"),
                unit=endpoint.unit,
                ci_low=ci_low,
                ci_high=ci_high,
                ci_level=to_float(analysis.get("ciPctValue")) if ci_low is not None else None,
                ci_sides=analysis.get("ciNumSides"),
                p_value_text=analysis.get("pValue"),
                method=analysis.get("statisticalMethod"),
                multiplicity_controlled=multiplicity.value,
                timepoint_weeks=weeks,
                comment="; ".join(filter(None, [comments or None, note])) or None,
                source_id=source.id,
                source_location=location,
                tier=EvidenceTier.REGISTRY_RESULTS.value,
                verification_status=VerificationStatus.MACHINE_EXTRACTED.value,
                missing_fields=missing,
            )
        )

    def _extract_adverse_events(self, session, study, results, source, arm_by_norm) -> None:
        module = results.get("adverseEventsModule")
        if not module:
            return
        tier = EvidenceTier.REGISTRY_RESULTS.value
        machine = VerificationStatus.MACHINE_EXTRACTED.value
        period = module.get("timeFrame")
        threshold = module.get("frequencyThreshold")
        groups = {g["id"]: g for g in module.get("eventGroups", [])}
        arm_for_group = {
            gid: self._get_or_create_arm(session, study, g.get("title", gid), arm_by_norm, arm_type="RESULTS_GROUP") for gid, g in groups.items()
        }
        session.flush()

        def add(gid, term, category, kind, *, affected, at_risk, events=None, organ=None, serious=False, location="", notes=None):
            missing = []
            if affected is None:
                missing.append("n_affected")
            if at_risk is None:
                missing.append("n_at_risk")
            session.add(
                AdverseEvent(
                    study_id=study.id,
                    arm_id=arm_for_group[gid].id,
                    origin=Origin.REGISTRY.value,
                    arm_label=groups[gid].get("title", gid),
                    term=term[:300],
                    organ_system=organ,
                    category=category.value,
                    measure_kind=kind.value,
                    is_serious=serious,
                    n_affected=affected,
                    n_at_risk=at_risk,
                    n_events=events,
                    period=period,
                    source_id=source.id,
                    source_location=location,
                    tier=tier,
                    verification_status=machine,
                    missing_fields=missing,
                    notes=notes,
                )
            )

        base = "resultsSection.adverseEventsModule"
        for gid, group in groups.items():
            add(gid, "All-cause mortality", AeCategory.DEATH, AeMeasureKind.DEATHS,
                affected=group.get("deathsNumAffected"), at_risk=group.get("deathsNumAtRisk"), location=f"{base}.eventGroups[{gid}]")
            add(gid, "Any serious adverse event", AeCategory.SERIOUS, AeMeasureKind.PARTICIPANTS_WITH_EVENT,
                affected=group.get("seriousNumAffected"), at_risk=group.get("seriousNumAtRisk"), serious=True, location=f"{base}.eventGroups[{gid}]")
            add(gid, "Any other (non-serious) adverse event above reporting threshold", AeCategory.OTHER, AeMeasureKind.PARTICIPANTS_WITH_EVENT,
                affected=group.get("otherNumAffected"), at_risk=group.get("otherNumAtRisk"), location=f"{base}.eventGroups[{gid}]",
                notes=f"Registry reports non-serious events only above a frequency threshold of {threshold}%." if threshold else None)

        for key, serious in (("seriousEvents", True), ("otherEvents", False)):
            for e_idx, event in enumerate(module.get(key, [])):
                stats = {s.get("groupId"): s for s in event.get("stats", [])}
                term = event.get("term", "")
                organ = event.get("organSystem")
                if serious:
                    max_affected = max((s.get("numAffected") or 0) for s in stats.values()) if stats else 0
                    if max_affected < _SERIOUS_MIN_AFFECTED and not _SERIOUS_TERMS_OF_INTEREST.search(term):
                        continue
                is_gi = (organ or "").lower().startswith("gastrointestinal")
                category = AeCategory.GASTROINTESTINAL if is_gi else (AeCategory.SERIOUS if serious else AeCategory.OTHER)
                for gid, stat in stats.items():
                    if gid not in groups:
                        continue
                    add(
                        gid, term, category, AeMeasureKind.PARTICIPANTS_WITH_EVENT,
                        affected=stat.get("numAffected"), at_risk=stat.get("numAtRisk"), events=stat.get("numEvents"),
                        organ=organ, serious=serious, location=f"{base}.{key}[{e_idx}]",
                        notes=None if serious else (f"Listed because frequency exceeded the registry reporting threshold ({threshold}%)." if threshold else None),
                    )

    def _extract_participant_flow(self, session, study, results, source, arm_by_norm) -> None:
        module = results.get("participantFlowModule")
        if not module:
            return
        groups = {g["id"]: g for g in module.get("groups", [])}
        arm_for_group = {
            gid: self._get_or_create_arm(session, study, g.get("title", gid), arm_by_norm, arm_type="RESULTS_GROUP") for gid, g in groups.items()
        }
        session.flush()
        tier = EvidenceTier.REGISTRY_RESULTS.value
        machine = VerificationStatus.MACHINE_EXTRACTED.value
        for p_idx, period in enumerate(module.get("periods", [])):
            started: dict[str, int | None] = {}
            not_completed: dict[str, int | None] = {}
            for milestone in period.get("milestones", []):
                counts = {a.get("groupId"): to_int(a.get("numSubjects")) for a in milestone.get("achievements", [])}
                if milestone.get("type") == "STARTED":
                    started = counts
                elif milestone.get("type") == "NOT COMPLETED":
                    not_completed = counts
            if p_idx == 0:
                for gid, n in started.items():
                    if gid in arm_for_group and n is not None and arm_for_group[gid].n_randomized is None:
                        arm_for_group[gid].n_randomized = n
            location = f"resultsSection.participantFlowModule.periods[{p_idx}]"
            title = period.get("title") or f"Period {p_idx + 1}"

            def add(gid, term, kind, affected, _started=started, _location=location, _title=title):
                at_risk = _started.get(gid)
                session.add(
                    AdverseEvent(
                        study_id=study.id,
                        arm_id=arm_for_group[gid].id,
                        origin=Origin.REGISTRY.value,
                        arm_label=groups[gid].get("title", gid),
                        term=term,
                        category=AeCategory.DISCONTINUATION.value,
                        measure_kind=kind.value,
                        n_affected=affected,
                        n_at_risk=at_risk,
                        period=_title,
                        source_id=source.id,
                        source_location=_location,
                        tier=tier,
                        verification_status=machine,
                        missing_fields=[f for f, v in (("n_affected", affected), ("n_at_risk", at_risk)) if v is None],
                        notes=(
                            "Registry participant flow counts participants who did not complete the study period; "
                            "this is not the same quantity as discontinuation of study treatment reported in publications."
                        ),
                    )
                )

            for gid in groups:
                if gid in not_completed:
                    add(gid, "Did not complete study period (any reason)", AeMeasureKind.DISCONTINUATION_ANY_REASON, not_completed.get(gid))
            for drop in period.get("dropWithdraws", []):
                if (drop.get("type") or "").strip().lower() != "adverse event":
                    continue
                for reason in drop.get("reasons", []):
                    gid = reason.get("groupId")
                    if gid in groups:
                        add(gid, "Did not complete study period — reason: adverse event", AeMeasureKind.STUDY_WITHDRAWAL_DUE_TO_AE, to_int(reason.get("numSubjects")))

    # -- discovery
    def discover(self, session: Session, terms: dict[str, list[str]], *, sponsor: str | None = "Eli Lilly") -> int:
        """Index registry trials matching drug terms (summary fields only) for the Trial Atlas."""
        seen = 0
        for slug, term_list in terms.items():
            for term in term_list:
                try:
                    studies = self.search(term, sponsor=sponsor)
                except Exception:  # offline without a recorded search — skip, discovery is best-effort
                    continue
                for item in studies:
                    p = item.get("protocolSection", {})
                    ident = p.get("identificationModule", {})
                    status = p.get("statusModule", {})
                    design = p.get("designModule", {})
                    nct = ident.get("nctId")
                    if not nct:
                        continue
                    row = session.scalar(select(DiscoveredTrial).where(DiscoveredTrial.nct_id == nct))
                    if row is None:
                        row = DiscoveredTrial(nct_id=nct, brief_title=ident.get("briefTitle", ""), drug_slugs=[])
                        session.add(row)
                        seen += 1
                    row.brief_title = ident.get("briefTitle", row.brief_title)
                    row.acronym = ident.get("acronym")
                    row.phase = "/".join(design.get("phases", []) or []) or None
                    row.overall_status = status.get("overallStatus")
                    row.has_results = bool(item.get("hasResults"))
                    row.enrollment = (design.get("enrollmentInfo") or {}).get("count")
                    row.start_date = (status.get("startDateStruct") or {}).get("date")
                    row.primary_completion_date = (status.get("primaryCompletionDateStruct") or {}).get("date")
                    row.last_update_posted = (status.get("lastUpdatePostDateStruct") or {}).get("date")
                    row.lead_sponsor = (p.get("sponsorCollaboratorsModule", {}).get("leadSponsor") or {}).get("name")
                    row.conditions = p.get("conditionsModule", {}).get("conditions", []) or []
                    row.interventions = [i.get("name") for i in p.get("armsInterventionsModule", {}).get("interventions", []) or [] if i.get("name")]
                    if slug not in (row.drug_slugs or []):
                        row.drug_slugs = [*(row.drug_slugs or []), slug]
                session.flush()
        return seen
