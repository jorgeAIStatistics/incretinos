"""Deterministic claim verification.

Each structured claim read from a sentence is compared, field by field, with the evidence facts the
sentence cites. A claim is

- ``SUPPORTED`` when one cited fact satisfies everything the claim asserts;
- ``CONTRADICTED`` when the best-matching cited fact disagrees on a named field (both values are
  reported);
- ``UNVERIFIABLE`` when the claim asserts something that cannot be bound to a cited fact.

Binding is the point: a number is accepted only as the value (or interval limit, time point, n,
dose) of the same fact whose drug, arm, study and endpoint the claim names. A draft is kept only if
every sentence that asserts something is SUPPORTED. No language model takes part in this decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..engines.interpretation import guard_statement
from ..enums import EndpointRole, EstimandBasis, EstimandLabel, TriState, VerificationStatus
from . import lexicon as lx
from .claims import (
    ArmMention,
    SentenceReading,
    StructuredClaim,
    Vocabulary,
    _build_arms,
    _scan_mentions,
    read_sentence,
    split_sentences,
    stems,
    vocabulary_of,
)
from .facts import (
    ARM_RESULT,
    BETWEEN_ARM_ANALYSIS,
    DERIVED_TEXT,
    REGULATORY_STATUS,
    EvidenceFact,
    expand_drugs,
    fact_from_text,
)

SUPPORTED = "SUPPORTED"
CONTRADICTED = "CONTRADICTED"
UNVERIFIABLE = "UNVERIFIABLE"
NOT_FACTUAL = "NOT_FACTUAL"

_FAMILY = {  # endpoint family -> (stored categories, pattern over the stored endpoint title)
    "WEIGHT": ({"WEIGHT_PCT_CHANGE", "WEIGHT_ABS_CHANGE", "WEIGHT_RESPONDER"}, r"weight"),
    "HBA1C": ({"HBA1C_CHANGE", "HBA1C_RESPONDER"}, r"hba1c|\ba1c\b|hemoglobin"),
    "MACE": ({"MACE"}, r"\bmace\b|major adverse cardiovascular"),
    "MORTALITY": ({"MORTALITY"}, r"death|mortality|died"),
    "CV_DEATH": (set(), r"cardiovascular death|cv death"),
    "LEAN": (set(), r"lean|fat[- ]free"),
    "FAT": (set(), r"fat mass|body fat"),
    "WAIST": ({"WAIST_CHANGE"}, r"waist"),
    "BMI": ({"BMI_CHANGE"}, r"\bbmi\b|body mass index"),
    "BP": ({"BLOOD_PRESSURE"}, r"blood pressure"),
    "GLUCOSE": ({"FASTING_GLUCOSE"}, r"glucose"),
    "RESPONDER": ({"WEIGHT_RESPONDER", "HBA1C_RESPONDER"}, r"(percentage|proportion|number) of (participants|patients)|≥|>="),
}
_POSITIVE_STATISTICS = {"SUPERIORITY", "SIGNIFICANT", "NO_DIFFERENCE", "NI_MET"}


@dataclass
class Mismatch:
    field: str
    claimed: Any
    actual: Any
    kind: str = CONTRADICTED

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "claimed": self.claimed, "actual": self.actual, "kind": self.kind}

    def describe(self) -> str:
        if self.kind == CONTRADICTED:
            return f"{self.field}: the sentence says {_show(self.claimed)}; the cited evidence says {_show(self.actual)}"
        return f"{self.field}: {_show(self.claimed)} cannot be bound to the cited evidence ({_show(self.actual)})"


def _show(value: Any) -> str:
    if value is None:
        return "not reported"
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text if text not in ("-0", "") else "0"
    if isinstance(value, list | tuple | set):
        return ", ".join(_show(v) for v in value) or "none"
    return str(value)


_STAT_LABEL = {
    "SUPERIORITY": "superiority", "HYPOTHETICAL_SUPERIORITY": "superiority", "NEG_SUPERIORITY": "superiority not shown", "SIGNIFICANT": "statistical significance",
    "NOT_SIGNIFICANT": "not significant", "NOMINAL": "nominal significance", "NO_DIFFERENCE": "no difference / no effect", "NI_MET": "non-inferiority met",
    "NI_NOT_MET": "non-inferiority not met", "NI_TEST": "a non-inferiority test", "CI_INCLUDES_NULL": "interval includes no difference", "CI_EXCLUDES_NULL": "interval excludes no difference",
}
_UNIT_LABEL = {"PCT": "%", "PCT_PEOPLE": "% of participants", "PP": "percentage points", "KG": "kg", "KG_M2": "kg/m2", "MMHG": "mmHg", "MG_DL": "mg/dL", "CM": "cm", "FOLD": "-fold", "UNSUPPORTED": "(a unit no stored value uses)"}


def _stat(stat: dict[str, Any]) -> str:
    return f"{_STAT_LABEL.get(stat['name'], stat['name'].lower())} (“{stat['text']}”)"


def _eq(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= 1e-9 + 1e-9 * abs(float(b))


# --------------------------------------------------------------------------- what a fact licenses


def ci_excludes_null(fact: EvidenceFact) -> bool | None:
    if fact.ci_low is None or fact.ci_high is None:
        return None
    null = 1.0 if fact.unit_class == "RATIO" else 0.0
    return not (min(fact.ci_low, fact.ci_high) <= null <= max(fact.ci_low, fact.ci_high))


def is_exploratory(fact: EvidenceFact) -> bool:
    return fact.endpoint_role in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value) or fact.multiplicity_controlled == TriState.NO.value


def is_confirmatory(fact: EvidenceFact) -> bool:
    if is_exploratory(fact):
        return False
    if fact.endpoint_role == EndpointRole.PRIMARY.value:
        return True
    return fact.endpoint_role == EndpointRole.KEY_SECONDARY.value and fact.multiplicity_controlled == TriState.YES.value


def p_values_in(fact: EvidenceFact) -> list[tuple[str, float, str]]:
    """(operator, value, the part of the p-value text it came from)."""
    out = []
    for segment in re.split(r";", fact.p_value_text or ""):
        for match in re.finditer(r"(<=|>=|<|>|=|≤|≥)?\s*(\d*\.\d+(?:e-?\d+)?|\d+(?:\.\d+)?(?:e-?\d+)?)", segment):
            op = {"≤": "<=", "≥": ">="}.get(match.group(1) or "=", match.group(1) or "=")
            try:
                out.append((op, float(match.group(2)), segment.lower()))
            except ValueError:
                continue
    return out


def p_significant(fact: EvidenceFact) -> bool | None:
    """Whether a reported p-value for a difference is below 0.05. A p-value for non-inferiority does not count."""
    values = [(op, value) for op, value, segment in p_values_in(fact) if not re.search(r"non-?inferior", segment)]
    if not values:
        return None
    op, value = values[0]
    return value < 0.05 or (op in ("<", "<=") and value <= 0.05)


def effect_supported(fact: EvidenceFact) -> tuple[bool, str]:
    """May a sentence say the treatment lowered / raised the endpoint relative to the comparator?"""
    if fact.hypothesis_type == "NON_INFERIORITY" and fact.superiority_met != TriState.YES.value:
        return False, "a non-inferiority analysis with no superiority conclusion on record"
    excludes = ci_excludes_null(fact)
    if excludes is True:
        return True, ""
    if excludes is False:
        return False, f"the confidence interval {_show(fact.ci_low)} to {_show(fact.ci_high)} includes no difference"
    if p_significant(fact):
        return True, ""
    return False, "no confidence interval or p-value for a difference is reported"


def superiority_supported(fact: EvidenceFact) -> tuple[bool, str]:
    if fact.superiority_met == TriState.YES.value:
        return True, ""
    if fact.hypothesis_type == "SUPERIORITY" and not is_exploratory(fact) and effect_supported(fact)[0]:
        return True, ""
    if fact.hypothesis_type == "NON_INFERIORITY":
        return False, f"hypothesis type NON_INFERIORITY; superiority tested: {fact.superiority_tested}; superiority met: {fact.superiority_met}"
    if is_exploratory(fact):
        return False, f"endpoint role {fact.endpoint_role}; multiplicity controlled: {fact.multiplicity_controlled}"
    return False, f"hypothesis type {fact.hypothesis_type}; {effect_supported(fact)[1] or 'no superiority conclusion on record'}"


# --------------------------------------------------------------------------- text facts


@dataclass
class TextSignature:
    """What the wording of a text fact contains, read with the same extractor used for sentences."""

    drugs: set[str] = field(default_factory=set)
    doses: set[float] = field(default_factory=set)
    arm_pairs: set[tuple[str, float]] = field(default_factory=set)  # (drug, dose) as the statement writes them together
    studies: set[str] = field(default_factory=set)
    magnitudes: list[dict[str, Any]] = field(default_factory=list)
    numbers: set[float] = field(default_factory=set)
    intervals: list[tuple[float, float]] = field(default_factory=list)
    times: set[float] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    phases: set[str] = field(default_factory=set)
    features: set[str] = field(default_factory=set)
    endpoint_variants: set[str] = field(default_factory=set)
    exhaustive: set[str] = field(default_factory=set)
    vocabulary: set[str] = field(default_factory=set)
    negated_clauses: list[set[str]] = field(default_factory=list)
    plain_clauses: list[set[str]] = field(default_factory=list)
    negated_features: set[str] = field(default_factory=set)  # features stated only inside a negated clause
    lowered: str = ""


def text_signature(fact: EvidenceFact, vocab: Vocabulary) -> TextSignature:
    from .claims import normalise

    text = normalise(fact.text)
    reader = _scan_mentions(text, vocab)
    sig = TextSignature(lowered=text.lower(), vocabulary=vocabulary_of(f"{fact.text} {fact.context}"))
    for arm in _build_arms(reader, vocab):
        sig.arm_pairs |= {(drug, dose) for drug, values in arm.doses.items() if drug for dose in values}
    for mention in reader.mentions:
        data = mention.data
        if mention.kind == "DRUG" and data.get("slug"):
            sig.drugs |= set(expand_drugs(data["slug"]))
        elif mention.kind == "DOSE":
            sig.doses |= set(data["values"])
            sig.numbers |= set(data["values"])
        elif mention.kind == "STUDY":
            sig.studies.add(data["nct"])
        elif mention.kind == "MAG":
            sig.magnitudes.append(data)
            sig.numbers.add(abs(data["value"]))
        elif mention.kind == "RANGE":
            sig.intervals.append((data["low"], data["high"]))
            sig.numbers |= {abs(data["low"]), abs(data["high"])}
        elif mention.kind == "CI":
            sig.intervals.append((data["low"], data["high"]))
            sig.numbers |= {abs(data["low"]), abs(data["high"])}
            if data.get("level"):
                sig.numbers.add(data["level"])
        elif mention.kind == "CI_LABEL" and data.get("level"):
            sig.numbers.add(data["level"])
        elif mention.kind == "TIME":
            sig.times.add(round(data["weeks"], 3))
        elif mention.kind in ("COUNT", "MARGIN", "THRESHOLD", "NUMBER", "P"):
            sig.numbers.add(abs(float(data["value"])))
        elif mention.kind == "DATE":
            sig.dates.add(data["date"])
        elif mention.kind == "PHASE":
            sig.phases |= set(data["phases"])
        elif mention.kind == "PARAM":
            sig.features.add(f"PARAM_{data['family']}")
        elif mention.kind == "FEATURE":
            sig.features.add(data["name"])
            if data.get("variant"):
                sig.endpoint_variants.add(data["variant"])
            if data["name"] == "EXHAUSTIVE":
                sig.exhaustive.add(data["text"].lower())
    plain_features: set[str] = set()
    for clause in re.split(r"(?<=[.;:])\s+|,\s+(?=but\b|whereas\b|while\b|so\b|because\b|which\b)", text):
        words = re.findall(r"[A-Za-z][A-Za-z0-9_]*(?:[-/'][A-Za-z0-9_]+)*", clause)
        content = set()
        for word in words:
            if word.lower() not in lx.NEUTRAL and word.lower() not in lx.NEGATORS:
                content |= stems(word.lower())
        # "without diabetes" describes a population; it does not negate the clause it stands in
        negated = any((word.lower() in lx.NEGATORS and word.lower() not in ("without", "non")) or word.lower().endswith("n't") for word in words)
        (sig.negated_clauses if negated else sig.plain_clauses).append(content)
        clause_features = {m.data["name"] for m in _scan_mentions(clause, vocab).mentions if m.kind == "FEATURE"}
        if negated:
            sig.negated_features |= clause_features
        else:
            plain_features |= clause_features
    sig.negated_features -= plain_features
    return sig


# --------------------------------------------------------------------------- statements inside a text card
#
# A text card (a curated claim, a drug's regulatory profile, a registry record, a rule-based reading) is prose. Its
# wording cannot be checked for meaning, so a sentence citing it must agree with it nearly word for word: the numbers,
# brands, other drugs, endpoints, populations and conclusions the sentence names must all stand in ONE statement of the
# card (a sentence, or a part of one between semicolons or "and as <brand>"), with the same polarity.


@dataclass
class Segment:
    text: str
    sentence: int  # index of the card sentence this segment belongs to
    lowered: str = ""
    vocabulary: set[str] = field(default_factory=set)
    negated: bool = False
    approval: bool = False  # part of a sentence stating an approval
    class_statement: bool = False  # the pharmacologic-class statement of a drug profile
    features: set[str] = field(default_factory=set)
    numbers: set[float] = field(default_factory=set)
    magnitudes: set[float] = field(default_factory=set)
    times: set[float] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    doses: set[float] = field(default_factory=set)
    drugs: set[str] = field(default_factory=set)
    variants: set[str] = field(default_factory=set)
    statements: list[StructuredClaim] | None = None


_SEGMENT_SPLIT = re.compile(r";\s*|,?\s+and\s+(?=as\s+[A-Z])|,\s+(?=but\b|whereas\b|while\b)")
_NEGATION_WORDS = re.compile(r"\b(?:not|no|never|neither|nor|cannot|none)\b|n't\b", re.IGNORECASE)


def _top_level_split(text: str, pattern: re.Pattern) -> list[str]:
    depth, cuts = 0, []
    depths = []
    for ch in text:
        if ch in ")]":
            depth = max(0, depth - 1)
        depths.append(depth)
        if ch in "([":
            depth += 1
    for match in pattern.finditer(text):
        if depths[match.start()] == 0:
            cuts.append((match.start(), match.end()))
    parts, position = [], 0
    for start, end in cuts:
        parts.append(text[position:start])
        position = end
    parts.append(text[position:])
    return [part.strip() for part in parts if part.strip()]


def _is_negated(text: str) -> bool:
    plain = re.sub(r"\bwithout (?:type 2 |type ii |known )*diabetes\b|\bno(?:n)?[- ]diabetic\b|\bno (?:doubt|question)\b", " ", text, flags=re.IGNORECASE)
    return bool(_NEGATION_WORDS.search(plain))


def segments_of(fact: EvidenceFact, vocab: Vocabulary) -> list[Segment]:
    from .claims import normalise

    text = normalise(fact.text)
    out: list[Segment] = []
    for index, sentence in enumerate(re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text)):
        sentence_features = {m.data["name"] for m in _scan_mentions(sentence, vocab).mentions if m.kind == "FEATURE"}
        approval = "REG_APPROVED" in sentence_features and "REG_NOT_APPROVED" not in sentence_features
        for part in _top_level_split(sentence, _SEGMENT_SPLIT):
            seg = Segment(text=part, sentence=index, lowered=part.lower(), vocabulary=vocabulary_of(part), negated=_is_negated(part), approval=approval,
                          class_statement="pharmacologic class" in part.lower() or "receptor agonist" in part.lower())
            reader = _scan_mentions(part, vocab)
            seg.drugs |= {slug for m in vocab.drug_pattern().finditer(part) for slug in expand_drugs(vocab.drug_slug(m.group(1)))}
            for arm in _build_arms(reader, vocab):
                seg.drugs |= set(arm.drugs)
            for mention in reader.mentions:
                data = mention.data
                if mention.kind == "DRUG" and data.get("slug"):
                    seg.drugs |= set(expand_drugs(data["slug"]))
                elif mention.kind == "DOSE":
                    seg.doses |= set(data["values"])
                    seg.numbers |= set(data["values"])
                elif mention.kind == "MAG":
                    seg.magnitudes.add(round(abs(data["value"]), 9))
                    seg.numbers.add(abs(data["value"]))
                elif mention.kind in ("RANGE", "CI", "PAREN"):
                    seg.numbers |= {abs(data["low"]), abs(data["high"])}
                    if data.get("level"):
                        seg.numbers.add(data["level"])
                elif mention.kind == "CI_LABEL" and data.get("level"):
                    seg.numbers.add(data["level"])
                elif mention.kind == "TIME":
                    seg.times.add(round(data["weeks"], 3))
                    seg.numbers.add(round(data["weeks"], 3))
                elif mention.kind in ("COUNT", "MARGIN", "THRESHOLD", "NUMBER", "P"):
                    seg.numbers.add(abs(float(data["value"])))
                elif mention.kind == "COUNT_OF":
                    seg.numbers |= {float(data["value"]), float(data["n"])}
                elif mention.kind == "DATE":
                    seg.dates.add(data["date"])
                elif mention.kind == "PARAM":
                    seg.features.add(f"PARAM_{data['family']}")
                elif mention.kind == "FEATURE":
                    seg.features.add(data["name"])
                    if data.get("variant"):
                        seg.variants.add(data["variant"])
            out.append(seg)
    return out


def _statements(seg: Segment, vocab: Vocabulary) -> list[StructuredClaim]:
    if seg.statements is None:
        seg.statements = read_sentence(seg.text, vocab).claims
    return seg.statements


# --------------------------------------------------------------------------- field checks


@dataclass
class Context:
    vocab: Vocabulary
    hypothetical: bool = False
    all_phases: set[str] = field(default_factory=set)
    truncated: bool = False
    signatures: dict[str, TextSignature] = field(default_factory=dict)
    segment_cache: dict[str, list[Segment]] = field(default_factory=dict)

    def signature(self, fact: EvidenceFact) -> TextSignature:
        if fact.fact_id not in self.signatures:
            self.signatures[fact.fact_id] = text_signature(fact, self.vocab)
        return self.signatures[fact.fact_id]

    def segments(self, fact: EvidenceFact) -> list[Segment]:
        if fact.fact_id not in self.segment_cache:
            self.segment_cache[fact.fact_id] = segments_of(fact, self.vocab)
        return self.segment_cache[fact.fact_id]


def _grounded(term: str, vocabulary: set[str]) -> bool:
    if stems(term) & vocabulary:
        return True
    parts = [part for part in re.split(r"[-/']", term) if part]
    if any(ch.isdigit() for ch in term):
        return False  # a name with a digit in it (GLP-1, MACE-4, SURPASS-2) is one name: "GLP-2" is not grounded by "GLP" and "2"
    return len(parts) > 1 and all(part in lx.NEUTRAL or stems(part) & vocabulary for part in parts)


def _phase_digits(phase: str | None) -> set[str]:
    return set(re.findall(r"\d", phase or ""))


def _dose_check(arm, allowed: dict[str, list[float]], label: str | None, out: list[Mismatch], side: str, primary: list[float] | None = None) -> None:
    every = sorted({d for values in allowed.values() for d in values})
    named = [dose for values in arm.doses.values() for dose in values]
    if primary and named and all(any(_eq(dose, known) for known in every) for dose in named) and not any(_eq(dose, p) for dose in named for p in primary):
        # "(2 mg)" in "12 mg LY3437943 (2 mg)" is a starting dose: naming it alone names a different arm
        out.append(Mismatch(f"{side} dose", f"{_show(named)} {arm.dose_unit}", f"{_show(primary)} mg ({label})"))
        return
    for key, values in arm.doses.items():
        pool = allowed.get(key, []) if key and key in allowed and allowed.get(key) else every
        for dose in values:
            unit = arm.unit_of(key)
            if not any(_eq(dose, known) for known in pool):
                out.append(Mismatch(f"{side} dose", f"{_show(dose)} {unit}", f"{_show(pool)} mg" if pool else "no single dose stated for this arm"))
                continue
            stored = re.search(rf"(?<![\d.]){re.escape(_show(dose))}(?:\.0+)?\s*-?\s*(mg/kg|mg|mcg|µg|μg)", (label or "").lower())
            stored_unit = stored.group(1).replace("μ", "µ") if stored else "mg"
            if unit != stored_unit:
                out.append(Mismatch(f"{side} dose unit", f"{_show(dose)} {unit}", f"{_show(dose)} {stored_unit}"))


def _check_arms(claim: StructuredClaim, fact: EvidenceFact, out: list[Mismatch]) -> None:
    treatment = [a for a in claim.arms if a.role == "TREATMENT"]
    comparator = [a for a in claim.arms if a.role == "COMPARATOR"]
    pair = [a for a in claim.arms if a.role == "PAIR"]
    fact_treatment, fact_comparator = set(fact.drugs), set(fact.comparator_drugs)

    def side_matches(arms, drugs: set[str], doses: dict[str, list[float]]) -> bool:
        named = {d for a in arms for d in a.drugs}
        if named and named != drugs:
            return False
        scratch: list[Mismatch] = []
        own = doses is fact.arm_doses
        for arm in arms:
            _dose_check(arm, doses, fact.arm_label if own else fact.comparator_label, scratch, "arm", fact.arm_primary_doses if own else fact.comparator_primary_doses)
        return not scratch

    if fact.kind == ARM_RESULT:
        if comparator or pair:
            named = sorted({d for a in comparator + pair for d in a.drugs}) or "a comparator"
            out.append(Mismatch("comparator", named, "none: this is one arm's value, not a comparison between arms"))
        named = {d for a in treatment for d in a.drugs}
        if named and not fact_treatment:
            out.append(Mismatch("drug", sorted(named), "not attributable to a single arm", UNVERIFIABLE))
        elif named and named != fact_treatment:
            out.append(Mismatch("drug", sorted(named), sorted(fact_treatment)))
        else:
            for arm in treatment:
                _dose_check(arm, fact.arm_doses, fact.arm_label, out, "arm", fact.arm_primary_doses)
        return

    # between-arm analysis
    if pair:
        sides = [[pair[0]], pair[1:]] if len(pair) >= 2 else [pair, []]
        forward = side_matches(sides[0], fact_treatment, fact.arm_doses) and side_matches(sides[1], fact_comparator, fact.comparator_doses)
        backward = side_matches(sides[0], fact_comparator, fact.comparator_doses) and side_matches(sides[1], fact_treatment, fact.arm_doses)
        if not (forward or backward):
            out.append(Mismatch("compared arms", [a.text for a in pair], f"{fact.arm_label} versus {fact.comparator_label}"))
        elif backward and not forward and len(pair) >= 2 and (claim.magnitude_signed or claim.direction in ("DECREASE", "INCREASE") or (claim.magnitude is not None and fact.unit_class == "RATIO")):
            # "the difference between placebo and X was -17.8": read in the order written, the sign is reversed
            out.append(Mismatch("compared arms", f"{pair[0].text} minus {pair[1].text}", f"the estimate is {fact.arm_label} versus {fact.comparator_label}; naming the sides the other way round reverses it", UNVERIFIABLE))
        treatment = [a for a in treatment if a not in pair]
    if (treatment or comparator) and not (side_matches(treatment, fact_treatment, fact.arm_doses) and side_matches(comparator, fact_comparator, fact.comparator_doses)):
        if (treatment or comparator) and side_matches(treatment, fact_comparator, fact.comparator_doses) and side_matches(comparator, fact_treatment, fact.arm_doses) and (treatment and comparator):
            flipped_direction = {"DECREASE": "INCREASE", "INCREASE": "DECREASE"}.get(claim.direction or "")
            if claim.comparative and flipped_direction and fact.direction == flipped_direction and not claim.magnitude_signed:
                # "the hazard was higher with placebo than with dulaglutide": comparator as subject, comparative turned round
                claim.orientation_flipped = True
                return
            out.append(Mismatch("comparator", f"{' + '.join(a.text for a in treatment)} versus {' + '.join(a.text for a in comparator)}", f"{fact.arm_label} versus {fact.comparator_label} (treatment and comparator are reversed)"))
            return
        named_t = {d for a in treatment for d in a.drugs}
        named_c = {d for a in comparator for d in a.drugs}
        if named_t and named_t != fact_treatment:
            out.append(Mismatch("drug", sorted(named_t), sorted(fact_treatment) or fact.arm_label))
        else:
            for arm in treatment:
                _dose_check(arm, fact.arm_doses, fact.arm_label, out, "treatment", fact.arm_primary_doses)
        if named_c and named_c != fact_comparator:
            out.append(Mismatch("comparator", sorted(named_c), sorted(fact_comparator) or fact.comparator_label))
        else:
            for arm in comparator:
                _dose_check(arm, fact.comparator_doses, fact.comparator_label, out, "comparator", fact.comparator_primary_doses)


def _asserted_direction(claim: StructuredClaim, fact: EvidenceFact) -> str | None:
    """The direction the claim gives this fact's value.

    For one arm's value it is what the verb or noun says ("lost 13.7%, less than tirzepatide" is still a loss; the
    "less" orders two arms and is checked as an order). For a between-arm estimate a comparative belongs to the
    estimate itself ("a smaller reduction than placebo"), so the net reading applies.
    """
    if fact.kind == ARM_RESULT:
        return claim.base_direction or claim.direction
    if claim.orientation_flipped:
        return {"DECREASE": "INCREASE", "INCREASE": "DECREASE"}.get(claim.direction or "", claim.direction)
    return claim.direction


def _check_magnitude(claim: StructuredClaim, fact: EvidenceFact, out: list[Mismatch]) -> None:
    value, signed = claim.magnitude, claim.magnitude_signed
    if value is None and claim.magnitude_range is None and len(claim.numbers) == 1 and not claim.statistical:
        value, signed = claim.numbers[0], claim.numbers[0] < 0  # a lone number with no other role is read as the value
        claim.numbers = []
    if claim.magnitude_range is not None:
        low, high = claim.magnitude_range
        if fact.value_low is None or fact.value_high is None:
            out.append(Mismatch("magnitude", f"range {_show(low)} to {_show(high)}", fact.value if fact.value is not None else "no range reported"))
        elif not ({round(low, 9), round(high, 9)} == {round(fact.value_low, 9), round(fact.value_high, 9)}):
            out.append(Mismatch("magnitude", f"range {_show(low)} to {_show(high)}", f"range {_show(fact.value_low)} to {_show(fact.value_high)}"))
    if value is None:
        return
    unit = claim.unit
    if unit is not None:
        allowed = set(lx.UNIT_COMPATIBLE.get(unit, set()))
        if unit == "PP" and fact.kind == BETWEEN_ARM_ANALYSIS:
            allowed |= {"PCT", "PCT_PARTICIPANTS"}
        if fact.unit_class not in allowed:
            out.append(Mismatch("unit", _UNIT_LABEL.get(unit, unit.lower()), fact.unit or ("ratio" if fact.unit_class == "RATIO" else "no unit")))
        elif unit == "PCT" and fact.unit_class == "PCT_POINTS" and (claim.comparative or re.search(r"\brelative\b", claim.text, re.IGNORECASE)):
            # "17.8% more than placebo" reads as a relative difference; the stored estimate is in percentage points
            out.append(Mismatch("unit", f"{_show(value)}% (a relative difference)", f"{_show(fact.value)} percentage points"))
    if claim.bound and claim.magnitude is not None:
        out.append(Mismatch("magnitude", f"“{claim.bound}” {_show(value)}", f"{_show(fact.value)} as reported (an inexact bound is not the reported value)", UNVERIFIABLE))
    if fact.value is None:
        out.append(Mismatch("magnitude", value, f"only a range was disclosed ({_show(fact.value_low)} to {_show(fact.value_high)})" if fact.value_low is not None else None))
        return
    if fact.signed_change is not None:
        if signed:
            ok = _eq(value, fact.signed_change)
        elif _asserted_direction(claim, fact) in ("DECREASE", "INCREASE"):
            ok = _eq(abs(value), abs(fact.signed_change))
        else:
            ok = _eq(value, fact.value)
    else:
        ok = _eq(value, fact.value)
    if not ok:
        claimed = f"{'' if signed or value < 0 else ''}{_show(value)}"
        note = ""
        if _eq(abs(value), abs(fact.value)) and fact.signed_change is not None:
            note = " (the sign or stated direction does not match)"
        out.append(Mismatch("magnitude", claimed, f"{_show(fact.value)}{note}"))


def _check_direction(claim: StructuredClaim, fact: EvidenceFact, out: list[Mismatch]) -> None:
    direction = _asserted_direction(claim, fact)
    if direction is None:
        return
    if direction == "AMBIGUOUS":
        out.append(Mismatch("direction", "both an increase and a decrease", "one direction", UNVERIFIABLE))
        return
    if claim.magnitude_signed and claim.magnitude is not None and fact.signed_change is not None and _eq(claim.magnitude, fact.signed_change):
        # "reduced by -25%": the sign already carries the direction
        if (direction == "DECREASE") == (claim.magnitude < 0) or claim.magnitude == 0:
            return
    if fact.direction is None:
        out.append(Mismatch("direction", direction.lower(), "the stored value is not a change or a ratio, so no direction is on record", UNVERIFIABLE))
    elif fact.direction != direction:
        shown = {"DECREASE": "decrease", "INCREASE": "increase", "NO_CHANGE": "no change"}
        out.append(Mismatch("direction", shown[direction], f"{shown[fact.direction]} (stored value {_show(fact.value)})"))


def _mirrored_interval(claim: StructuredClaim, fact: EvidenceFact, low: float, high: float) -> bool:
    """ "a reduction of 20.9% (95% CI 19.9 to 21.8)": limits written as sizes of the reduction the sentence names."""
    if fact.signed_change is None or claim.magnitude is None or claim.magnitude_signed or _asserted_direction(claim, fact) != fact.direction:
        return False
    if min(low, high) < 0 or not (fact.ci_low <= 0 and fact.ci_high <= 0 or fact.ci_low >= 0 and fact.ci_high >= 0):
        return False
    return {round(low, 9), round(high, 9)} == {round(abs(fact.ci_low), 9), round(abs(fact.ci_high), 9)}


def _title_has(fact: EvidenceFact, text: str) -> bool:
    """The words are part of the endpoint's or arm's own name ("clinically significant hypoglycaemia", "equivalent dosage")."""
    return bool(text) and text.lower() in f"{fact.endpoint_title or ''} | {fact.arm_label or ''} | {fact.comparator_label or ''}".lower()


def _check_statistics(claim: StructuredClaim, fact: EvidenceFact, ctx: Context, out: list[Mismatch]) -> None:
    arm_level = fact.kind == ARM_RESULT
    names = {s["name"] for s in claim.statistical}
    labelled = bool({"EXPLORATORY", "POST_HOC", "NOT_MULTIPLICITY_CONTROLLED"} & set(claim.endpoint_role)) or "NOMINAL" in names
    for stat in claim.statistical:
        name, text = stat["name"], stat["text"]
        if _title_has(fact, text):
            continue
        if name == "EFFECT_WORD":
            if arm_level and not _grounded(text, ctx.signature(fact).vocabulary):
                out.append(Mismatch("statistical interpretation", f"“{text}”", "an evaluation one arm's reported value does not carry", UNVERIFIABLE))
            continue
        if name in ("SUPERIORITY", "HYPOTHETICAL_SUPERIORITY", "RANKING"):
            if ctx.hypothetical and name == "HYPOTHETICAL_SUPERIORITY" and not claim.subject_drugs and not claim.comparator_drugs:
                continue  # "showing superiority would require ...": named as a requirement, not asserted
            if arm_level:
                out.append(Mismatch("statistical interpretation", f"superiority (“{text}”)", "one arm's value: no between-arm test is attached to it"))
            else:
                ok, why = superiority_supported(fact)
                if not ok:
                    out.append(Mismatch("statistical interpretation", f"superiority (“{text}”)", why))
        elif name == "NEG_SUPERIORITY":
            if not arm_level and superiority_supported(fact)[0]:
                out.append(Mismatch("statistical interpretation", "superiority not shown", "superiority is on record for this analysis"))
        elif name == "SIGNIFICANT":
            if arm_level:
                out.append(Mismatch("statistical interpretation", f"statistical significance (“{text}”)", "one arm's value: no test is reported for it"))
            elif fact.hypothesis_type == "NON_INFERIORITY" and fact.superiority_met != TriState.YES.value:
                out.append(Mismatch("statistical interpretation", f"statistical significance (“{text}”)", "a non-inferiority analysis: its p-value is not a test of difference"))
            elif is_exploratory(fact) and not labelled:
                out.append(Mismatch("statistical interpretation", f"statistical significance (“{text}”)", f"nominal only: endpoint role {fact.endpoint_role}, multiplicity controlled {fact.multiplicity_controlled}"))
            elif not (p_significant(fact) or ci_excludes_null(fact)):
                out.append(Mismatch("statistical interpretation", f"statistical significance (“{text}”)", effect_supported(fact)[1] or f"p {fact.p_value_text or 'not reported'}"))
        elif name == "NOT_SIGNIFICANT":
            if arm_level:
                out.append(Mismatch("statistical interpretation", "not significant", "one arm's value: no test is reported for it", UNVERIFIABLE))
            elif not (ci_excludes_null(fact) is False or p_significant(fact) is False):
                out.append(Mismatch("statistical interpretation", "not significant", f"CI {_show(fact.ci_low)} to {_show(fact.ci_high)}; p {fact.p_value_text or 'not reported'}"))
        elif name == "NOMINAL":
            if fact.multiplicity_controlled != TriState.NO.value and "nominal" not in (fact.p_value_text or "").lower():
                out.append(Mismatch("multiplicity", "nominal", f"multiplicity controlled: {fact.multiplicity_controlled}"))
        elif name == "NO_DIFFERENCE":
            null = 1.0 if fact.unit_class == "RATIO" else 0.0
            if not (fact.value is not None and _eq(fact.value, null)):
                if fact.value is None:
                    why = "no value is reported; a missing value is not zero"
                elif ci_excludes_null(fact) is None:
                    why = f"reported value {_show(fact.value)}; no interval is reported, and a missing interval is not evidence of no difference"
                elif ci_excludes_null(fact) is False:
                    why = f"reported value {_show(fact.value)}; an interval that includes no difference ({_show(fact.ci_low)} to {_show(fact.ci_high)}) does not show there is none"
                else:
                    why = f"reported value {_show(fact.value)} with an interval that excludes no difference"
                out.append(Mismatch("statistical interpretation", f"no difference / no effect (“{text}”)", why))
        elif name == "NI_MET":
            if not arm_level and fact.hypothesis_type == "NON_INFERIORITY" and fact.ni_met == TriState.NOT_STATED.value:
                out.append(Mismatch("statistical interpretation", "non-inferiority met",
                                    f"the stored analysis does not state whether the non-inferiority margin was met (ni_met NOT_STATED; p {fact.p_value_text or 'not reported'}); "
                                    "a p-value is not read as the conclusion, so the curated record must state it", UNVERIFIABLE))
            elif arm_level or fact.hypothesis_type != "NON_INFERIORITY" or fact.ni_met != TriState.YES.value:
                out.append(Mismatch("statistical interpretation", "non-inferiority met", f"hypothesis type {fact.hypothesis_type or 'none'}; non-inferiority margin met: {fact.ni_met or 'not applicable'}"))
        elif name == "NI_NOT_MET":
            if arm_level or fact.ni_met != TriState.NO.value:
                out.append(Mismatch("statistical interpretation", "non-inferiority not met", f"non-inferiority margin met: {fact.ni_met or 'not applicable'}"))
        elif name == "NI_TEST":
            if arm_level or (fact.hypothesis_type != "NON_INFERIORITY" and not re.search(r"non-?inferior", fact.p_value_text or "", re.IGNORECASE)):
                out.append(Mismatch("hypothesis type", "non-inferiority", fact.hypothesis_type or "none: one arm's value"))
        elif name in ("CI_INCLUDES_NULL", "CI_EXCLUDES_NULL"):
            excludes = ci_excludes_null(fact)
            if excludes is None:
                out.append(Mismatch("confidence interval", text, None))
            elif excludes != (name == "CI_EXCLUDES_NULL"):
                out.append(Mismatch("confidence interval", text, f"{_show(fact.ci_low)} to {_show(fact.ci_high)}"))
    # effect language on a comparison needs an interval or test that supports it
    if not arm_level and claim.effect_asserted and not ({"NEG_SUPERIORITY", "NOT_SIGNIFICANT", "NO_DIFFERENCE"} & names) and "NOT_ESTABLISHED" not in claim.endpoint_role:
        ok, why = effect_supported(fact)
        if not ok:
            out.append(Mismatch("statistical interpretation", "a difference between the arms is asserted", why))
        elif is_exploratory(fact) and not labelled:
            out.append(Mismatch("endpoint role", "an effect stated without its exploratory / not multiplicity-controlled label", f"{fact.endpoint_role}; multiplicity controlled: {fact.multiplicity_controlled}"))


def _check_roles(claim: StructuredClaim, fact: EvidenceFact, out: list[Mismatch]) -> None:
    role, controlled = fact.endpoint_role, fact.multiplicity_controlled
    shown = f"{role}; multiplicity controlled: {controlled}"
    for asserted in claim.endpoint_role:
        if asserted == "PRIMARY" and role != EndpointRole.PRIMARY.value:
            out.append(Mismatch("endpoint role", "primary", role))
        elif asserted == "KEY_SECONDARY" and role != EndpointRole.KEY_SECONDARY.value:
            out.append(Mismatch("endpoint role", "key secondary", role))
        elif asserted == "SECONDARY" and role not in (EndpointRole.SECONDARY.value, EndpointRole.KEY_SECONDARY.value):
            out.append(Mismatch("endpoint role", "secondary", role))
        elif asserted == "EXPLORATORY" and role not in (EndpointRole.EXPLORATORY.value, EndpointRole.POST_HOC.value):
            out.append(Mismatch("endpoint role", "exploratory", role))
        elif asserted == "POST_HOC" and role != EndpointRole.POST_HOC.value:
            out.append(Mismatch("endpoint role", "post hoc", role))
        elif asserted == "CONFIRMATORY" and not is_confirmatory(fact):
            out.append(Mismatch("endpoint role", "confirmatory", shown))
        elif asserted == "DEMONSTRATED" and is_exploratory(fact):
            out.append(Mismatch("endpoint role", "demonstrated (confirmatory wording)", shown))
        elif asserted == "NOT_ESTABLISHED" and fact.kind == BETWEEN_ARM_ANALYSIS and is_confirmatory(fact) and effect_supported(fact)[0]:
            out.append(Mismatch("endpoint role", "not established", shown))
        elif asserted == "MULTIPLICITY_CONTROLLED" and controlled != TriState.YES.value:
            out.append(Mismatch("multiplicity", "controlled for multiplicity", controlled))
        elif asserted == "NOT_MULTIPLICITY_CONTROLLED" and controlled != TriState.NO.value:
            out.append(Mismatch("multiplicity", "not controlled for multiplicity", controlled))


def _check_provenance(claim: StructuredClaim, fact: EvidenceFact, sig: TextSignature | None, out: list[Mismatch]) -> None:
    for tier in claim.tier:
        if fact.tier is None:
            if sig is None or f"TIER_{tier}" not in sig.features:
                out.append(Mismatch("evidence tier", tier.lower().replace("_", " "), "no tier is recorded for this statement", UNVERIFIABLE))
        elif tier == "NOT_PEER_REVIEWED":
            if fact.tier == "PEER_REVIEWED":
                out.append(Mismatch("evidence tier", "not peer reviewed", fact.tier))
        elif fact.tier != tier and not (sig is not None and f"TIER_{tier}" in sig.features):
            out.append(Mismatch("evidence tier", tier, fact.tier))
    for status in claim.verification:
        actual = fact.verification_status or "no review status on record"
        if status == "HUMAN_VERIFIED" and not fact.all_human_verified:
            out.append(Mismatch("verification status", VerificationStatus.HUMAN_VERIFIED.value, actual))
        elif status == "NOT_HUMAN_VERIFIED" and fact.all_human_verified:
            out.append(Mismatch("verification status", "not human-verified", actual))
        elif status in ("QUOTE_ANCHORED", "MACHINE_EXTRACTED") and not (fact.verification_statuses and all(s == status for s in fact.verification_statuses)):
            out.append(Mismatch("verification status", status, actual))


def _check_wording(claim: StructuredClaim, vocabulary: set[str], out: list[Mismatch]) -> None:
    missing = [term for term in claim.residual_terms if not _grounded(term, vocabulary)]
    if missing:
        out.append(Mismatch("wording", missing, "these terms are not in the cited evidence", UNVERIFIABLE))
    for word in claim.exhaustive:
        same_claim = next((group for group in lx.EXHAUSTIVE_CLASSES if word in group), {word})
        if not any(_grounded(member, vocabulary) for member in same_claim):
            out.append(Mismatch("completeness", f"“{word}”", "an exhaustive statement the cited evidence does not make", UNVERIFIABLE))


def check_structured(claim: StructuredClaim, fact: EvidenceFact, ctx: Context) -> list[Mismatch]:
    """Every field of ``claim`` against one arm-level result or between-arm analysis."""
    out: list[Mismatch] = []
    if claim.unknown_studies:
        out.append(Mismatch("study", claim.unknown_studies, fact.study_name))
    if claim.studies and fact.study_nct not in claim.studies:
        out.append(Mismatch("study", claim.studies, f"{fact.study_name} ({fact.study_nct})"))
    elif len(claim.studies) > 1:
        out.append(Mismatch("study", claim.studies, f"one study: {fact.study_name}", UNVERIFIABLE))
    _check_arms(claim, fact, out)

    title = (fact.endpoint_title or "").lower()
    for endpoint in claim.endpoints:
        categories, pattern = _FAMILY.get(endpoint["family"], (set(), r"$^"))
        if not (fact.endpoint_category in categories or re.search(pattern, title)):
            out.append(Mismatch("endpoint", endpoint["text"], fact.endpoint_title))
        elif endpoint.get("variant") and endpoint["variant"] not in title and re.search(r"mace-\d", title):
            out.append(Mismatch("endpoint", endpoint["text"], fact.endpoint_title))
        elif endpoint.get("variant") and endpoint["variant"] not in title:
            out.append(Mismatch("endpoint", endpoint["text"], fact.endpoint_title, UNVERIFIABLE))
    if claim.threshold is not None:
        stated = fact.endpoint_threshold
        if stated is None:
            found = re.search(r"(?:≥|>=|at least)\s*(\d+(?:\.\d+)?)\s*%", title)
            stated = float(found.group(1)) if found else None
        if stated is None or not _eq(claim.threshold, stated):
            out.append(Mismatch("responder threshold", f"≥{_show(claim.threshold)}%", f"≥{_show(stated)}%" if stated is not None else "no threshold: not a responder endpoint"))
    if claim.parameter:
        family = fact.param_family
        if claim.parameter == "DIFFERENCE":
            if fact.kind == ARM_RESULT:
                out.append(Mismatch("effect measure", "between-arm difference", "one arm's value, not a difference between arms"))
            elif family not in ("DIFFERENCE", None):
                out.append(Mismatch("effect measure", "difference", fact.param_type))
        elif family != claim.parameter:
            out.append(Mismatch("effect measure", {"HR": "hazard ratio", "OR": "odds ratio", "RR": "risk ratio"}.get(claim.parameter, claim.parameter), fact.param_type or "one arm's value"))

    _check_magnitude(claim, fact, out)
    _check_direction(claim, fact, out)

    for weeks in claim.timepoints_weeks:
        if fact.timepoint_weeks is not None and abs(weeks - fact.timepoint_weeks) <= (0.05 if float(weeks).is_integer() else 0.02 * fact.timepoint_weeks):
            continue
        assessment = next((i for i, t in enumerate(claim.timepoints_weeks) if fact.timepoint_weeks is not None and abs(t - fact.timepoint_weeks) <= 0.05), None)
        if assessment is not None and claim.timepoints_weeks.index(weeks) < assessment and float(weeks).is_integer() and re.search(rf"\bweek\s*{int(weeks)}\b", (fact.baseline_definition or "").lower()):
            continue  # the baseline itself, named before the assessment: "from randomization (week 36) to week 88"
        out.append(Mismatch("time point", f"week {_show(weeks)}", f"week {_show(fact.timepoint_weeks)}" if fact.timepoint_weeks is not None else None))
    for population in claim.population:
        if population == fact.diabetes_status:
            continue
        labels = {"T2D": "with type 2 diabetes", "NO_T2D": "without type 2 diabetes", "MIXED": "with or without type 2 diabetes", "T1D": "type 1 diabetes", "NOT_STATED": "diabetes status not stated"}
        if population == "T2D" and re.search(r"diabetes", title):
            continue  # the endpoint itself names diabetes (e.g. onset of type 2 diabetes)
        out.append(Mismatch("population", labels.get(population, population), labels.get(fact.diabetes_status or "NOT_STATED", fact.diabetes_status)))
    for estimand in claim.estimand:
        actual = fact.estimand_label or EstimandLabel.NOT_STATED.value
        if estimand == "INFERRED":
            if fact.estimand_basis != EstimandBasis.INFERRED_FROM_ANALYSIS_DESCRIPTION.value:
                out.append(Mismatch("estimand basis", "inferred", fact.estimand_basis))
        elif estimand == "NOT_STATED":
            if actual in (EstimandLabel.EFFICACY.value, EstimandLabel.TREATMENT_REGIMEN.value):
                out.append(Mismatch("estimand", "not stated", actual))
        elif estimand != actual:
            out.append(Mismatch("estimand", estimand, actual))

    if claim.ci is not None:
        low, high, level = claim.ci["low"], claim.ci["high"], claim.ci.get("level")
        if fact.ci_low is None or fact.ci_high is None:
            out.append(Mismatch("confidence interval", f"{_show(low)} to {_show(high)}", None))
        elif {round(low, 9), round(high, 9)} != {round(fact.ci_low, 9), round(fact.ci_high, 9)} and not _mirrored_interval(claim, fact, low, high):
            out.append(Mismatch("confidence interval", f"{_show(low)} to {_show(high)}", f"{_show(fact.ci_low)} to {_show(fact.ci_high)}"))
        elif level is not None and fact.ci_level is not None and not _eq(level, fact.ci_level):
            out.append(Mismatch("confidence level", f"{_show(level)}%", f"{_show(fact.ci_level)}%"))
    elif claim.ci_level_only is not None and fact.ci_level is not None and not _eq(claim.ci_level_only, fact.ci_level):
        out.append(Mismatch("confidence level", f"{_show(claim.ci_level_only)}%", f"{_show(fact.ci_level)}%"))
    for item in claim.missing:
        if item == "CI" and fact.ci_low is not None:
            out.append(Mismatch("confidence interval", "not reported", f"{_show(fact.ci_low)} to {_show(fact.ci_high)}"))
        elif item == "N" and fact.n is not None:
            out.append(Mismatch("n", "not reported", fact.n))
        elif item == "P" and fact.p_value_text:
            out.append(Mismatch("p-value", "not reported", fact.p_value_text))
        elif item == "MULTIPLICITY" and fact.multiplicity_controlled != TriState.NOT_STATED.value:
            out.append(Mismatch("multiplicity", "not stated", fact.multiplicity_controlled))
        elif item == "TIME" and fact.timepoint_weeks is not None:
            out.append(Mismatch("time point", "not stated", f"week {_show(fact.timepoint_weeks)}"))
    for count in claim.counts:
        if not (_eq(count, fact.n) or (fact.unit_class == "COUNT_PARTICIPANTS" and _eq(count, fact.value))):
            out.append(Mismatch("n", count, fact.n))
    if claim.dispersion is not None:
        stored_kind = "SE" if "error" in (fact.dispersion_type or "").lower() else "SD" if "deviation" in (fact.dispersion_type or "").lower() else None
        if fact.dispersion_value is None or stored_kind != claim.dispersion["measure"] or not _eq(claim.dispersion["value"], fact.dispersion_value):
            out.append(Mismatch("dispersion", f"{claim.dispersion['measure']} {_show(claim.dispersion['value'])}", f"{fact.dispersion_type} {_show(fact.dispersion_value)}" if fact.dispersion_value is not None else None))
    if claim.p_value is not None:
        known = p_values_in(fact)
        op, value = claim.p_value["op"], claim.p_value["value"]
        if not known:
            out.append(Mismatch("p-value", f"p {op} {_show(value)}", None))
        elif not any(_eq(value, v) and ((o in ("<", "<=")) == (op in ("<", "<="))) and ((o in (">", ">=")) == (op in (">", ">="))) for o, v, _ in known):
            out.append(Mismatch("p-value", f"p {op} {_show(value)}", f"p {fact.p_value_text}"))
    if claim.ni_margin is not None and not _eq(claim.ni_margin, fact.ni_margin):
        out.append(Mismatch("non-inferiority margin", claim.ni_margin, fact.ni_margin))
    for phase in claim.phases:
        if phase not in _phase_digits(fact.phase):
            out.append(Mismatch("phase", f"Phase {phase}", (fact.phase or "not stated").replace("PHASE", "Phase ")))

    if claim.event_count is not None:
        if fact.unit_class != "COUNT_PARTICIPANTS" or not _eq(claim.event_count, fact.value):
            out.append(Mismatch("count", claim.event_count, f"{_show(fact.value)} {fact.unit or ''}".strip() if fact.value is not None else None))
    for phrase in claim.causal:
        if _title_has(fact, phrase):
            continue
        if fact.kind == ARM_RESULT or fact.is_placebo or "placebo" in claim.subject_drugs:
            out.append(Mismatch("causation", f"“{phrase}”", "one arm's change from baseline: a within-arm change is not a treatment effect, and a placebo arm's change is not caused by placebo", UNVERIFIABLE))
        else:
            ok, why = effect_supported(fact)
            if not ok:
                out.append(Mismatch("causation", f"“{phrase}”", why))
    if claim.subgroup:
        # "adults with knee osteoarthritis": a subgroup must be the stored population, not a registered condition of the trial
        population = vocabulary_of(" ".join(part for part in (fact.population_text, fact.endpoint_title, fact.arm_label, fact.comparator_label) if part))
        outside = [word for word in claim.subgroup if not _grounded(word, population)]
        if outside:
            out.append(Mismatch("population", f"participants with {' '.join(claim.subgroup)}", f"the result is reported for {fact.population_text or 'the trial population'}; a registered condition is not a subgroup result", UNVERIFIABLE))

    _check_statistics(claim, fact, ctx, out)
    _check_roles(claim, fact, out)
    _check_provenance(claim, fact, None, out)

    sig = ctx.signature(fact)
    for date in claim.dates:
        if not any(known.startswith(date) for known in sig.dates):
            out.append(Mismatch("date", date, "not part of this result", UNVERIFIABLE))
    for number in claim.numbers:
        out.append(Mismatch("number", number, "no role could be read for this number", UNVERIFIABLE))
    for fragment in claim.unread:
        out.append(Mismatch("number", f"“{fragment}”", "digits that could not be read as a number or as part of a known name", UNVERIFIABLE))
    for status in claim.regulatory:
        out.append(Mismatch("regulatory status", status.lower().replace("_", " "), "a trial result says nothing about regulatory status", UNVERIFIABLE))
    for negator in claim.negators:
        out.append(Mismatch("negation", f"“{negator}”", "a negated statement cannot be bound to a reported value", UNVERIFIABLE))
    _check_wording(claim, sig.vocabulary, out)
    return out


def check_text(claim: StructuredClaim, fact: EvidenceFact, ctx: Context) -> list[Mismatch]:
    """A claim against a fact that exists only as a statement: the sentence may assert nothing the statement does not."""
    out: list[Mismatch] = []
    sig = ctx.signature(fact)
    known_studies = sig.studies | ({fact.study_nct} if fact.study_nct else set())
    for nct in claim.studies:
        if nct not in known_studies:
            out.append(Mismatch("study", nct, sorted(known_studies) or "no study named"))
    for name in claim.unknown_studies:
        if name.lower() not in sig.lowered:
            out.append(Mismatch("study", name, sorted(known_studies) or "no study named"))
    # a drug named anywhere in the card's wording, including inside a study's name ("Retatrutide Phase 2 (obesity)")
    known_drugs = sig.drugs | set(fact.drugs) | {slug for m in ctx.vocab.drug_pattern().finditer(fact.text) for slug in expand_drugs(ctx.vocab.drug_slug(m.group(1)))}
    named = {d for arm in claim.arms for d in arm.drugs}
    if fact.kind == REGULATORY_STATUS and claim.subject_drugs and not set(claim.subject_drugs) <= set(fact.drugs):
        out.append(Mismatch("drug", sorted(claim.subject_drugs), sorted(fact.drugs)))
    elif named - known_drugs:
        out.append(Mismatch("drug", sorted(named - known_drugs), sorted(known_drugs) or "no drug named"))
    for arm in claim.arms:
        for drug, values in arm.doses.items():
            paired = {dose for name, dose in sig.arm_pairs if name == drug}
            for dose in values:
                if paired and not any(_eq(dose, known) for known in paired):
                    out.append(Mismatch("dose", f"{drug} {_show(dose)} mg", f"{drug} {_show(sorted(paired))} mg"))
                elif not any(_eq(dose, known) for known in sig.doses):
                    out.append(Mismatch("dose", f"{_show(dose)} mg", f"{_show(sorted(sig.doses))} mg" if sig.doses else "no dose named"))

    text_directions = {name[4:].split("_")[0] for name in sig.features if name.startswith("DIR_") and "COMPARATIVE" not in name}
    value, signed = claim.magnitude, claim.magnitude_signed
    if value is not None:
        claim_sign = (-1 if value < 0 else 1) if signed else {"DECREASE": -1, "INCREASE": 1}.get(claim.direction or "")
        matched = False
        for candidate in sig.magnitudes:
            if not _eq(abs(value), abs(candidate["value"])):
                continue
            if claim.unit and candidate["unit"] and claim.unit != candidate["unit"] and {claim.unit, candidate["unit"]} not in ({"PCT", "PP"}, {"PCT", "PCT_PEOPLE"}):
                continue
            cand_sign = (-1 if candidate["value"] < 0 else 1) if candidate["signed"] else (-1 if text_directions == {"DECREASE"} else 1 if text_directions == {"INCREASE"} else None)
            if claim_sign is None or cand_sign is None or claim_sign == cand_sign:
                matched = True
                break
        if not matched and claim.unit is None and abs(value) in sig.numbers:
            matched = True
        if not matched:
            out.append(Mismatch("magnitude", f"{_show(value)}{' ' + _UNIT_LABEL.get(claim.unit, claim.unit) if claim.unit else ''}", [f"{_show(m['value'])}{' ' + _UNIT_LABEL.get(m['unit'], m['unit']) if m['unit'] else ''}" for m in sig.magnitudes] or "no such value in the cited statement"))
    if claim.magnitude_range is not None:
        low, high = claim.magnitude_range
        if not any({round(low, 9), round(high, 9)} == {round(a, 9), round(b, 9)} for a, b in sig.intervals):
            out.append(Mismatch("magnitude", f"range {_show(low)} to {_show(high)}", "no such range in the cited statement"))
    if claim.direction in ("DECREASE", "INCREASE") and claim.direction not in text_directions and not (claim.magnitude_signed and value is not None):
        opposite = {"DECREASE": "INCREASE", "INCREASE": "DECREASE"}[claim.direction]
        out.append(Mismatch("direction", claim.direction.lower(), "the opposite direction" if opposite in text_directions else "no direction stated", CONTRADICTED if opposite in text_directions else UNVERIFIABLE))
    elif claim.direction == "AMBIGUOUS" and len(text_directions) < 2:
        out.append(Mismatch("direction", "both an increase and a decrease", "one direction", UNVERIFIABLE))
    if claim.ci is not None:
        low, high = claim.ci["low"], claim.ci["high"]
        if not any({round(low, 9), round(high, 9)} == {round(a, 9), round(b, 9)} for a, b in sig.intervals):
            out.append(Mismatch("confidence interval", f"{_show(low)} to {_show(high)}", [f"{_show(a)} to {_show(b)}" for a, b in sig.intervals] or None))
        elif claim.ci.get("level") and claim.ci["level"] not in sig.numbers:
            out.append(Mismatch("confidence level", f"{_show(claim.ci['level'])}%", "not stated"))
    for weeks in claim.timepoints_weeks:
        if not any(abs(weeks - known) <= 0.05 for known in sig.times):
            out.append(Mismatch("time point", f"week {_show(weeks)}", [f"week {_show(t)}" for t in sorted(sig.times)] or None))
    for fragment in claim.unread:
        out.append(Mismatch("number", f"“{fragment}”", "digits that could not be read as a number or as part of a known name", UNVERIFIABLE))
    for date in claim.dates:
        if not any(known.startswith(date) for known in sig.dates):
            out.append(Mismatch("date", date, sorted(sig.dates) or None))
    for phase in claim.phases:
        if phase not in (sig.phases | _phase_digits(fact.phase) | (ctx.all_phases if fact.kind == DERIVED_TEXT else set())):
            out.append(Mismatch("phase", f"Phase {phase}", sorted(sig.phases | _phase_digits(fact.phase)) or "not stated"))
    loose_numbers = [*claim.counts, *claim.numbers, *([claim.threshold] if claim.threshold is not None else []), *([claim.ni_margin] if claim.ni_margin is not None else []),
                     *([claim.dispersion["value"]] if claim.dispersion else []),
                     *([claim.p_value["value"]] if claim.p_value else []), *([claim.ci_level_only] if claim.ci_level_only else [])]
    for number in loose_numbers:
        if not any(_eq(abs(float(number)), known) for known in sig.numbers):
            out.append(Mismatch("number", number, "not in the cited statement"))

    def need(feature: str, label: str, shown: str) -> None:
        if feature not in sig.features:
            out.append(Mismatch(label, shown, "not stated in the cited statement", UNVERIFIABLE))

    for estimand in claim.estimand:
        need(f"ESTIMAND_{estimand}", "estimand", estimand)
    for population in claim.population:
        need(f"POP_{population}", "population", population)
    for role in claim.endpoint_role:
        feature = {"NOT_ESTABLISHED": "NEG_CONFIRMATORY", "MULTIPLICITY_CONTROLLED": "MULT_CONTROLLED", "NOT_MULTIPLICITY_CONTROLLED": "MULT_NOT_CONTROLLED"}.get(role, role)
        if feature == "NEG_CONFIRMATORY" and ({"NEG_CONFIRMATORY", "NEG_SUPERIORITY"} & sig.features):
            continue
        need(feature, "endpoint role", role.lower().replace("_", " "))
    for item in claim.missing:
        need({"CI": "CI_NOT_REPORTED", "N": "N_NOT_REPORTED", "P": "P_NOT_REPORTED", "MULTIPLICITY": "MULT_NOT_STATED", "TIME": "TIME_NOT_STATED"}[item], "missing information", item)
    for endpoint in claim.endpoints:
        if endpoint["family"] != "RESPONDER" and f"EP_{endpoint['family']}" not in sig.features and not _grounded(endpoint["text"].split()[-1], sig.vocabulary):
            out.append(Mismatch("endpoint", endpoint["text"], "not named in the cited statement", UNVERIFIABLE))
        elif endpoint.get("variant") and endpoint["variant"] not in sig.endpoint_variants:
            out.append(Mismatch("endpoint", endpoint["text"], sorted(sig.endpoint_variants) or "not named in the cited statement"))
    param_word = {"HR": "hazard", "OR": "odds", "RR": "risk", "DIFFERENCE": "difference"}.get(claim.parameter or "")
    if claim.parameter and f"PARAM_{claim.parameter}" not in sig.features and not (param_word and param_word in sig.vocabulary):
        out.append(Mismatch("effect measure", claim.parameter, "not named in the cited statement", UNVERIFIABLE))
    drugs_named = bool(named)
    for stat in claim.statistical:
        name = stat["name"]
        if name == "HYPOTHETICAL_SUPERIORITY" and ctx.hypothetical:
            # "Showing superiority would require ...": a requirement, not a result. The frame must govern the mention (the
            # mention is the subject of "would require"), and a sentence that names drugs is not a requirement but a claim about them.
            if _requirement_only(claim, stat, ctx):
                continue
            if fact.kind == DERIVED_TEXT:
                out.append(Mismatch("statistical interpretation", _stat(stat), "a rule-based reading never asserts superiority; a sentence naming the drugs is not a requirement but a claim about them"))
                continue
        if name == "RANKING" and fact.kind == DERIVED_TEXT:
            out.append(Mismatch("statistical interpretation", _stat(stat), "a rule-based reading never ranks treatments"))
            continue
        if name == "EFFECT_WORD":
            if name not in sig.features and not _grounded(stat["text"], sig.vocabulary):
                out.append(Mismatch("statistical interpretation", f"“{stat['text']}”", "an evaluation the cited statement does not make", UNVERIFIABLE))
            continue
        if name in _POSITIVE_STATISTICS and fact.kind == DERIVED_TEXT:
            out.append(Mismatch("statistical interpretation", _stat(stat), "a rule-based reading never asserts this"))
        elif name == "HYPOTHETICAL_SUPERIORITY":
            if not ({"SUPERIORITY", "HYPOTHETICAL_SUPERIORITY", "NEG_SUPERIORITY"} & sig.features):
                out.append(Mismatch("statistical interpretation", _stat(stat), "not stated in the cited statement", UNVERIFIABLE))
        elif name not in sig.features:
            contradicted = {"SUPERIORITY": "NEG_SUPERIORITY", "SIGNIFICANT": "NOT_SIGNIFICANT", "NI_MET": "NI_NOT_MET"}.get(name)
            out.append(Mismatch("statistical interpretation", _stat(stat), "the cited statement says the opposite" if contradicted in sig.features else "not stated in the cited statement", CONTRADICTED if contradicted in sig.features else UNVERIFIABLE))
    if claim.effect_asserted and claim.comparative and (claim.comparator_drugs or len(named) >= 2) and not ({"DIR_UP_COMPARATIVE", "DIR_DOWN_COMPARATIVE"} & sig.features):
        out.append(Mismatch("statistical interpretation", "a comparison between treatments", "the cited statement makes no such comparison", UNVERIFIABLE))
    elif fact.kind == DERIVED_TEXT and claim.comparative and drugs_named and not claim.negators:
        out.append(Mismatch("statistical interpretation", "a comparison between named treatments", "a rule-based reading never orders treatments"))

    for status in claim.regulatory:
        if fact.kind == REGULATORY_STATUS:
            actual = fact.regulatory_status
            if status == "APPROVED" and actual != "APPROVED":
                out.append(Mismatch("regulatory status", "approved", actual))
            elif status in ("NOT_APPROVED", "INVESTIGATIONAL") and actual == "APPROVED" and f"REG_{status}" not in sig.features:
                out.append(Mismatch("regulatory status", status.lower().replace("_", " "), actual))
            elif status in ("NOT_APPROVED", "INVESTIGATIONAL") and actual not in ("APPROVED", "INVESTIGATIONAL") and f"REG_{status}" not in sig.features:
                out.append(Mismatch("regulatory status", status.lower().replace("_", " "), actual, UNVERIFIABLE))
        elif f"REG_{status}" not in sig.features:
            out.append(Mismatch("regulatory status", status.lower().replace("_", " "), "not stated in the cited statement", UNVERIFIABLE))
    _check_provenance(claim, fact, sig, out)

    # a negation may be neither dropped nor added
    sentence_stems: set[str] = set()
    for term in claim.residual_terms:
        sentence_stems |= stems(term)
    has_negation = bool(claim.negators) or bool({"NOT_APPROVED"} & set(claim.regulatory)) or any(s["name"].startswith(("NEG_", "NOT_", "NO_")) for s in claim.statistical) or "NOT_ESTABLISHED" in claim.endpoint_role
    claimed_features = (
        {f"POP_{p}" for p in claim.population} | {f"EP_{e['family']}" for e in claim.endpoints if e["family"] != "RESPONDER"} | {f"REG_{r}" for r in claim.regulatory}
        | {f"TIER_{t}" for t in claim.tier} | {s["name"] for s in claim.statistical} | {f"ESTIMAND_{e}" for e in claim.estimand} | set(claim.endpoint_role)
    )
    if not has_negation and claimed_features & sig.negated_features:
        # "No type 2 diabetes indication appears in the label" cannot support "approved for type 2 diabetes"
        out.append(Mismatch("negation", sorted(f.lower().replace("_", " ") for f in claimed_features & sig.negated_features), "the cited statement says this only in the negative", CONTRADICTED))
    if not has_negation:
        for clause in sig.negated_clauses:
            shared = sentence_stems & clause
            if len({min(stems(t), key=len) for t in claim.residual_terms if stems(t) & clause}) >= 2 and not any(len({min(stems(t), key=len) for t in claim.residual_terms if stems(t) & plain}) >= 2 and shared <= plain for plain in sig.plain_clauses):
                out.append(Mismatch("negation", "an unqualified statement", "the cited statement is negated", CONTRADICTED))
                break
    elif claim.negators and not sig.negated_clauses:
        out.append(Mismatch("negation", f"“{claim.negators[0]}”", "the cited statement is not negated", UNVERIFIABLE))
    _check_wording(claim, sig.vocabulary, out)
    _check_text_binding(claim, fact, ctx, out)
    return out


_POSITIVE_FEATURES = {"SUPERIORITY", "SIGNIFICANT", "NI_MET", "NI_TEST", "NO_DIFFERENCE", "CI_EXCLUDES_NULL", "RANKING", "EFFECT_WORD", "NOMINAL", "HYPOTHETICAL_SUPERIORITY"}
_ROLE_FEATURES = {"PRIMARY": "PRIMARY", "KEY_SECONDARY": "KEY_SECONDARY", "SECONDARY": "SECONDARY", "EXPLORATORY": "EXPLORATORY", "POST_HOC": "POST_HOC", "CONFIRMATORY": "CONFIRMATORY",
                  "DEMONSTRATED": "DEMONSTRATED", "MULTIPLICITY_CONTROLLED": "MULT_CONTROLLED"}


_MODALS = frozenset({"should", "must", "shall", "ought", "need", "needs"})


def _requirement_only(claim: StructuredClaim, stat: dict[str, Any], ctx: Context) -> bool:
    """ "Showing superiority would require ...": superiority named as a requirement, governed by the frame that follows it,
    in a sentence that names no drug (a sentence about named drugs is a claim about them, whatever its frame)."""
    if not ctx.hypothetical or stat["name"] != "HYPOTHETICAL_SUPERIORITY" or claim.subject_drugs or claim.comparator_drugs:
        return False
    position = claim.text.lower().find(stat["text"])
    if position < 0 or lx.HYPOTHETICAL_FRAME_RE.search(claim.text, position) is None:
        return False
    return not re.search(r"\b(?:achiev|attain|reach|met|meet)\w*", claim.text[:position], re.IGNORECASE)


def _content_stems(text: str, skip: set[str]) -> set[str]:
    """Stems of the words of ``text`` that carry content: not neutral, not a negator, not a name the card is about."""
    out: set[str] = set()
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]*(?:[-/'][A-Za-z0-9]+)*", text):
        low = re.sub(r"'s$", "", word.lower())
        if low in lx.NEUTRAL or low in lx.NEGATORS or low in _MODALS or low.endswith("n't") or low in skip or any(ch.isdigit() for ch in low) and "-" not in low:
            continue
        out.add(min(stems(low), key=len))
    return out


def _subject_names(fact: EvidenceFact, vocab: Vocabulary) -> set[str]:
    """Words naming what the card is about (its drug's generic and code names, its study): bound to the whole card, not to one statement."""
    names: set[str] = set()
    for slug in fact.drugs:
        for alias in vocab.drug_aliases.get(slug, [slug]):
            if alias == slug or alias.replace(" ", "-") == slug or re.fullmatch(r"ly\d+", alias):
                names |= set(alias.lower().split())
        names |= set(slug.split("-"))
    for name in (fact.study_name, fact.study_acronym, fact.study_nct):
        if name:
            names |= set(name.lower().split())
    return names


def _brand_or_other_drug_needs(claim: StructuredClaim, fact: EvidenceFact, vocab: Vocabulary) -> list[tuple[str, Any]]:
    """Drug names a statement of the card must itself contain: brands, and any drug that is not the card's own subject."""
    needs: list[tuple[str, Any]] = []
    for match in vocab.drug_pattern().finditer(claim.text):
        alias = match.group(1).lower()
        slug = vocab.drug_slug(alias)
        if slug is None:
            continue
        generic = alias == slug or alias.replace(" ", "-") == slug or re.fullmatch(r"ly\d+", alias)
        if slug in fact.drugs and generic:
            continue
        if generic:
            needs.append((alias, lambda seg, slug=slug: slug in seg.drugs or any(a in seg.lowered for a in vocab.drug_aliases.get(slug, [slug]))))
        else:  # a brand names one product, so the statement must name that brand
            needs.append((alias, lambda seg, alias=alias: re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", seg.lowered) is not None))
    return needs


def _segment_needs(claim: StructuredClaim, fact: EvidenceFact, ctx: Context) -> list[tuple[str, Any]]:
    """Everything the claim asserts that must stand together in one statement of the card, as (label, test) pairs."""
    needs: list[tuple[str, Any]] = []

    def number(label: str, value: float) -> None:
        needs.append((label, lambda seg, value=value: any(_eq(abs(float(value)), known) for known in seg.numbers)))

    if claim.magnitude is not None:
        number(f"value {_show(claim.magnitude)}", claim.magnitude)
    if claim.magnitude_range is not None:
        number(f"range limit {_show(claim.magnitude_range[0])}", claim.magnitude_range[0])
        number(f"range limit {_show(claim.magnitude_range[1])}", claim.magnitude_range[1])
    if claim.ci is not None:
        number(f"interval limit {_show(claim.ci['low'])}", claim.ci["low"])
        number(f"interval limit {_show(claim.ci['high'])}", claim.ci["high"])
    for weeks in claim.timepoints_weeks:
        needs.append((f"week {_show(weeks)}", lambda seg, weeks=weeks: any(abs(weeks - t) <= 0.05 for t in seg.times)))
    for count in claim.counts:
        number(f"n {count}", count)
    if claim.event_count is not None:
        number(f"count {claim.event_count}", claim.event_count)
    for extra, label in ((claim.threshold, "threshold"), (claim.ni_margin, "margin"), (claim.p_value["value"] if claim.p_value else None, "p-value"), (claim.dispersion["value"] if claim.dispersion else None, "dispersion")):
        if extra is not None:
            number(f"{label} {_show(extra)}", extra)
    for date in claim.dates:
        needs.append((f"date {date}", lambda seg, date=date: any(known.startswith(date) for known in seg.dates)))
    for arm in claim.arms:
        for values in arm.doses.values():
            for dose in values:
                needs.append((f"dose {_show(dose)} mg", lambda seg, dose=dose: any(_eq(dose, known) for known in seg.doses)))
    needs.extend(_brand_or_other_drug_needs(claim, fact, ctx.vocab))
    for endpoint in claim.endpoints:
        if endpoint["family"] == "RESPONDER":
            continue
        family, word = endpoint["family"], endpoint["text"].split()[-1]
        needs.append((endpoint["text"], lambda seg, family=family, word=word: f"EP_{family}" in seg.features or _grounded(word, seg.vocabulary)))
        if endpoint.get("variant"):
            needs.append((endpoint["text"], lambda seg, variant=endpoint["variant"]: variant in seg.variants))
    for population in claim.population:
        needs.append((population.lower().replace("_", " "), lambda seg, population=population: f"POP_{population}" in seg.features))
    for stat in claim.statistical:
        if stat["name"] == "HYPOTHETICAL_SUPERIORITY" and _requirement_only(claim, stat, ctx):
            continue
        if stat["name"] == "HYPOTHETICAL_SUPERIORITY":
            needs.append((stat["text"], lambda seg: bool({"SUPERIORITY", "HYPOTHETICAL_SUPERIORITY", "NEG_SUPERIORITY"} & seg.features)))
        elif stat["name"] in _POSITIVE_FEATURES and stat["name"] != "EFFECT_WORD":
            needs.append((stat["text"], lambda seg, name=stat["name"]: name in seg.features))
    for role in claim.endpoint_role:
        if role in _ROLE_FEATURES:
            needs.append((role.lower().replace("_", " "), lambda seg, feature=_ROLE_FEATURES[role]: feature in seg.features))
    for estimand in claim.estimand:
        if estimand != "NOT_STATED":
            needs.append((estimand.lower().replace("_", "-") + " estimand", lambda seg, estimand=estimand: f"ESTIMAND_{estimand}" in seg.features))
    if claim.causal:
        needs.append((claim.causal[0], lambda seg: "CAUSAL" in seg.features))
    for word in claim.subgroup:
        needs.append((word, lambda seg, word=word: _grounded(word, seg.vocabulary)))
    if fact.kind != REGULATORY_STATUS:  # a profile's status is checked against its stored status, not its wording
        for status in claim.regulatory:
            needs.append((status.lower().replace("_", " "), lambda seg, status=status: f"REG_{status}" in seg.features))
    return needs


def _role_binding(claim: StructuredClaim, fact: EvidenceFact, seg: Segment, ctx: Context) -> str | None:
    """When a statement lists several values, the claim's value must belong, in that statement, to the arm and endpoint the claim names."""
    if claim.magnitude is None or len(seg.magnitudes) < 2:
        return None
    candidates = [c for c in _statements(seg, ctx.vocab) if c.magnitude is not None and _eq(abs(c.magnitude), abs(claim.magnitude)) and not c.problems]
    if not candidates:
        return f"the statement lists several values and {_show(claim.magnitude)} cannot be read as one of them on its own"
    named_arms = [a for a in claim.arms if a.drugs or a.doses]
    seg_arms = len({tuple(sorted(a.drugs)) or ("dose", *sorted(d for v in a.doses.values() for d in v)) for c in _statements(seg, ctx.vocab) for a in c.arms})
    claim_families = {e["family"] for e in claim.endpoints if e["family"] != "RESPONDER"}
    seg_families = {f for f in seg.features if f.startswith("EP_")}
    problems = []
    for candidate in candidates:
        problems = []
        own = [a for a in candidate.arms if a.role != "COMPARATOR"] if not candidate.carried_arms else []
        if seg_arms >= 2 or named_arms:
            if not named_arms:
                problems.append("the sentence does not say which arm the value belongs to")
            elif not own:
                problems.append("in the cited statement no arm is written next to this value")
            else:
                own_drugs = {d for a in own for d in a.drugs} or set(fact.drugs)
                claim_drugs = {d for a in named_arms if a.role != "COMPARATOR" for d in a.drugs}
                if claim_drugs and claim_drugs != own_drugs:
                    problems.append(f"in the cited statement {_show(claim.magnitude)} belongs to {', '.join(a.text for a in own)}")
                claim_doses = {d for a in named_arms for v in a.doses.values() for d in v}
                own_doses = {d for a in own for v in a.doses.values() for d in v}
                if claim_doses and own_doses and not claim_doses <= own_doses:
                    problems.append(f"in the cited statement {_show(claim.magnitude)} belongs to {', '.join(a.text for a in own)}")
        if claim_families and len(seg_families) >= 2:
            own_families = {e["family"] for e in candidate.endpoints}
            if not claim_families <= own_families:
                problems.append("the statement lists values for several endpoints and does not name this one next to the value")
        if claim.threshold is not None and (candidate.threshold is None or not _eq(candidate.threshold, claim.threshold)):
            problems.append("the responder threshold does not belong to this value in the cited statement")
        if claim.timepoints_weeks and len(seg.times) >= 2 and not set(claim.timepoints_weeks) <= set(candidate.timepoints_weeks):
            problems.append("the statement lists several time points and does not name this one next to the value")
        if not problems:
            return None
    return "; ".join(dict.fromkeys(problems))


def _check_text_binding(claim: StructuredClaim, fact: EvidenceFact, ctx: Context, out: list[Mismatch]) -> None:
    """Near-verbatim agreement with one statement of a text card (see ``Segment``)."""
    segments = ctx.segments(fact)
    if not segments:
        return
    needs = _segment_needs(claim, fact, ctx)
    approval_claim = fact.kind == REGULATORY_STATUS and "APPROVED" in claim.regulatory
    candidates = segments
    if approval_claim:
        # the approved use must be one the card states for the brand or drug the sentence names
        class_vocabulary = set().union(*(seg.vocabulary for seg in segments if seg.class_statement)) if any(seg.class_statement for seg in segments) else set()
        for term in claim.residual_terms:
            if not _grounded(term, class_vocabulary):
                needs.append((term, lambda seg, term=term: _grounded(term, seg.vocabulary)))
        candidates = [seg for seg in segments if seg.approval and not seg.class_statement]
    best, best_missing = None, None
    for seg in candidates:
        missing = [label for label, test in needs if not test(seg)]
        if best_missing is None or len(missing) < len(best_missing):
            best, best_missing = seg, missing
        if not missing:
            break
    if needs and (best is None or best_missing):
        shown = best_missing if best_missing is not None else [label for label, _ in needs]
        out.append(Mismatch("statement", sorted(dict.fromkeys(shown)), "no single statement of the cited card says these together" + (" (for the approved use named)" if approval_claim else ""), UNVERIFIABLE))
        return
    if best is not None:
        problem = _role_binding(claim, fact, best, ctx)
        if problem:
            out.append(Mismatch("magnitude", f"{_show(claim.magnitude)} attributed to {', '.join(a.text for a in claim.arms) or 'no named arm'}", problem, UNVERIFIABLE))

    # a negated part of the sentence must restate a negated statement of the card
    skip = _subject_names(fact, ctx.vocab)
    for part in re.split(r"[;,()]|\b(?:but|whereas|while|which|and|because|so)\b", claim.text):
        if not _is_negated(part):
            continue
        content = _content_stems(part, skip)
        if not content:
            continue
        if not any(seg.negated and all(stems(word) & seg.vocabulary for word in content) for seg in segments):
            out.append(Mismatch("negation", f"“{part.strip()}”", "no negated statement of the cited card says this", UNVERIFIABLE))
            break
    # an asserted word whose opposite the card states ("the same trial" against "different trials")
    sentence_words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z-]*", claim.text)}
    if not _is_negated(claim.text):
        plain_vocabulary = set().union(*(seg.vocabulary for seg in segments if not seg.negated)) if any(not seg.negated for seg in segments) else set()
        for word in sentence_words & set(lx.ANTONYMS):
            if lx.ANTONYMS[word] & plain_vocabulary and not stems(word) & plain_vocabulary:
                out.append(Mismatch("wording", f"“{word}”", f"the cited statement says {sorted(lx.ANTONYMS[word] & plain_vocabulary)[0]}", CONTRADICTED))
                break


# --------------------------------------------------------------------------- claim, sentence, draft


def verify_claim(claim: StructuredClaim, cards: dict[str, dict[str, Any]], ctx: Context) -> None:
    """Bind the claim to one cited fact, or record why no cited fact supports it."""
    if claim.problems:
        claim.verdict = UNVERIFIABLE
        claim.mismatches = [Mismatch("structure", problem, "one value per clause", UNVERIFIABLE).as_dict() for problem in claim.problems]
        return
    candidates = [(cid, cards[cid]["fact"]) for cid in claim.cited if cid in cards]
    if not candidates:
        claim.verdict = UNVERIFIABLE
        claim.mismatches = [Mismatch("citation", "an assertion", "no evidence card is cited for it", UNVERIFIABLE).as_dict()]
        return
    tainted = [cid for cid, fact in candidates if instruction_like(fact)]
    candidates = [(cid, fact) for cid, fact in candidates if cid not in tainted]
    if not candidates:
        claim.verdict = UNVERIFIABLE
        claim.mismatches = [Mismatch("evidence", tainted, "the cited card contains instruction-like text and is never used as evidence", UNVERIFIABLE).as_dict()
                            | {"message": "The cited card contains instruction-like text and is never used as evidence."}]
        return
    best: tuple[int, str, EvidenceFact, list[Mismatch]] | None = None
    for cid, fact in candidates:
        probe = StructuredClaim(**{**claim.__dict__, "numbers": list(claim.numbers)})
        mismatches = check_structured(probe, fact, ctx) if fact.is_structured else check_text(probe, fact, ctx)
        score = sum(2 if m.kind == CONTRADICTED else 1 for m in mismatches)
        if best is None or score < best[0]:
            best = (score, cid, fact, mismatches)
        if not mismatches:
            break
    assert best is not None
    derived = [(cid, fact) for cid, fact in candidates if fact.kind == DERIVED_TEXT]
    if best[0] > 0 and len(derived) >= 2 and len(derived) == len(candidates) and not claim.subject_drugs and not claim.comparator_drugs:
        # Rule-based readings have no record behind them: a sentence may restate two of them together.
        merged = fact_from_text("+".join(fact.fact_id for _, fact in derived), " ".join(fact.text for _, fact in derived), [sid for _, fact in derived for sid in fact.source_ids])
        probe = StructuredClaim(**{**claim.__dict__, "numbers": list(claim.numbers)})
        mismatches = check_text(probe, merged, ctx)
        if not mismatches:
            best = (0, derived[0][0], merged, mismatches)
    _, cid, fact, mismatches = best
    claim.bound_card, claim.bound_fact = cid, fact.fact_id
    claim.mismatches = [m.as_dict() | {"message": m.describe()} for m in mismatches]
    if not mismatches:
        claim.verdict = SUPPORTED
    elif any(m.kind == CONTRADICTED for m in mismatches):
        claim.verdict = CONTRADICTED
    else:
        claim.verdict = UNVERIFIABLE


def instruction_like(fact: EvidenceFact) -> bool:
    """Evidence text that addresses a reader or a model ("Ignore all previous instructions ..."). Such a card grounds nothing."""
    return bool(lx.CARD_INJECTION_RE.search(fact.text or ""))


def _guard(sentence: str, facts: list[EvidenceFact]) -> list[str]:
    """The keyword guard as a second net, told what kind of evidence the sentence cites."""
    analyses = [f for f in facts if f.kind == BETWEEN_ARM_ANALYSIS]
    non_inferiority = [f for f in analyses if f.hypothesis_type == "NON_INFERIORITY"]
    studies = {f.study_nct for f in facts if f.study_nct and f.is_structured}
    plain = re.sub(r"\[\s*C\d+(?:\s*[,;]\s*C\d+)*\s*\]", " ", sentence)
    violations = guard_statement(
        plain,
        hypothesis_type="NON_INFERIORITY" if non_inferiority else None,
        superiority_met=TriState.YES.value if non_inferiority and all(f.superiority_met == TriState.YES.value for f in non_inferiority) else None,
        cross_trial=len(studies) >= 2,
        lean_mass=True,
        data_family="SPONTANEOUS" if re.search(r"faers|spontaneous", plain, re.IGNORECASE) else None,
        exploratory=any(is_exploratory(f) for f in facts if f.is_structured),
    )
    return [f"{v['message']} [{v['code']}]" for v in violations]


def _side_by_side_comparison(supported: list[StructuredClaim], cards: dict[str, dict[str, Any]]) -> str | None:
    """A sentence that orders two separately cited values ("more weight loss with X (a) than with Y (b)").

    Two arms of one randomized trial, on the same endpoint, estimand and time point, may be described in the
    order their reported values have. Anything else - above all values from two different trials - is a
    comparison no cited analysis supports.
    """
    refusal = "Compares two separately cited results. A comparison is supported only by one reported between-arm analysis, or by two arms of the same trial on the same endpoint, estimand and time point."
    facts = [cards[c.bound_card]["fact"] for c in supported]
    if len(supported) != 2 or len({f.fact_id for f in facts}) != 2 or any(f.kind != ARM_RESULT or f.value is None for f in facts):
        return refusal
    first, second = facts
    same = first.study_nct == second.study_nct and first.endpoint_category == second.endpoint_category and first.timepoint_weeks == second.timepoint_weeks and first.estimand_label == second.estimand_label and first.unit_class == second.unit_class
    if not same or any(s["name"] in ("SUPERIORITY", "SIGNIFICANT") for c in supported for s in c.statistical):
        return refusal
    polarity = next((c.comparative_polarity for c in supported if c.comparative_polarity), None)
    base = supported[0].base_direction or supported[1].base_direction
    if polarity is None:
        return refusal
    more_negative_expected = (polarity == "UP") == (base == "DECREASE") if base else polarity == "DOWN"
    if (first.value < second.value) != more_negative_expected or first.value == second.value:
        return f"The stated order does not match the reported values ({_show(first.value)} for {first.arm_label}, {_show(second.value)} for {second.arm_label})."
    return None


@dataclass
class Subject:
    """What an earlier sentence was about, for a later "It ...", "Its 95% CI ...", "That dose ..." or "That trial ..."."""

    arms: list[ArmMention] = field(default_factory=list)
    studies: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, reading: SentenceReading) -> Subject:
        arms: dict[tuple, ArmMention] = {}
        for claim in reading.claims:
            for arm in claim.arms:
                if arm.role != "COMPARATOR" and arm.drugs:
                    arms.setdefault((tuple(arm.drugs), tuple(sorted((k, tuple(v)) for k, v in arm.doses.items()))), arm)
        studies = list(dict.fromkeys(nct for claim in reading.claims for nct in claim.studies))
        return cls(list(arms.values()), studies)


def _resolve_reference(reading: SentenceReading, previous: Subject | None, cards: dict[str, dict[str, Any]]) -> None:
    """Bind "It", "Its", "That dose" to the previous sentence's subject, or mark the claim unresolvable. Never guessed."""
    cited_facts = [cards[cid]["fact"] for cid in reading.cited if cid in cards]
    text_only = bool(cited_facts) and not any(f.is_structured for f in cited_facts)
    for claim in reading.claims:
        own = [a for a in claim.arms if a.role != "COMPARATOR" and a.drugs]
        if reading.anaphor and own and lx.ANAPHOR_START_RE.match(reading.text) and previous and len(previous.arms) == 1:
            # "Retatrutide ... [C2]. It is approved as Zepbound [C3]": the pronoun is the subject, the drug named later is not
            meant = set(previous.arms[0].drugs)
            if not meant <= {d for a in own for d in a.drugs}:
                claim.problems.append(f"“{reading.anaphor}” refers to {previous.arms[0].text}, but the sentence attributes the statement to {', '.join(a.text for a in own)}")
        if reading.anaphor and not own:
            arms = previous.arms if previous else []
            if len(arms) == 1:
                source = arms[0]
                doses = {} if text_only else {k: list(v) for k, v in source.doses.items()}
                carried = ArmMention(list(source.drugs), doses, "TREATMENT", source.start, source.end, f"{source.text} (from “{reading.anaphor}”)", source.dose_unit, dict(source.dose_units))
                claim.arms = [carried, *[a for a in claim.arms if a.drugs or a.role == "COMPARATOR"]]
                claim.subject_drugs = list(dict.fromkeys([*carried.drugs, *claim.subject_drugs]))
            elif not arms:
                claim.problems.append(f"“{reading.anaphor}” refers to an earlier subject, but no earlier sentence names one")
            else:
                claim.problems.append(f"“{reading.anaphor}” could refer to more than one earlier arm ({'; '.join(a.text for a in arms)})")
        if reading.study_anaphor and not claim.studies and not claim.unknown_studies and previous and previous.studies:
            if len(previous.studies) == 1:
                claim.studies = list(previous.studies)
            else:
                claim.problems.append(f"“{reading.study_anaphor}” could refer to more than one earlier trial")


def verify_sentence(reading: SentenceReading, cards: dict[str, dict[str, Any]], ctx: Context, previous: Subject | None = None) -> dict[str, Any]:
    reasons: list[str] = []
    verdict = SUPPORTED
    unknown = [cid for cid in reading.cited if cid not in cards]
    if reading.instruction:
        verdict = UNVERIFIABLE
        reasons.append(f"Instruction-like, imperative or recommendation wording (“{reading.instruction[:40]}”) is not an evidence statement and is never shown.")
    if unknown:
        verdict = UNVERIFIABLE
        reasons.append(f"Cites unknown card(s) {unknown}.")
    if lx.is_meta_sentence(reading.text):  # framing that asserts nothing ("In summary:"): neither supported nor refused
        return {"sentence": reading.text, "cited": reading.cited, "claims": [], "verdict": NOT_FACTUAL if verdict == SUPPORTED else verdict, "reasons": reasons, "bound": []}
    if not reading.is_factual:
        # Fail closed: a sentence the extractor reads nothing in is still a statement. Only the short list of framing and
        # hedge sentences (lexicon.META_SENTENCE_RES) may stand without evidence.
        reasons.append("Asserts something the verifier cannot bind to evidence and is not one of the recognised framing sentences; only those may stand without support.")
        return {"sentence": reading.text, "cited": reading.cited, "claims": [c.as_dict() for c in reading.claims], "verdict": UNVERIFIABLE, "reasons": reasons, "bound": []}
    if reading.anaphor or reading.study_anaphor:
        _resolve_reference(reading, previous, cards)
    if not reading.cited:
        numeric = any(c.magnitude is not None or c.magnitude_range or c.counts or c.numbers or c.timepoints_weeks or c.ci or c.p_value for c in reading.claims)
        reasons.append("States a number without a citation." if numeric else "States a fact without a citation.")
        return {"sentence": reading.text, "cited": [], "claims": [c.as_dict() for c in reading.claims], "verdict": UNVERIFIABLE, "reasons": reasons, "bound": []}
    for problem in reading.problems:
        verdict = UNVERIFIABLE
        reasons.append(f"Ambiguous sentence structure: {problem}.")

    ctx.hypothetical = reading.hypothetical
    for claim in reading.claims:
        verify_claim(claim, cards, ctx)
        if claim.verdict == CONTRADICTED:
            verdict = CONTRADICTED
        elif claim.verdict == UNVERIFIABLE and verdict == SUPPORTED:
            verdict = UNVERIFIABLE
        for mismatch in claim.mismatches:
            reasons.append(f"[{claim.bound_card or '-'}] {mismatch.get('message') or mismatch['claimed']}")
    bound = list(dict.fromkeys((c.bound_card, c.bound_fact) for c in reading.claims if c.bound_fact and c.verdict == SUPPORTED))
    # Comparative wording about arm-level values needs something to stand on. (A comparative bound to a between-arm
    # analysis already has it: that analysis is the comparison.)
    on_arms = [c for c in reading.claims if c.verdict == SUPPORTED and c.bound_card in cards and cards[c.bound_card]["fact"].kind == ARM_RESULT]
    compares = any(c.comparative or any(s["name"] == "SUPERIORITY" for s in c.statistical) for c in on_arms)
    if compares and len({c.bound_fact for c in on_arms}) >= 2 and reading.comparative:
        problem = _side_by_side_comparison(on_arms, cards)
        if problem:
            verdict = CONTRADICTED if problem.startswith("The stated order") and verdict == SUPPORTED else (UNVERIFIABLE if verdict == SUPPORTED else verdict)
            reasons.append(problem)
    elif compares:
        verdict = UNVERIFIABLE if verdict == SUPPORTED else verdict
        reasons.append("Uses a comparative (more / less / greater / lower) about one arm's value without a second cited value or a between-arm analysis to compare it with.")
    if ctx.truncated and any(c.exhaustive for c in reading.claims):
        verdict = UNVERIFIABLE if verdict == SUPPORTED else verdict
        reasons.append("Makes an exhaustive statement although not every evidence card was offered to the model.")
    cited_facts = [cards[cid]["fact"] for cid in reading.cited if cid in cards]
    for violation in _guard(reading.text, cited_facts):
        verdict = UNVERIFIABLE if verdict == SUPPORTED else verdict
        reasons.append(violation)
    return {
        "sentence": reading.text,
        "cited": reading.cited,
        "claims": [c.as_dict() for c in reading.claims],
        "verdict": verdict,
        "reasons": list(dict.fromkeys(reasons)),
        "bound": [{"card": card, "fact": fact} for card, fact in bound],
    }


def prepare_cards(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index cards by id. A card that arrives as text only is checked as a statement, never as numbers on trust."""
    prepared: dict[str, dict[str, Any]] = {}
    for card in cards:
        fact = card.get("fact")
        if fact is None:
            fact = fact_from_text(f"text:{card['id']}", card.get("text", ""), list(card.get("source_ids") or []))
        prepared[card["id"]] = {**card, "fact": fact}
    return prepared


def verify_draft(draft: str, cards: list[dict[str, Any]], vocabulary: Vocabulary | None = None, truncated: bool = False) -> dict[str, Any]:
    """Verify every sentence of a draft. ``passed`` is true only if every sentence that asserts something is SUPPORTED."""
    prepared = prepare_cards(cards)
    facts = [card["fact"] for card in prepared.values()]
    vocab = (vocabulary or Vocabulary.default()).with_facts(facts)
    ctx = Context(vocab=vocab, truncated=truncated, all_phases={digit for fact in facts for digit in _phase_digits(fact.phase)})
    for fact in facts:
        if fact.kind == DERIVED_TEXT or fact.phase is None:
            ctx.all_phases |= ctx.signature(fact).phases
    reports = []
    previous: Subject | None = None
    for sentence in split_sentences(draft):
        reading = read_sentence(sentence, vocab)
        reports.append(verify_sentence(reading, prepared, ctx, previous))
        if reading.is_factual:
            previous = Subject.of(reading)
    problems: list[str] = []
    for index, report in enumerate(reports, start=1):
        if report["verdict"] in (SUPPORTED, NOT_FACTUAL):
            continue
        for reason in report["reasons"] or ["The sentence could not be verified."]:
            problems.append(f"Sentence {index} ({report['verdict'].lower()}): {reason}")
    factual = [r for r in reports if r["verdict"] != NOT_FACTUAL]
    if not factual and not problems:
        problems.append("The draft makes no statement that cites evidence.")
    counts: dict[str, int] = {}
    for report in reports:
        counts[report["verdict"]] = counts.get(report["verdict"], 0) + 1
    return {
        "passed": not problems,
        "problems": problems,
        "sentences_checked": len(reports),
        "sentences": reports,
        "verdicts": counts,
        "method": "deterministic structured-claim verification; no language model takes part in the decision",
    }


def withheld_report(check: dict[str, Any]) -> dict[str, Any]:
    """The report of a draft that was withheld, without the draft.

    A withheld draft is never shown, so its sentences are not returned either (they may carry injected or simply false
    text). What remains is enough to see why: per sentence, the verdict, the cited cards, the reasons, and per claim the
    field that failed with the claimed and the stored value.
    """
    sentences = []
    for index, report in enumerate(check.get("sentences", []), start=1):
        sentences.append({
            "index": index,
            "verdict": report["verdict"],
            "cited": report.get("cited", []),
            "reasons": report.get("reasons", []),
            "claims": [
                {"verdict": claim.get("verdict"), "bound_card": claim.get("bound_card"),
                 "mismatches": [{key: mismatch.get(key) for key in ("field", "claimed", "actual", "kind")} for mismatch in claim.get("mismatches", [])]}
                for claim in report.get("claims", [])
            ],
        })
    return {key: check[key] for key in ("passed", "problems", "sentences_checked", "verdicts", "method") if key in check} | {"sentences": sentences, "draft_withheld": True}
