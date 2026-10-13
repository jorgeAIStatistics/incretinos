"""Controlled vocabularies.

These enums are the backbone of the platform's epistemic discipline: every stored
number or statement carries an evidence tier (where it came from), an epistemic
kind (documented vs inferred vs hypothetical), and a verification status (how it
was checked). They are plain string enums so they serialize identically in SQLite,
PostgreSQL, JSON and YAML.
"""

from __future__ import annotations

from enum import StrEnum


class EvidenceTier(StrEnum):
    """Provenance class of a source, ordered roughly by regulatory/scientific scrutiny."""

    REGULATORY_LABEL = "REGULATORY_LABEL"  # FDA-approved prescribing information
    REGULATORY_REVIEW = "REGULATORY_REVIEW"  # FDA review documents
    PEER_REVIEWED = "PEER_REVIEWED"  # journal article
    REGISTRY_RESULTS = "REGISTRY_RESULTS"  # sponsor-posted ClinicalTrials.gov results (NLM QC, not peer review)
    REGISTRY_PROTOCOL = "REGISTRY_PROTOCOL"  # registered design information
    PRESS_RELEASE = "PRESS_RELEASE"  # sponsor topline disclosure
    SPONSOR_PIPELINE = "SPONSOR_PIPELINE"  # sponsor pipeline page
    CONFERENCE = "CONFERENCE"  # congress abstract / presentation
    GUIDANCE = "GUIDANCE"  # ICH / FDA guidance
    SPONTANEOUS_REPORTS = "SPONTANEOUS_REPORTS"  # FAERS via openFDA
    SECONDARY = "SECONDARY"  # news or other secondary reporting


TIER_RANK: dict[EvidenceTier, int] = {
    EvidenceTier.REGULATORY_LABEL: 1,
    EvidenceTier.REGULATORY_REVIEW: 1,
    EvidenceTier.PEER_REVIEWED: 2,
    EvidenceTier.REGISTRY_RESULTS: 3,
    EvidenceTier.REGISTRY_PROTOCOL: 3,
    EvidenceTier.CONFERENCE: 4,
    EvidenceTier.PRESS_RELEASE: 5,
    EvidenceTier.SPONSOR_PIPELINE: 5,
    EvidenceTier.GUIDANCE: 1,
    EvidenceTier.SPONTANEOUS_REPORTS: 6,
    EvidenceTier.SECONDARY: 7,
}

TIER_LABEL: dict[EvidenceTier, str] = {
    EvidenceTier.REGULATORY_LABEL: "FDA prescribing information",
    EvidenceTier.REGULATORY_REVIEW: "FDA review document",
    EvidenceTier.PEER_REVIEWED: "Peer-reviewed publication",
    EvidenceTier.REGISTRY_RESULTS: "Registry-posted results (sponsor-submitted)",
    EvidenceTier.REGISTRY_PROTOCOL: "Registry protocol record",
    EvidenceTier.PRESS_RELEASE: "Sponsor press release (topline)",
    EvidenceTier.SPONSOR_PIPELINE: "Sponsor pipeline disclosure",
    EvidenceTier.CONFERENCE: "Conference presentation",
    EvidenceTier.GUIDANCE: "Regulatory guidance",
    EvidenceTier.SPONTANEOUS_REPORTS: "Spontaneous adverse-event reports",
    EvidenceTier.SECONDARY: "Secondary reporting",
}


class ClaimKind(StrEnum):
    """Epistemic class of a statement — never collapse these in the UI."""

    DOCUMENTED = "DOCUMENTED"  # reported by the cited source
    INFERENCE = "INFERENCE"  # deterministic calculation / rule applied to documented values
    HYPOTHESIS = "HYPOTHESIS"  # plausible but untested
    SIMULATION = "SIMULATION"  # output of a synthetic scenario
    UNSUPPORTED = "UNSUPPORTED"  # no supporting source located


class VerificationStatus(StrEnum):
    MACHINE_EXTRACTED = "MACHINE_EXTRACTED"  # parsed from a structured API payload whose snapshot hash is stored
    QUOTE_ANCHORED = "QUOTE_ANCHORED"  # curated value whose locator quote was found in the source text
    PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"  # no mechanical support could be established; awaits a human decision
    CURATED_PENDING_REVIEW = "CURATED_PENDING_REVIEW"  # legacy name of PENDING_HUMAN_REVIEW (stored values are migrated)
    HUMAN_VERIFIED = "HUMAN_VERIFIED"  # approved by an explicit, authorized human decision on this exact evidence version
    REJECTED = "REJECTED"  # rejected by a human decision; excluded from comparisons, summaries and reports
    SOURCE_CHANGED_RECHECK_REQUIRED = "SOURCE_CHANGED_RECHECK_REQUIRED"  # reviewed earlier, but the evidence or its source has changed since
    CONFLICT = "CONFLICT"  # unused legacy value; cross-source disagreement is tracked by the conflict engine, not as a status


class Origin(StrEnum):
    REGISTRY = "REGISTRY"  # produced by an ingestion adapter
    CURATED = "CURATED"  # produced from the curated evidence files


class RegulatoryStatus(StrEnum):
    APPROVED = "APPROVED"
    INVESTIGATIONAL = "INVESTIGATIONAL"
    SUBMITTED = "SUBMITTED"
    DISCONTINUED = "DISCONTINUED"
    UNKNOWN = "UNKNOWN"


class ReceptorTarget(StrEnum):
    GLP1R = "GLP1R"
    GIPR = "GIPR"
    GCGR = "GCGR"
    AMYR = "AMYR"  # amylin receptors (calcitonin receptor + RAMP complexes)
    CTR = "CTR"  # calcitonin receptor
    ACTRII = "ACTRII"  # activin type II receptors
    OTHER = "OTHER"


class MechanismAction(StrEnum):
    AGONIST = "AGONIST"
    ANTAGONIST = "ANTAGONIST"
    ANTIBODY_BLOCKADE = "ANTIBODY_BLOCKADE"
    OTHER = "OTHER"


class EndpointRole(StrEnum):
    PRIMARY = "PRIMARY"
    KEY_SECONDARY = "KEY_SECONDARY"
    SECONDARY = "SECONDARY"
    OTHER_PRESPECIFIED = "OTHER_PRESPECIFIED"
    EXPLORATORY = "EXPLORATORY"
    POST_HOC = "POST_HOC"
    NOT_STATED = "NOT_STATED"


class TriState(StrEnum):
    YES = "YES"
    NO = "NO"
    NOT_STATED = "NOT_STATED"


class EndpointCategory(StrEnum):
    WEIGHT_PCT_CHANGE = "WEIGHT_PCT_CHANGE"
    WEIGHT_ABS_CHANGE = "WEIGHT_ABS_CHANGE"
    WEIGHT_RESPONDER = "WEIGHT_RESPONDER"
    BMI_CHANGE = "BMI_CHANGE"
    WAIST_CHANGE = "WAIST_CHANGE"
    HBA1C_CHANGE = "HBA1C_CHANGE"
    HBA1C_RESPONDER = "HBA1C_RESPONDER"
    FASTING_GLUCOSE = "FASTING_GLUCOSE"
    BLOOD_PRESSURE = "BLOOD_PRESSURE"
    LIPIDS = "LIPIDS"
    MACE = "MACE"
    MORTALITY = "MORTALITY"
    CV_COMPONENT = "CV_COMPONENT"
    KIDNEY = "KIDNEY"
    BODY_COMPOSITION = "BODY_COMPOSITION"
    PHYSICAL_FUNCTION = "PHYSICAL_FUNCTION"
    PATIENT_REPORTED = "PATIENT_REPORTED"
    DIABETES_ONSET = "DIABETES_ONSET"
    PAIN = "PAIN"
    PHARMACOKINETICS = "PHARMACOKINETICS"
    OTHER = "OTHER"


# Endpoint categories regarded as surrogate/intermediate rather than clinical-event outcomes.
SURROGATE_CATEGORIES = {
    EndpointCategory.WEIGHT_PCT_CHANGE,
    EndpointCategory.WEIGHT_ABS_CHANGE,
    EndpointCategory.WEIGHT_RESPONDER,
    EndpointCategory.BMI_CHANGE,
    EndpointCategory.WAIST_CHANGE,
    EndpointCategory.HBA1C_CHANGE,
    EndpointCategory.HBA1C_RESPONDER,
    EndpointCategory.FASTING_GLUCOSE,
    EndpointCategory.BLOOD_PRESSURE,
    EndpointCategory.LIPIDS,
}


class EstimandLabel(StrEnum):
    """Sponsor-style estimand names used across Lilly incretin programmes (ICH E9(R1))."""

    EFFICACY = "EFFICACY"  # hypothetical strategy: effect had all participants remained on treatment
    TREATMENT_REGIMEN = "TREATMENT_REGIMEN"  # treatment-policy strategy: effect regardless of discontinuation
    OTHER = "OTHER"
    NOT_STATED = "NOT_STATED"


class IceStrategy(StrEnum):
    HYPOTHETICAL = "HYPOTHETICAL"
    TREATMENT_POLICY = "TREATMENT_POLICY"
    COMPOSITE = "COMPOSITE"
    WHILE_ON_TREATMENT = "WHILE_ON_TREATMENT"
    PRINCIPAL_STRATUM = "PRINCIPAL_STRATUM"
    NOT_STATED = "NOT_STATED"


class IceEvent(StrEnum):
    TREATMENT_DISCONTINUATION = "TREATMENT_DISCONTINUATION"
    PROHIBITED_OR_RESCUE_THERAPY = "PROHIBITED_OR_RESCUE_THERAPY"
    DEATH = "DEATH"
    OTHER = "OTHER"


class EstimandBasis(StrEnum):
    STATED_BY_SOURCE = "STATED_BY_SOURCE"
    INFERRED_FROM_ANALYSIS_DESCRIPTION = "INFERRED_FROM_ANALYSIS_DESCRIPTION"
    NOT_DETERMINABLE = "NOT_DETERMINABLE"


class HypothesisType(StrEnum):
    SUPERIORITY = "SUPERIORITY"
    NON_INFERIORITY = "NON_INFERIORITY"
    EQUIVALENCE = "EQUIVALENCE"
    OTHER = "OTHER"
    NOT_STATED = "NOT_STATED"


class DiabetesStatus(StrEnum):
    NO_T2D = "NO_T2D"
    T2D = "T2D"
    MIXED = "MIXED"
    NOT_STATED = "NOT_STATED"


class AeMeasureKind(StrEnum):
    """What an adverse-event number actually counts. These must never share an axis."""

    PARTICIPANTS_WITH_EVENT = "PARTICIPANTS_WITH_EVENT"  # incidence proportion within a trial arm
    # Participants who stopped study TREATMENT because of any adverse event (as reported in publications/releases).
    TREATMENT_DISCONTINUATION_DUE_TO_AE = "TREATMENT_DISCONTINUATION_DUE_TO_AE"
    # Participants who did not complete the STUDY (period) with "adverse event" as the reason (registry participant flow).
    STUDY_WITHDRAWAL_DUE_TO_AE = "STUDY_WITHDRAWAL_DUE_TO_AE"
    DISCONTINUATION_ANY_REASON = "DISCONTINUATION_ANY_REASON"
    DEATHS = "DEATHS"


AE_KIND_LABEL: dict[str, str] = {
    "PARTICIPANTS_WITH_EVENT": "Participants with ≥1 event (within-trial incidence proportion)",
    "TREATMENT_DISCONTINUATION_DUE_TO_AE": "Discontinued study treatment because of an adverse event",
    "STUDY_WITHDRAWAL_DUE_TO_AE": "Did not complete the study period; reason recorded as adverse event",
    "DISCONTINUATION_ANY_REASON": "Did not complete the study period (any reason)",
    "DEATHS": "Deaths (all causes)",
}

DISCONTINUATION_KINDS = {
    "TREATMENT_DISCONTINUATION_DUE_TO_AE",
    "STUDY_WITHDRAWAL_DUE_TO_AE",
    "DISCONTINUATION_ANY_REASON",
}


class AeCategory(StrEnum):
    GASTROINTESTINAL = "GASTROINTESTINAL"
    SERIOUS = "SERIOUS"
    DEATH = "DEATH"
    DISCONTINUATION = "DISCONTINUATION"
    OTHER = "OTHER"


class ComparabilityClass(StrEnum):
    DIRECT_RANDOMIZED_COMPARISON = "DIRECT RANDOMIZED COMPARISON"
    PARTIALLY_COMPARABLE = "PARTIALLY COMPARABLE"
    DESCRIPTIVE_CROSS_STUDY = "DESCRIPTIVE CROSS-STUDY COMPARISON"
    NOT_SUITABLE = "NOT SUITABLE FOR EFFICACY COMPARISON"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT EVIDENCE"


class FactorAssessment(StrEnum):
    MATCH = "MATCH"
    MINOR_DIFFERENCE = "MINOR_DIFFERENCE"
    MAJOR_DIFFERENCE = "MAJOR_DIFFERENCE"
    BLOCKING = "BLOCKING"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EquivalenceBasis(StrEnum):
    DOCUMENTED_BIOEQUIVALENCE = "DOCUMENTED_BIOEQUIVALENCE"
    LABEL_STATEMENT = "LABEL_STATEMENT"
    SPONSOR_STATEMENT = "SPONSOR_STATEMENT"
    NOT_DOCUMENTED = "NOT_DOCUMENTED"


class GapNature(StrEnum):
    """Why a question is open — a public reporting gap is not proof that data do not exist."""

    PUBLIC_REPORTING_GAP = "PUBLIC_REPORTING_GAP"  # study completed; results not (fully) public
    EVIDENCE_IN_GENERATION = "EVIDENCE_IN_GENERATION"  # a registered study is ongoing
    NOT_KNOWN_TO_BE_STUDIED = "NOT_KNOWN_TO_BE_STUDIED"  # no registered study located in tracked sources
    METHODOLOGICAL = "METHODOLOGICAL"  # data exist but cannot answer the question as analysed
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    RECORD_CHANGE = "RECORD_CHANGE"


class ReviewStatus(StrEnum):
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"
    RESOLVED = "RESOLVED"


class LinkBasis(StrEnum):
    """How a publication was tied to a study."""

    REGISTRY_REFERENCE = "REGISTRY_REFERENCE"  # listed in the registry record's references module
    PUBMED_DATABANK = "PUBMED_DATABANK"  # NCT accession in the PubMed record's DataBank list
    ABSTRACT_MENTION = "ABSTRACT_MENTION"  # NCT id appears in abstract text
    CURATED = "CURATED"


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    RESTRICTED = "RESTRICTED"  # reserved for future authorized sponsor data; never ingested in this build
