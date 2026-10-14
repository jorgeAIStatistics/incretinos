"""Shared text vocabulary: drug aliases, endpoint categorisation, label normalisation, time parsing."""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from .enums import EndpointCategory


class DirectionBasis(StrEnum):
    """How the treatment and the comparator of a between-arm analysis were told apart."""

    GROUP_DESCRIPTION = "REGISTRY_GROUP_DESCRIPTION"  # the registry analysis names the contrast ("A vs B")
    ARM_TYPE = "REGISTRY_ARM_TYPE"  # exactly one group is a registry comparator arm (placebo / active comparator)
    PLACEBO_GROUP = "PLACEBO_GROUP_TITLE"  # exactly one group is titled as placebo only; no registry arm type available
    CURATED = "CURATED_SOURCE_STATEMENT"  # contrast written "A vs B" by the curated source entry
    NOT_STATED = "NOT_STATED"  # the source does not say which group is the reference; labels are in posted order


#: Registry arm types whose arm is the reference of a contrast with an experimental arm.
COMPARATOR_ARM_TYPES = {"PLACEBO_COMPARATOR", "ACTIVE_COMPARATOR", "SHAM_COMPARATOR", "NO_INTERVENTION"}

# Alias -> drug slug. Only names/codes confirmed by registry records or labels.
DRUG_ALIASES: dict[str, list[str]] = {
    "tirzepatide": ["tirzepatide", "ly3298176", "mounjaro", "zepbound"],
    "orforglipron": ["orforglipron", "ly3502970", "foundayo"],
    "retatrutide": ["retatrutide", "ly3437943"],
    "eloralintide": ["eloralintide", "ly3841136"],
    "dulaglutide": ["dulaglutide", "ly2189265", "trulicity"],
    "brenipatide": ["brenipatide", "ly3537031"],
    "macupatide": ["macupatide", "ly3532226"],
    "bimagrumab": ["bimagrumab"],
    "semaglutide": ["semaglutide", "wegovy", "ozempic", "rybelsus"],
    "insulin-glargine": ["insulin glargine", "glargine"],
    "placebo": ["placebo"],
}

COMPARATOR_SLUGS = {"placebo", "semaglutide", "insulin-glargine"}

# Words that never distinguish one arm from another. Dosing frequency is NOT here: QD and QW are different regimens.
_ARM_STOPWORDS = {"sc", "participants", "of", "the", "arm", "group"}


def detect_drugs(text: str) -> list[str]:
    """Drug slugs mentioned in a label, in order of first appearance."""
    lowered = text.lower()
    found: list[tuple[int, str]] = []
    for slug, aliases in DRUG_ALIASES.items():
        positions = [lowered.find(alias) for alias in aliases if alias in lowered]
        if positions:
            found.append((min(positions), slug))
    return [slug for _, slug in sorted(found)]


# Combination products tracked as their own drug entry.
COMBINATION_SLUGS = {frozenset({"eloralintide", "tirzepatide"}): "eloralintide-tirzepatide"}


_UNIT_SLASH = re.compile(r"\b(mg|mcg|µg|ug|g|iu|units?)\s*/\s*(kg|m2|day|week|wk)\b")
_PLACEBO_FOR = re.compile(r"placebo\s+(?:to\s+match(?:ing)?|matching|matched\s+to|for|of)\s+")
_COMPONENT_SPLIT = re.compile(r"\+|/|;|,|→|\bthen\b|\bfollowed by\b|\bto\b|\band\b|\bor\b|\bplus\b|\bvs\.?(?=\s)|\bversus\b|\bminus\b")


_UNIT_WORD = (
    (re.compile(r"\bmilligrams?\b(?:\s*\(\s*mg\s*\))?"), "mg"),  # "10 milligrams (mg)" is "10 mg"
    (re.compile(r"\bmicrograms?\b(?:\s*\(\s*(?:mcg|µg)\s*\))?"), "mcg"),
)


def _protect(label: str) -> str:
    """Lower-case a label, spell units one way, and shield the slashes of a unit (mg/kg) or of a dose list (10/30)."""
    text = label.lower()
    for pattern, unit in _UNIT_WORD:
        text = pattern.sub(unit, text)
    text = _UNIT_SLASH.sub(r"\1 per \2", text)
    text = _PLACEBO_FOR.sub("placebo-for ", text)
    return re.sub(r"(?<=\d)\s*/\s*(?=\d)", "|", text)


def arm_components(label: str) -> list[str]:
    """The parts of a combination or sequence arm label, in the order written ('A + B', 'A then B', 'A/B')."""
    return [part.strip() for part in _COMPONENT_SPLIT.split(_protect(label)) if part.strip()]


def arm_drug_slug(text: str) -> tuple[str | None, bool]:
    """(drug slug, is_placebo) for an arm label; combinations map to their combination slug.

    A drug named together with "placebo" in one component ("Tirzepatide Placebo", "placebo for bimagrumab") is a
    matching placebo, not an active drug: "Bimagrumab Placebo + Tirzepatide Placebo" is a placebo arm and
    "Bimagrumab Placebo + Tirzepatide Dose 2" is a tirzepatide arm.
    """
    active: list[str] = []
    has_placebo = False
    for component in arm_components(text):
        drugs = detect_drugs(component)
        if "placebo" in drugs:
            has_placebo = True
            continue
        active.extend(d for d in drugs if d not in active)
    if not active:
        return ("placebo", True) if has_placebo else (None, False)
    if len(active) == 1:
        return active[0], False
    return COMBINATION_SLUGS.get(frozenset(active), "+".join(sorted(active))), False


def arm_identity_key(label: str) -> str:
    """Exact identity of an arm label: only case, whitespace and incidental punctuation are folded.

    Word order, combination order ('A + B' vs 'B + A'), doses, units and dosing frequency all stay significant,
    so two labels with the same key are the same text for every scientific purpose.
    """
    text = re.sub(r"[^a-z0-9.+/|]+", " ", label.lower())
    text = re.sub(r"\s*([+/|])\s*", r" \1 ", text)
    return " ".join(text.split())[:300]


_FREQUENCY = (
    (re.compile(r"\bonce[- ]daily\b|\bq\.?d\.?(?![a-z0-9])|\bdaily\b"), " qd "),
    (re.compile(r"\bonce[- ]weekly\b|\bq\.?w\.?(?![a-z0-9])|\bweekly\b"), " qw "),
)
_DESIGNATOR = re.compile(r"\b(dose|cohort|sequence|part|period|level|group|arm|stage|step)\s+([a-z]?\d+[a-z]?)\b")
_NUMBER_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(mg per kg|mcg per kg|mg|milligrams?|mcg|µg|kg|iu)\b")


def norm_arm_label(label: str) -> str:
    """Matching hint that tolerates word order inside ONE component ('5 mg Tirzepatide' = 'Tirzepatide 5 mg').

    It never ignores what distinguishes arms: the order of components and the connector between them
    ('A then B' vs 'B then A'; 'X Placebo + Y Dose 2' vs 'X Dose 2 + Y Placebo'), which number belongs to which
    unit or designator ('5 mg' vs 'Dose 5'), and dosing frequency (QD vs QW). It is a hint for attaching a
    differently worded label to an existing arm; arm identity itself is ``arm_identity_key``.
    """
    text = _protect(label)
    for pattern, token in _FREQUENCY:
        text = pattern.sub(token, text)
    text = _NUMBER_UNIT.sub(lambda m: f"{m.group(1)}{m.group(2).replace(' ', '')} ", text)
    text = _DESIGNATOR.sub(lambda m: f"{m.group(1)}{m.group(2)} ", text)
    parts: list[str] = []
    cursor = 0
    for match in _COMPONENT_SPLIT.finditer(text):
        parts.append(text[cursor : match.start()])
        parts.append(f"<{match.group(0).strip().rstrip('.')}>")
        cursor = match.end()
    parts.append(text[cursor:])
    out: list[str] = []
    for part in parts:
        if part.startswith("<") and part.endswith(">"):
            out.append(part)
            continue
        tokens = [t.strip(".") for t in re.sub(r"[^a-z0-9.|]+", " ", part).split()]
        tokens = [t for t in tokens if t and t not in _ARM_STOPWORDS]
        if tokens:
            out.append(" ".join(sorted(tokens)))
    return " ".join(out)[:300]


def _without_frequency(hint: str) -> tuple[str, set[str]]:
    tokens = hint.split()
    return " ".join(t for t in tokens if t not in ("qd", "qw")), {t for t in tokens if t in ("qd", "qw")}


def match_arm_label(label: str, existing: Sequence[str], *, hint_among: Sequence[int] | None = None) -> int | None:
    """Index in ``existing`` of the label that names the same arm as ``label``, or None.

    1. Exact identity (``arm_identity_key``) wins.
    2. Otherwise the order-tolerant hint, accepted only when exactly one candidate shares it.
    3. Otherwise the hint with dosing frequency left out — only when one of the two labels gives no frequency
       at all ('7 mg Semaglutide' / '7 mg Semaglutide once daily'); QD never matches QW — and again only
       when exactly one candidate fits.
    An ambiguous hint attaches to nothing, so distinct arms are never merged. ``hint_among`` restricts steps
    2 and 3 to the given indices.
    """
    identity = arm_identity_key(label)
    for index, other in enumerate(existing):
        if arm_identity_key(other) == identity:
            return index
    eligible = range(len(existing)) if hint_among is None else hint_among
    hints = {index: norm_arm_label(existing[index]) for index in eligible}
    hint = norm_arm_label(label)
    candidates = [index for index, other in hints.items() if other == hint]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        return None
    bare, frequency = _without_frequency(hint)
    loose = []
    for index, other in hints.items():
        other_bare, other_frequency = _without_frequency(other)
        if other_bare == bare and not (frequency and other_frequency):
            loose.append(index)
    return loose[0] if len(loose) == 1 else None


_DOSE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg per kg|mcg per kg|µg per kg|mg|milligrams?|mcg|µg|micrograms?)(?![a-z])")
_DOSE_UNIT = {"milligram": "mg", "milligrams": "mg", "microgram": "mcg", "micrograms": "mcg", "µg": "mcg", "mg per kg": "mg/kg", "mcg per kg": "mcg/kg", "µg per kg": "mcg/kg"}


def parse_single_dose(label: str) -> tuple[float, str] | None:
    """(amount, unit) only when the label names exactly one dose. The unit is kept as reported.

    '30 mg/kg Bimagrumab' is 30 mg/kg, never 30 mg. A titration or pooled label ('10/30 mg/kg', '1/3/6 mg',
    '6 mg/12 mg/36 mg') names several doses and yields None.
    """
    text = _protect(label)
    if re.search(r"\d\|\d", text):  # dose list written with slashes: more than one dose
        return None
    if re.search(r"\d\s*(?:-|–|—|\bto\b)\s*\d+(?:\.\d+)?\s*(?:mg|mcg|µg|milligram|microgram)", text):  # dose range '6-9 mg'
        return None
    doses = {(float(amount), _DOSE_UNIT.get(unit, unit)) for amount, unit in _DOSE.findall(text)}
    return doses.pop() if len(doses) == 1 else None


def parse_single_dose_mg(label: str) -> float | None:
    """The dose in milligrams, only when the label names exactly one dose and that dose is in mg (not mg/kg)."""
    dose = parse_single_dose(label)
    return dose[0] if dose is not None and dose[1] == "mg" else None


def to_float(value: object) -> float | None:
    """Parse a reported number; anything non-numeric (NA, '', None) stays None — never zero.

    A comma is accepted only as a thousands separator ('1,234'). '1,5' is not a number this parser understands
    (it could be a decimal comma) and stays None rather than becoming 15.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace("−", "-")
    if re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text):
        return None
    return float(text)


def to_int(value: object) -> int | None:
    number = to_float(value)
    return int(number) if number is not None and float(number).is_integer() else None


_RANGE_WORD = r"(?:\bto\b|\bthrough\b|\bup to\b|\buntil\b|\bthru\b)"


def _weeks_in(text: str) -> list[float]:
    """Every assessment time named in a time-frame fragment, in weeks."""
    found: list[float] = []
    # "Week 24", "Weeks 8, 16, and 36", "Week 0, 1, 4, 12": the numbers that continue a week list belong to it.
    for match in re.finditer(r"\bweeks?\s*(\d+(?:\.\d+)?(?:\s*(?:,|and|&)\s*(?:and\s+)?\d+(?:\.\d+)?(?!\s*(?:hours?|days?|weeks?|months?|years?|\.\d)))*)", text, flags=re.IGNORECASE):
        found += [float(n) for n in re.findall(r"\d+(?:\.\d+)?", match.group(1))]
    found += [float(w) for w in re.findall(r"(\d+(?:\.\d+)?)\s*weeks?\b", text, flags=re.IGNORECASE)]
    found += [float(m) * 52.0 / 12.0 for m in re.findall(r"(\d+(?:\.\d+)?)\s*months?\b", text, flags=re.IGNORECASE)]
    found += [float(y) * 52.0 for y in re.findall(r"(\d+(?:\.\d+)?)\s*years?\b", text, flags=re.IGNORECASE)]
    return found


def parse_timeframe(timeframe: str | None) -> tuple[float | None, str | None]:
    """Return (assessment time in weeks, baseline definition) from a registry time-frame string.

    The assessment time is returned only when the text names exactly ONE: 'Baseline, Week 72' is 72 and
    'Randomization (Week 36) to Week 88' is 88. A frame that lists several assessment times
    ('Baseline, Week 24, Week 48'; 'Predose: Week 0, 1, 4, 12; Postdose: Week 2, 8') has no single time and
    yields None — the largest number mentioned is not the time of every value in that outcome.
    """
    if not timeframe:
        return None, None
    text = timeframe.strip()
    baseline = None
    first = re.split(r",|\bto\b|\bthrough\b|\bup to\b", text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    rest = text
    if re.search(r"baseline|randomi[sz]ation", first, flags=re.IGNORECASE):
        baseline = first[:200]
        rest = text[len(first) :]
    # The start of the observation window is not an assessment time: drop week 0 and any repeated "Baseline ..." anchor.
    rest = re.sub(r"\b(?:baseline|randomi[sz]ation)\b\s*(?:\(\s*week\s*\d+(?:\.\d+)?\s*\))?", " ", rest, flags=re.IGNORECASE)
    times = sorted({w for w in _weeks_in(rest) if w > 0})
    return (times[0] if len(times) == 1 else None), baseline


_THRESHOLD_SIGN = r"(?:≥|>=|=>|at least|greater than or equal to|more than or equal to)"
_RESPONDER = re.compile(
    _THRESHOLD_SIGN + r"\s*(\d+(?:\.\d+)?)\s*%?\s*(?:percent\s*)?(?:of\s+)?(?:body[\s-]*)?weight[\s-]*(?:reduction|loss)", re.IGNORECASE
)
_PERCENT_THRESHOLD = re.compile(
    _THRESHOLD_SIGN + r"\s*(\d+(?:\.\d+)?)\s*(?:%|percent)|(\d+(?:\.\d+)?)\s*(?:%|percent)\s*or\s*(?:more|greater)", re.IGNORECASE
)


def percent_thresholds(text: str | None) -> set[float]:
    """Responder thresholds a text names ('≥10%', '>=15 %', 'at least 5%', '20% or more')."""
    if not text:
        return set()
    # Registry titles spell the sign out and repeat it: "Greater Than or Equal to (≥) 5%".
    cleaned = re.sub(r"\(\s*(?:≥|>=)\s*\)", " ", text.replace("≧", "≥"))
    return {float(a or b) for a, b in _PERCENT_THRESHOLD.findall(cleaned)}


def responder_threshold(title: str) -> float | None:
    """The weight-reduction threshold of a responder endpoint, when its title names exactly one.

    "Maintain ≥80% of the body weight lost" is not a weight-reduction threshold (it is a share of an earlier
    loss), and a title that lists several thresholds has none of its own: both yield None.
    """
    if re.search(r"maintain", title, re.IGNORECASE):
        return None
    named = percent_thresholds(title)
    return named.pop() if len(named) == 1 else None


def categorize_endpoint(title: str, unit: str | None = None) -> tuple[EndpointCategory, float | None]:
    """Map an outcome title to a canonical category (and responder threshold where applicable)."""
    t = title.lower()
    u = (unit or "").lower()
    if re.search(r"pharmacokinetic|\bauc\b|\bcmax\b|concentration", t):
        return EndpointCategory.PHARMACOKINETICS, None
    if re.search(r"fat mass|lean mass|fat[- ]free mass|body composition|\bdxa\b|visceral adipose|body fat", t):
        return EndpointCategory.BODY_COMPOSITION, None
    responder = _RESPONDER.search(title)
    if responder or re.search(r"(percentage|proportion|number) of (participants|patients).*(body[ -]?)?weight[ -](reduction|loss|lost)", t):
        if percent_thresholds(title):
            return EndpointCategory.WEIGHT_RESPONDER, responder_threshold(title)  # None when several are listed
        return EndpointCategory.WEIGHT_RESPONDER, float(responder.group(1)) if responder else None
    if re.search(r"onset of type 2 diabetes|progression to type 2 diabetes", t):
        return EndpointCategory.DIABETES_ONSET, None
    if re.search(r"\bmace\b|major adverse cardiovascular", t):
        return EndpointCategory.MACE, None
    if re.search(r"all[- ]cause (death|mortality)", t):
        return EndpointCategory.MORTALITY, None
    if re.search(r"\bcv death|cardiovascular death|myocardial infarction|\bstroke\b|heart failure|revasculari[sz]ation", t):
        return EndpointCategory.CV_COMPONENT, None
    if re.search(r"nephropathy|\begfr\b|glomerular|\buacr\b|albumin to creatinine|kidney", t):
        return EndpointCategory.KIDNEY, None
    if re.search(r"physical function|6[- ]minute walk|\b6mwd\b", t):
        return EndpointCategory.PHYSICAL_FUNCTION, None
    if re.search(r"\bpain\b", t):
        return EndpointCategory.PAIN, None
    if re.search(r"sf-?36|short form|iwqol|eq-?5d|patient[- ]reported|quality of life|pgis|promis", t):
        return EndpointCategory.PATIENT_REPORTED, None
    if re.search(r"hba1c|hemoglobin a1c|glycated h(a)?emoglobin", t):
        if re.search(r"(percentage|proportion|number) of participants|achiev|<\s*7|≤\s*6\.5|<\s*5\.7", t):
            return EndpointCategory.HBA1C_RESPONDER, None
        return EndpointCategory.HBA1C_CHANGE, None
    if re.search(r"fasting (serum |plasma |blood )?glucose", t):
        return EndpointCategory.FASTING_GLUCOSE, None
    if re.search(r"blood pressure|\bsbp\b|\bdbp\b", t):
        return EndpointCategory.BLOOD_PRESSURE, None
    if re.search(r"cholesterol|triglyceride|lipid|fatty acid|\bldl\b|\bhdl\b|\bvldl\b", t):
        return EndpointCategory.LIPIDS, None
    if re.search(r"body mass index|\bbmi\b", t):
        return EndpointCategory.BMI_CHANGE, None
    if re.search(r"waist circumference", t):
        return EndpointCategory.WAIST_CHANGE, None
    if re.search(r"body[ -]?weight", t):
        if re.search(r"percent|percentage|%", t) or "percent" in u or u.strip().startswith("%"):
            return EndpointCategory.WEIGHT_PCT_CHANGE, None
        return EndpointCategory.WEIGHT_ABS_CHANGE, None
    return EndpointCategory.OTHER, None


ELIGIBILITY_TAGS: dict[str, str] = {
    "BMI": r"\bbmi\b|body mass index",
    "DIABETES": r"diabet|hba1c|\bt2dm?\b|\bt1dm?\b",
    "WEIGHT_STABILITY": r"change in body weight|weight (change|gain|loss) (of )?(more|greater)|stable body weight|self-reported change",
    "PRIOR_OBESITY_TREATMENT": r"bariatric|metabolic surgery|weight[- ]loss (medication|drug)|anti-?obesity|glp-1|incretin",
    "PANCREATITIS": r"pancreatitis",
    "THYROID_MTC_MEN2": r"medullary thyroid|\bmen-?2\b|multiple endocrine neoplasia|calcitonin",
    "CARDIOVASCULAR": r"myocardial infarction|stroke|heart failure|cardiovascular|coronary|nyha",
    "RENAL": r"\begfr\b|renal|kidney|dialysis",
    "HEPATIC": r"hepat|liver|\balt\b|\bast\b",
    "PSYCHIATRIC": r"depress|psychiatric|suicid|phq-?9|c-ssrs",
    "PREGNANCY": r"pregnan|breast-?feed|lactat|contracept",
    "MALIGNANCY": r"malignan|cancer|neoplasm",
    "AGE": r"\bage\b|years of age|years old",
    "GASTROINTESTINAL": r"gastroparesis|gastric emptying|inflammatory bowel|gastrointestinal",
}


def tag_criterion(text: str) -> list[str]:
    lowered = text.lower()
    return [tag for tag, pattern in ELIGIBILITY_TAGS.items() if re.search(pattern, lowered)]
