"""ClinicalTrials.gov API v2 adapter.

Fetches a study record, snapshots it (content-hashed, versioned), normalizes the
protocol section, and — when results are posted — extracts arm-level outcome results,
between-arm statistical analyses, adverse events and participant flow exactly as
reported. Nothing is imputed: a value the registry reports as "NA" stays NULL.

Three rules keep the registry's own statements intact:

* Arms are identified by their label as posted. Two labels are the same arm only when they are the same
  text (case, spacing and incidental punctuation aside) or when an order-tolerant hint points at exactly
  one arm; distinct arms are never merged. A PLACEBO_COMPARATOR arm is a placebo arm whatever drug its
  label also names, and a dose keeps the unit it was posted in (mg/kg is not mg).
* A between-arm analysis keeps the compared groups in posted order. Treatment and comparator are assigned
  from the registry's own contrast statement ("A vs B") or from the registry arm type; otherwise the
  reference is recorded as not stated instead of being guessed.
* Everything derived from the record (arms, estimands, results, analyses, adverse events, participant
  flow) is rebuilt on every ingest, so nothing from an earlier version of the record survives.

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

from ..engines.estimands import (
    _HYPOTHETICAL,
    _RESCUE_HYPOTHETICAL,
    _TREATMENT_POLICY,
    EstimandClassification,
    classify_estimand_text,
)
from ..enums import (
    AeCategory,
    AeMeasureKind,
    DiabetesStatus,
    EndpointRole,
    EstimandBasis,
    EstimandLabel,
    EvidenceTier,
    HypothesisType,
    IceEvent,
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
    ParticipantFlow,
    Population,
    Publication,
    StatisticalAnalysis,
    Study,
    StudyArm,
    StudyDrug,
    StudyPublication,
    StudyVersion,
)
from ..vocab import (
    COMBINATION_SLUGS,
    COMPARATOR_ARM_TYPES,
    SEQUENCE_ARM_TYPE,
    DirectionBasis,
    arm_drug_slug,
    arm_identity_key,
    categorize_endpoint,
    classify_arm_role,
    detect_drugs,
    is_sequence_arm,
    match_arm_label,
    norm_arm_label,
    parse_single_dose,
    parse_timeframe,
    tag_criterion,
    to_float,
    to_int,
)
from .snapshots import SnapshotOutcome, canonical_json, record_snapshot, sha256_text
from .transport import Transport

CTGOV_LICENSE = "ClinicalTrials.gov record (U.S. National Library of Medicine); U.S. Government work, stored in full."
NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)

RESULTS_GROUP = "RESULTS_GROUP"  # arm_type of a results group that is not one of the registered arm groups

# ---- multiplicity statements in analysis comments. YES needs an affirmative statement; a sentence that negates
# the same vocabulary ("No adjustments for multiplicity were made") is NO; anything else stays NOT_STATED.
_MULTIPLICITY_TOPIC = r"multiplicity|multiple (?:comparisons?|testing)|type[ -]?(?:1|i)\b error|family-?wise|graphical (?:approach|testing|procedure)|gatekeep|hierarchical testing"
_MULTIPLICITY_NEGATED = re.compile(
    rf"\b(?:no|not|without|never|neither|nor|outside|excluded from)\b[^.;]{{0,80}}(?:{_MULTIPLICITY_TOPIC})|(?:{_MULTIPLICITY_TOPIC})[^.;]{{0,40}}\b(?:not|no)\b|nominal p|unadjusted for multipl",
    re.IGNORECASE,
)
_MULTIPLICITY_AFFIRMED = re.compile(
    r"(?:adjust\w*|control\w*) for multipl|multiplicity adjust\w*|multiplicity[- ](?:adjusted|controlled)|"
    r"control\w* (?:the )?(?:overall |family-?wise |study-?wise )?type[ -]?(?:1|i)\b error|"
    r"type[ -]?(?:1|i)\b error (?:rate )?(?:was|is|were) (?:strongly )?controlled|"
    r"(?:tested|included|evaluated) (?:in|within|under|as part of) (?:the |a )?(?:graphical|gatekeeping|hierarchical|fixed-sequence)",
    re.IGNORECASE,
)


def multiplicity_from_comment(comment: str | None) -> TriState:
    """Whether an analysis comment says the test was inside the type I error control strategy."""
    affirmed = negated = False
    for sentence in re.split(r"(?<=[.;])\s+|\n+", comment or ""):
        if _MULTIPLICITY_NEGATED.search(sentence):
            negated = True
        elif _MULTIPLICITY_AFFIRMED.search(sentence):
            affirmed = True
    if negated and not affirmed:
        return TriState.NO
    if affirmed and not negated:
        return TriState.YES
    return TriState.NOT_STATED  # nothing said, or statements pointing both ways: not decided here


# ---- which group of a registry analysis is the treatment and which the comparator
_VS = re.compile(r"^\s*(.+?)\s+(?:vs\.?|versus)\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)
_UNIT_SLASH = re.compile(r"\b(mg|mcg|µg|g)\s*/\s*(kg)\b")


def _contrast_tokens(text: str) -> list[str]:
    text = re.sub(r"(?<=\d)(?=[a-zµ])", " ", text.lower())  # "30mg/kg" -> "30 mg/kg"
    text = _UNIT_SLASH.sub(r"\1per\2", text)
    return [t.strip(".") for t in re.split(r"[^a-z0-9.µ]+", text) if t.strip(".") and t not in {"and", "the", "of", "with", "plus"}]


def _side_score(side: str, group_title: str) -> float:
    """Share of the words of one side of 'A vs B' found in a group title (an abbreviation counts: 'Bima' ~ 'Bimagrumab')."""
    wanted, have = _contrast_tokens(side), _contrast_tokens(group_title)
    if not wanted:
        return 0.0

    def found(token: str) -> bool:
        return any(token == h or (token.isalpha() and len(token) >= 3 and h.startswith(token)) for h in have)

    return sum(found(t) for t in wanted) / len(wanted)


def stated_direction(group_description: str | None, titles: list[str]) -> tuple[int, int] | None:
    """(treatment index, comparator index) when the registry names the contrast as 'A vs B'.

    Both sides must be recognised completely in different groups, and the opposite assignment must fit worse;
    otherwise the description is not treated as a statement of direction.
    """
    match = _VS.match(group_description or "")
    if match is None or len(titles) != 2:
        return None
    a, b = match.group(1), match.group(2)
    a0, a1, b0, b1 = _side_score(a, titles[0]), _side_score(a, titles[1]), _side_score(b, titles[0]), _side_score(b, titles[1])
    if a0 == 1.0 and b1 == 1.0 and a0 + b1 > a1 + b0:
        return 0, 1
    if a1 == 1.0 and b0 == 1.0 and a1 + b0 > a0 + b1:
        return 1, 0
    return None


def _norm_text(text: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9≥≤<>%.]+", " ", (text or "").lower()).split())


def analysis_class(group_description: str | None, class_titles: list[str]) -> str | None:
    """The outcome class or category a registry analysis belongs to, when its groupDescription names one."""
    described = _norm_text(group_description)
    if not described:
        return None
    best = None
    for title in class_titles:
        wanted = _norm_text(title)
        if wanted and (described == wanted or described.startswith(wanted + " ")) and (best is None or len(wanted) > len(_norm_text(best))):
            best = title
    return best


def _matched_phrase(patterns: list[str] | None, *texts: str | None) -> str | None:
    text = " ".join(t for t in texts if t)
    for pattern in patterns or []:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def estimand_support(cls: EstimandClassification, *texts: str | None) -> str | None:
    """The phrase in THIS outcome's own text on which the estimand label rests (the classifier's match, no context)."""
    return _matched_phrase({EstimandLabel.EFFICACY: _HYPOTHETICAL, EstimandLabel.TREATMENT_REGIMEN: _TREATMENT_POLICY}.get(cls.label), *texts)


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


class _StudyArms:
    """The arms of one study during one ingest.

    Every registry-derived field of an arm is written from the current record the first time the arm is met in
    an ingest, so a value from an earlier version of the record cannot persist. Arm rows themselves are kept
    (curated evidence points at them) unless the record no longer contains them.
    """

    def __init__(self, session: Session, study: Study):
        self.session = session
        self.study = study
        self.arms: list[StudyArm] = list(session.scalars(select(StudyArm).where(StudyArm.study_id == study.id).order_by(StudyArm.id)))
        self.seen: set[int] = set()  # id() of arms already rewritten from this record

    # -- lookup
    def _find(self, label: str) -> StudyArm | None:
        # An identical label is the same arm. A differently worded one may attach to a registry arm through the
        # order-tolerant hint, but only when exactly one arm fits; it never attaches to a curated-only arm.
        registry = [i for i, a in enumerate(self.arms) if a.origin == Origin.REGISTRY.value]
        index = match_arm_label(label, [a.label for a in self.arms], hint_among=registry)
        return None if index is None else self.arms[index]

    def _new(self, label: str) -> StudyArm:
        arm = StudyArm(study_id=self.study.id, origin=Origin.REGISTRY.value, label=label[:300], norm_label=norm_arm_label(label))
        self.session.add(arm)
        self.arms.append(arm)
        return arm

    # -- registry-derived fields
    @staticmethod
    def _describe(arm: StudyArm, *, arm_type: str | None, description: str | None, interventions: list[str]) -> None:
        """Set every registry-derived field of an arm from its label, registry type and interventions."""
        label = arm.label
        named = detect_drugs(label)
        # A label that names no drug (e.g. "Dose 1") is characterised by the arm's interventions instead.
        drug_slug, is_placebo = arm_drug_slug(label if named else " + ".join(interventions))
        dose = parse_single_dose(label) if _single_drug(drug_slug) else None
        sequence_text = None
        stored_type = arm_type
        if is_sequence_arm(label, description, arm_type):
            # Registered as the placebo comparator, but the arm receives placebo in one period and an active drug in
            # another ("Placebo/30 mg/kg Bimagrumab": placebo to week 40, bimagrumab at weeks 52 and 64). It is
            # neither a placebo arm nor an arm of that drug: no drug, no dose, not placebo; the registered type is
            # kept in registered_arm_type and the sequence in dose_text.
            stored_type, drug_slug, is_placebo, dose = SEQUENCE_ARM_TYPE, None, False, None
            sequence_text = f"treatment sequence as registered (registered arm type PLACEBO_COMPARATOR): {description or label}"
        elif arm_type == "PLACEBO_COMPARATOR":
            # The registry classifies the arm as the placebo comparator; a matching placebo named after a drug
            # ("Bimagrumab Placebo + Tirzepatide Placebo") does not make it an arm of that drug.
            if not is_placebo:
                sequence_text = f"placebo-comparator arm as registered; arm label: {label}"
            drug_slug, is_placebo, dose = "placebo", True, None
        arm.origin = Origin.REGISTRY.value
        arm.norm_label = norm_arm_label(label)
        arm.arm_type = stored_type
        arm.registered_arm_type = arm_type if arm_type != RESULTS_GROUP else None
        arm.description = description
        arm.interventions = list(interventions or [])
        arm.drug_slug = drug_slug
        arm.is_placebo = is_placebo
        arm.is_pooled = bool(re.search(r"pooled|all (tirzepatide|orforglipron|retatrutide|eloralintide)|overall", label, re.IGNORECASE))
        arm.dose_amount, arm.dose_unit = dose if dose is not None else (None, None)
        arm.dose_text = sequence_text or ("maximum tolerated dose" if re.search(r"\bMTD\b", label) else None)
        arm.n_randomized = None  # set again from participant flow, if the record posts one

    def registered(self, label: str, *, arm_type: str | None, description: str | None, interventions: list[str] | None) -> StudyArm:
        """A registered arm group. Each one is its own arm: only an identical label reuses an existing row."""
        identity = arm_identity_key(label)
        arm = next((a for a in self.arms if arm_identity_key(a.label) == identity), None)
        if arm is None:
            arm = self._new(label)
        if id(arm) not in self.seen:
            arm.label = label[:300]
            self._describe(arm, arm_type=arm_type, description=description, interventions=list(interventions or []))
            self.seen.add(id(arm))
        return arm

    def results_group(self, title: str, *, description: str | None = None) -> StudyArm:
        """A group of the results section: the registered arm with that label when there is one, else its own row."""
        arm = self._find(title)
        if arm is None:
            arm = self._new(title)
        if id(arm) not in self.seen:
            self._describe(arm, arm_type=RESULTS_GROUP, description=description, interventions=[])
            self.seen.add(id(arm))
        elif not arm.description and description:
            arm.description = description
        return arm

    def retire_unused(self) -> None:
        """Arms the current record no longer contains: delete them, or hand them to the curated rows that use them."""
        stale = [a for a in self.arms if a.origin == Origin.REGISTRY.value and id(a) not in self.seen and a.id is not None]
        if not stale:
            return
        curated = Origin.CURATED.value
        used: set[int] = set()
        for model, columns in ((OutcomeResult, ("arm_id",)), (AdverseEvent, ("arm_id",)), (StatisticalAnalysis, ("treatment_arm_id", "comparator_arm_id"))):
            for column in columns:
                attr = getattr(model, column)
                used |= {x for (x,) in self.session.execute(select(attr).where(model.study_id == self.study.id, model.origin == curated, attr.is_not(None)))}
        for arm in stale:
            if arm.id in used:
                # Still cited by curated evidence: keep the row, but nothing registry-derived stays on it.
                drug_slug, is_placebo = arm_drug_slug(arm.label)
                dose = parse_single_dose(arm.label) if _single_drug(drug_slug) else None
                arm.origin, arm.arm_type, arm.description, arm.interventions, arm.n_randomized = curated, "CURATED", None, [], None
                arm.registered_arm_type = None
                arm.drug_slug, arm.is_placebo = drug_slug, is_placebo
                arm.dose_amount, arm.dose_unit = dose if dose is not None else (None, None)
            else:
                self.session.delete(arm)
                self.arms.remove(arm)


def assign_arm_roles(session: Session, study: Study) -> None:
    """Store every arm's role (``vocab.classify_arm_role``) from its stored data and the trial's drug roles.

    Run after the registry ingest, after the watchlist sets the trial's drug roles and after curated arms are
    created, so the stored role always reflects the current arm data.
    """
    drug_roles = {link.drug.slug: link.role for link in session.scalars(select(StudyDrug).where(StudyDrug.study_id == study.id))}
    for arm in session.scalars(select(StudyArm).where(StudyArm.study_id == study.id)):
        role, basis = classify_arm_role(arm_type=arm.arm_type, is_placebo=arm.is_placebo, drug_slug=arm.drug_slug, label=arm.label, drug_roles=drug_roles)
        arm.arm_role, arm.arm_role_basis = role.value, basis
    session.flush()


def _single_drug(drug_slug: str | None) -> bool:
    """True for one active drug: not placebo, not an ad-hoc combination ('a+b'), not a tracked fixed combination."""
    return bool(drug_slug) and drug_slug != "placebo" and "+" not in drug_slug and drug_slug not in COMBINATION_SLUGS.values()


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
        arms = _StudyArms(session, study)
        for arm in parsed.arms:
            arms.registered(arm.get("label", "Unnamed arm"), arm_type=arm.get("type"), description=arm.get("description"), interventions=arm.get("interventionNames", []))
        session.flush()
        self._upsert_population(session, study, parsed, source)
        self._insert_eligibility(session, study, parsed)
        self._insert_protocol_endpoints(session, study, parsed, source)
        self._link_references(session, study, parsed)
        if parsed.has_results and "resultsSection" in parsed.raw:
            self._extract_results(session, study, parsed, source, arms)
        arms.retire_unused()
        session.flush()
        assign_arm_roles(session, study)
        return study

    @staticmethod
    def _replace_registry_children(session: Session, study: Study) -> None:
        """Registry-derived rows are rebuilt on each ingest; curated rows are untouched.

        Estimands derived from registry text are rebuilt too. Arms are not deleted here, because curated rows
        point at them: ``_StudyArms`` rewrites every registry-derived field of each arm from the current record
        and retires arms the record no longer contains.
        """
        reg = Origin.REGISTRY.value
        session.execute(delete(OutcomeResult).where(OutcomeResult.study_id == study.id, OutcomeResult.origin == reg))
        session.execute(delete(StatisticalAnalysis).where(StatisticalAnalysis.study_id == study.id, StatisticalAnalysis.origin == reg))
        session.execute(delete(AdverseEvent).where(AdverseEvent.study_id == study.id, AdverseEvent.origin == reg))
        session.execute(delete(ParticipantFlow).where(ParticipantFlow.study_id == study.id, ParticipantFlow.origin == reg))
        session.execute(delete(Endpoint).where(Endpoint.study_id == study.id, Endpoint.origin == reg))
        session.execute(
            delete(IntercurrentEventStrategy).where(
                IntercurrentEventStrategy.estimand_id.in_(select(Estimand.id).where(Estimand.study_id == study.id, Estimand.origin == reg))
            )
        )
        session.execute(delete(Estimand).where(Estimand.study_id == study.id, Estimand.origin == reg))
        session.execute(delete(InclusionCriterion).where(InclusionCriterion.study_id == study.id))
        session.execute(delete(ExclusionCriterion).where(ExclusionCriterion.study_id == study.id))
        session.flush()
        session.expire(study)

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
        """The estimand record for one outcome measure.

        Records are shared only between outcomes whose own text carries the same supporting phrase, so the
        quote on a result's estimand always occurs in that result's analysis-population or description text.
        """
        cls = classify_estimand_text(*texts)
        support = estimand_support(cls, *texts)
        rescue = _matched_phrase(_RESCUE_HYPOTHETICAL, *texts)
        strategies = tuple(sorted((f.event.value, f.strategy.value) for f in cls.strategies))
        key = (cls.label.value, cls.basis.value, (support or "").lower(), strategies, (rescue or "").lower() if strategies else "")
        estimand = cache.get(key)
        if estimand is None:
            estimand = Estimand(
                study_id=study.id,
                origin=Origin.REGISTRY.value,
                label=cls.label.value,
                basis=cls.basis.value,
                description=cls.rationale,
                source_id=source.id,
                quote=support,
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
                        # the matched phrase itself, so it holds for every outcome that shares this record
                        quote=(rescue if finding.event == IceEvent.PROHIBITED_OR_RESCUE_THERAPY else support) or finding.quote,
                    )
                )
        cache[key] = estimand
        missing = ["estimand_label"] if cls.label == EstimandLabel.NOT_STATED else []
        if cls.basis == EstimandBasis.INFERRED_FROM_ANALYSIS_DESCRIPTION:
            missing.append("estimand_label_stated_by_source")
        return estimand, missing

    def _extract_results(self, session: Session, study: Study, parsed: ParsedStudy, source: EvidenceSource, arms: _StudyArms) -> None:
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

            arm_for_group: dict[str, StudyArm] = {gid: arms.results_group(group.get("title", gid), description=group.get("description")) for gid, group in groups.items()}
            session.flush()

            class_titles: list[str] = []
            for c_idx, cls in enumerate(om.get("classes", [])):
                class_denoms = self._denoms(cls.get("denoms")) or measure_denoms
                for cat_idx, cat in enumerate(cls.get("categories", [])):
                    class_title = " — ".join(filter(None, [cls.get("title"), cat.get("title")])) or None
                    class_titles += [t for t in (class_title, cls.get("title"), cat.get("title")) if t]
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
                        if weeks is None:
                            missing.append("timepoint_weeks")
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
                self._add_analysis(session, study, endpoint, estimand, analysis, groups, arm_for_group, source, f"{base_loc}.analyses[{a_idx}]", weeks, class_titles)

        self._extract_adverse_events(session, study, results, source, arms)
        self._extract_participant_flow(session, study, results, source, arms)

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

    @staticmethod
    def _direction(group_description: str | None, labels: list[str], arms: list[StudyArm | None]) -> tuple[int, int, DirectionBasis, str | None]:
        """(treatment index, comparator index, basis, caveat) for a two-group registry analysis.

        The registry posts the two groups in an order that carries no meaning by itself. Treatment and
        comparator are assigned only on a statement the registry makes; failing that the posted order is kept
        and the basis says the reference group is not stated.
        """
        stated = stated_direction(group_description, labels)
        if stated is not None:
            return stated[0], stated[1], DirectionBasis.GROUP_DESCRIPTION, None
        types = [a.arm_type if a is not None else None for a in arms]
        for kinds in ({"PLACEBO_COMPARATOR"}, COMPARATOR_ARM_TYPES):
            is_reference = [t in kinds for t in types]
            if is_reference[0] != is_reference[1]:
                comp = 0 if is_reference[0] else 1
                other = types[1 - comp]
                if other == RESULTS_GROUP or other is None:
                    break  # the other group is not a registered arm: its role is unknown
                return 1 - comp, comp, DirectionBasis.ARM_TYPE, (
                    f"Reference group taken from the registry arm type ({types[comp]}); the analysis itself does not state the direction of the contrast."
                )
        placebo = [bool(a is not None and a.is_placebo) for a in arms]
        if placebo[0] != placebo[1]:
            comp = 0 if placebo[0] else 1
            return 1 - comp, comp, DirectionBasis.PLACEBO_GROUP, (
                "Reference group taken to be the placebo group; the analysis itself does not state the direction of the contrast."
            )
        return 0, 1, DirectionBasis.NOT_STATED, (
            "The registry does not state which group is the reference: the two groups are listed in posted order, "
            "and the sign of the estimate cannot be read as 'first minus second' or the reverse."
        )

    def _add_analysis(self, session, study, endpoint, estimand, analysis, groups, arm_for_group, source, location, weeks, class_titles=()) -> None:
        gids = analysis.get("groupIds", [])
        labels = [groups.get(g, {}).get("title", g) for g in gids]
        group_description = (analysis.get("groupDescription") or "").strip() or None
        treatment_id = comparator_id = None
        treatment_label = comparator_label = None
        if len(gids) == 2:
            arms = [arm_for_group.get(g) for g in gids]
            trt_idx, comp_idx, basis, note = self._direction(group_description, labels, arms)
            treatment_label, comparator_label = labels[trt_idx], labels[comp_idx]
            treatment_id = arms[trt_idx].id if arms[trt_idx] else None
            comparator_id = arms[comp_idx].id if arms[comp_idx] else None
        else:
            basis = DirectionBasis.NOT_STATED
            treatment_label = " vs ".join(labels)
            note = f"Analysis spans {len(gids)} groups."
        # A groupDescription that is not a contrast statement names what was analysed (a class of the outcome).
        class_title = None if basis == DirectionBasis.GROUP_DESCRIPTION else analysis_class(group_description, list(class_titles))

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
        multiplicity = multiplicity_from_comment(comments)

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
        if basis == DirectionBasis.NOT_STATED:
            missing.append("reference_group")
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
                group_labels=labels,
                direction_basis=basis.value,
                group_description=group_description,
                class_title=class_title,
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
                # The margin as the registry words it; no number is parsed out of free text.
                ni_margin_text=analysis.get("nonInferiorityComment") if hypothesis == HypothesisType.NON_INFERIORITY else None,
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

    def _extract_adverse_events(self, session, study, results, source, arms: _StudyArms) -> None:
        """Deaths, serious and other adverse events exactly as posted — every posted term, for every group."""
        module = results.get("adverseEventsModule")
        if not module:
            return
        tier = EvidenceTier.REGISTRY_RESULTS.value
        machine = VerificationStatus.MACHINE_EXTRACTED.value
        period = module.get("timeFrame")
        threshold = module.get("frequencyThreshold")
        groups = {g["id"]: g for g in module.get("eventGroups", [])}
        arm_for_group = {gid: arms.results_group(g.get("title", gid), description=g.get("description")) for gid, g in groups.items()}
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
                is_gi = (organ or "").lower().startswith("gastrointestinal")
                category = AeCategory.GASTROINTESTINAL if is_gi else (AeCategory.SERIOUS if serious else AeCategory.OTHER)
                if serious:
                    notes = "Serious adverse event table: participants with a SERIOUS event of this term (a different table from the non-serious events)."
                else:
                    notes = f"Non-serious event table; listed because frequency exceeded the registry reporting threshold ({threshold}%)." if threshold else "Non-serious event table."
                for gid, stat in stats.items():
                    if gid not in groups:
                        continue
                    add(
                        gid, term, category, AeMeasureKind.PARTICIPANTS_WITH_EVENT,
                        affected=stat.get("numAffected"), at_risk=stat.get("numAtRisk"), events=stat.get("numEvents"),
                        organ=organ, serious=serious, location=f"{base}.{key}[{e_idx}]", notes=notes,
                    )

    def _extract_participant_flow(self, session, study, results, source, arms: _StudyArms) -> None:
        """Participant flow as posted: every milestone and every withdrawal reason, with its denominator.

        All rows go to ``participant_flow``. Two summaries are also kept as adverse-event-table rows, as before,
        because the safety views read them there: non-completion for any reason and non-completion with the
        reason "Adverse Event" (study withdrawal — not treatment discontinuation).
        """
        module = results.get("participantFlowModule")
        if not module:
            return
        groups = {g["id"]: g for g in module.get("groups", [])}
        arm_for_group = {gid: arms.results_group(g.get("title", gid), description=g.get("description")) for gid, g in groups.items()}
        session.flush()
        tier = EvidenceTier.REGISTRY_RESULTS.value
        machine = VerificationStatus.MACHINE_EXTRACTED.value
        for p_idx, period in enumerate(module.get("periods", [])):
            location = f"resultsSection.participantFlowModule.periods[{p_idx}]"
            title = period.get("title") or f"Period {p_idx + 1}"
            started: dict[str, int | None] = {}
            for milestone in period.get("milestones", []):
                if milestone.get("type") == "STARTED":
                    started = {a.get("groupId"): to_int(a.get("numSubjects")) for a in milestone.get("achievements", [])}
            # A group the registry posts as STARTED 0 is not part of the period (e.g. randomized arms during a
            # lead-in): its posted zeros are structural and are not stored as "0 of 0". Only a posted zero excludes a
            # group: a STARTED count that is missing or cannot be read stays NULL and the group's rows are kept.
            in_period = {gid for gid in groups if started.get(gid) != 0}
            for gid in in_period:
                # Participants entering the study in this group: the first period in which the group has any.
                if gid in arm_for_group and arm_for_group[gid].n_randomized is None and started.get(gid):
                    arm_for_group[gid].n_randomized = started[gid]

            def flow(gid, kind, label, n, *, comment=None, where="", _started=started, _p_idx=p_idx, _title=title):
                at_risk = _started.get(gid)
                session.add(
                    ParticipantFlow(
                        study_id=study.id,
                        arm_id=arm_for_group[gid].id,
                        origin=Origin.REGISTRY.value,
                        arm_label=groups[gid].get("title", gid),
                        period_index=_p_idx,
                        period_title=_title,
                        kind=kind,
                        label=label,
                        n_participants=n,
                        n_started=at_risk,
                        comment=comment,
                        source_id=source.id,
                        source_location=where,
                        tier=tier,
                        verification_status=machine,
                        missing_fields=[f for f, v in (("n_participants", n), ("n_started", at_risk)) if v is None],
                    )
                )

            def summary(gid, term, kind, affected, _started=started, _location=location, _title=title):
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

            for m_idx, milestone in enumerate(period.get("milestones", [])):
                kind = milestone.get("type") or "Milestone"
                for achievement in milestone.get("achievements", []):
                    gid = achievement.get("groupId")
                    if gid not in groups or gid not in in_period:
                        continue
                    n = to_int(achievement.get("numSubjects"))
                    comment = "; ".join(filter(None, [milestone.get("comment"), achievement.get("comment")])) or None
                    flow(gid, "MILESTONE", kind, n, comment=comment, where=f"{location}.milestones[{m_idx}]")
                    if kind == "NOT COMPLETED":
                        summary(gid, "Did not complete study period (any reason)", AeMeasureKind.DISCONTINUATION_ANY_REASON, n)
            for d_idx, drop in enumerate(period.get("dropWithdraws", [])):
                reason_type = (drop.get("type") or "").strip() or "Reason not stated"
                for reason in drop.get("reasons", []):
                    gid = reason.get("groupId")
                    if gid not in groups or gid not in in_period:
                        continue
                    n = to_int(reason.get("numSubjects"))
                    comment = "; ".join(filter(None, [drop.get("comment"), reason.get("comment")])) or None
                    flow(gid, "WITHDRAWAL_REASON", reason_type, n, comment=comment, where=f"{location}.dropWithdraws[{d_idx}]")
                    if reason_type.lower() == "adverse event":
                        summary(gid, "Did not complete study period — reason: adverse event", AeMeasureKind.STUDY_WITHDRAWAL_DUE_TO_AE, n)

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
