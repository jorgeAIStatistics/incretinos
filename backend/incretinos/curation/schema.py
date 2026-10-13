"""Pydantic schema for the curated evidence files.

Curated files hold only what the structured APIs cannot supply: publication- and
release-reported results, mechanism and regulatory statements, and design annotations.
Every entry names its source and gives a short verbatim locator quote. The schema
rejects a numeric value whose digits do not appear in its own quote, so a number
cannot be entered without the text that supports it.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..enums import (
    AeCategory,
    AeMeasureKind,
    ClaimKind,
    DiabetesStatus,
    EndpointCategory,
    EndpointRole,
    EquivalenceBasis,
    EstimandLabel,
    EvidenceTier,
    HypothesisType,
    IceEvent,
    IceStrategy,
    MechanismAction,
    ReceptorTarget,
    RegulatoryStatus,
    TriState,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def number_tokens(value: float) -> list[str]:
    """Textual forms under which a number may appear in a source quote."""
    magnitude = abs(value)
    forms = {f"{magnitude:g}", f"{magnitude:.1f}", f"{magnitude:.2f}", f"{magnitude:.3f}"}
    if float(magnitude).is_integer():
        forms.add(str(int(magnitude)))
        forms.add(f"{int(magnitude):,}")
    return sorted(forms, key=len, reverse=True)


def number_in_quote(value: float | None, quote: str | None) -> bool:
    if value is None:
        return True
    if not quote:
        return False
    text = quote.replace("−", "-").replace("·", ".")
    for token in number_tokens(value):
        if re.search(rf"(?<![\d.]){re.escape(token)}(?![\d]|\.\d)", text):
            return True
    return False


class SourceDef(Strict):
    key: str
    tier: EvidenceTier
    title: str
    publisher: str | None = None
    url: str | None = None
    published_date: str | None = None
    retrieved_date: str
    license_note: str | None = None
    source_type: str = "WEB"


class Cited(Strict):
    source: str
    quote: str | None = None
    locator: str | None = None


class MechanismDef(Cited):
    target: ReceptorTarget
    action: MechanismAction
    description: str | None = None


class RegulatoryDef(Cited):
    status: RegulatoryStatus
    summary: str
    as_of: str


class IndicationDef(Cited):
    brand: str | None = None
    status: RegulatoryStatus
    text: str
    jurisdiction: str = "US"
    approval_date: str | None = None


class DoseDef(Strict):
    key: str
    amount: float
    unit: str = "mg"
    frequency: str | None = None
    role: str | None = None


class FormulationDef(Strict):
    key: str
    name: str
    dosage_form: str | None = None
    route: str | None = None
    stage: str
    notes: str | None = None
    source: str | None = None
    doses: list[DoseDef] = Field(default_factory=list)


class EquivalenceDef(Cited):
    a: str
    b: str
    basis: EquivalenceBasis
    statement: str


class DrugDef(Strict):
    slug: str
    name: str
    code_name: str | None = None
    brand_names: list[str] = Field(default_factory=list)
    sponsor: str | None = None
    modality: str | None = None
    route: str | None = None
    tier_group: str = "CORE"
    is_combination: bool = False
    component_slugs: list[str] = Field(default_factory=list)
    faers_generic: str | None = None
    label_brands: list[str] = Field(default_factory=list)
    label_manufacturer: str | None = "Eli Lilly"
    # For investigational drugs: generic name to watch for in the FDA label database. A hit raises a review item.
    label_watch_generic: str | None = None
    notes: str | None = None
    regulatory: RegulatoryDef
    mechanisms: list[MechanismDef] = Field(default_factory=list)
    indications: list[IndicationDef] = Field(default_factory=list)
    formulations: list[FormulationDef] = Field(default_factory=list)
    equivalences: list[EquivalenceDef] = Field(default_factory=list)


class StudyDrugDef(Strict):
    slug: str
    role: str = "INVESTIGATIONAL"


class PopulationOverride(Strict):
    diabetes_status: DiabetesStatus
    basis: str


class WatchStudy(Strict):
    nct: str
    display_name: str
    program: str | None = None
    drugs: list[StudyDrugDef]
    primary_pmid: str | None = None
    design_features: dict[str, Any] = Field(default_factory=dict)
    population: PopulationOverride | None = None


class Watchlist(Strict):
    studies: list[WatchStudy]
    discovery_terms: dict[str, list[str]] = Field(default_factory=dict)


class ArmAnnotation(Strict):
    label: str
    drug: str | None = None
    dose_amount: float | None = None
    dose_unit: str | None = "mg"
    dose_text: str | None = None
    formulation: str | None = None
    is_placebo: bool | None = None
    n_randomized: int | None = None
    arm_type: str | None = None


class IceDef(Strict):
    event: IceEvent
    strategy: IceStrategy
    quote: str | None = None


class EstimandDef(Cited):
    key: str
    label: EstimandLabel
    description: str
    strategies: list[IceDef] = Field(default_factory=list)


class EndpointDef(Strict):
    key: str
    title: str
    role: EndpointRole
    category: EndpointCategory
    threshold: float | None = None
    unit: str | None = None
    timepoint_weeks: float | None = None
    timeframe_text: str | None = None
    description: str | None = None
    hierarchy_rank: int | None = None
    multiplicity_controlled: TriState = TriState.NOT_STATED


class ResultDef(Cited):
    endpoint: str
    estimand: str | None = None
    arm: str
    scope: str = "ARM"
    param_type: str | None = None
    value: float | None = None
    value_low: float | None = None
    value_high: float | None = None
    unit: str | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    ci_level: float | None = None
    dispersion_type: str | None = None
    dispersion_value: float | None = None
    n_analyzed: int | None = None
    analysis_population: str | None = None
    analysis_method: str | None = None
    baseline_definition: str | None = None
    timepoint_weeks: float | None = None
    class_title: str | None = None
    notes: str | None = None
    # Numbers that legitimately come from a second quote (e.g. CI in a table) may be listed here.
    extra_quotes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _numbers_supported(self) -> ResultDef:
        text = " ".join([self.quote or "", *self.extra_quotes])
        for name in ("value", "value_low", "value_high", "ci_low", "ci_high"):
            number = getattr(self, name)
            if number is not None and not number_in_quote(number, text):
                raise ValueError(f"{name}={number} is not present in the locator quote(s) for arm '{self.arm}' ({self.endpoint})")
        if self.value is None and self.value_low is None and self.value_high is None:
            raise ValueError(f"result for arm '{self.arm}' ({self.endpoint}) has no value; omit it instead of entering an empty row")
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("confidence interval needs both limits or neither")
        return self


class AnalysisDef(Cited):
    endpoint: str
    estimand: str | None = None
    treatment: str
    comparator: str | None = None
    hypothesis_type: HypothesisType = HypothesisType.NOT_STATED
    param_type: str
    estimate: float | None = None
    unit: str | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    ci_level: float | None = None
    p_value_text: str | None = None
    method: str | None = None
    ni_margin: float | None = None
    ni_margin_text: str | None = None
    ni_met: TriState = TriState.NOT_STATED
    superiority_tested: TriState = TriState.NOT_STATED
    superiority_met: TriState = TriState.NOT_STATED
    multiplicity_controlled: TriState = TriState.NOT_STATED
    timepoint_weeks: float | None = None
    comment: str | None = None
    notes: str | None = None
    extra_quotes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _numbers_supported(self) -> AnalysisDef:
        text = " ".join([self.quote or "", *self.extra_quotes])
        for name in ("estimate", "ci_low", "ci_high", "ni_margin"):
            number = getattr(self, name)
            if number is not None and not number_in_quote(number, text):
                raise ValueError(f"{name}={number} is not present in the locator quote(s) for analysis '{self.treatment}' ({self.endpoint})")
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("confidence interval needs both limits or neither")
        return self


class AdverseEventDef(Cited):
    arm: str
    scope: str = "ARM"
    term: str
    category: AeCategory
    measure_kind: AeMeasureKind
    is_serious: bool = False
    severity: str | None = None
    n_affected: int | None = None
    n_at_risk: int | None = None
    percent: float | None = None
    percent_low: float | None = None
    percent_high: float | None = None
    period: str | None = None
    notes: str | None = None
    extra_quotes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _numbers_supported(self) -> AdverseEventDef:
        text = " ".join([self.quote or "", *self.extra_quotes])
        for name in ("percent", "percent_low", "percent_high"):
            number = getattr(self, name)
            if number is not None and not number_in_quote(number, text):
                raise ValueError(f"{name}={number} is not present in the locator quote(s) for AE '{self.term}' / '{self.arm}'")
        if self.scope == "RANGE_ACROSS_ARMS" and (self.percent_low is None or self.percent_high is None):
            raise ValueError("a range-across-arms entry needs percent_low and percent_high")
        return self


class ClaimDef(Cited):
    key: str
    topic: str
    statement: str
    claim_kind: ClaimKind = ClaimKind.DOCUMENTED
    drug: str | None = None


class StudyEvidenceFile(Strict):
    study: str
    arms: list[ArmAnnotation] = Field(default_factory=list)
    population_summary: dict[str, Any] = Field(default_factory=dict)
    estimands: list[EstimandDef] = Field(default_factory=list)
    endpoints: list[EndpointDef] = Field(default_factory=list)
    results: list[ResultDef] = Field(default_factory=list)
    analyses: list[AnalysisDef] = Field(default_factory=list)
    adverse_events: list[AdverseEventDef] = Field(default_factory=list)
    claims: list[ClaimDef] = Field(default_factory=list)


class SourcesFile(Strict):
    sources: list[SourceDef]


class DrugsFile(Strict):
    drugs: list[DrugDef]


class ClaimsFile(Strict):
    claims: list[ClaimDef]
