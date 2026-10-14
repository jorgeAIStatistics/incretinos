"""Automated, explained checks on stored evidence.

A QUOTE_ANCHORED record proves two things only: the stored number occurs in the locator quote, and the
quote occurs in the source. It does not prove that the number belongs to the stated study, arm, endpoint,
estimand, population or time point. This module looks at each stored record and reports, as explained
risk flags, what its own evidence does and does not establish.

Rules every check follows:

* A flag states a checkable fact about the record, its quote and its sibling records, and quotes the
  tokens and numbers it saw. It never proposes a replacement value, and it only says that the record is
  contradicted when the record's own evidence (its quote, its other fields, or values from the same
  source) settles that.
* Severity has one meaning per level. HIGH: the record's own evidence contradicts it, or attribution
  cannot be established at all. MEDIUM: attribution depends on context outside the quote or outside the
  record. LOW: metadata are incomplete.
* Registry records are parsed from structured JSON and carry no quote, so quote-based checks never apply
  to them. Structural checks (intervals, denominators, direction, units, duplicates) apply to every record.
* Nothing here writes to the database or changes a verification status.

Public interface: ``CHECKS`` (the registry of codes), ``evaluate`` (one record) and ``evaluate_all`` (the
whole database in one pass). Both produce the same flags for the same record; ``evaluate`` can also
report what changed since the previous ingestion when it is given the previous field values.

A flag's ``fields`` are column names of the record it is about. A dotted name (``arm.dose_unit``,
``endpoint.category``) is a column of the arm or endpoint the record points at. Explanations describe other
records by their values, never by database id, so they stay the same when ids change.

Reading a quote is heuristic by nature: numbers are classified by what stands next to them (a dose, a
confidence level, a threshold, a week, a sample size, a p-value, a confidence limit, a dispersion, or a
reported value), and an arm is taken to be named by its dose, its drug or brand name, or the word placebo.
Where the reading is uncertain the checks stay silent or report the weaker fact; they are a guide for a
reviewer and do not replace one.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..enums import DISCONTINUATION_KINDS, EstimandBasis, Origin
from ..models import (
    AdverseEvent,
    Drug,
    Endpoint,
    Estimand,
    EvidenceClaim,
    EvidenceSource,
    OutcomeResult,
    StatisticalAnalysis,
    Study,
    StudyArm,
)
from ..vocab import DRUG_ALIASES
from .policy import SEVERITY_ORDER, CheckFlag

__all__ = ["CHECKS", "QUOTE_BASED_CODES", "evaluate", "evaluate_all"]

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"
# The four record types that carry a verification status (review.policy.EVIDENCE_TYPES).
_RESULT, _ANALYSIS, _AE, _CLAIM = "result", "analysis", "adverse_event", "claim"
_ALL = (_RESULT, _ANALYSIS, _AE, _CLAIM)
_NUMERIC = (_RESULT, _ANALYSIS, _AE)


def _check(title: str, severity: str, help_text: str, *, quote_based: bool = False, applies_to: tuple[str, ...]) -> dict[str, Any]:
    return {"title": title, "severity": severity, "help": help_text, "quote_based": quote_based, "applies_to": applies_to}


#: Every code a check can emit. ``help`` is one plain-English sentence; ``quote_based`` marks checks that
#: read the locator quote and therefore never fire on registry records.
CHECKS: dict[str, dict[str, Any]] = {
    # --- the quote and the numbers it supports
    "QUOTE_MISSING": _check(
        "No locator quote", HIGH,
        "This curated record has no quote from its source, so nothing stored shows where in the source it comes from.",
        quote_based=True, applies_to=_ALL),
    "NUMBER_NOT_IN_QUOTE": _check(
        "Stored number is not in the quote", HIGH,
        "A stored value or interval limit does not appear as a number in the record's own quote.",
        quote_based=True, applies_to=_NUMERIC),
    "VALUE_ONLY_AS_OTHER_TOKEN": _check(
        "Value matches only a non-value number in the quote", HIGH,
        "The stored value appears in the quote only as something else, such as a dose, a confidence level, a threshold, a week, a sample size, a p-value or a confidence limit.",
        quote_based=True, applies_to=_NUMERIC),
    "N_NOT_IN_QUOTE": _check(
        "Sample size is not in the quote", MEDIUM,
        "A number of participants is stored but the quote does not contain it, so it was taken from somewhere other than the quoted text.",
        quote_based=True, applies_to=(_RESULT, _AE)),
    "STATISTIC_NOT_IN_QUOTE": _check(
        "Supporting statistic is not in the quote", MEDIUM,
        "A p-value, margin, standard error or confidence level is stored but the quote does not contain it.",
        quote_based=True, applies_to=(_RESULT, _ANALYSIS)),
    "TIMEPOINT_NOT_IN_QUOTE": _check(
        "Time point is not in the quote", MEDIUM,
        "The record says when the measurement was made, but the quote does not mention that time point.",
        quote_based=True, applies_to=_NUMERIC),
    # --- which arm the value belongs to
    "QUOTE_MULTIPLE_CANDIDATES": _check(
        "Several candidate values in the quote", HIGH,
        "The quote lists several numbers of the same kind and does not itself show which one belongs to this record's arm, endpoint or estimand.",
        quote_based=True, applies_to=_NUMERIC),
    "QUOTE_ARM_MISMATCH": _check(
        "Quote puts another arm next to the value", HIGH,
        "In the quote, the stored value sits next to a different dose or arm than the one this record is filed under.",
        quote_based=True, applies_to=_NUMERIC),
    "ARM_NOT_IN_QUOTE": _check(
        "Arm is not named in the quote", MEDIUM,
        "The quote does not name this record's arm (its drug, dose or placebo), so the link between the value and the arm rests on text outside the quote.",
        quote_based=True, applies_to=_NUMERIC),
    "QUOTE_TOKEN_SHARED": _check(
        "One number in the quote used by several records", MEDIUM,
        "More records cite this quote for the same number than the number of times it appears there, so at least two records rely on a single printed value.",
        quote_based=True, applies_to=_NUMERIC),
    "ARM_DOSE_INCONSISTENT": _check(
        "Arm dose disagrees with the arm label", MEDIUM,
        "The dose stored for the arm disagrees with the arm's own label, for example milligrams stored where the label says milligrams per kilogram.",
        applies_to=_NUMERIC),
    "ARM_DOSE_SIMPLIFIED": _check(
        "Arm label names several doses, one is stored", LOW,
        "The arm's label names more than one dose (a range, a titration or alternatives) but a single dose is stored for it.",
        applies_to=_NUMERIC),
    # --- which endpoint the value belongs to
    "ENDPOINT_THRESHOLD_CONTRADICTED": _check(
        "Responder threshold in the quote differs from the endpoint", HIGH,
        "The quote names a different responder threshold (for example 15% or more) than the endpoint this record is filed under.",
        quote_based=True, applies_to=(_RESULT, _ANALYSIS)),
    "ENDPOINT_THRESHOLD_NOT_IN_QUOTE": _check(
        "Responder threshold is not in the quote", MEDIUM,
        "The endpoint has a responder threshold but the quote does not state one, so which threshold the number refers to rests on text outside the quote.",
        quote_based=True, applies_to=(_RESULT, _ANALYSIS)),
    "ENDPOINT_KEYWORD_CONTRADICTED": _check(
        "Quote names a different measurement than the endpoint", HIGH,
        "The quote names a measured quantity (such as HbA1c, body weight, fat mass or Cmax) that the endpoint this record is filed under does not name.",
        quote_based=True, applies_to=(_RESULT, _ANALYSIS)),
    "ENDPOINT_BASELINE_MISMATCH": _check(
        "Record's baseline differs from the endpoint title", HIGH,
        "The endpoint title says the change is measured from one starting point (randomization or study entry) and the record's own baseline definition says the other.",
        applies_to=(_RESULT,)),
    "UNIT_CONTRADICTS_CATEGORY": _check(
        "Unit does not fit the endpoint category", HIGH,
        "The record's unit is a different kind of quantity (percent, kilograms, share of participants) than the endpoint category it is filed under.",
        applies_to=(_RESULT,)),
    "UNIT_MISSING": _check(
        "No unit stored", LOW,
        "A numeric value is stored without a unit.",
        applies_to=(_RESULT, _ANALYSIS)),
    "QUOTE_UNIT_MISMATCH": _check(
        "Quote gives the number in another unit", HIGH,
        "The quote prints the stored number with a different unit (for example kg) than the unit stored on the record (for example %).",
        quote_based=True, applies_to=_NUMERIC),
    "UNIT_NOT_IN_QUOTE": _check(
        "Unit is not in the quote", MEDIUM,
        "The quote shows the number without its unit, so the unit stored on the record rests on text outside the quote.",
        quote_based=True, applies_to=_NUMERIC),
    "VALUE_OUT_OF_RANGE": _check(
        "Value outside the possible range of its unit", HIGH,
        "A stored number is impossible for its unit, such as a share of participants above 100%, a negative count or a p-value above 1.",
        applies_to=_NUMERIC),
    # --- estimand and baseline
    "ESTIMAND_CONTRADICTED_BY_QUOTE": _check(
        "Quote names a different estimand", HIGH,
        "The quote names one estimand (efficacy or treatment-regimen) and the record is filed under the other.",
        quote_based=True, applies_to=(_RESULT, _ANALYSIS)),
    "ESTIMAND_NOT_SUPPORTED": _check(
        "Estimand is not supported by quoted text", MEDIUM,
        "The record says its estimand is stated by the source, but neither the record's quote nor a quote on the estimand record from the same source names it.",
        quote_based=True, applies_to=(_RESULT, _ANALYSIS)),
    "ESTIMAND_DIFFERENCE_UNEXPLAINED": _check(
        "Different values without a named estimand", MEDIUM,
        "Two records for the same study, arm, endpoint and time point report different values and at least one of them names no estimand, so nothing stored explains the difference.",
        applies_to=(_RESULT, _ANALYSIS)),
    "BASELINE_DEFINITION_CONFLICT": _check(
        "Incompatible baseline definitions", MEDIUM,
        "Records filed as the same quantity (same study, endpoint, arm, estimand and time point) measure change from different starting points.",
        applies_to=(_RESULT,)),
    # --- sign and direction
    "SIGN_CONTRADICTED_BY_QUOTE": _check(
        "Sign disagrees with the quote", HIGH,
        "The quote shows the opposite sign or describes the opposite direction (a loss stored as a gain, or the reverse).",
        quote_based=True, applies_to=_NUMERIC),
    "SIGN_NOT_IN_QUOTE": _check(
        "Negative sign is not supported by the quote", MEDIUM,
        "The value is stored as negative, but the quote prints the number without a minus sign and contains no word for a decrease.",
        quote_based=True, applies_to=(_RESULT, _ANALYSIS)),
    # --- intervals
    "INTERVAL_LIMITS_REVERSED": _check(
        "Lower limit above upper limit", HIGH,
        "The stored lower limit of an interval or range is larger than its upper limit.",
        applies_to=_NUMERIC),
    "ESTIMATE_OUTSIDE_INTERVAL": _check(
        "Estimate outside its own interval", HIGH,
        "The stored estimate lies outside the confidence interval stored with it.",
        applies_to=(_RESULT, _ANALYSIS)),
    "INTERVAL_ZERO_WIDTH": _check(
        "Interval of zero width", MEDIUM,
        "The stored lower and upper confidence limits are the same number.",
        applies_to=(_RESULT, _ANALYSIS)),
    "INTERVAL_WITHOUT_LEVEL": _check(
        "Interval without a confidence level", LOW,
        "Confidence limits are stored without saying whether they are 95% limits or some other level.",
        applies_to=(_RESULT, _ANALYSIS)),
    "INTERVAL_SINGLE_LIMIT": _check(
        "Only one interval limit stored", LOW,
        "One confidence limit is stored without the other.",
        applies_to=(_RESULT, _ANALYSIS)),
    "CI_LEVEL_IMPLAUSIBLE": _check(
        "Impossible confidence level", HIGH,
        "The stored confidence level is not a percentage between 50 and 100.",
        applies_to=(_RESULT, _ANALYSIS)),
    "CI_NOT_NEXT_TO_VALUE": _check(
        "Interval in the quote belongs to another value", HIGH,
        "In the quote, the stored confidence limits do not follow the stored value; they follow a different number.",
        quote_based=True, applies_to=(_RESULT, _ANALYSIS)),
    # --- between-arm analyses
    "ANALYSIS_DIRECTION_VS_ARMS": _check(
        "Difference has the opposite sign to the arm values", HIGH,
        "The stored between-arm difference has the opposite sign to treatment minus comparator computed from the arm values of the same source.",
        applies_to=(_ANALYSIS,)),
    "ANALYSIS_SCALE_VS_ARMS": _check(
        "Difference is on another scale than the arm values", HIGH,
        "The stored between-arm difference matches the arm values of the same source only after multiplying by 100, so it is on a 0 to 1 scale while its unit says percent.",
        applies_to=(_ANALYSIS,)),
    "RATIO_NOT_POSITIVE": _check(
        "Ratio that is not positive", HIGH,
        "A hazard, odds or other ratio, or one of its limits, is stored as zero or negative, which a ratio cannot be.",
        applies_to=(_ANALYSIS,)),
    "ANALYSIS_SCALE_AMBIGUOUS": _check(
        "Parameter type and unit disagree about the scale", MEDIUM,
        "The parameter type says the estimate is a ratio and the unit says it is not (or the reverse), so the scale of the number is unclear from the record.",
        applies_to=(_ANALYSIS,)),
    "ANALYSIS_REFERENCE_NOT_STATED": _check(
        "Reference group was assumed", MEDIUM,
        "The registry did not say which group is the reference, so treatment and comparator were assigned by listing order and the sign of the difference cannot be interpreted from the record alone.",
        applies_to=(_ANALYSIS,)),
    "HYPOTHESIS_FIELDS_INCONSISTENT": _check(
        "Hypothesis fields contradict each other", HIGH,
        "The stored hypothesis type, non-inferiority and superiority fields, and the confidence interval cannot all be true together.",
        applies_to=(_ANALYSIS,)),
    "NI_MARGIN_MISSING": _check(
        "Non-inferiority analysis without a margin", LOW,
        "The analysis is stored as a non-inferiority test but no non-inferiority margin is stored with it.",
        applies_to=(_ANALYSIS,)),
    # --- denominators
    "AFFECTED_EXCEEDS_AT_RISK": _check(
        "More participants affected than at risk", HIGH,
        "The stored number of participants affected is larger than the stored number at risk.",
        applies_to=(_AE,)),
    "PERCENT_DISAGREES_WITH_COUNTS": _check(
        "Percentage disagrees with the counts", HIGH,
        "The stored percentage differs from affected divided by at risk by more than rounding can explain.",
        applies_to=(_AE,)),
    "PERCENT_WITHOUT_DENOMINATOR": _check(
        "Percentage without a denominator", LOW,
        "A percentage is stored without the number of participants it is a percentage of.",
        applies_to=(_AE,)),
    "DENOMINATOR_ZERO": _check(
        "Nobody at risk", LOW,
        "The stored number of participants at risk is zero, so no proportion can be calculated from this record.",
        applies_to=(_AE,)),
    "RESULT_WITHOUT_N": _check(
        "Result without a sample size", LOW,
        "A result value is stored without the number of participants analysed.",
        applies_to=(_RESULT,)),
    # --- adverse events
    "AE_KIND_CONTRADICTED": _check(
        "Measure kind contradicted by the term or quote", HIGH,
        "What the record says it counts (for example treatment stopped because of an adverse event) is contradicted by its own term or quote.",
        applies_to=(_AE,)),
    "AE_TERM_NOT_IN_QUOTE": _check(
        "Event is not named in the quote", MEDIUM,
        "The quote does not name the event this record is about, so what the number counts rests on text outside the quote.",
        quote_based=True, applies_to=(_AE,)),
    # --- claims
    "CLAIM_NO_NUMBER_IN_QUOTE": _check(
        "None of the statement's numbers are in the quote", HIGH,
        "The statement gives quantitative values (percentages, estimates, counts or p-values), and the quote contains none of the numbers in the statement.",
        quote_based=True, applies_to=(_CLAIM,)),
    "CLAIM_NUMBER_NOT_IN_QUOTE": _check(
        "Statement has numbers the quote lacks", MEDIUM,
        "The statement contains numbers that do not appear in its quote.",
        quote_based=True, applies_to=(_CLAIM,)),
    "CLAIM_ENTITY_NOT_IN_QUOTE": _check(
        "Statement names a drug or trial the quote does not", MEDIUM,
        "The statement names a drug or a trial that its quote does not name, so the quote alone does not show what it is about.",
        quote_based=True, applies_to=(_CLAIM,)),
    # --- duplicates
    "DUPLICATE_KEY_DIFFERENT_VALUES": _check(
        "Same key, different values", HIGH,
        "Other records have the same study, endpoint, arm, estimand, source and time point but different values, so nothing stored tells them apart.",
        applies_to=_NUMERIC),
    "DUPLICATE_ROW": _check(
        "Duplicate record", MEDIUM,
        "Another record has the same key and the same values, so the same fact is stored twice.",
        applies_to=_ALL),
    # --- history
    "CHANGED_SINCE_LAST_INGESTION": _check(
        "Changed since the last ingestion", MEDIUM,
        "One or more fields of this record differ from the values it had at the previous ingestion.",
        applies_to=_ALL),
}

#: Codes that read the locator quote. They never fire on a record of registry origin.
QUOTE_BASED_CODES: frozenset[str] = frozenset(code for code, spec in CHECKS.items() if spec["quote_based"])


def _flag(code: str, explanation: str, *fields: str) -> CheckFlag:
    return CheckFlag(code=code, severity=CHECKS[code]["severity"], explanation=explanation, fields=tuple(fields))


# ------------------------------------------------------------------------------------------------ text


_TRANSLATE = str.maketrans(
    {"·": ".", "−": "-", "–": "-", "—": "-", "‐": "-", "‑": "-", " ": " ", " ": " ", " ": " ", "≧": "≥", "≦": "≤"}
)


def _norm(text: str | None) -> str:
    """Same length as the input; typographic minus signs, decimal middle dots and thin spaces made plain."""
    return (text or "").translate(_TRANSLATE)


def _g(x: float | int | None) -> str:
    if x is None:
        return "none"
    if isinstance(x, bool):
        return str(x)
    return format(float(x), ".10g")


def _clip(text: str | None, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _q(text: str | None, limit: int = 90) -> str:
    return "'" + _clip(text, limit) + "'"


def _same_number(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def _decimals(x: float) -> int:
    text = repr(float(x))
    if "e" in text or "E" in text:
        return 6
    frac = text.split(".", 1)[1] if "." in text else ""
    return 0 if frac == "0" else len(frac)


def _half_unit(x: float) -> float:
    """Half of the last printed decimal place: the most a correctly rounded number can be off by."""
    return 0.5 * 10 ** (-_decimals(x))


# --------------------------------------------------------------------------------------- quote tokens

_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(?:\d{1,3}(?:,\d{3})+(?![\d,])(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)")
_DOSE_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?(?:\s?(?:/|-|to|or)\s?\d+(?:\.\d+)?)*)[\s-]?(?:mgs?|milligrams?|mcg|µg|μg)\b"
    r"(\s?/\s?kg\b|\s+per\s+kg\b|\s+kg\b)?",
    re.IGNORECASE,
)
_DOSE_LIST_BEFORE = re.compile(r"(?<![\w.%])(\d+(?:\.\d+)?)\s?(?:,\s?and|,|and)\s?$")
_GROUP_RE = re.compile(r"[\(\[]([^()\[\]]*)[\)\]]")
_UNIT_AFTER: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PP", re.compile(r"\s?(?:percentage|percent)[- ]points?\b", re.IGNORECASE)),
    ("PCT", re.compile(r"\s?(?:%|percent\b|per cent\b)", re.IGNORECASE)),
    ("KG", re.compile(r"\s?(?:kg|kilograms?)\b(?!\s?/)", re.IGNORECASE)),
    ("LB", re.compile(r"\s?(?:lbs?|pounds?)\b", re.IGNORECASE)),
    ("CM", re.compile(r"\s?(?:cm|centimet(?:er|re)s?)\b", re.IGNORECASE)),
    ("MMHG", re.compile(r"\s?mm\s?Hg\b", re.IGNORECASE)),
)
_CI_LEVEL_AFTER = re.compile(r"\s?%\s?(?:two[- ]sided\s)?(?:CI\b|confidence|credible)", re.IGNORECASE)
_CI_LEAD = re.compile(
    r"\s?%\s?(?:two[- ]sided\s)?(?:CI|confidence intervals?|credible intervals?)(?:\s?\[CI\])?[\s,:;=\[\(]*", re.IGNORECASE
)
_PAIR_GAP = re.compile(r"\s?%?\s*(?:to|,|-|;|and)\s*", re.IGNORECASE)
_RANGE_GAP = re.compile(r"\s?%?\s?(?:to|-)\s?", re.IGNORECASE)
_LIST_GAP = re.compile(r"\s?(?:,\s?and|,|and|to|-|or|&)\s?", re.IGNORECASE)
_CELL_GAP = re.compile(r"[\s|%*]*")
_VALUE_LIST_GAP = re.compile(r"\s*(?:,\s*and|,|and|&|or)\s*", re.IGNORECASE)
_UNIT_WORDS = re.compile(r"%|percentage points?|\bkg\b|\blbs?\b|\*", re.IGNORECASE)
_TIME_BEFORE = re.compile(r"\b(?:weeks?|wks?|months?|years?|days?)\s$", re.IGNORECASE)
_TIME_AFTER = re.compile(r"[\s-]?(?:weeks?|wks?|months?|years?|yrs?|days?)\b", re.IGNORECASE)
_P_BEFORE = re.compile(r"\bp\s?(?:-?\s?values?)?\s?[<>=≤≥]\s?$", re.IGNORECASE)
_THRESHOLD_BEFORE = re.compile(
    r"(?:≥|>=|≤|<=|>|<|\bat least|\bmore than|\bless than|\bgreater than|\bfewer than|\bbelow|\babove)\s?$", re.IGNORECASE
)
_THRESHOLD_AFTER = re.compile(r"\s?%?\s+or\s+(?:more|greater|higher|less|lower|fewer)\b", re.IGNORECASE)
_N_BEFORE = re.compile(r"\bn\s?=\s?$", re.IGNORECASE)
_OF_BEFORE = re.compile(r"(?:\)|\]|%)\s?of\s(?:the\s)?$", re.IGNORECASE)
_PEOPLE_AFTER = re.compile(r"\s(?:[A-Za-z][\w-]*\s){0,2}(?:participants|patients|adults|subjects|people|individuals)\b", re.IGNORECASE)
_COUNT_AFTER = re.compile(r"\s?[\(\[]\s?\d+(?:\.\d+)?\s?%?\s?[\)\]]")
_LABEL_BEFORE = re.compile(
    r"\b(?:table|figure|fig\.?|study|studies|trial|trials|cohort|part|phase|period|visit|section|type|stage|grade|class|sequence|indication|appendix|step|version)\s$",
    re.IGNORECASE,
)
_DISPERSION_INNER = re.compile(r"\s*(?:SE|SD|SEM)?\s*[+-]?[\d.]+\s*", re.IGNORECASE)
_PAIR_INNER = re.compile(r"[\s,;:]*[+-]?[\d.]+\s?%?\s*(?:,|to|-|;|and)\s*[+-]?[\d.]+\s?%?\s*", re.IGNORECASE)
_AFTER_VALUE = re.compile(r"\s?(?:%|kg|lbs?|percentage points?)?\s?", re.IGNORECASE)

#: Roles a number can play in a quote. Only the first four can be the reported value itself.
_VALUE_ROLES = frozenset({"VALUE", "RANGE_HI", "PAREN_SINGLE", "COUNT"})
_ROLE_WORDS = {
    "DOSE": "a dose",
    "CI_LEVEL": "a confidence level",
    "CI_BOUND": "a confidence limit",
    "THRESHOLD": "a threshold",
    "TIME": "a time point",
    "N": "a number of participants",
    "P": "a p-value",
    "LABEL": "part of a name or label",
}
_UNIT_WORD = {"PCT": "percentages", "PP": "percentage-point values", "KG": "kg values", "LB": "lb values", "CM": "cm values", "MMHG": "mmHg values", "BARE": "numbers without a unit"}
_UNIT_SYMBOL = {"PCT": "%", "PP": "percentage points", "KG": "kg", "LB": "lbs", "CM": "cm", "MMHG": "mmHg"}


@dataclass(slots=True)
class _Tok:
    """One number in a quote: where it is, how it is printed and what it appears to be."""

    start: int  # first character, sign included
    end: int
    num_start: int
    text: str  # as printed, sign included
    val: float  # magnitude
    sign: str  # '', '-' or '+'
    decimals: int
    role: str = "VALUE"
    unit: str = "BARE"
    pair: int | None = None  # index of the upper limit when this number opens a range "a to b"


@dataclass(frozen=True, slots=True)
class _DoseMark:
    start: int
    end: int
    doses: frozenset[float]
    per_kg: bool
    text: str


@dataclass(slots=True)
class _Quote:
    text: str
    lower: str
    toks: list[_Tok]
    doses: list[_DoseMark]
    free_text: str  # the quote with every number's own unit blanked: what is left can label a bare number

    def equal(self, value: float) -> list[_Tok]:
        magnitude = abs(value)
        return [t for t in self.toks if _same_number(t.val, magnitude)]

    def has_number(self, value: float) -> bool:
        return bool(self.equal(value))


def _dose_marks(text: str) -> list[_DoseMark]:
    marks: list[_DoseMark] = []
    for m in _DOSE_RE.finditer(text):
        doses = frozenset(float(x) for x in re.findall(r"\d+(?:\.\d+)?", m.group(1)))
        per_kg = bool(m.group(2))
        marks.append(_DoseMark(m.start(), m.end(), doses, per_kg, " ".join(m.group(0).split())))
        # "4, 9 and 12 mg": the numbers listed before the one that carries the unit are doses too.
        cursor = m.start()
        while True:
            lead = _DOSE_LIST_BEFORE.search(text[:cursor])
            if lead is None or _TIME_BEFORE.search(text[: lead.start(1)]) or _N_BEFORE.search(text[: lead.start(1)]):
                break  # "week 24, 12 mg" and "n=292, 9 mg" do not list doses
            marks.append(_DoseMark(lead.start(1), lead.end(1), frozenset({float(lead.group(1))}), per_kg, lead.group(1) + " mg"))
            cursor = lead.start(1)
    marks.sort(key=lambda d: d.start)
    return marks


def _sign_at(text: str, num_start: int) -> str:
    if num_start == 0 or text[num_start - 1] not in "+-":
        return ""
    before = text[num_start - 2] if num_start >= 2 else " "
    # "0.79-0.99", "6-9 mg", "14.0-21.4%" and "MACE-4" are ranges or names, not negative numbers.
    return "" if (before.isalnum() or before in "%)]") else text[num_start - 1]


@lru_cache(maxsize=16384)
def _parse(raw: str) -> _Quote:
    text = _norm(raw)
    doses = _dose_marks(text)
    toks: list[_Tok] = []
    free = list(text)
    for m in _NUM_RE.finditer(text):
        s, e = m.span()
        literal = m.group()
        sign = _sign_at(text, s)
        frac = literal.split(".", 1)[1] if "." in literal else ""
        tok = _Tok(start=s - len(sign), end=e, num_start=s, text=sign + literal, val=float(literal.replace(",", "")), sign=sign, decimals=len(frac))
        before, after = text[max(0, s - 40) : tok.start], text[e : e + 60]
        for unit, pattern in _UNIT_AFTER:
            attached = pattern.match(after)
            if attached:
                tok.unit = unit
                free[e : e + attached.end()] = " " * attached.end()
                break
        if any(d.start <= s < d.end for d in doses):
            tok.role, tok.unit = "DOSE", "BARE"
        elif (s >= 2 and text[s - 1] == "-" and text[s - 2].isalpha()) or _LABEL_BEFORE.search(before):
            tok.role = "LABEL"
        elif (text[e : e + 1] == ":" and text[e + 1 : e + 2].isdigit()) or (text[s - 1 : s] == ":" and text[s - 2 : s - 1].isdigit()):
            tok.role = "LABEL"  # allocation ratio such as 1:1:1
        elif _CI_LEVEL_AFTER.match(after):
            tok.role, tok.unit = "CI_LEVEL", "BARE"
        elif _P_BEFORE.search(before):
            tok.role = "P"
        elif _THRESHOLD_BEFORE.search(before) or _THRESHOLD_AFTER.match(after):
            tok.role = "THRESHOLD"
        elif _TIME_BEFORE.search(before) or _TIME_AFTER.match(after):
            tok.role = "TIME"
        elif tok.decimals == 0 and tok.unit == "BARE" and (
            _N_BEFORE.search(before) or _OF_BEFORE.search(before) or _PEOPLE_AFTER.match(after)
        ):
            tok.role = "N"
        elif tok.decimals == 0 and not sign and tok.unit == "BARE" and _COUNT_AFTER.match(after):
            tok.role = "COUNT"
        elif re.search(r"(?:±|\+/-)\s?$", before):
            tok.role = "PAREN_SINGLE"  # "-9.6 ± 0.5": a dispersion, not a second value
        toks.append(tok)

    # "-19.8 and -21.0 percentage points", "14.0-21.4%": a bare number in a list takes the unit of the next one.
    for i in range(len(toks) - 2, -1, -1):
        a, b = toks[i], toks[i + 1]
        gap = text[a.end : b.start]
        listed = b.role == "VALUE" and _LIST_GAP.fullmatch(gap)
        spread = b.role == "PAREN_SINGLE" and re.fullmatch(r"\s?(?:±|\+/-)\s?", gap)  # "-9.6 ± 0.5 kg"
        if a.unit == "BARE" and b.unit != "BARE" and a.role == "VALUE" and (listed or spread):
            a.unit = b.unit

    # Two numbers straight after "95% CI" are confidence limits.
    for i, level in enumerate(toks):
        if level.role != "CI_LEVEL":
            continue
        rest = [t for t in toks[i + 1 :] if t.role != "DOSE"][:2]
        if len(rest) == 2 and all(t.role == "VALUE" for t in rest) and _CI_LEAD.fullmatch(text[level.end : rest[0].start]) and _PAIR_GAP.fullmatch(text[rest[0].end : rest[1].start]):
            for t in rest:
                t.role = "CI_BOUND"

    # A bracketed pair straight after a value is its interval; a bracketed single number without a
    # percent sign straight after a value is its dispersion (SE or SD).
    for g in _GROUP_RE.finditer(text):
        inside = [t for t in toks if g.start() < t.start and t.end <= g.end()]
        values = [t for t in inside if t.role == "VALUE"]
        if not values or any(t.role == "CI_LEVEL" for t in inside):
            continue
        prev = max((t for t in toks if t.end <= g.start() and t.role in ("VALUE", "COUNT", "RANGE_HI")), key=lambda t: t.end, default=None)
        if prev is None or not _AFTER_VALUE.fullmatch(text[prev.end : g.start()]):
            continue
        inner = list(g.group(1))
        for d in doses:
            if g.start() < d.start and d.end <= g.end():
                for k in range(d.start - g.start() - 1, d.end - g.start() - 1):
                    inner[k] = " "
        body = "".join(inner)
        if len(values) == 2 and len(inside) - sum(t.role == "DOSE" for t in inside) == 2 and _PAIR_INNER.fullmatch(body):
            for t in values:
                t.role = "CI_BOUND"
        elif len(inside) == 1 and values[0].unit == "BARE" and _DISPERSION_INNER.fullmatch(body):
            values[0].role = "PAREN_SINGLE"

    # "-5.6% to -8.6%" and "14.0-21.4%" are ranges: one candidate, not two.
    for i in range(len(toks) - 1):
        a, b = toks[i], toks[i + 1]
        if a.role == "VALUE" and b.role == "VALUE" and a.pair is None and _RANGE_GAP.fullmatch(text[a.end : b.start]):
            a.pair, b.role = i + 1, "RANGE_HI"
    return _Quote(text=text, lower=text.lower(), toks=toks, doses=doses, free_text="".join(free))


# ------------------------------------------------------------------------------------- arm identity

_DESCRIPTIVE_DOSE = re.compile(r"(?:\bMTD|maximum tolerated dose)\s*\(|\bpooled\b", re.IGNORECASE)
_ARM_STOPWORDS = frozenset(
    "group groups arm arms range across four three two five six monotherapy therapy dose doses pooled all the and with once daily "
    "weekly oral maximum tolerated titrated continued switched open-label lead-in escalation high-dose low-dose label dosage "
    "investigational formulation shown equivalent reported stated capsule tablet subcutaneous participants patients".split()
)


@dataclass(frozen=True, slots=True)
class _ArmIdentity:
    """What in a quote would identify an arm: its dose expressions, its drug names, 'placebo', or a label word."""

    label: str
    exprs: tuple[frozenset[float], ...]
    drugs: frozenset[str]
    placebo: bool
    words: tuple[str, ...]
    descriptive_dose: bool = False  # "MTD (10 or 15 mg)", "pooled 9 mg + 12 mg": the doses describe a regimen, the drug names the arm

    @property
    def dose_identifies(self) -> bool:
        return bool(self.exprs) and not self.descriptive_dose

    @property
    def dose_text(self) -> str:
        return " / ".join("-".join(_g(d) for d in sorted(e)) + " mg" for e in self.exprs)

    @property
    def identifiable(self) -> bool:
        return bool(self.exprs or self.drugs or self.placebo or self.words)


@dataclass(frozen=True, slots=True)
class _Marker:
    start: int
    end: int
    text: str
    kind: str  # DOSE | PLACEBO | DRUG | WORD
    own: bool


@dataclass(frozen=True, slots=True)
class _Cand:
    """A candidate value in a quote: one number, or a range 'a to b' taken as one."""

    start: int
    end: int
    text: str
    toks: tuple[_Tok, ...]


def _fold(text: str | None) -> str:
    return " ".join((text or "").lower().split())


# ------------------------------------------------------------------------------- units and endpoints

_PEOPLE_UNIT = re.compile(r"participants?|patients?|subjects?|responders?|people|individuals")


def _unit_class(unit: str | None) -> str | None:
    u = (unit or "").strip().lower()
    if not u:
        return None
    if re.search(r"percentage[- ]points?", u):
        return "PCT_POINTS"
    percent = "%" in u or "percent" in u or "proportion" in u
    if _PEOPLE_UNIT.search(u):
        return "PEOPLE_PCT" if percent else "PEOPLE_COUNT"
    if re.search(r"kg\s?/\s?m|kilograms? per (?:square )?met(?:er|re)", u):
        return "BMI"
    if percent:
        return "PERCENT"
    if re.search(r"\bkg\b|kilogram|\blbs?\b|\bpounds?\b", u):
        return "MASS"
    if re.search(r"\bcm\b|\bcms\b|centimet", u):
        return "LENGTH"
    if "ratio" in u:
        return "RATIO"
    return "OTHER"


_UNIT_CLASS_WORDS = {
    "PCT_POINTS": "a difference in percentage points",
    "PEOPLE_PCT": "a share of participants",
    "PEOPLE_COUNT": "a number of participants",
    "BMI": "a body-mass index (kg/m²)",
    "PERCENT": "a percentage",
    "MASS": "a mass (kg or lb)",
    "LENGTH": "a length (cm)",
}
#: Endpoint category -> (what its values are, unit classes that cannot be that).
_CATEGORY_UNITS: dict[str, tuple[str, frozenset[str]]] = {
    "WEIGHT_PCT_CHANGE": ("a percent change in body weight", frozenset({"MASS", "PEOPLE_PCT", "PEOPLE_COUNT", "BMI", "LENGTH"})),
    "WEIGHT_ABS_CHANGE": ("an absolute change in body weight (kg or lb)", frozenset({"PERCENT", "PCT_POINTS", "PEOPLE_PCT", "PEOPLE_COUNT", "BMI", "LENGTH"})),
    "WEIGHT_RESPONDER": ("a share of participants reaching a weight threshold", frozenset({"MASS", "BMI", "LENGTH"})),
    "HBA1C_RESPONDER": ("a share of participants reaching an HbA1c threshold", frozenset({"MASS", "BMI", "LENGTH"})),
    "BMI_CHANGE": ("a change in body-mass index", frozenset({"PEOPLE_PCT", "PEOPLE_COUNT", "LENGTH"})),
    "WAIST_CHANGE": ("a change in waist circumference", frozenset({"PEOPLE_PCT", "PEOPLE_COUNT", "MASS", "BMI"})),
    "HBA1C_CHANGE": ("a change in HbA1c", frozenset({"PEOPLE_PCT", "PEOPLE_COUNT", "MASS", "BMI", "LENGTH"})),
}
#: Quote units that cannot be the record's unit class.
_QUOTE_UNIT_CONFLICT: dict[str, frozenset[str]] = {
    "PERCENT": frozenset({"KG", "LB", "CM", "MMHG"}),
    "PCT_POINTS": frozenset({"KG", "LB", "CM", "MMHG"}),
    "PEOPLE_PCT": frozenset({"KG", "LB", "CM", "MMHG"}),
    "LENGTH": frozenset({"PCT", "PP", "KG", "LB", "MMHG"}),
}
_UNIT_IN_QUOTE: dict[str, re.Pattern[str]] = {
    "PERCENT": re.compile(r"%|percent", re.IGNORECASE),
    "PCT_POINTS": re.compile(r"%|percent", re.IGNORECASE),
    "PEOPLE_PCT": re.compile(r"%|percent|proportion", re.IGNORECASE),
    "MASS": re.compile(r"\bkg\b|kilogram|\blbs?\b|pound", re.IGNORECASE),
    "LENGTH": re.compile(r"\bcm\b|centimet", re.IGNORECASE),
}

_THRESHOLD_RE = re.compile(
    r"(?P<p>\bp\s?)?(?:≥|>=|≤|<=|>|<|\bat least|\bgreater than or equal to|\bless than or equal to|\bmore than|\bless than)\s?(?P<n>\d+(?:\.\d+)?)\s?(?:%|percent)",
    re.IGNORECASE,
)
_OR_MORE_RE = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s?(?:%|percent)\s+or\s+(?:more|greater|higher|less|lower)", re.IGNORECASE)


def _thresholds(text: str | None) -> dict[float, str]:
    """Percent thresholds named in a text ('≥15%', '5% or more'), each with the words that state it."""
    found: dict[float, str] = {}
    norm = _norm(text)
    for m in _THRESHOLD_RE.finditer(norm):
        if not m.group("p"):
            found.setdefault(float(m.group("n")), m.group(0).strip())
    for m in _OR_MORE_RE.finditer(norm):
        found.setdefault(float(m.group("n")), m.group(0).strip())
    return found


_QUANTITY_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("body weight", re.compile(r"body[- ]?weight|\bweight\b(?![- ](?:related|management))", re.IGNORECASE)),
    ("HbA1c", re.compile(r"hba1c|\ba1c\b|glycated h|h(?:a)?emoglobin a1c", re.IGNORECASE)),
    ("waist circumference", re.compile(r"\bwaist\b", re.IGNORECASE)),
    ("body-mass index", re.compile(r"\bbmi\b|body[- ]mass index", re.IGNORECASE)),
    ("fat mass", re.compile(r"fat mass|body fat\b", re.IGNORECASE)),
    ("lean mass", re.compile(r"lean (?:body |soft tissue )?mass|fat-free mass", re.IGNORECASE)),
    ("AUC", re.compile(r"\bauc\b", re.IGNORECASE)),
    ("Cmax", re.compile(r"\bcmax\b", re.IGNORECASE)),
)


def _quantities(text: str | None) -> dict[str, str]:
    """Measured quantities named in a text, each with the words that name it."""
    found: dict[str, str] = {}
    for name, pattern in _QUANTITY_FAMILIES:
        m = pattern.search(text or "")
        if m:
            found[name] = m.group(0)
    if "fat mass" in found or "lean mass" in found:
        found.pop("body weight", None)  # "share of weight loss due to fat mass" is a body-composition endpoint
    return found


_ESTIMAND_KEYWORD = {
    "EFFICACY": re.compile(r"efficacy[- ]estimand", re.IGNORECASE),
    "TREATMENT_REGIMEN": re.compile(r"treatment[- ]regimen", re.IGNORECASE),
}
_ESTIMAND_NAME = {"EFFICACY": "efficacy estimand", "TREATMENT_REGIMEN": "treatment-regimen estimand"}

_DECREASE = re.compile(r"\blost\b|\bloss(?:es)?\b|\blose\b|reduc\w*|decreas\w*|declin\w*|\blower(?:ed|ing)?\b|\bfell\b|\bdrop\w*", re.IGNORECASE)
_INCREASE = re.compile(r"\bgain\w*|\bregain\w*|increas\w*|\brose\b", re.IGNORECASE)
_RATIO_PARAM = re.compile(r"ratio|\bhr\b|\bor\b|\brr\b", re.IGNORECASE)
_ESTIMATE_KEYWORD = re.compile(r"differences?|hazard ratio|odds ratio|risk ratio|relative risk|\bratio\b|\bhr\b|\betd\b", re.IGNORECASE)
_REFERENCE_NOTE = re.compile(r"does not state which group is the reference", re.IGNORECASE)

_DISCONTINUATION_WORD = re.compile(r"discontinu\w*|withdr\w*|did not complete|stopp\w*|drop(?:ped)?[- ]?out\w*", re.IGNORECASE)
_ANY_REASON = re.compile(r"\bany reason\b|\bany cause\b|\ball[- ]cause\b", re.IGNORECASE)
_DEATH_WORD = re.compile(r"death|died|mortal|fatal", re.IGNORECASE)
_AE_TERM_STOPWORDS = frozenset(
    "adverse event events any treatment study participants with permanent before after randomization reason period due other for the of to and".split()
)


def _term_words(term: str | None) -> list[tuple[str, str]]:
    """(word, stem) for the distinctive words of an adverse-event term; the stem finds inflected forms such as 'discontinued'."""
    words = []
    for word in re.findall(r"[a-z]+", (term or "").lower()):
        if len(word) >= 5 and word not in _AE_TERM_STOPWORDS:
            words.append((word, word[: max(5, min(len(word) - 3, 8))]))
    return words


def _weeks_in(text: str) -> list[tuple[float, float, str]]:
    """(weeks, tolerance, words) for every time point a text mentions."""
    found: list[tuple[float, float, str]] = []
    for m in re.finditer(r"\b(?:weeks?|wks?)\s?(\d+(?:\.\d+)?)", text, re.IGNORECASE):
        found.append((float(m.group(1)), 0.01, m.group(0)))
    for m in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)[\s-]?(?:weeks?|wks?)\b", text, re.IGNORECASE):
        found.append((float(m.group(1)), 0.01, m.group(0)))
    for m in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)[\s-]?months?\b", text, re.IGNORECASE):
        months = float(m.group(1))
        found.append((months * 52.0 / 12.0, months * 0.4 + 0.5, m.group(0)))
    for m in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)[\s-]?(?:years?|yrs?)\b", text, re.IGNORECASE):
        years = float(m.group(1))
        found.append((years * 52.0, years + 0.5, m.group(0)))
    return found


def _baseline_anchor(text: str | None) -> tuple[bool, bool, frozenset[float]]:
    """(names randomization, names study entry, week numbers) of a baseline description."""
    lower = (text or "").lower()
    weeks = frozenset(float(w) for w in re.findall(r"week\s?(\d+(?:\.\d+)?)", lower))
    return bool(re.search(r"randomi[sz]", lower)), bool(re.search(r"study entry|lead-in start|enrol", lower)), weeks


def _baselines_incompatible(a: str | None, b: str | None) -> bool:
    if not a or not b or _fold(a) == _fold(b):
        return False
    rand_a, entry_a, weeks_a = _baseline_anchor(a)
    rand_b, entry_b, weeks_b = _baseline_anchor(b)
    if weeks_a and weeks_b and not (weeks_a & weeks_b):
        return True
    return (entry_a and rand_b and not entry_b and not rand_a) or (entry_b and rand_a and not entry_a and not rand_b)


# -------------------------------------------------------------------------------------------- claims

_CLAIM_STRIP: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:doi:?\s*)?10\.\d{4,9}/\S+", re.IGNORECASE),
    re.compile(r"\b(?:PMID|PMCID|NDA|BLA|ANDA|IND)\s?:?\s?[A-Z]*\d+", re.IGNORECASE),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{4}\b"),
    re.compile(r"\b(?:19|20)\d{2}\s?;\s?\d+(?:\s?\(\d+\))?\s?:\s?\w?\d+(?:\s?-\s?\w?\d+)?"),  # 2022;387:205-216
    re.compile(r"\b(?:volume|issue|pages?|pp?\.)\s\d+(?:\s?-\s?\d+)?", re.IGNORECASE),
    re.compile(r"\b(?:label|sections?|sec\.)\s\d+(?:\.\d+)*", re.IGNORECASE),  # label 12.1
    re.compile(r"(?<=[A-Za-z]\s)\(\s?\d{1,2}(?:\.\d{1,2})?\s?\)"),  # a label section number after its heading: Dosage (2.1)
    re.compile(r"\bQ[1-4]\s?\d{4}\b"),
    re.compile(
        r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s(?:\d{1,2},\s)?\d{4}\b", re.IGNORECASE
    ),
    re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE),
)
_YEAR = re.compile(r"(?:19[5-9]\d|20[0-4]\d)")
_RESULT_UNITS = frozenset({"PCT", "PP", "KG", "LB", "CM", "MMHG"})
_TRIAL_NAME = re.compile(r"[A-Z][A-Z0-9]+(?:-[A-Za-z0-9]+)*")


def _claim_numbers(statement: str) -> list[_Tok]:
    """Numbers a statement asserts, leaving out identifiers, citations, dates and names such as 'Phase 3'."""
    text = _norm(statement)
    for pattern in _CLAIM_STRIP:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    asserted: list[_Tok] = []
    previous: _Tok | None = None
    for tok in _parse(text).toks:
        label = tok.role in ("LABEL", "CI_LEVEL")
        if not label and previous is not None and previous.role == "LABEL" and re.fullmatch(r"\s?(?:and|or|,)\s?", text[previous.end : tok.start]):
            tok = _Tok(tok.start, tok.end, tok.num_start, tok.text, tok.val, tok.sign, tok.decimals, "LABEL", tok.unit)  # "Studies 1 and 2"
            label = True
        year = tok.role == "VALUE" and tok.unit == "BARE" and tok.decimals == 0 and not tok.sign and bool(_YEAR.fullmatch(tok.text))
        if not label and not year:
            asserted.append(tok)
        previous = tok
    return asserted


def _is_result_number(tok: _Tok) -> bool:
    """A number that reports a result (a percentage, an estimate, a limit, a count, a p-value) rather than a dose, duration or criterion."""
    if tok.role in ("CI_BOUND", "PAREN_SINGLE", "COUNT", "N", "P", "RANGE_HI"):
        return True
    return tok.role == "VALUE" and (tok.unit in _RESULT_UNITS or tok.decimals > 0 or bool(tok.sign))


# ------------------------------------------------------------------------------------------ context

_RESULT_COLS = (
    "id", "study_id", "endpoint_id", "arm_id", "estimand_id", "origin", "arm_label", "scope", "class_title", "param_type",
    "value", "value_low", "value_high", "unit", "dispersion_type", "dispersion_value", "ci_low", "ci_high", "ci_level",
    "n_analyzed", "baseline_definition", "timepoint_weeks", "source_id", "locator_quote",
)  # fmt: skip
_ANALYSIS_COLS = (
    "id", "study_id", "endpoint_id", "estimand_id", "origin", "treatment_arm_id", "comparator_arm_id", "treatment_label",
    "comparator_label", "hypothesis_type", "param_type", "estimate", "unit", "ci_low", "ci_high", "ci_level", "ci_sides",
    "p_value_text", "ni_margin", "ni_met", "superiority_tested", "superiority_met", "timepoint_weeks", "comment", "source_id",
    "locator_quote",
)  # fmt: skip
_AE_COLS = (
    "id", "study_id", "arm_id", "origin", "arm_label", "scope", "term", "organ_system", "category", "measure_kind", "is_serious",
    "n_affected", "n_at_risk", "n_events", "percent", "percent_low", "percent_high", "period", "source_id", "locator_quote",
)  # fmt: skip
_CLAIM_COLS = ("id", "key", "drug_id", "study_id", "topic", "statement", "claim_kind", "source_id", "quote", "origin")
_ARM_COLS = ("id", "study_id", "label", "drug_slug", "is_placebo", "dose_amount", "dose_unit", "dose_text")
_ENDPOINT_COLS = ("id", "study_id", "title", "category", "threshold", "unit")
_ESTIMAND_COLS = ("id", "study_id", "label", "basis", "source_id", "quote")

_MODELS: dict[str, tuple[type, tuple[str, ...]]] = {
    _RESULT: (OutcomeResult, _RESULT_COLS),
    _ANALYSIS: (StatisticalAnalysis, _ANALYSIS_COLS),
    _AE: (AdverseEvent, _AE_COLS),
    _CLAIM: (EvidenceClaim, _CLAIM_COLS),
}
_QUOTE_FIELD = {_RESULT: "locator_quote", _ANALYSIS: "locator_quote", _AE: "locator_quote", _CLAIM: "quote"}

Record = dict[str, Any]


def _rows(session: Session, model: type, cols: Sequence[str], *where: Any) -> list[Record]:
    """Plain dictionaries of the listed columns. The select is ORM-enabled, so pending changes are flushed first."""
    stmt = select(*[getattr(model, c) for c in cols])
    for clause in where:
        stmt = stmt.where(clause)
    return [dict(zip(cols, row, strict=True)) for row in session.execute(stmt).all()]


def _record_from(row: Any, cols: Sequence[str]) -> Record:
    return {c: getattr(row, c) for c in cols}


@dataclass
class _Context:
    """Everything the checks read, as plain dictionaries: the records themselves and the entities they point at."""

    records: dict[str, list[Record]] = field(default_factory=lambda: {t: [] for t in _ALL})
    arms: dict[int, Record] = field(default_factory=dict)
    endpoints: dict[int, Record] = field(default_factory=dict)
    estimands: dict[int, Record] = field(default_factory=dict)
    studies: dict[int, Record] = field(default_factory=dict)
    sources: dict[int, str] = field(default_factory=dict)
    drug_aliases: dict[str, set[str]] = field(default_factory=dict)


def _load_reference(session: Session, ctx: _Context, study_ids: Iterable[int] | None) -> None:
    """Arms, endpoints and estimands (of the given studies, or all), plus the small study, source and drug tables."""
    ids = None if study_ids is None else sorted({s for s in study_ids if s is not None})
    for model, cols, target in ((StudyArm, _ARM_COLS, ctx.arms), (Endpoint, _ENDPOINT_COLS, ctx.endpoints), (Estimand, _ESTIMAND_COLS, ctx.estimands)):
        if ids is None:
            loaded = _rows(session, model, cols)
        elif ids:
            loaded = _rows(session, model, cols, model.study_id.in_(ids))
        else:
            loaded = []
        target.update({r["id"]: r for r in loaded})
    ctx.studies = {r["id"]: r for r in _rows(session, Study, ("id", "nct_id", "acronym", "display_name"))}
    ctx.sources = {r["id"]: r["key"] for r in _rows(session, EvidenceSource, ("id", "key"))}
    aliases: dict[str, set[str]] = {slug: {a.lower() for a in names} for slug, names in DRUG_ALIASES.items()}
    for drug in _rows(session, Drug, ("slug", "name", "code_name", "brand_names")):
        names = aliases.setdefault(drug["slug"], set())
        if "+" not in (drug["name"] or "") and drug["name"]:
            names.add(drug["name"].lower())
        names.update(m.lower() for m in re.findall(r"\(([A-Za-z][\w-]+)\)", drug["name"] or ""))
        if drug["code_name"]:
            names.add(drug["code_name"].lower())
        names.update(b.lower() for b in (drug["brand_names"] or []) if isinstance(b, str))
    ctx.drug_aliases = aliases


def _load_all(session: Session) -> _Context:
    ctx = _Context()
    _load_reference(session, ctx, None)
    for etype, (model, cols) in _MODELS.items():
        ctx.records[etype] = _rows(session, model, cols)
    return ctx


def _load_for(session: Session, etype: str, target: Record) -> _Context:
    """The target record's siblings: its study's records, and records elsewhere that cite the same quote."""
    ctx = _Context()
    model, cols = _MODELS[etype]
    study_id, source_id, quote = target.get("study_id"), target.get("source_id"), target.get(_QUOTE_FIELD[etype])
    if etype == _CLAIM:
        if source_id is not None:
            ctx.records[_CLAIM] = _rows(session, model, cols, model.source_id == source_id)
    else:
        clauses = []
        if study_id is not None:
            clauses.append(model.study_id == study_id)
        if source_id is not None and quote:
            clauses.append((model.source_id == source_id) & (model.locator_quote == quote))
        if clauses:
            ctx.records[etype] = _rows(session, model, cols, or_(*clauses))
        if etype == _ANALYSIS and study_id is not None:
            ctx.records[_RESULT] = _rows(session, OutcomeResult, _RESULT_COLS, OutcomeResult.study_id == study_id)
    _load_reference(session, ctx, [study_id])
    return ctx


# ------------------------------------------------------------------------------------------- engine


def _arm_key(rec: Record, id_field: str = "arm_id", label_field: str = "arm_label") -> Any:
    return rec.get(id_field) if rec.get(id_field) is not None else "label:" + _fold(rec.get(label_field))


def _result_key(r: Record) -> tuple:
    return (r["study_id"], r["endpoint_id"], _arm_key(r), r["scope"], r["estimand_id"], r["source_id"], r["timepoint_weeks"], _fold(r["class_title"]), _fold(r["baseline_definition"]), _fold(r["param_type"]))


def _result_group(r: Record) -> tuple:
    return (r["study_id"], r["endpoint_id"], _arm_key(r), r["scope"], r["timepoint_weeks"], _fold(r["class_title"]), _fold(r["param_type"]))


def _analysis_key(a: Record) -> tuple:
    return (
        a["study_id"], a["endpoint_id"], _arm_key(a, "treatment_arm_id", "treatment_label"), _arm_key(a, "comparator_arm_id", "comparator_label"),
        a["estimand_id"], a["source_id"], a["timepoint_weeks"], _fold(a["param_type"]), a["hypothesis_type"],
    )  # fmt: skip


def _analysis_group(a: Record) -> tuple:
    return (a["study_id"], a["endpoint_id"], _arm_key(a, "treatment_arm_id", "treatment_label"), _arm_key(a, "comparator_arm_id", "comparator_label"), a["timepoint_weeks"], _fold(a["param_type"]), a["hypothesis_type"])


def _ae_key(e: Record) -> tuple:
    return (e["study_id"], _arm_key(e), e["scope"], _fold(e["term"]), _fold(e["organ_system"]), e["measure_kind"], bool(e["is_serious"]), e["source_id"], _fold(e["period"]))


def _claim_key(c: Record) -> tuple:
    return (c["study_id"], c["drug_id"], c["source_id"], _fold(c["statement"]))


_PAYLOAD = {
    _RESULT: ("value", "value_low", "value_high", "ci_low", "ci_high"),
    _ANALYSIS: ("estimate", "ci_low", "ci_high"),
    _AE: ("n_affected", "n_at_risk", "n_events", "percent", "percent_low", "percent_high"),
    _CLAIM: (),
}
_MAIN_VALUE = {_RESULT: "value", _ANALYSIS: "estimate", _AE: "percent"}
_KEY_WORDS = {
    _RESULT: "study, endpoint, arm, estimand, source, time point and baseline",
    _ANALYSIS: "study, endpoint, treatment and comparator arms, parameter, estimand, source and time point",
    _AE: "study, arm, term, measure kind, source and period",
    _CLAIM: "study, drug, source and statement",
}


def _named(estimand: Record | None) -> bool:
    return estimand is not None and (estimand.get("label") or "NOT_STATED") not in ("NOT_STATED", "")


class _Engine:
    """Runs the checks over a context. The same code serves one record and the whole database."""

    def __init__(self, ctx: _Context) -> None:
        self.ctx = ctx
        alias_to_slug: dict[str, str] = {}
        for slug, names in ctx.drug_aliases.items():
            for name in names:
                alias_to_slug.setdefault(name, slug)
        self._alias_to_slug = alias_to_slug
        ordered = sorted(alias_to_slug, key=len, reverse=True)
        self._alias_re = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(a) for a in ordered) + r")(?![A-Za-z0-9])", re.IGNORECASE) if ordered else None
        self._trials: list[list[tuple[str, re.Pattern[str]]]] = []
        for study in ctx.studies.values():
            names: list[str] = []
            for candidate in (study.get("acronym"), study.get("display_name")):
                if candidate and _TRIAL_NAME.fullmatch(candidate) and candidate not in names:
                    names.append(candidate)
            if study.get("nct_id"):
                names.append(study["nct_id"])
            # "SURMOUNT-1" must not be found inside "SURMOUNT-10" or "SURMOUNT-1-EXT"
            self._trials.append([(n, re.compile(r"(?<![A-Za-z0-9-])" + re.escape(n) + r"(?![A-Za-z0-9]|-[A-Za-z0-9])")) for n in names])
        self._identity_cache: dict[tuple[str, int | None], _ArmIdentity] = {}
        self._dose_cache: dict[tuple[str, int | None, str], list[CheckFlag]] = {}

        self.by_key: dict[str, dict[tuple, list[Record]]] = {t: defaultdict(list) for t in _ALL}
        self.by_quote: dict[str, dict[tuple, list[Record]]] = {t: defaultdict(list) for t in _NUMERIC}
        self.result_groups: dict[tuple, list[Record]] = defaultdict(list)
        self.analysis_groups: dict[tuple, list[Record]] = defaultdict(list)
        self.arm_values: dict[tuple, list[Record]] = defaultdict(list)
        keyers = {_RESULT: _result_key, _ANALYSIS: _analysis_key, _AE: _ae_key, _CLAIM: _claim_key}
        for etype, keyer in keyers.items():
            for rec in ctx.records[etype]:
                self.by_key[etype][keyer(rec)].append(rec)
                if etype != _CLAIM and rec["origin"] != Origin.REGISTRY.value and rec["locator_quote"] and rec["source_id"] is not None:
                    self.by_quote[etype][(rec["source_id"], rec["locator_quote"])].append(rec)
        for rec in ctx.records[_RESULT]:
            self.result_groups[_result_group(rec)].append(rec)
            if rec["value"] is not None and rec["arm_id"] is not None and rec["scope"] == "ARM":
                self.arm_values[(rec["endpoint_id"], rec["arm_id"], rec["source_id"])].append(rec)
        for rec in ctx.records[_ANALYSIS]:
            self.analysis_groups[_analysis_group(rec)].append(rec)

    # ---------------------------------------------------------------------------------------- dispatch

    def flags(self, etype: str, rec: Record) -> list[CheckFlag]:
        out: list[CheckFlag] = []
        if etype == _RESULT:
            self._result(out, rec)
        elif etype == _ANALYSIS:
            self._analysis(out, rec)
        elif etype == _AE:
            self._adverse_event(out, rec)
        elif etype == _CLAIM:
            self._claim(out, rec)
        else:
            raise ValueError(f"Unknown evidence type {etype!r}; expected one of {_ALL}")
        self._duplicates(out, etype, rec)
        return _ordered(out)

    # --------------------------------------------------------------------------------------- identity

    def _drugs_in(self, text: str) -> set[str]:
        if not self._alias_re:
            return set()
        return {self._alias_to_slug[m.group(1).lower()] for m in self._alias_re.finditer(text)} - {"placebo"}

    def _identity(self, label: str | None, arm_id: int | None) -> _ArmIdentity:
        key = (label or "", arm_id)
        cached = self._identity_cache.get(key)
        if cached is not None:
            return cached
        arm = self.ctx.arms.get(arm_id) if arm_id is not None else None
        text = _norm(label)
        exprs = tuple(dict.fromkeys(d.doses for d in _dose_marks(text)))
        if not exprs and arm is not None and arm.get("dose_amount") is not None:
            exprs = (frozenset({float(arm["dose_amount"])}),)
        drugs = self._drugs_in(text)
        if arm is not None and arm.get("drug_slug") and arm["drug_slug"] != "placebo":
            slug = arm["drug_slug"]
            drugs |= {slug} if slug in self.ctx.drug_aliases else set(slug.split("+"))
        placebo = "placebo" in text.lower() or bool(arm and arm.get("is_placebo"))
        words: tuple[str, ...] = ()
        if not exprs and not drugs and not placebo:
            head = text.split("(", 1)[0].lower()
            words = tuple(w for w in re.findall(r"[a-z][a-z-]{3,}", head) if w not in _ARM_STOPWORDS)
        descriptive = bool(exprs and drugs and _DESCRIPTIVE_DOSE.search(text))
        identity = _ArmIdentity(label=label or "", exprs=exprs, drugs=frozenset(drugs), placebo=placebo, words=words, descriptive_dose=descriptive)
        self._identity_cache[key] = identity
        return identity

    def _markers(self, q: _Quote, arms: Sequence[_ArmIdentity]) -> list[_Marker]:
        """Everything in the quote that names an arm, marked as this record's own or another arm's."""
        marks: list[_Marker] = []
        own_exprs = {e for arm in arms for e in arm.exprs}
        exact = any(d.doses in own_exprs for d in q.doses)
        for d in q.doses:
            own = d.doses in own_exprs or (not exact and any(d.doses < e for e in own_exprs))
            marks.append(_Marker(d.start, d.end, d.text, "DOSE", own))
        for m in re.finditer(r"\bplacebo\b", q.lower):
            marks.append(_Marker(m.start(), m.end(), "placebo", "PLACEBO", any(a.placebo for a in arms)))
        own_drugs = {d for arm in arms for d in arm.drugs}
        dose_free_drugs = {d for arm in arms if not arm.dose_identifies for d in arm.drugs}
        if self._alias_re is not None:
            for m in self._alias_re.finditer(q.text):
                slug = self._alias_to_slug[m.group(1).lower()]
                if slug == "placebo":
                    continue
                if not q.doses or slug in dose_free_drugs:
                    marks.append(_Marker(m.start(), m.end(), m.group(1), "DRUG", slug in own_drugs))
        for arm in arms:
            for word in arm.words:
                for m in re.finditer(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", q.lower):
                    marks.append(_Marker(m.start(), m.end(), word, "WORD", True))
        marks.sort(key=lambda m: m.start)
        return marks

    def _arm_named(self, q: _Quote, arm: _ArmIdentity) -> tuple[bool | None, bool | None]:
        """(dose named, drug or group named) in the quote; None where the arm has no such identifier."""
        dose = None
        if arm.dose_identifies:
            dose = any(d.doses in arm.exprs or any(d.doses < e for e in arm.exprs) for d in q.doses)
        name: bool | None = None
        if arm.placebo:
            name = bool(re.search(r"\bplacebo\b", q.lower))
        if arm.drugs and not name:
            name = bool(self._drugs_in(q.text) & arm.drugs)
        if arm.words and not name:
            name = any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", q.lower) for w in arm.words)
        return dose, name

    # ----------------------------------------------------------------------------- quote: one record

    def _candidates(self, q: _Quote, hit: _Tok) -> list[_Cand]:
        """Numbers in the quote that are the same kind of thing as the matched one (same role and unit)."""
        role = "VALUE" if hit.role == "RANGE_HI" else hit.role
        cands: list[_Cand] = []
        for tok in q.toks:
            if tok.role != role or tok.unit != hit.unit:
                continue
            if tok.pair is not None:
                upper = q.toks[tok.pair]
                cands.append(_Cand(tok.start, upper.end, q.text[tok.start : upper.end], (tok, upper)))
            else:
                cands.append(_Cand(tok.start, tok.end, tok.text, (tok,)))
        return cands

    def _binding(self, q: _Quote, cands: list[_Cand], mine: list[_Cand], primary: _ArmIdentity, neutral: Sequence[_ArmIdentity], *, analysis: bool) -> tuple[str, dict[str, Any]]:
        """How the quote ties the stored value to this record's arm: single, bound, remainder, unbound, mismatch or row_label.

        ``primary`` is the arm the value is attributed to (for an analysis, the treatment arm). ``neutral``
        arms (an analysis's comparator) are part of the record, so their names are neither evidence for nor
        against the attribution.
        """
        neutral_marks = [m for m in self._markers(q, neutral) if m.own] if neutral else []
        ignore = {(m.start, m.end) for m in neutral_marks}
        marks = [m for m in self._markers(q, [primary]) if m.own or (m.start, m.end) not in ignore]
        detail: dict[str, Any] = {"marks": marks, "named": sorted({*neutral_marks, *(m for m in marks if m.own)}, key=lambda m: m.start)}
        dose_bearing = primary.dose_identifies
        if len(cands) <= 1:
            if dose_bearing and mine and not any(m.own for m in marks if m.kind == "DOSE"):
                near = [m for m in marks if m.kind == "DOSE" and (0 <= m.start - mine[0].end <= 60 or 0 <= mine[0].start - m.end <= 25)]
                if near:
                    detail["other"] = near[0]
                    return "mismatch", detail
            return "single", detail

        if analysis:
            # "difference, -19.4%": the only number after the parameter name is the between-arm estimate.
            keyword = _ESTIMATE_KEYWORD.search(q.text)
            if keyword:
                governed = [c for c in cands if c.start >= keyword.end()]
                if len(governed) == 1 and governed[0] in mine:
                    return "bound", detail

        own_marks = [m for m in marks if m.own]
        if own_marks and len(own_marks) == len(marks) and all(m.end <= cands[0].start for m in marks):
            return "row_label", detail  # the quote opens with this arm's label and then lists several values

        # "a, b and c ... with 4 mg, 9 mg and 12 mg, respectively": the k-th value belongs to the k-th marker.
        respectively = q.lower.find("respectively")
        if respectively >= 0:
            listed = [c for c in cands if c.end <= respectively]
            names = [m for m in marks if m.start < respectively and m.kind != "WORD"]
            for cand in mine:
                if cand in listed and len(listed) > 1:
                    if len(names) == len(listed):
                        mapped = names[listed.index(cand)]
                        if mapped.own:
                            return "bound", detail
                        detail.update(other=mapped, order=listed.index(cand) + 1)
                        return "mismatch", detail
                    if not names:
                        detail["respectively"] = True
                        return "unbound", detail

        # A table: a header row of arm names and then a row of as many bare values, nothing but spaces or
        # cell separators between them ("Placebo 5 mg 15 mg | Change, % -3.1 -15.0 -20.9"). They pair in order.
        names = [m for m in marks if m.kind != "WORD"]
        if len(names) == len(cands) and names[-1].end <= cands[0].start:
            header = all(_CELL_GAP.fullmatch(q.text[a.end : b.start]) for a, b in zip(names, names[1:], strict=False))
            cells = all(_CELL_GAP.fullmatch(q.text[a.end : b.start]) for a, b in zip(cands, cands[1:], strict=False))
            if header and cells:
                for cand in mine:
                    mapped = names[cands.index(cand)]
                    if mapped.own:
                        return "bound", detail
                    detail.update(other=mapped, order=cands.index(cand) + 1)
                return "mismatch", detail

        # Values separated only by commas or "and" form one list; a marker next to a list cannot single one out.
        units: list[list[_Cand]] = []
        for cand in cands:
            if units and _VALUE_LIST_GAP.fullmatch(_UNIT_WORDS.sub("", q.text[units[-1][-1].end : cand.start])):
                units[-1].append(cand)
            else:
                units.append([cand])

        def gap(index: int, side: str) -> list[_Marker]:
            if side == "after":
                lo, hi = units[index][-1].end, units[index + 1][0].start if index + 1 < len(units) else len(q.text)
            else:
                lo, hi = units[index - 1][-1].end if index > 0 else 0, units[index][0].start
            return [m for m in marks if lo <= m.start < hi]

        after_complete = all(gap(i, "after") for i in range(len(units)))
        before_complete = all(gap(i, "before") for i in range(len(units)))
        side = "after" if after_complete and not before_complete else "before" if before_complete and not after_complete else None
        outcome = "unbound"
        for cand in mine:
            index = next(i for i, unit in enumerate(units) if cand in unit)
            unit = units[index]
            if side is not None:
                here = gap(index, side)
                if len(unit) > 1:
                    names = [m for m in here if m.kind != "WORD"]
                    if len(names) == len(unit):
                        mapped = names[unit.index(cand)]
                        if mapped.own:
                            return "bound", detail
                        detail.update(other=mapped, order=unit.index(cand) + 1)
                        outcome = "mismatch"
                    continue
                if any(m.own for m in here):
                    return "bound", detail
                if dose_bearing or (primary.placebo and not primary.drugs):
                    comparable = [m for m in here if m.kind == "DOSE"]
                else:
                    comparable = [m for m in here if m.kind in ("DRUG", "PLACEBO")]
                if comparable:
                    detail["other"] = comparable[0]
                    outcome = "mismatch"
            elif len(unit) > 1:
                for way in ("after", "before"):
                    listed_names = [m for m in gap(index, way) if m.kind != "WORD"]
                    if len(listed_names) == len(unit) and listed_names[unit.index(cand)].own:
                        return "bound", detail
            else:
                if any(m.own for m in gap(index, "after")) or any(m.own for m in gap(index, "before")):
                    return "bound", detail
                # Every other value has an arm named after it (or before it) and this one has none:
                # it is the remaining arm's value, though the quote does not name that arm.
                for way in ("after", "before"):
                    if not gap(index, way) and all(gap(i, way) for i in range(len(units)) if i != index):
                        return "remainder", detail
        return outcome, detail

    def _quote_value(self, out: list[CheckFlag], etype: str, rec: Record, q: _Quote, *, arms: Sequence[_ArmIdentity], arm_fields: tuple[str, ...], unit: str | None, signed: bool, change_endpoint: bool, endpoint_title: str | None) -> bool:
        """Checks of the record's main value against its quote. Returns True when the quote fails to tie the value to the arm."""
        value_field = _MAIN_VALUE[etype]
        low_field, high_field = ("percent_low", "percent_high") if etype == _AE else ("value_low", "value_high")
        ci = (rec.get("ci_low"), rec.get("ci_high")) if etype != _AE else (None, None)
        value = rec.get(value_field)
        low, high = (rec.get(low_field), rec.get(high_field)) if etype != _ANALYSIS else (None, None)

        # 1. every stored value and limit must be a number in the quote
        supported = True
        for name, number in ((value_field, value), (low_field, low), (high_field, high), ("ci_low", ci[0]), ("ci_high", ci[1])):
            if number is None or q.has_number(number):
                continue
            supported = supported and name not in (value_field, low_field, high_field)
            digits = _g(abs(number))
            inside = next((t.text for t in q.toks if digits in t.text.replace(",", "") and not _same_number(t.val, abs(number))), None)
            where = f"; the digits {digits} occur only inside the number {inside}" if inside else ""
            out.append(_flag("NUMBER_NOT_IN_QUOTE", f"{name} = {_g(number)} is not a number in the quote {_q(q.text)}{where}.", name, "locator_quote"))

        range_row = value is None and low is not None and high is not None
        if not supported or (value is None and not range_row):
            return False

        # 2. the number must appear as a value, not only as a dose, level, threshold, week, n or p
        if range_row:
            wanted_pair = sorted((abs(low), abs(high)))
            hits = [t for t in q.toks if t.pair is not None and all(_same_number(x, y) for x, y in zip(sorted((t.val, q.toks[t.pair].val)), wanted_pair, strict=True))]
            hits = hits or [t for t in q.equal(low) if t.role in _VALUE_ROLES]
            fields: tuple[str, ...] = (low_field, high_field)
        else:
            equal = q.equal(value)
            hits = [t for t in equal if t.role in _VALUE_ROLES]
            fields = (value_field,)
            if not hits:
                roles = sorted({_ROLE_WORDS.get(t.role, t.role.lower()) for t in equal})
                seen = ", ".join(_q(q.text[max(0, t.start - 12) : t.end + 14].strip(), 40) for t in equal[:3])
                out.append(_flag("VALUE_ONLY_AS_OTHER_TOKEN", f"The stored {value_field} {_g(value)} occurs in the quote only as {' and '.join(roles)} ({seen}).", value_field, "locator_quote"))
                return False
        if not hits:
            return False

        # 3. the unit printed next to the number
        unit_class = _unit_class(unit)
        conflict = _QUOTE_UNIT_CONFLICT.get(unit_class or "", frozenset())
        if unit_class == "MASS":
            wants_lb = bool(re.search(r"lb|pound", (unit or "").lower()))
            conflict = frozenset({"PCT", "PP", "CM", "MMHG", "KG" if wants_lb else "LB"})
        compatible = [t for t in hits if t.unit not in conflict]
        if not compatible:
            printed = ", ".join(sorted({f"{t.text} {_UNIT_SYMBOL[t.unit]}" for t in hits}))
            out.append(_flag("QUOTE_UNIT_MISMATCH", f"The quote prints the stored number as {printed}; the unit stored on the record is {_q(unit)}.", *fields, "unit", "locator_quote"))
            return False
        preferred = {"PERCENT": ("PCT", "PP"), "PCT_POINTS": ("PP", "PCT"), "PEOPLE_PCT": ("PCT",), "MASS": ("KG", "LB"), "LENGTH": ("CM",)}.get(unit_class or "", ())
        role_order = ("VALUE", "RANGE_HI", "PAREN_SINGLE", "COUNT")

        def rank(t: _Tok) -> tuple[int, int]:
            return (0 if t.unit in preferred else 1, role_order.index(t.role))

        best = min(rank(t) for t in compatible)
        hit = next(t for t in compatible if rank(t) == best)
        hits = [t for t in compatible if t.role == hit.role and t.unit == hit.unit]
        marker = _UNIT_IN_QUOTE.get(unit_class or "")
        if marker is not None and hit.unit == "BARE" and not marker.search(q.free_text):
            out.append(_flag("UNIT_NOT_IN_QUOTE", f"The quote {_q(q.text)} shows {hit.text} without a unit; the record stores the unit {_q(unit)}.", "unit", "locator_quote"))

        # 4. which arm (or endpoint, or estimand) the number belongs to
        cands = self._candidates(q, hit)
        mine = [c for c in cands if any(t in hits for t in c.toks)]
        if not mine:
            return False
        status, detail = self._binding(q, cands, mine, arms[0], arms[1:], analysis=etype == _ANALYSIS)
        label = _q(arms[0].label, 60)
        unresolved = False
        listed = ", ".join(c.text for c in cands[:10]) + (", …" if len(cands) > 10 else "")
        kind = _UNIT_WORD.get(hit.unit, "numbers")
        if status == "mismatch":
            other: _Marker = detail["other"]
            if "order" in detail:
                how = f"the values and the arms are listed in order, and {mine[0].text} is value {detail['order']}, which the order pairs with {_q(other.text)}"
            else:
                how = f"{_q(other.text)} is the arm named next to {mine[0].text}"
            own_dose = f" ({arms[0].dose_text})" if arms[0].dose_identifies else ""
            out.append(_flag("QUOTE_ARM_MISMATCH", f"In the quote {_q(q.text, 150)}, {how}; this record is filed under the arm {label}{own_dose}.", *fields, arm_fields[0], "locator_quote"))
            unresolved = True
        elif status == "row_label":
            names = ", ".join(dict.fromkeys(_q(m.text) for m in detail["named"]))
            out.append(_flag("QUOTE_MULTIPLE_CANDIDATES", f"The quote {_q(q.text, 150)} names only this record's own arm{'s' if len(arms) > 1 else ''} ({names}) and then lists {len(cands)} {kind} ({listed}); it does not say which of them is this record's endpoint, estimand or time point.", *fields, "endpoint_id", "estimand_id", "locator_quote"))
            unresolved = True
        elif status == "unbound":
            own = list(dict.fromkeys(_q(m.text) for m in detail["marks"] if m.own))
            if detail.get("respectively"):
                why = "says 'respectively' without listing the arms"
            elif own:
                why = f"names {', '.join(own)} but not next to {mine[0].text}"
            else:
                why = f"does not name this record's arm {label}"
            out.append(_flag("QUOTE_MULTIPLE_CANDIDATES", f"The quote {_q(q.text, 150)} lists {len(cands)} {kind} ({listed}) and {why}, so the quote alone does not show which of them belongs to this record.", *fields, arm_fields[0], "locator_quote"))
            unresolved = True

        # 5. sign and direction (the printed sign is compared for every record; wording only for signed quantities)
        quote_uses_signs = any(t.sign for c in cands for t in c.toks)
        for name, number in ((low_field, low), (high_field, high)) if range_row else ((value_field, value),):
            if number is None or number == 0:
                continue
            tokens = [t for t in q.equal(number) if t.role in _VALUE_ROLES and t.unit not in conflict]
            if not tokens:
                continue
            signs = {t.sign for t in tokens}
            first = tokens[0]
            if (number > 0 and signs == {"-"}) or (number < 0 and signs == {"+"}):
                out.append(_flag("SIGN_CONTRADICTED_BY_QUOTE", f"The quote prints {first.text}; the record stores {name} = {_g(number)}.", name, "locator_quote"))
                continue
            if "-" in signs or "+" in signs or not signed:
                continue
            sentence_start = max(q.text.rfind(". ", 0, first.start), q.text.rfind(";", 0, first.start), first.start - 81) + 1
            context = q.text[max(0, sentence_start) : first.start] + " " + q.text[first.end : first.end + 25]
            down, up = _DECREASE.search(context), _INCREASE.search(context)
            if number < 0:
                if up and not down and not quote_uses_signs:
                    out.append(_flag("SIGN_CONTRADICTED_BY_QUOTE", f"The quote describes an increase ({_q(up.group(0))} with {first.text}) and prints no minus sign; the record stores {name} = {_g(number)}.", name, "locator_quote"))
                elif not _DECREASE.search(q.text):
                    out.append(_flag("SIGN_NOT_IN_QUOTE", f"The record stores {name} = {_g(number)}; the quote {_q(q.text)} prints {first.text} without a minus sign and contains no word for a decrease.", name, "locator_quote"))
            elif change_endpoint and down and not up and not quote_uses_signs:
                title_gives_direction = bool(_DECREASE.search(endpoint_title or "") or _INCREASE.search(endpoint_title or "") or _DECREASE.search(unit or ""))
                if not title_gives_direction:
                    out.append(_flag("SIGN_CONTRADICTED_BY_QUOTE", f"The quote describes a decrease ({_q(down.group(0))} with {first.text}); the record stores {name} = +{_g(number)} under the endpoint {_q(endpoint_title)}, where a positive number is an increase.", name, "locator_quote"))

        # 6. the stored interval must be the one printed after the stored value
        if len(cands) > 1 and ci[0] is not None and ci[1] is not None and not range_row and q.has_number(ci[0]) and q.has_number(ci[1]):
            wanted = sorted((abs(ci[0]), abs(ci[1])))

            def window(cand: _Cand) -> list[_Tok]:
                position = cands.index(cand)
                end = cands[position + 1].start if position + 1 < len(cands) else len(q.text)
                return [t for t in q.toks if cand.end <= t.start < end and t.role not in ("DOSE", "CI_LEVEL", "LABEL")]

            def holds(tokens: list[_Tok]) -> bool:
                return any(_same_number(t.val, wanted[0]) for t in tokens) and any(_same_number(t.val, wanted[1]) for t in tokens)

            if not any(holds(window(c)) for c in mine):
                follows = ", ".join(t.text for t in window(mine[0])[:2]) or "no number"
                owner = next((c.text for c in cands if c not in mine and holds(window(c))), None)
                belongs = f"; the quote prints those limits after {owner}" if owner else ""
                out.append(_flag("CI_NOT_NEXT_TO_VALUE", f"In the quote {_q(q.text, 150)}, {mine[0].text} is followed by {follows}, not by the stored limits {_g(ci[0])} and {_g(ci[1])}{belongs}.", value_field, "ci_low", "ci_high", "locator_quote"))
        return unresolved

    def _arm_naming(self, out: list[CheckFlag], q: _Quote, arms: Sequence[tuple[str, _ArmIdentity]]) -> None:
        missing: list[str] = []
        fields: list[str] = []
        for field_name, arm in arms:
            if not arm.identifiable:
                continue
            dose, name = self._arm_named(q, arm)
            if dose is False and name:
                missing.append(f"the {arm.dose_text} dose of {_q(arm.label, 60)} (the quote names the drug or group but not that dose)")
            elif dose is False or (dose is None and name is False):
                missing.append(f"{_q(arm.label, 60)}")
            else:
                continue
            fields.append(field_name)
        if missing:
            out.append(_flag("ARM_NOT_IN_QUOTE", f"The quote {_q(q.text)} does not name {' or '.join(missing)}.", *fields, "locator_quote"))

    def _shared_token(self, out: list[CheckFlag], etype: str, rec: Record, q: _Quote) -> None:
        value_field = _MAIN_VALUE[etype]
        value = rec.get(value_field)
        if value is None or rec.get("source_id") is None:
            return
        group = self.by_quote[etype].get((rec["source_id"], rec["locator_quote"]), [])
        same = [r for r in group if r.get(value_field) is not None and _same_number(abs(r[value_field]), abs(value))]
        printed = len(q.equal(value))
        if printed and len(same) > printed:
            label = "treatment_label" if etype == _ANALYSIS else "arm_label"
            who = sorted({f"{_q(r.get(label), 50)} in {self._study_name(r.get('study_id'))}" for r in same})
            out.append(_flag("QUOTE_TOKEN_SHARED", f"The number {_g(abs(value))} is printed {printed} time(s) in the quote {_q(q.text)}, but {len(same)} records cite this quote for it: {'; '.join(who[:6])}.", value_field, "locator_quote"))

    def _study_name(self, study_id: int | None) -> str:
        study = self.ctx.studies.get(study_id) if study_id is not None else None
        return (study.get("display_name") or study.get("nct_id") or "the study") if study else "the study"

    def _context_in_quote(self, out: list[CheckFlag], q: _Quote, *, weeks: float | None, weeks_field: str, weeks_shown: str | None, counts: Sequence[tuple[str, int | None]], statistics: Sequence[tuple[str, float | None]], p_value: str | None) -> None:
        if weeks is not None:
            mentioned = _weeks_in(q.text)
            if not any(abs(w - weeks) <= tolerance for w, tolerance, _ in mentioned):
                seen = ", ".join(dict.fromkeys(_q(words) for _, _, words in mentioned))
                out.append(_flag("TIMEPOINT_NOT_IN_QUOTE", f"The record stores the time point {weeks_shown or 'week ' + _g(weeks)}; the quote {_q(q.text)} " + (f"mentions only {seen}." if seen else "mentions no time point."), weeks_field, "locator_quote"))
        absent = [(name, n) for name, n in counts if n is not None and not any(t.decimals == 0 and _same_number(t.val, n) for t in q.toks)]
        if absent:
            out.append(_flag("N_NOT_IN_QUOTE", f"{' and '.join(f'{name} = {n}' for name, n in absent)} {'is' if len(absent) == 1 else 'are'} stored, but the quote {_q(q.text)} does not contain {'that number' if len(absent) == 1 else 'those numbers'}.", *[name for name, _ in absent], "locator_quote"))
        missing = [(name, _g(x)) for name, x in statistics if x is not None and not q.has_number(x)]
        if p_value:
            lacking = [t.text for t in _parse(p_value).toks if not q.has_number(t.val)]
            if lacking:
                missing.append(("p_value_text", p_value))
        if missing:
            out.append(_flag("STATISTIC_NOT_IN_QUOTE", f"{' and '.join(f'{name} = {shown}' for name, shown in missing)} {'is' if len(missing) == 1 else 'are'} stored, but the quote {_q(q.text)} does not contain {'it' if len(missing) == 1 else 'them'}.", *[name for name, _ in missing], "locator_quote"))

    def _estimand_in_quote(self, out: list[CheckFlag], rec: Record, q: _Quote) -> None:
        estimand = self.ctx.estimands.get(rec["estimand_id"]) if rec.get("estimand_id") is not None else None
        if estimand is None or estimand.get("label") not in _ESTIMAND_KEYWORD:
            return
        label = estimand["label"]
        own, others = _ESTIMAND_KEYWORD[label], [(k, p) for k, p in _ESTIMAND_KEYWORD.items() if k != label]
        if own.search(q.text):
            return
        for other_label, pattern in others:
            m = pattern.search(q.text)
            if m:
                out.append(_flag("ESTIMAND_CONTRADICTED_BY_QUOTE", f"The quote names {_q(m.group(0))} and not the {_ESTIMAND_NAME[label]}; the record is filed under the estimand {label} (the quote would fit {other_label}).", "estimand_id", "locator_quote"))
                return
        if estimand.get("basis") != EstimandBasis.STATED_BY_SOURCE.value:
            return
        est_quote = estimand.get("quote")
        if est_quote and own.search(est_quote) and estimand.get("source_id") == rec.get("source_id"):
            return
        if not est_quote:
            record = "the estimand record has no quote"
        elif estimand.get("source_id") != rec.get("source_id"):
            record = f"the estimand record's quote comes from another source ({self.ctx.sources.get(estimand.get('source_id'), 'unknown')}) than this record's ({self.ctx.sources.get(rec.get('source_id'), 'unknown')})"
        else:
            record = f"the estimand record's quote {_q(est_quote, 60)} does not name it either"
        out.append(_flag("ESTIMAND_NOT_SUPPORTED", f"The record is filed under the {_ESTIMAND_NAME[label]} as stated by the source, but its quote {_q(q.text)} does not name that estimand, and {record}.", "estimand_id", "locator_quote"))

    def _endpoint_in_quote(self, out: list[CheckFlag], rec: Record, q: _Quote) -> None:
        endpoint = self.ctx.endpoints.get(rec["endpoint_id"]) if rec.get("endpoint_id") is not None else None
        if endpoint is None:
            return
        title = endpoint.get("title") or ""
        wanted = _thresholds(title)
        if endpoint.get("threshold") is not None:
            wanted.setdefault(float(endpoint["threshold"]), f"threshold {_g(endpoint['threshold'])}")
        if wanted:
            printed = _thresholds(q.text)
            if printed and not set(printed) & set(wanted):
                out.append(_flag("ENDPOINT_THRESHOLD_CONTRADICTED", f"The quote says {', '.join(_q(w) for w in printed.values())}; the record is filed under the endpoint {_q(title)}, whose threshold is {', '.join(_g(n) + '%' for n in sorted(wanted))}.", "endpoint_id", "locator_quote"))
            elif not printed:
                out.append(_flag("ENDPOINT_THRESHOLD_NOT_IN_QUOTE", f"The endpoint {_q(title)} has the threshold {', '.join(_g(n) + '%' for n in sorted(wanted))}, but the quote {_q(q.text)} states no threshold.", "endpoint_id", "locator_quote"))
        named_by_quote, named_by_title = _quantities(q.text), _quantities(title)
        if named_by_quote and named_by_title and not set(named_by_quote) & set(named_by_title):
            out.append(_flag("ENDPOINT_KEYWORD_CONTRADICTED", f"The quote names {', '.join(_q(w) for w in named_by_quote.values())}; the record is filed under the endpoint {_q(title)}, which names {', '.join(_q(w) for w in named_by_title.values())}.", "endpoint_id", "locator_quote"))

    # --------------------------------------------------------------------------------- structure: shared

    def _interval(self, out: list[CheckFlag], estimate: float | None, low: float | None, high: float | None, level: float | None, *, estimate_field: str) -> None:
        if level is not None and not (50.0 <= level < 100.0):
            out.append(_flag("CI_LEVEL_IMPLAUSIBLE", f"ci_level = {_g(level)} is not a confidence level between 50 and 100 percent.", "ci_level"))
        if (low is None) != (high is None):
            present, number = ("ci_low", low) if low is not None else ("ci_high", high)
            out.append(_flag("INTERVAL_SINGLE_LIMIT", f"Only {present} = {_g(number)} is stored; the other confidence limit is empty.", "ci_low", "ci_high"))
            return
        if low is None or high is None:
            return
        if level is None:
            out.append(_flag("INTERVAL_WITHOUT_LEVEL", f"The limits {_g(low)} and {_g(high)} are stored without a confidence level (ci_level is empty).", "ci_low", "ci_high", "ci_level"))
        if low > high:
            out.append(_flag("INTERVAL_LIMITS_REVERSED", f"ci_low = {_g(low)} is larger than ci_high = {_g(high)}.", "ci_low", "ci_high"))
            return
        if _same_number(low, high):
            out.append(_flag("INTERVAL_ZERO_WIDTH", f"ci_low and ci_high are both {_g(low)}.", "ci_low", "ci_high"))
        if estimate is not None:
            slack = _half_unit(estimate)
            if estimate < low - slack - _half_unit(low) or estimate > high + slack + _half_unit(high):
                out.append(_flag("ESTIMATE_OUTSIDE_INTERVAL", f"{estimate_field} = {_g(estimate)} lies outside its stored interval {_g(low)} to {_g(high)}.", estimate_field, "ci_low", "ci_high"))

    def _arm_dose(self, out: list[CheckFlag], label: str | None, arm_id: int | None, label_field: str) -> None:
        key = (label or "", arm_id, label_field)
        cached = self._dose_cache.get(key)
        if cached is None:
            cached = []
            arm = self.ctx.arms.get(arm_id) if arm_id is not None else None
            if arm is not None and arm.get("dose_amount") is not None:
                amount, unit = float(arm["dose_amount"]), (arm.get("dose_unit") or "").strip().lower()
                marks = _dose_marks(_norm(label or arm.get("label")))
                shown = _q(label or arm.get("label"), 70)
                per_kg = [m for m in marks if m.per_kg and amount in m.doses]
                if per_kg and "kg" not in unit:
                    cached.append(_flag("ARM_DOSE_INCONSISTENT", f"The arm label {shown} gives {_q(per_kg[0].text)}, a dose per kilogram of body weight; the arm stores dose_amount = {_g(amount)} with dose_unit = {_q(unit or 'none')}.", label_field, "arm.dose_amount", "arm.dose_unit"))
                named = sorted({d for m in marks for d in m.doses})
                if named and amount not in named:
                    cached.append(_flag("ARM_DOSE_INCONSISTENT", f"The arm label {shown} names the dose(s) {', '.join(_g(d) for d in named)} mg; the arm stores dose_amount = {_g(amount)}.", label_field, "arm.dose_amount"))
                elif len(named) > 1:
                    cached.append(_flag("ARM_DOSE_SIMPLIFIED", f"The arm label {shown} names {len(named)} doses ({', '.join(_g(d) for d in named)} mg); the arm stores the single dose_amount = {_g(amount)}.", label_field, "arm.dose_amount"))
            self._dose_cache[key] = cached
        out.extend(cached)

    def _duplicates(self, out: list[CheckFlag], etype: str, rec: Record) -> None:
        keyer = {_RESULT: _result_key, _ANALYSIS: _analysis_key, _AE: _ae_key, _CLAIM: _claim_key}[etype]
        others = [r for r in self.by_key[etype].get(keyer(rec), []) if r is not rec]
        if not others:
            return
        payload = _PAYLOAD[etype]
        mine = tuple(rec.get(f) for f in payload)

        def same(a: tuple, b: tuple) -> bool:
            return all((x is None and y is None) or (x is not None and y is not None and _same_number(x, y)) for x, y in zip(a, b, strict=True))

        differing = [r for r in others if not same(mine, tuple(r.get(f) for f in payload))]
        if differing:

            def show(r: Record) -> str:
                return ", ".join(f"{f} {_g(r[f])}" for f in payload if r.get(f) is not None) or "no value"

            values = "; ".join(show(r) for r in differing[:8]) + ("; …" if len(differing) > 8 else "")
            out.append(_flag("DUPLICATE_KEY_DIFFERENT_VALUES", f"{len(differing)} other record(s) have the same {_KEY_WORDS[etype]} as this one ({show(rec)}) but different values ({values}); nothing stored distinguishes them.", *(f for f in payload if rec.get(f) is not None)))
        else:
            what = f"the same values ({', '.join(_g(v) for v in mine if v is not None)})" if any(v is not None for v in mine) else f"the same statement {_q(rec.get('statement'), 60)}"
            out.append(_flag("DUPLICATE_ROW", f"{len(others)} other record(s) have the same {_KEY_WORDS[etype]} and {what}.", *([f for f in payload if rec.get(f) is not None] or ["statement"])))

    # ----------------------------------------------------------------------------------------- results

    def _result(self, out: list[CheckFlag], r: Record) -> None:
        endpoint = self.ctx.endpoints.get(r["endpoint_id"]) or {}
        estimand = self.ctx.estimands.get(r["estimand_id"]) if r["estimand_id"] is not None else None
        category, title = endpoint.get("category") or "", endpoint.get("title") or ""
        unit_class = _unit_class(r["unit"])
        value = r["value"]
        curated = r["origin"] != Origin.REGISTRY.value

        if curated:
            if not (r["locator_quote"] or "").strip():
                out.append(_flag("QUOTE_MISSING", f"The curated result for {_q(r['arm_label'], 60)} under {_q(title, 60)} has no locator quote.", "locator_quote"))
            else:
                q = _parse(r["locator_quote"])
                identity = self._identity(r["arm_label"], r["arm_id"])
                change = category.endswith("_CHANGE") or bool(re.search(r"\bchange\b", title, re.IGNORECASE))
                severe = self._quote_value(out, _RESULT, r, q, arms=[identity], arm_fields=("arm_label",), unit=r["unit"], signed=True, change_endpoint=change, endpoint_title=title)
                if not severe:
                    self._arm_naming(out, q, [("arm_label", identity)])
                self._shared_token(out, _RESULT, r, q)
                self._context_in_quote(
                    out, q, weeks=r["timepoint_weeks"], weeks_field="timepoint_weeks", weeks_shown=None,
                    counts=[("n_analyzed", r["n_analyzed"])],
                    statistics=[("dispersion_value", r["dispersion_value"]), ("ci_level", r["ci_level"] if r["ci_low"] is not None else None)],
                    p_value=None,
                )  # fmt: skip
                self._estimand_in_quote(out, r, q)
                self._endpoint_in_quote(out, r, q)

        # structure
        self._arm_dose(out, r["arm_label"], r["arm_id"], "arm_label")
        self._interval(out, value, r["ci_low"], r["ci_high"], r["ci_level"], estimate_field="value")
        if r["value_low"] is not None and r["value_high"] is not None and r["value_low"] > r["value_high"]:
            out.append(_flag("INTERVAL_LIMITS_REVERSED", f"value_low = {_g(r['value_low'])} is larger than value_high = {_g(r['value_high'])}.", "value_low", "value_high"))
        has_number = value is not None or r["value_low"] is not None or r["value_high"] is not None
        if has_number and unit_class is None:
            out.append(_flag("UNIT_MISSING", f"The value {_g(value if value is not None else r['value_low'])} is stored without a unit.", "unit"))
        expects = _CATEGORY_UNITS.get(category)
        if has_number and expects and unit_class in expects[1]:
            out.append(_flag("UNIT_CONTRADICTS_CATEGORY", f"The unit {_q(r['unit'])} is {_UNIT_CLASS_WORDS[unit_class]}, but the endpoint {_q(title, 70)} is filed in the category {category}, which holds {expects[0]}.", "unit", "endpoint.category"))
        share = unit_class == "PEOPLE_PCT" or (category in ("WEIGHT_RESPONDER", "HBA1C_RESPONDER") and unit_class == "PERCENT")
        for name in ("value", "value_low", "value_high"):
            number = r[name]
            if number is None:
                continue
            if share and not (0.0 <= number <= 100.0):
                out.append(_flag("VALUE_OUT_OF_RANGE", f"{name} = {_g(number)} with the unit {_q(r['unit'])}: a share of participants cannot be below 0 or above 100.", name, "unit"))
            elif unit_class == "PEOPLE_COUNT" and number < 0 and not re.search(r"change|difference", title, re.IGNORECASE):
                out.append(_flag("VALUE_OUT_OF_RANGE", f"{name} = {_g(number)} with the unit {_q(r['unit'])}: a count cannot be negative.", name, "unit"))
        if r["n_analyzed"] is not None and r["n_analyzed"] < 0:
            out.append(_flag("VALUE_OUT_OF_RANGE", f"n_analyzed = {r['n_analyzed']}: a number of participants cannot be negative.", "n_analyzed"))
        if r["dispersion_value"] is not None and r["dispersion_value"] < 0:
            out.append(_flag("VALUE_OUT_OF_RANGE", f"dispersion_value = {_g(r['dispersion_value'])} ({r['dispersion_type'] or 'dispersion'}): a spread cannot be negative.", "dispersion_value"))
        if value is not None and r["n_analyzed"] is None:
            out.append(_flag("RESULT_WITHOUT_N", f"The value {_g(value)} is stored without n_analyzed.", "n_analyzed"))
        title_rand, title_entry, _ = _baseline_anchor(title)
        row_rand, row_entry, _ = _baseline_anchor(r["baseline_definition"])
        if r["baseline_definition"] and ((title_rand and not title_entry and row_entry and not row_rand) or (title_entry and not title_rand and row_rand and not row_entry)):
            out.append(_flag("ENDPOINT_BASELINE_MISMATCH", f"The endpoint title {_q(title)} measures change from {'randomization' if title_rand else 'study entry'}; this record's baseline_definition is {_q(r['baseline_definition'])}.", "baseline_definition", "endpoint.title"))

        # siblings: same study, endpoint, arm and time point
        siblings = [s for s in self.result_groups.get(_result_group(r), []) if s is not r]
        if value is not None:
            unexplained = []
            for s in siblings:
                if s["value"] is None or _same_number(s["value"], value) or (s["source_id"] == r["source_id"] and s["estimand_id"] == r["estimand_id"]):
                    continue
                if _baselines_incompatible(s["baseline_definition"], r["baseline_definition"]):
                    continue
                other = self.ctx.estimands.get(s["estimand_id"]) if s["estimand_id"] is not None else None
                if not _named(estimand) or not _named(other):
                    unexplained.append((s, other))
            if unexplained:
                mine = estimand["label"] if _named(estimand) else "no named estimand"
                theirs = "; ".join(f"{_g(s['value'])} ({o['label'] if _named(o) else 'no named estimand'}, source {self.ctx.sources.get(s['source_id'], 'unknown')})" for s, o in unexplained[:5])
                when = f"week {_g(r['timepoint_weeks'])}" if r["timepoint_weeks"] is not None else "time point"
                out.append(_flag("ESTIMAND_DIFFERENCE_UNEXPLAINED", f"This record reports {_g(value)} ({mine}) for {_q(r['arm_label'], 50)}; other record(s) for the same study, arm, endpoint and {when} report {theirs}.", "value", "estimand_id"))
        conflicting = [s for s in siblings if s["estimand_id"] == r["estimand_id"] and _baselines_incompatible(s["baseline_definition"], r["baseline_definition"])]
        if conflicting:
            theirs = ", ".join(dict.fromkeys(f"{_q(s['baseline_definition'], 50)} (value {_g(s['value'])})" for s in conflicting[:4]))
            out.append(_flag("BASELINE_DEFINITION_CONFLICT", f"This record ({_g(value)}) measures change from {_q(r['baseline_definition'], 50)}; record(s) filed under the same study, endpoint, arm, estimand and time point measure it from {theirs}.", "baseline_definition", "endpoint_id"))

    # ---------------------------------------------------------------------------------------- analyses

    def _analysis(self, out: list[CheckFlag], a: Record) -> None:
        endpoint = self.ctx.endpoints.get(a["endpoint_id"]) or {}
        estimand = self.ctx.estimands.get(a["estimand_id"]) if a["estimand_id"] is not None else None
        title, category = endpoint.get("title") or "", endpoint.get("category") or ""
        estimate, low, high = a["estimate"], a["ci_low"], a["ci_high"]
        unit_class = _unit_class(a["unit"])
        param_ratio = bool(_RATIO_PARAM.search(a["param_type"] or ""))
        ratio = param_ratio or unit_class == "RATIO"
        curated = a["origin"] != Origin.REGISTRY.value
        pair = f"{_q(a['treatment_label'], 50)} vs {_q(a['comparator_label'], 50)}"

        if curated:
            if not (a["locator_quote"] or "").strip():
                out.append(_flag("QUOTE_MISSING", f"The curated analysis {pair} under {_q(title, 60)} has no locator quote.", "locator_quote"))
            else:
                q = _parse(a["locator_quote"])
                treatment = self._identity(a["treatment_label"], a["treatment_arm_id"])
                arms = [("treatment_label", treatment)]
                if a["comparator_label"] or a["comparator_arm_id"] is not None:
                    arms.append(("comparator_label", self._identity(a["comparator_label"], a["comparator_arm_id"])))
                change = category.endswith("_CHANGE") or bool(re.search(r"\bchange\b", title, re.IGNORECASE))
                severe = self._quote_value(out, _ANALYSIS, a, q, arms=[i for _, i in arms], arm_fields=tuple(f for f, _ in arms), unit=a["unit"], signed=not ratio, change_endpoint=change, endpoint_title=title)
                if not severe:
                    self._arm_naming(out, q, arms)
                self._shared_token(out, _ANALYSIS, a, q)
                self._context_in_quote(
                    out, q, weeks=a["timepoint_weeks"], weeks_field="timepoint_weeks", weeks_shown=None, counts=[],
                    statistics=[("ni_margin", a["ni_margin"]), ("ci_level", a["ci_level"] if low is not None else None)],
                    p_value=a["p_value_text"] if re.search(r"\d", a["p_value_text"] or "") else None,
                )  # fmt: skip
                self._estimand_in_quote(out, a, q)
                self._endpoint_in_quote(out, a, q)

        # structure
        self._arm_dose(out, a["treatment_label"], a["treatment_arm_id"], "treatment_label")
        self._arm_dose(out, a["comparator_label"], a["comparator_arm_id"], "comparator_label")
        self._interval(out, estimate, low, high, a["ci_level"], estimate_field="estimate")
        if estimate is not None and not ratio and unit_class is None:
            out.append(_flag("UNIT_MISSING", f"The estimate {_g(estimate)} ({a['param_type'] or 'parameter not stated'}) is stored without a unit.", "unit"))
        if param_ratio and unit_class not in (None, "RATIO"):
            out.append(_flag("ANALYSIS_SCALE_AMBIGUOUS", f"param_type {_q(a['param_type'])} is a ratio, but the unit stored with the estimate {_g(estimate)} is {_q(a['unit'])}.", "param_type", "unit"))
        elif not param_ratio and unit_class == "RATIO":
            out.append(_flag("ANALYSIS_SCALE_AMBIGUOUS", f"The unit is {_q(a['unit'])}, but param_type {_q(a['param_type'])} does not describe the estimate {_g(estimate)} as a ratio.", "param_type", "unit"))
        if ratio:
            bad = [(n, x) for n, x in (("estimate", estimate),) if x is not None and x <= 0] + [(n, x) for n, x in (("ci_low", low), ("ci_high", high)) if x is not None and x < 0]
            if bad:
                out.append(_flag("RATIO_NOT_POSITIVE", f"{', '.join(f'{n} = {_g(x)}' for n, x in bad)} for {_q(a['param_type'] or a['unit'])}: a ratio and its limits are positive.", *[n for n, _ in bad]))
        for t in _parse(a["p_value_text"]).toks if a["p_value_text"] else []:
            if t.sign == "-" or (t.val > 1 and t.decimals > 0):
                out.append(_flag("VALUE_OUT_OF_RANGE", f"p_value_text {_q(a['p_value_text'])} contains {t.text}; a p-value lies between 0 and 1.", "p_value_text"))
                break
        if _REFERENCE_NOTE.search(a["comment"] or ""):
            out.append(_flag("ANALYSIS_REFERENCE_NOT_STATED", f"The record's comment says the registry does not state which group is the reference; {pair} follows the registry's listing order, so the direction of the estimate {_g(estimate)} cannot be read from this record alone.", "treatment_label", "comparator_label", "estimate"))

        hypothesis = a["hypothesis_type"]
        null = 1.0 if ratio else 0.0
        problems: list[tuple[str, tuple[str, ...]]] = []
        if a["ni_met"] == "YES" and hypothesis not in ("NON_INFERIORITY", "EQUIVALENCE"):
            problems.append((f"ni_met is YES but hypothesis_type is {hypothesis}", ("ni_met", "hypothesis_type")))
        if a["ni_margin"] is not None and hypothesis == "SUPERIORITY":
            problems.append((f"ni_margin = {_g(a['ni_margin'])} is stored but hypothesis_type is SUPERIORITY", ("ni_margin", "hypothesis_type")))
        if a["superiority_met"] == "YES" and a["superiority_tested"] == "NO":
            problems.append(("superiority_met is YES but superiority_tested is NO", ("superiority_met", "superiority_tested")))
        if a["superiority_met"] == "YES" and low is not None and high is not None and low < null < high:
            problems.append((f"superiority_met is YES but the interval {_g(low)} to {_g(high)} contains {_g(null)} (no difference)", ("superiority_met", "ci_low", "ci_high")))
        if ratio and a["ni_margin"] is not None and a["ni_margin"] > 1 and high is not None:
            if a["ni_met"] == "YES" and high >= a["ni_margin"]:
                problems.append((f"ni_met is YES but ci_high = {_g(high)} is not below the margin {_g(a['ni_margin'])}", ("ni_met", "ci_high", "ni_margin")))
            elif a["ni_met"] == "NO" and high < a["ni_margin"]:
                problems.append((f"ni_met is NO but ci_high = {_g(high)} is below the margin {_g(a['ni_margin'])}", ("ni_met", "ci_high", "ni_margin")))
        if problems:
            out.append(_flag("HYPOTHESIS_FIELDS_INCONSISTENT", "; ".join(p for p, _ in problems) + ".", *dict.fromkeys(f for _, fs in problems for f in fs)))
        if hypothesis == "NON_INFERIORITY" and a["ni_margin"] is None:
            out.append(_flag("NI_MARGIN_MISSING", f"hypothesis_type is NON_INFERIORITY for the estimate {_g(estimate)}, but ni_margin is empty.", "hypothesis_type", "ni_margin"))

        # the estimate against the arm-level values of the same source and endpoint
        if estimate is not None and not ratio and a["treatment_arm_id"] is not None and a["comparator_arm_id"] is not None:
            treated = self.arm_values.get((a["endpoint_id"], a["treatment_arm_id"], a["source_id"]), [])
            compared = self.arm_values.get((a["endpoint_id"], a["comparator_arm_id"], a["source_id"]), [])
            if len(treated) == 1 and len(compared) == 1:
                t, c = treated[0]["value"], compared[0]["value"]
                diff = t - c
                noise = _half_unit(t) + _half_unit(c)
                arithmetic = f"{_q(a['treatment_label'], 40)} {_g(t)} minus {_q(a['comparator_label'], 40)} {_g(c)} = {_g(round(diff, 6))}"
                tolerance = noise + _half_unit(estimate) + 0.1 * abs(diff)
                if abs(estimate - diff) > tolerance:
                    if abs(estimate + diff) <= tolerance and abs(diff) > noise:
                        out.append(_flag("ANALYSIS_DIRECTION_VS_ARMS", f"The stored estimate is {_g(estimate)}; the arm values of the same source and endpoint give {arithmetic}, the opposite sign.", "estimate", "treatment_label", "comparator_label"))
                    else:
                        noise100 = noise + 100 * _half_unit(estimate)
                        scaled, flipped = abs(100 * estimate - diff) <= noise100 + 0.05 * abs(diff), abs(100 * estimate + diff) <= noise100 + 0.05 * abs(diff)
                        if (scaled or flipped) and abs(diff) > 4 * noise100:
                            sign_note = " and reversing its sign" if flipped and not scaled else ""
                            out.append(_flag("ANALYSIS_SCALE_VS_ARMS", f"The stored estimate is {_g(estimate)} with the unit {_q(a['unit'])}; the arm values of the same source and endpoint give {arithmetic}; the estimate equals that, within rounding, only after multiplying it by 100 ({_g(round(100 * estimate, 6))}){sign_note}.", "estimate", "unit"))
                            if flipped and not scaled:
                                out.append(_flag("ANALYSIS_DIRECTION_VS_ARMS", f"The stored estimate is {_g(estimate)} (on a 0 to 1 scale); the arm values of the same source and endpoint give {arithmetic}, the opposite sign.", "estimate", "treatment_label", "comparator_label"))

        # siblings: the same contrast reported with another value
        if estimate is not None:
            unexplained = []
            for s in self.analysis_groups.get(_analysis_group(a), []):
                if s is a or s["estimate"] is None or _same_number(s["estimate"], estimate) or (s["source_id"] == a["source_id"] and s["estimand_id"] == a["estimand_id"]):
                    continue
                other = self.ctx.estimands.get(s["estimand_id"]) if s["estimand_id"] is not None else None
                if not _named(estimand) or not _named(other):
                    unexplained.append((s, other))
            if unexplained:
                mine = estimand["label"] if _named(estimand) else "no named estimand"
                theirs = "; ".join(f"{_g(s['estimate'])} ({o['label'] if _named(o) else 'no named estimand'}, source {self.ctx.sources.get(s['source_id'], 'unknown')})" for s, o in unexplained[:5])
                out.append(_flag("ESTIMAND_DIFFERENCE_UNEXPLAINED", f"This record reports {_g(estimate)} ({mine}) for {pair}; other record(s) for the same study, endpoint, arms and time point report {theirs}.", "estimate", "estimand_id"))

    # ---------------------------------------------------------------------------------- adverse events

    def _adverse_event(self, out: list[CheckFlag], e: Record) -> None:
        curated = e["origin"] != Origin.REGISTRY.value
        kind, term = e["measure_kind"], e["term"] or ""
        q: _Quote | None = None
        if curated:
            if not (e["locator_quote"] or "").strip():
                out.append(_flag("QUOTE_MISSING", f"The curated adverse-event record {_q(term, 60)} for {_q(e['arm_label'], 60)} has no locator quote.", "locator_quote"))
            else:
                q = _parse(e["locator_quote"])
                identity = self._identity(e["arm_label"], e["arm_id"])
                severe = self._quote_value(out, _AE, e, q, arms=[identity], arm_fields=("arm_label",), unit="% of participants", signed=False, change_endpoint=False, endpoint_title=None)
                if not severe:
                    self._arm_naming(out, q, [("arm_label", identity)])
                self._shared_token(out, _AE, e, q)
                period_weeks = _weeks_in(e["period"] or "")
                self._context_in_quote(
                    out, q, weeks=period_weeks[0][0] if len(period_weeks) == 1 else None, weeks_field="period", weeks_shown=_q(e["period"]),
                    counts=[("n_affected", e["n_affected"]), ("n_at_risk", e["n_at_risk"])], statistics=[], p_value=None,
                )  # fmt: skip
                words = _term_words(term)
                if words and not any(stem in q.lower for _, stem in words):
                    out.append(_flag("AE_TERM_NOT_IN_QUOTE", f"The record's term is {_q(term)}; the quote {_q(q.text)} contains none of the term's words ({', '.join(dict.fromkeys(w for w, _ in words))}).", "term", "locator_quote"))

        # what the record says it counts, against its own term and quote
        discontinuation_kind = kind in DISCONTINUATION_KINDS
        any_reason = _ANY_REASON.search(term)
        if any_reason and kind in ("TREATMENT_DISCONTINUATION_DUE_TO_AE", "STUDY_WITHDRAWAL_DUE_TO_AE"):
            out.append(_flag("AE_KIND_CONTRADICTED", f"measure_kind is {kind} (stopped because of an adverse event), but the term {_q(term)} says {_q(any_reason.group(0))}.", "measure_kind", "term"))
        elif discontinuation_kind and not _DISCONTINUATION_WORD.search(term) and not (q is not None and _DISCONTINUATION_WORD.search(q.text)):
            where = f"nor the quote {_q(q.text)} " if q is not None else ""
            out.append(_flag("AE_KIND_CONTRADICTED", f"measure_kind is {kind}, but neither the term {_q(term)} {where}mentions stopping or withdrawing.", "measure_kind", "term", *(["locator_quote"] if q is not None else [])))
        elif kind == "PARTICIPANTS_WITH_EVENT" and re.search(r"discontinu|did not complete", term, re.IGNORECASE):
            out.append(_flag("AE_KIND_CONTRADICTED", f"measure_kind is PARTICIPANTS_WITH_EVENT (participants who had the event), but the term {_q(term)} describes stopping treatment or the study.", "measure_kind", "term"))
        elif kind == "DEATHS" and not _DEATH_WORD.search(term):
            out.append(_flag("AE_KIND_CONTRADICTED", f"measure_kind is DEATHS, but the term {_q(term)} does not mention death.", "measure_kind", "term"))

        # structure
        self._arm_dose(out, e["arm_label"], e["arm_id"], "arm_label")
        affected, at_risk, percent = e["n_affected"], e["n_at_risk"], e["percent"]
        for name in ("n_affected", "n_at_risk", "n_events"):
            if e[name] is not None and e[name] < 0:
                out.append(_flag("VALUE_OUT_OF_RANGE", f"{name} = {e[name]}: a count cannot be negative.", name))
        for name in ("percent", "percent_low", "percent_high"):
            if e[name] is not None and not (0.0 <= e[name] <= 100.0):
                out.append(_flag("VALUE_OUT_OF_RANGE", f"{name} = {_g(e[name])}: a percentage of participants cannot be below 0 or above 100.", name))
        if e["percent_low"] is not None and e["percent_high"] is not None and e["percent_low"] > e["percent_high"]:
            out.append(_flag("INTERVAL_LIMITS_REVERSED", f"percent_low = {_g(e['percent_low'])} is larger than percent_high = {_g(e['percent_high'])}.", "percent_low", "percent_high"))
        if affected is not None and at_risk is not None and affected > at_risk:
            out.append(_flag("AFFECTED_EXCEEDS_AT_RISK", f"n_affected = {affected} is larger than n_at_risk = {at_risk}.", "n_affected", "n_at_risk"))
        if at_risk == 0:
            out.append(_flag("DENOMINATOR_ZERO", f"n_at_risk = 0 for {_q(term, 60)} in {_q(e['arm_label'], 50)}{' during ' + _q(e['period'], 50) if e['period'] else ''}; n_affected = {_g(affected)}.", "n_at_risk"))
        if percent is not None or e["percent_low"] is not None or e["percent_high"] is not None:
            shown = _g(percent) if percent is not None else f"{_g(e['percent_low'])} to {_g(e['percent_high'])}"
            if at_risk is None:
                out.append(_flag("PERCENT_WITHOUT_DENOMINATOR", f"The percentage {shown} is stored without n_at_risk.", "percent" if percent is not None else "percent_low", "n_at_risk"))
            elif percent is not None and affected is not None and at_risk > 0 and affected <= at_risk:
                computed = 100.0 * affected / at_risk
                if abs(computed - percent) > 1.1 * _half_unit(percent):
                    out.append(_flag("PERCENT_DISAGREES_WITH_COUNTS", f"n_affected / n_at_risk = {affected} / {at_risk} = {computed:.2f}%, which differs from the stored percent = {_g(percent)} by more than rounding.", "percent", "n_affected", "n_at_risk"))

    # ------------------------------------------------------------------------------------------ claims

    def _claim(self, out: list[CheckFlag], c: Record) -> None:
        statement = c["statement"] or ""
        if not (c["quote"] or "").strip():
            if c["origin"] != Origin.REGISTRY.value:
                out.append(_flag("QUOTE_MISSING", f"The claim {_q(statement, 110)} has no quote from its source.", "quote"))
            return
        if c["origin"] == Origin.REGISTRY.value:
            return
        q = _parse(c["quote"])
        asserted = _claim_numbers(statement)
        def printed(t: _Tok) -> str:
            return t.text + ("%" if t.unit == "PCT" else " mg" if t.role == "DOSE" else "")

        absent = list(dict.fromkeys(printed(t) for t in asserted if not q.has_number(t.val)))
        if absent:
            present = list(dict.fromkeys(t.text.lstrip("+-") for t in asserted if q.has_number(t.val)))
            shown = ", ".join(absent[:12]) + (", …" if len(absent) > 12 else "")
            if not present and any(_is_result_number(t) for t in asserted):
                out.append(_flag("CLAIM_NO_NUMBER_IN_QUOTE", f"The statement gives the numbers {shown}; its quote {_q(q.text)} contains none of the statement's numbers.", "statement", "quote"))
            else:
                also = f" (the quote does contain the numbers {', '.join(present[:8])})" if present else ""
                out.append(_flag("CLAIM_NUMBER_NOT_IN_QUOTE", f"The statement contains {shown}, which the quote {_q(q.text)} does not{also}.", "statement", "quote"))

        missing: list[str] = []
        if self._alias_re is not None:
            quoted = self._drugs_in(q.text)
            seen: set[str] = set()
            for m in self._alias_re.finditer(_norm(statement)):
                slug = self._alias_to_slug[m.group(1).lower()]
                if slug != "placebo" and slug not in quoted and slug not in seen:
                    seen.add(slug)
                    missing.append(m.group(1))
        for names in self._trials:
            written = next((name for name, pattern in names if pattern.search(statement)), None)
            if written and not any(pattern.search(q.text) for _, pattern in names):
                missing.append(written)
        if missing:
            out.append(_flag("CLAIM_ENTITY_NOT_IN_QUOTE", f"The statement names {', '.join(_q(n) for n in missing[:8])}; the quote {_q(q.text)} contains neither {'that name' if len(missing) == 1 else 'those names'} nor a known synonym.", "statement", "quote"))


def _ordered(flags: list[CheckFlag]) -> list[CheckFlag]:
    unique = list(dict.fromkeys(flags))
    return sorted(unique, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.code, f.explanation))


# -------------------------------------------------------------------------------------------- public


def _values_equal(old: Any, new: Any) -> bool:
    if old is None or new is None:
        return old is None and new is None
    if isinstance(old, bool) or isinstance(new, bool):
        return bool(old) == bool(new)
    if isinstance(old, int | float) and isinstance(new, int | float):
        return _same_number(float(old), float(new))
    return old == new


def _show(value: Any) -> str:
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        return _g(value)
    return _q(str(value), 70)


def _change_flag(row: Any, previous_fields: Mapping[str, Any]) -> CheckFlag | None:
    """One flag listing every field whose current value differs from ``previous_fields`` (keys the record lacks are ignored)."""
    changes = [(name, previous_fields[name], getattr(row, name)) for name in sorted(previous_fields) if hasattr(row, name) and not _values_equal(previous_fields[name], getattr(row, name))]
    if not changes:
        return None
    listing = "; ".join(f"{name}: {_show(old)} → {_show(new)}" for name, old, new in changes)
    return _flag("CHANGED_SINCE_LAST_INGESTION", f"{len(changes)} field(s) differ from the previous ingestion: {listing}.", *[name for name, _, _ in changes])


_RELATED: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("arm_id", "arm", _ARM_COLS, "arms"),
    ("treatment_arm_id", "treatment_arm", _ARM_COLS, "arms"),
    ("comparator_arm_id", "comparator_arm", _ARM_COLS, "arms"),
    ("endpoint_id", "endpoint", _ENDPOINT_COLS, "endpoints"),
    ("estimand_id", "estimand", _ESTIMAND_COLS, "estimands"),
)


def _attach_unsaved(ctx: _Context, row: Any, target: Record) -> None:
    """A record that is not saved yet has no foreign keys; read its arm, endpoint and estimand from the objects it holds."""
    for fk, relation, cols, store_name in _RELATED:
        if fk not in target or target[fk] is not None:
            continue
        related = getattr(row, relation, None)
        if related is None:
            continue
        store: dict[int, Record] = getattr(ctx, store_name)
        key = getattr(related, "id", None)
        if key is None:
            key = -(len(store) + 1)
        store.setdefault(key, _record_from(related, cols))
        target[fk] = key


def evaluate(session: Session, evidence_type: str, row: Any, previous_fields: Mapping[str, Any] | None = None) -> list[CheckFlag]:
    """Flags for one stored record (an OutcomeResult, StatisticalAnalysis, AdverseEvent or EvidenceClaim).

    The record's siblings (records of its study, and records elsewhere citing the same quote) are loaded
    from ``session`` so that set-level checks give the same answer as ``evaluate_all``. ``previous_fields``
    maps column names to the values the record had at the previous ingestion; when given, a
    CHANGED_SINCE_LAST_INGESTION flag lists each field that differs. Flags are ordered HIGH first.
    """
    if evidence_type not in _MODELS:
        raise ValueError(f"Unknown evidence type {evidence_type!r}; expected one of {_ALL}")
    model, cols = _MODELS[evidence_type]
    if not isinstance(row, model):
        raise TypeError(f"evidence_type {evidence_type!r} expects a {model.__name__}, got {type(row).__name__}")
    target = _record_from(row, cols)
    ctx = _load_for(session, evidence_type, target)
    _attach_unsaved(ctx, row, target)
    records = ctx.records[evidence_type]
    # The object in hand is the record to check, whether or not its latest state has reached the database.
    position = next((i for i, r in enumerate(records) if target["id"] is not None and r["id"] == target["id"]), None)
    if position is None:
        records.append(target)
    else:
        records[position] = target
    flags = _Engine(ctx).flags(evidence_type, target)
    if previous_fields is not None:
        change = _change_flag(row, previous_fields)
        if change is not None:
            flags = _ordered([*flags, change])
    return flags


def evaluate_all(session: Session) -> dict[tuple[str, int], list[CheckFlag]]:
    """Flags for every stored result, analysis, adverse event and claim, keyed by (evidence_type, id).

    One pass over plain column data: a handful of queries, no per-record loading. Every record has an entry;
    a record with nothing to report has an empty list.
    """
    ctx = _load_all(session)
    engine = _Engine(ctx)
    return {(etype, rec["id"]): engine.flags(etype, rec) for etype in _ALL for rec in ctx.records[etype]}
