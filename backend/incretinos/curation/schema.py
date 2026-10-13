"""Pydantic schema for the curated evidence files.

Curated files hold only what the structured APIs cannot supply: publication- and
release-reported results, mechanism and regulatory statements, and design annotations.
Every entry names its source and gives a short verbatim locator quote. The schema
rejects a numeric value whose digits do not appear in its own quote, so a number
cannot be entered without the text that supports it.

It also rejects the attribution errors that a number-in-quote check cannot see:

* a responder result whose quote names a different threshold from its endpoint, and a responder endpoint
  without a threshold;
* an adverse-event row whose measure kind contradicts its own term (an incidence stored as a
  discontinuation, "any reason" stored as "due to adverse events");
* an estimand marked as stated by the source without a quote that names it, and a row linked to an estimand
  record that cites a different source;
* a value whose sign is not the sign printed in its quote, and a unit that does not belong to the
  endpoint category's convention (a positive "% reduction" among signed percent changes);
* two endpoints under one key, and two different values for one endpoint, arm, estimand, source and time.
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
    EstimandBasis,
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
from ..vocab import percent_thresholds


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def number_tokens(value: float) -> list[str]:
    """Textual forms under which a number may appear in a source quote (sign not included).

    A source may print trailing zeros ("0.9560" for 0.956, "1.20" for 1.2), so every form up to four decimals
    is listed. ``number_in_quote`` itself compares numerically and does not depend on this list.
    """
    magnitude = abs(value)
    shortest = f"{magnitude:.4f}".rstrip("0").rstrip(".") if "e" in f"{magnitude:g}" or "." in f"{magnitude:g}" else f"{magnitude:g}"
    decimals = len(shortest.split(".")[1]) if "." in shortest else 0
    forms = {shortest, f"{magnitude:g}"}
    for places in range(max(decimals, 1), 5):
        forms.add(f"{magnitude:.{places}f}")
    if float(magnitude).is_integer():
        forms.add(str(int(magnitude)))
        forms.add(f"{int(magnitude):,}")
    return sorted(forms, key=len, reverse=True)


_MINUS = str.maketrans({"−": "-", "–": "-", "‒": "-", "—": "-", "·": "."})
_NUMBER = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d]|\.\d)")
_DIRECTION_WORDING = re.compile(r"\b(?:lost|loss(?:es)?|losing|lose|reduc\w+|decreas\w+|lower\w*|declin\w+|fell|dropp?\w*)\b", re.IGNORECASE)


def _minus_before(text: str, start: int) -> bool:
    """Whether the number starting at ``start`` carries a printed minus sign.

    A dash that follows a digit, a letter, '%' or a closing bracket is a range dash or a hyphen
    ("0.59-1.20", "5%-10%", "GLP-1"), not a minus.
    """
    if start >= 1 and text[start - 1] == "-":  # "-5.5"
        previous = text[start - 2] if start >= 2 else ""
        return previous == "" or not (previous.isalnum() or previous in "%)]")
    if start >= 2 and text[start - 1] == " " and text[start - 2] == "-":  # "- 5.5": a minus only at the start of a clause
        previous = text[: start - 2].rstrip()[-1:]
        return previous == "" or previous in "([{,;:=|"
    return False


def quote_numbers(quote: str | None) -> list[tuple[float, int]]:
    """Every number printed in a quote as (magnitude, sign); sign is -1 only when a minus sign is printed."""
    if not quote:
        return []
    text = quote.translate(_MINUS)
    return [(float(m.group(1).replace(",", "")), -1 if _minus_before(text, m.start()) else 1) for m in _NUMBER.finditer(text)]


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def number_in_quote(value: float | None, quote: str | None) -> bool:
    """True when the quote prints this number (compared numerically, so '0.9560' supports 0.956; sign-blind)."""
    if value is None:
        return True
    if not quote:
        return False
    return any(_same(magnitude, abs(value)) for magnitude, _ in quote_numbers(quote))


def printed_signs(value: float, quote: str | None) -> set[int]:
    """The signs (+1 / -1) with which a number is printed in a quote."""
    return {sign for magnitude, sign in quote_numbers(quote) if _same(magnitude, abs(value))}


def has_direction_wording(quote: str | None) -> bool:
    """Whether a quote says in words that the quantity went down ("lost", "reduction", "lower")."""
    return bool(quote and _DIRECTION_WORDING.search(quote))


def sign_problem(name: str, value: float | None, quote: str, sign_basis: str | None) -> str | None:
    """Why a stored sign is not supported by its quote, or None.

    A negative value must be printed with a minus sign, or be declared ``sign_basis: DIRECTION_WORDING`` with a
    quote that states the reduction in words. A positive value must not be printed only with a minus sign.
    """
    if value is None or value == 0:
        return None
    signs = printed_signs(value, quote)
    if not signs:
        return None  # absence is reported by the number-in-quote check
    if value < 0 and -1 not in signs:
        if sign_basis != SIGN_FROM_WORDING:
            return f"{name}={value:g} is stored as negative but its quote prints {abs(value):g} without a minus sign; if the quote states a loss or reduction in words set sign_basis: {SIGN_FROM_WORDING}, otherwise the direction is not established by the quote"
        if not has_direction_wording(quote):
            return f"{name}={value:g}: sign_basis {SIGN_FROM_WORDING} needs a quote that states the loss or reduction in words; this quote does not"
    if value > 0 and signs == {-1}:
        return f"{name}={value:g} is stored as positive but its quote prints it with a minus sign"
    return None


SIGN_FROM_WORDING = "DIRECTION_WORDING"
SIGN_BASES = {None, "AS_PRINTED", SIGN_FROM_WORDING}

# ---- estimand named by a quote
_ESTIMAND_NAME = {
    EstimandLabel.EFFICACY: r"efficacy[\s-]+estimand",
    EstimandLabel.TREATMENT_REGIMEN: r"treatment[\s-]+regimen[\s-]+estimand",
}


def estimand_named_in(label: EstimandLabel, quote: str | None) -> bool:
    """Whether a quote names this estimand ("efficacy estimand", "treatment-regimen estimand")."""
    if not quote:
        return False
    return bool(re.search(_ESTIMAND_NAME.get(label, r"estimand"), quote, re.IGNORECASE))


# ---- adverse-event measure kind versus term
_DISCONTINUATION_TERM = re.compile(r"discontinu|stopp(?:ed|ing) (?:study )?(?:treatment|drug)|withdr[ae]w|non-?completion|did not complete", re.IGNORECASE)
_DUE_TO_AE = re.compile(r"adverse[- ]events?|\bAEs?\b|side effects?", re.IGNORECASE)
_ANY_REASON = re.compile(r"any[- ]reason|all[- ]cause|any cause|for all reasons", re.IGNORECASE)
_STUDY_LEVEL = re.compile(r"non-?completion|did not complete|study withdrawal|withdr[ae]w\w* from (?:the )?study|discontinu\w+ (?:from |of )?(?:the )?study(?! (?:treatment|drug|medication|intervention))", re.IGNORECASE)
_DEATH_TERM = re.compile(r"death|died|mortality|fatal", re.IGNORECASE)
_DISCONTINUATION_KINDS = {
    AeMeasureKind.TREATMENT_DISCONTINUATION_DUE_TO_AE,
    AeMeasureKind.TREATMENT_DISCONTINUATION_ANY_REASON,
    AeMeasureKind.STUDY_WITHDRAWAL_DUE_TO_AE,
    AeMeasureKind.DISCONTINUATION_ANY_REASON,
}


def main_term(term: str) -> str:
    """A term without its parenthetical remarks: "(NOT discontinuation)" says what the term is not."""
    return re.sub(r"\([^)]*\)", " ", term).strip() or term


def ae_kind_for_term(term: str) -> AeMeasureKind:
    """The measure kind a term describes, read from the term itself (parenthetical disclaimers excluded)."""
    text = main_term(term)
    if not _DISCONTINUATION_TERM.search(text):
        if _DEATH_TERM.search(text) and not re.search(r"non-?fatal", text, re.IGNORECASE):
            return AeMeasureKind.DEATHS
        return AeMeasureKind.PARTICIPANTS_WITH_EVENT
    study_level = bool(_STUDY_LEVEL.search(text))
    if _ANY_REASON.search(text):
        return AeMeasureKind.DISCONTINUATION_ANY_REASON if study_level else AeMeasureKind.TREATMENT_DISCONTINUATION_ANY_REASON
    return AeMeasureKind.STUDY_WITHDRAWAL_DUE_TO_AE if study_level else AeMeasureKind.TREATMENT_DISCONTINUATION_DUE_TO_AE


def ae_kind_problem(term: str, kind: AeMeasureKind, category: AeCategory) -> str | None:
    """Why a measure kind contradicts the row's own term or category, or None."""
    text = main_term(term)
    is_discontinuation = bool(_DISCONTINUATION_TERM.search(text))
    if kind == AeMeasureKind.PARTICIPANTS_WITH_EVENT and is_discontinuation:
        return f"term '{term}' describes a discontinuation but the measure kind is an event incidence"
    if kind in _DISCONTINUATION_KINDS:
        if not is_discontinuation:
            return f"term '{term}' does not describe a discontinuation or withdrawal but the measure kind is {kind.value}"
        any_reason = bool(_ANY_REASON.search(text))
        if kind in (AeMeasureKind.TREATMENT_DISCONTINUATION_DUE_TO_AE, AeMeasureKind.STUDY_WITHDRAWAL_DUE_TO_AE):
            if any_reason:
                return f"term '{term}' says any reason but the measure kind is {kind.value}"
            if not _DUE_TO_AE.search(text):
                return f"term '{term}' does not say the discontinuation was due to adverse events but the measure kind is {kind.value}"
        elif not any_reason:
            return f"term '{term}' does not say any reason but the measure kind is {kind.value}"
        if category != AeCategory.DISCONTINUATION:
            return f"measure kind {kind.value} needs category DISCONTINUATION, not {category.value}"
    elif category == AeCategory.DISCONTINUATION:
        return f"category DISCONTINUATION needs a discontinuation measure kind, not {kind.value}"
    if kind == AeMeasureKind.DEATHS and not _DEATH_TERM.search(text):
        return f"term '{term}' does not describe deaths but the measure kind is DEATHS"
    return None


# ---- unit convention per endpoint category
PERCENT_UNITS = {"%", "percent", "percentage points", "% of body weight"}
MASS_UNITS = {"kg", "lb", "lbs"}
PROPORTION_UNITS = {"%", "% of participants", "% of patients", "percent of participants"}
CATEGORY_UNITS: dict[EndpointCategory, set[str]] = {
    EndpointCategory.WEIGHT_PCT_CHANGE: PERCENT_UNITS,
    EndpointCategory.WEIGHT_ABS_CHANGE: MASS_UNITS,
    EndpointCategory.WEIGHT_RESPONDER: PROPORTION_UNITS,
    EndpointCategory.HBA1C_RESPONDER: PROPORTION_UNITS,
}
#: Categories whose values are signed changes (negative = decrease). A magnitude ("20.9% reduction") is not one.
SIGNED_CHANGE_CATEGORIES = {
    EndpointCategory.WEIGHT_PCT_CHANGE,
    EndpointCategory.WEIGHT_ABS_CHANGE,
    EndpointCategory.BMI_CHANGE,
    EndpointCategory.WAIST_CHANGE,
    EndpointCategory.HBA1C_CHANGE,
    EndpointCategory.FASTING_GLUCOSE,
}
_MAGNITUDE_UNIT = re.compile(r"reduction|loss|decrease", re.IGNORECASE)


def unit_problem(category: EndpointCategory, unit: str | None, *, is_ratio: bool = False) -> str | None:
    """Why a unit does not follow the endpoint category's convention, or None."""
    if is_ratio or unit is None:
        return None
    if category in SIGNED_CHANGE_CATEGORIES and _MAGNITUDE_UNIT.search(unit):
        return f"unit '{unit}' is a magnitude of reduction; category {category.value} holds signed changes (enter the signed change, or use category OTHER for a reduction reported as a positive number)"
    allowed = CATEGORY_UNITS.get(category)
    if allowed is not None and unit.strip().lower() not in allowed:
        return f"unit '{unit}' does not belong to category {category.value} (expected one of: {', '.join(sorted(allowed))})"
    return None


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
    """An estimand as one source uses it. Rows may link only to an estimand that cites their own source."""

    key: str
    label: EstimandLabel
    description: str
    # STATED_BY_SOURCE needs a quote from the cited source that names the estimand. Without such a quote the
    # label is the curator's attribution and must say so (INFERRED_BY_CURATOR).
    basis: EstimandBasis = EstimandBasis.STATED_BY_SOURCE
    strategies: list[IceDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _basis_supported(self) -> EstimandDef:
        if self.basis == EstimandBasis.STATED_BY_SOURCE:
            if not self.quote:
                raise ValueError(
                    f"estimand '{self.key}' is marked STATED_BY_SOURCE but has no quote from {self.source}; "
                    "add a quote that names the estimand or use basis INFERRED_BY_CURATOR"
                )
            if not estimand_named_in(self.label, self.quote):
                raise ValueError(f"estimand '{self.key}' is marked STATED_BY_SOURCE but its quote does not name the {self.label.value} estimand")
        elif self.basis != EstimandBasis.INFERRED_BY_CURATOR:
            raise ValueError(f"estimand '{self.key}': a curated estimand is STATED_BY_SOURCE (quoted) or INFERRED_BY_CURATOR, not {self.basis.value}")
        return self


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

    @model_validator(mode="after")
    def _responder_threshold(self) -> EndpointDef:
        if self.category == EndpointCategory.WEIGHT_RESPONDER:
            if self.threshold is None:
                raise ValueError(f"responder endpoint '{self.key}' has no threshold; a responder proportion is meaningless without it (≥5 % is not ≥20 %)")
            named = percent_thresholds(self.title)
            if named and self.threshold not in named:
                raise ValueError(f"responder endpoint '{self.key}': threshold {self.threshold:g} is not the threshold its title names ({sorted(named)})")
        problem = unit_problem(self.category, self.unit)
        if problem:
            raise ValueError(f"endpoint '{self.key}': {problem}")
        return self


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
    # How the sign of a negative number is supported: printed with a minus (default), or DIRECTION_WORDING when
    # the quote prints the number unsigned and states the loss / reduction in words.
    sign_basis: str | None = None

    @model_validator(mode="after")
    def _numbers_supported(self) -> ResultDef:
        text = " ".join([self.quote or "", *self.extra_quotes])
        if self.sign_basis not in SIGN_BASES:
            raise ValueError(f"unknown sign_basis '{self.sign_basis}'")
        for name in ("value", "value_low", "value_high", "ci_low", "ci_high"):
            number = getattr(self, name)
            if number is not None and not number_in_quote(number, text):
                raise ValueError(f"{name}={number} is not present in the locator quote(s) for arm '{self.arm}' ({self.endpoint})")
            problem = sign_problem(name, number, text, self.sign_basis)
            if problem:
                raise ValueError(f"{problem} — arm '{self.arm}' ({self.endpoint})")
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
    sign_basis: str | None = None  # see ResultDef.sign_basis

    @property
    def is_ratio(self) -> bool:
        return bool(re.search(r"ratio", self.param_type or "", re.IGNORECASE))

    @model_validator(mode="after")
    def _numbers_supported(self) -> AnalysisDef:
        text = " ".join([self.quote or "", *self.extra_quotes])
        if self.sign_basis not in SIGN_BASES:
            raise ValueError(f"unknown sign_basis '{self.sign_basis}'")
        for name in ("estimate", "ci_low", "ci_high", "ni_margin"):
            number = getattr(self, name)
            if number is not None and not number_in_quote(number, text):
                raise ValueError(f"{name}={number} is not present in the locator quote(s) for analysis '{self.treatment}' ({self.endpoint})")
            problem = sign_problem(name, number, text, self.sign_basis)
            if problem:
                raise ValueError(f"{problem} — analysis '{self.treatment}' ({self.endpoint})")
            if self.is_ratio and number is not None and number <= 0:
                raise ValueError(f"{name}={number} cannot be a {self.param_type}: a ratio is positive")
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
            if number is not None and not 0 <= number <= 100:
                raise ValueError(f"{name}={number} is not a percentage of participants (AE '{self.term}' / '{self.arm}')")
        if self.scope == "RANGE_ACROSS_ARMS" and (self.percent_low is None or self.percent_high is None):
            raise ValueError("a range-across-arms entry needs percent_low and percent_high")
        problem = ae_kind_problem(self.term, self.measure_kind, self.category)
        if problem:
            raise ValueError(f"adverse-event row for '{self.arm}': {problem}")
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

    @model_validator(mode="after")
    def _rows_agree_with_their_endpoint_and_estimand(self) -> StudyEvidenceFile:
        """Checks that need a row together with the endpoint or estimand it points at."""
        endpoints: dict[str, EndpointDef] = {}
        for endpoint in self.endpoints:
            if endpoint.key in endpoints:
                raise ValueError(f"{self.study}: endpoint key '{endpoint.key}' is defined twice ('{endpoints[endpoint.key].title}' and '{endpoint.title}')")
            endpoints[endpoint.key] = endpoint
        estimands: dict[str, EstimandDef] = {}
        for estimand in self.estimands:
            if estimand.key in estimands:
                raise ValueError(f"{self.study}: estimand key '{estimand.key}' is defined twice")
            estimands[estimand.key] = estimand

        seen: dict[tuple, float | None] = {}
        for row in [*self.results, *self.analyses]:
            kind = "result" if isinstance(row, ResultDef) else "analysis"
            who = row.arm if isinstance(row, ResultDef) else f"{row.treatment} vs {row.comparator}"
            where = f"{self.study}: {kind} '{who}' ({row.endpoint})"
            endpoint = endpoints.get(row.endpoint)
            if endpoint is None:
                raise ValueError(f"{where} refers to an endpoint key that this file does not define")
            estimand = None
            if row.estimand is not None:
                estimand = estimands.get(row.estimand)
                if estimand is None:
                    raise ValueError(f"{where} refers to an estimand key that this file does not define")
            problem = row_problem(row, endpoint, estimand)
            if problem:
                raise ValueError(f"{where}: {problem}")
            if isinstance(row, ResultDef):
                identity = result_identity(row)
                value = row.value if row.value is not None else row.value_low
                if identity in seen and seen[identity] != value:
                    raise ValueError(
                        f"{where}: two different values ({seen[identity]:g} and {value:g}) for one endpoint, arm, estimand, source and time point; "
                        "they cannot both be this quantity — one of them belongs to another endpoint or needs a class_title"
                    )
                seen[identity] = value
        return self


def result_identity(row: ResultDef) -> tuple:
    """What a curated result is a value OF. Two different values under one identity cannot both be right."""
    return (row.endpoint, row.arm, row.scope, row.estimand, row.source, row.timepoint_weeks, row.class_title, row.analysis_population, row.baseline_definition)


def row_problem(row: ResultDef | AnalysisDef, endpoint: EndpointDef, estimand: EstimandDef | None) -> str | None:
    """Why a result or analysis does not belong to the endpoint / estimand it points at, or None.

    Used by the file validator and by the notes converter, so both apply the same rules.
    """
    if estimand is not None and estimand.source != row.source:
        return (
            f"the row cites {row.source} but its estimand record '{estimand.key}' cites {estimand.source}; "
            "an estimand label is recorded per source and is not inherited from another one"
        )
    quote = " ".join([row.quote or "", *row.extra_quotes])
    if endpoint.category == EndpointCategory.WEIGHT_RESPONDER and endpoint.threshold is not None:
        named = percent_thresholds(quote)
        if named and endpoint.threshold not in named:
            return f"the quote names the threshold(s) {', '.join(f'≥{t:g} %' for t in sorted(named))} but the endpoint is the ≥{endpoint.threshold:g} % responder endpoint"
    is_ratio = isinstance(row, AnalysisDef) and row.is_ratio
    problem = unit_problem(endpoint.category, row.unit or endpoint.unit, is_ratio=is_ratio)
    if problem:
        return problem
    if isinstance(row, ResultDef):
        if endpoint.category in CATEGORY_UNITS and (row.unit or endpoint.unit) is None:
            return f"a {endpoint.category.value} value needs its unit"
        if endpoint.category in (EndpointCategory.WEIGHT_RESPONDER, EndpointCategory.HBA1C_RESPONDER):
            for number in (row.value, row.value_low, row.value_high, row.ci_low, row.ci_high):
                if number is not None and not 0 <= number <= 100:
                    return f"{number:g} is not a proportion of participants"
    return None


class SourcesFile(Strict):
    sources: list[SourceDef]


class DrugsFile(Strict):
    drugs: list[DrugDef]


class ClaimsFile(Strict):
    claims: list[ClaimDef]
