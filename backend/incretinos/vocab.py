"""Shared text vocabulary: drug aliases, endpoint categorisation, label normalisation, time parsing."""

from __future__ import annotations

import re

from .enums import EndpointCategory

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

_ARM_STOPWORDS = {"qw", "sc", "qd", "once", "weekly", "daily", "participants", "of", "the", "and", "arm", "group"}


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


def arm_drug_slug(text: str) -> tuple[str | None, bool]:
    """(drug slug, is_placebo) for an arm label; combinations map to their combination slug."""
    drugs = detect_drugs(text)
    active = [d for d in drugs if d != "placebo"]
    if not active:
        return ("placebo", True) if "placebo" in drugs else (None, False)
    if len(active) == 1:
        return active[0], False
    return COMBINATION_SLUGS.get(frozenset(active), "+".join(sorted(active))), False


def norm_arm_label(label: str) -> str:
    """Order-insensitive key so '5 mg Tirzepatide' and 'Tirzepatide 5 mg' identify the same arm."""
    text = re.sub(r"[^a-z0-9.+ ]", " ", label.lower())
    tokens = [t for t in text.split() if t not in _ARM_STOPWORDS]
    return " ".join(sorted(tokens))[:300]


def parse_single_dose_mg(label: str) -> float | None:
    """Return the dose only when the label names exactly one milligram amount."""
    amounts = {float(m) for m in re.findall(r"(\d+(?:\.\d+)?)\s*(?:mg\b|milligram)", label.lower())}
    return amounts.pop() if len(amounts) == 1 else None


def to_float(value: object) -> float | None:
    """Parse a reported number; anything non-numeric (NA, '', None) stays None — never zero."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace(",", "").replace("−", "-")
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text):
        return None
    return float(text)


def to_int(value: object) -> int | None:
    number = to_float(value)
    return int(number) if number is not None and float(number).is_integer() else None


def parse_timeframe(timeframe: str | None) -> tuple[float | None, str | None]:
    """Return (assessment time in weeks, baseline definition) from a registry time-frame string."""
    if not timeframe:
        return None, None
    text = timeframe.strip()
    weeks = [float(w) for w in re.findall(r"week\s*(\d+(?:\.\d+)?)", text, flags=re.IGNORECASE)]
    weeks += [float(w) for w in re.findall(r"(\d+(?:\.\d+)?)\s*weeks?\b", text, flags=re.IGNORECASE)]
    months = [float(m) * 52.0 / 12.0 for m in re.findall(r"(\d+(?:\.\d+)?)\s*months?\b", text, flags=re.IGNORECASE)]
    years = [float(y) * 52.0 for y in re.findall(r"(\d+(?:\.\d+)?)\s*years?\b", text, flags=re.IGNORECASE)]
    candidates = weeks + months + years
    timepoint = max(candidates) if candidates else None
    baseline = None
    first = re.split(r",|\bto\b|\bthrough\b|\bup to\b", text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if re.search(r"baseline|randomi[sz]ation", first, flags=re.IGNORECASE):
        baseline = first[:200]
    return timepoint, baseline


_RESPONDER = re.compile(
    r"(?:≥|>=|at least|greater than or equal to)\s*(\d+(?:\.\d+)?)\s*%?\s*(?:percent\s*)?(?:body\s*)?weight (?:reduction|loss)", re.IGNORECASE
)


def categorize_endpoint(title: str, unit: str | None = None) -> tuple[EndpointCategory, float | None]:
    """Map an outcome title to a canonical category (and responder threshold where applicable)."""
    t = title.lower()
    u = (unit or "").lower()
    if re.search(r"pharmacokinetic|\bauc\b|\bcmax\b|concentration", t):
        return EndpointCategory.PHARMACOKINETICS, None
    if re.search(r"fat mass|lean mass|fat[- ]free mass|body composition|\bdxa\b|visceral adipose|body fat", t):
        return EndpointCategory.BODY_COMPOSITION, None
    responder = _RESPONDER.search(title)
    if responder or re.search(r"(percentage|proportion|number) of participants.*(body )?weight (reduction|loss)", t):
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
    if re.search(r"body ?weight", t):
        if re.search(r"percent|percentage|%", t) or "percent" in u or u.strip() == "%":
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
