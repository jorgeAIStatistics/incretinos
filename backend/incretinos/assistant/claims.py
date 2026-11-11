"""Structured claims: what a sentence asserts, read deterministically.

``read_sentence`` turns one sentence into one or more :class:`StructuredClaim` objects using only the
controlled vocabulary in ``lexicon`` plus the drug and study names known to the platform. It does not
judge anything. A sentence that names two arms and two values yields two claims, each carrying the
arm, value, interval, time point and other qualifiers that belong to it, so that the verifier can
require one stored fact to satisfy each claim as a whole.

Reading is positional and conservative. When the structure of a sentence cannot be resolved into
claims (two values with nothing separating them, an interval with no value to attach to), the reading
is marked ambiguous and the verifier refuses it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..vocab import DRUG_ALIASES
from . import lexicon as lx
from .facts import COMBINATION_COMPONENTS, EvidenceFact, expand_drugs

MARKER_RE = re.compile(r"\[\s*(C\d+(?:\s*[,;]\s*C\d+)*)\s*\]")
_ABBREVIATIONS = [(re.compile(r"\bvs\.", re.IGNORECASE), "vs"), (re.compile(r"\be\.g\.", re.IGNORECASE), "eg"), (re.compile(r"\bi\.e\.", re.IGNORECASE), "ie"),
                  (re.compile(r"\bet al\.", re.IGNORECASE), "et al"), (re.compile(r"\bapprox\.", re.IGNORECASE), "approximately"), (re.compile(r"\bno\.\s*(?=\d)", re.IGNORECASE), "number ")]
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]*[A-Z0-9\[])")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-/'][A-Za-z0-9]+)*")
_STRONG_SEPARATOR = re.compile(r";|,?\s+\bwhereas\b|,?\s+\bwhile\b|,\s+\band\b|\s+\band\b|,?\s+\bbut\b|\bversus\b|\bvs\b|\bcompared (?:with|to)\b|\bthan\b", re.IGNORECASE)
_CLAUSE_SEPARATOR = re.compile(r";|,?\s+\bwhereas\b|,?\s+\bwhile\b|,\s+\band\b|,\s+\bbut\b", re.IGNORECASE)
_HEADING = re.compile(r"^((?:in summary|summary|overview|in brief|in short|to summari[sz]e|key findings)\s*:)\s+(?=\S)", re.IGNORECASE)
_LIST_GAP = re.compile(r"^[\s,]*(?:and|or)?\s*$", re.IGNORECASE)
# "superior to X", "non-inferior to X", "outperformed X", "beyond X": the arm that follows is the comparator
_COMPARISON_OF = re.compile(r"\bcompar(?:ison|ing|ed)\s+(?:of\s+)?(?:the\s+)?(?:oral\s+)?$", re.IGNORECASE)
_COMPARISON_OF_GAP = re.compile(r"\s*(?:with|to|and|versus|vs\.?|against)\s+(?:the\s+)?(?:oral\s+|once[- ]weekly\s+|subcutaneous\s+)?", re.IGNORECASE)
_COMPARATOR_CUE = re.compile(
    r"(?:\b(?:superior|inferior|non-?inferior|equivalent|comparable|similar|identical|preferable)\s+to|\b(?:better|worse)\s+than|\boutperform(?:ed|s|ing)?|\bbeat(?:s|ing)?|\bsurpass(?:ed|es|ing)?|\bexceed(?:ed|s|ing)?|\bbeyond|\bover)"
    r"\s+(?:that of\s+|those with\s+|those of\s+|that with\s+)?(?:the\s+)?(?:oral\s+)?$"
    r"|\bdifferences?\s+(?:from|versus|vs\.?|relative to|compared (?:with|to))\s+(?:the\s+)?(?:oral\s+)?$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- vocabulary


@dataclass
class Vocabulary:
    """Names the extractor recognises: drug aliases and study names. Built from the database when one is at hand."""

    drug_aliases: dict[str, list[str]] = field(default_factory=dict)
    studies: dict[str, str] = field(default_factory=dict)  # lower-cased name -> NCT id
    _drug_re: re.Pattern | None = None
    _study_re: re.Pattern | None = None

    @classmethod
    def default(cls) -> Vocabulary:
        aliases = {slug: list(names) for slug, names in DRUG_ALIASES.items()}
        aliases.setdefault("eloralintide-tirzepatide", []).append("eloratzp")
        return cls(drug_aliases=aliases)

    @classmethod
    def from_session(cls, session: Session) -> Vocabulary:
        from ..models import Drug, Study

        vocab = cls.default()
        for drug in session.scalars(select(Drug)):
            names = vocab.drug_aliases.setdefault(drug.slug, [])
            extra = [drug.code_name, *(drug.brand_names or [])]
            if not drug.is_combination:
                extra.append(drug.name)
            for name in extra:
                if name and name.lower() not in names:
                    names.append(name.lower())
        for study in session.scalars(select(Study)):
            for name in (study.display_name, study.acronym):
                if name and len(name) >= 5:
                    vocab.studies[name.lower()] = study.nct_id
        return vocab

    def with_facts(self, facts: list[EvidenceFact]) -> Vocabulary:
        for fact in facts:
            if fact.study_nct:
                for name in (fact.study_name, fact.study_acronym):
                    if name and len(name) >= 5:
                        self.studies.setdefault(name.lower(), fact.study_nct)
        self._study_re = None
        return self

    def drug_pattern(self) -> re.Pattern:
        if self._drug_re is None:
            names = sorted({alias for aliases in self.drug_aliases.values() for alias in aliases}, key=len, reverse=True)
            self._drug_re = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9])", re.IGNORECASE)
        return self._drug_re

    def drug_slug(self, alias: str) -> str | None:
        low = alias.lower()
        for slug, aliases in self.drug_aliases.items():
            if low in aliases:
                return slug
        return None

    def study_pattern(self) -> re.Pattern | None:
        if self._study_re is None and self.studies:
            names = sorted(self.studies, key=len, reverse=True)
            self._study_re = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9\-])", re.IGNORECASE)
        return self._study_re


# --------------------------------------------------------------------------- text preparation


def normalise(text: str) -> str:
    """One spelling for the things scientific prose spells many ways (minus signs, dashes, thousands separators)."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")  # zero-width and bidi controls
    for dash in ("−", "‐", "‑", "‒"):  # minus sign and hyphen variants
        text = text.replace(dash, "-")
    text = re.sub(r"(?<=[\d%])\s*[–—]\s*(?=[-+]?\d)", " to ", text)  # 21.8 en-dash 19.9 is a range
    text = re.sub(r"[–—](?=\d)", "-", text)  # en-dash 20.9 is a negative number
    text = re.sub(r"\s*[–—]\s*", ", ", text)  # any other dash separates clauses
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    text = text.replace("‹", "<").replace("›", ">")  # the look-alikes the prompt sanitiser writes for < and >
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)  # 2,749 -> 2749
    text = re.sub(r"\bper cent\b", "percent", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*%", "%", text)
    text = lx.NUMBER_WORD_RE.sub(lambda m: str(lx.number_word_value(m)), text)
    text = lx.NUMBER_WORD_AFTER_RE.sub(lambda m: f"{m.group('lead')} {lx.number_word_value(m)}", text)  # "at week eighty"
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(draft: str) -> list[str]:
    """Sentences of a draft. A citation marker placed after the full stop stays with its sentence."""
    text = normalise(draft)
    for pattern, replacement in _ABBREVIATIONS:
        text = pattern.sub(replacement, text)
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]
    # a leading heading ("In summary: ...") is a sentence of its own
    parts = [piece for part in parts for piece in (_HEADING.split(part, maxsplit=1)[1:] if _HEADING.match(part) else [part]) if piece.strip()]
    sentences: list[str] = []
    for part in parts:
        leading = re.match(r"^((?:\s*\[\s*C\d+(?:\s*[,;]\s*C\d+)*\s*\])+)\s*(.*)$", part, re.DOTALL)
        if leading and sentences:
            sentences[-1] = f"{sentences[-1]} {leading.group(1).strip()}"
            part = leading.group(2).strip()
        if part:
            sentences.append(part)
    return sentences


def stems(word: str) -> set[str]:
    """A small set of forms a word may share with its inflections. Deliberately weak: it never joins unrelated words."""
    w = re.sub(r"'s$|'$", "", word.lower())  # "Lilly's", "group's"
    w = lx.SYNONYMS.get(w, w)
    out = {w}
    for suffix, replacements in (("ies", ("y",)), ("sses", ("ss",)), ("es", ("", "e")), ("s", ("",)), ("ed", ("", "e")), ("ing", ("", "e")), ("ly", ("",)), ("ion", ("", "e")), ("ions", ("", "e")),
                                 ("ation", ("", "e")), ("ment", ("",)), ("ive", ("", "e", "ion")), ("al", ("",))):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            for replacement in replacements:
                out.add(w[: -len(suffix)] + replacement)
    return out


def vocabulary_of(text: str) -> set[str]:
    """Every stem a text uses. Enumeration values such as ACTIVE_NOT_RECRUITING contribute their parts as well."""
    found: set[str] = set()
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_]*(?:[-/'][A-Za-z0-9_]+)*", text):
        low = word.lower()
        found |= stems(low)
        for part in re.split(r"[-/'_]", low):
            if part:
                found |= stems(part)
    return found


# --------------------------------------------------------------------------- mentions


@dataclass
class Mention:
    kind: str
    start: int
    end: int
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArmMention:
    drugs: list[str]
    doses: dict[str, list[float]]
    role: str  # TREATMENT | COMPARATOR | PAIR
    start: int
    end: int
    text: str
    dose_unit: str = "mg"
    dose_units: dict[str, str] = field(default_factory=dict)  # per component of a combination ("bimagrumab 30 mg/kg + semaglutide 2.4 mg")

    def as_dict(self) -> dict[str, Any]:
        return {"drugs": self.drugs, "doses": self.doses, "role": self.role, "text": self.text}

    def unit_of(self, key: str) -> str:
        return self.dose_units.get(key, self.dose_unit)

    def split_doses(self) -> list[ArmMention]:
        """One arm per dose for a list such as "tirzepatide 5, 10 and 15 mg"."""
        if len(self.drugs) != 1 or len(self.doses.get(self.drugs[0], [])) < 2:
            return [self]
        return [ArmMention(list(self.drugs), {self.drugs[0]: [dose]}, self.role, self.start, self.end, self.text, self.dose_unit, dict(self.dose_units)) for dose in self.doses[self.drugs[0]]]


@dataclass
class StructuredClaim:
    """One assertion: who, on what, how much, when, in whom, under which reading, from which evidence."""

    text: str
    cited: list[str] = field(default_factory=list)  # card ids this claim may be bound to
    arms: list[ArmMention] = field(default_factory=list)
    subject_drugs: list[str] = field(default_factory=list)
    comparator_drugs: list[str] = field(default_factory=list)
    studies: list[str] = field(default_factory=list)  # NCT ids
    unknown_studies: list[str] = field(default_factory=list)
    endpoints: list[dict[str, Any]] = field(default_factory=list)  # {family, text, variant}
    threshold: float | None = None
    parameter: str | None = None
    direction: str | None = None  # DECREASE | INCREASE | AMBIGUOUS
    base_direction: str | None = None  # what the verbs and nouns say, before a comparative turns it round
    comparative_polarity: str | None = None  # UP ("more", "greater") | DOWN ("less", "smaller")
    effect_asserted: bool = False
    comparative: bool = False
    magnitude: float | None = None
    magnitude_signed: bool = False
    magnitude_range: tuple[float, float] | None = None
    bound: str | None = None  # an inexact qualifier written in front of the value ("up to", "about")
    dispersion: dict[str, Any] | None = None  # {measure: SE | SD, value}
    unit: str | None = None
    timepoints_weeks: list[float] = field(default_factory=list)
    population: list[str] = field(default_factory=list)
    estimand: list[str] = field(default_factory=list)
    ci: dict[str, Any] | None = None  # {low, high, level}
    ci_level_only: float | None = None
    counts: list[int] = field(default_factory=list)
    p_value: dict[str, Any] | None = None
    ni_margin: float | None = None
    phases: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    numbers: list[float] = field(default_factory=list)  # numbers with no recognised role
    unread: list[str] = field(default_factory=list)  # digits that could not be read as a number or as part of a name
    statistical: list[dict[str, Any]] = field(default_factory=list)  # {name, text}
    endpoint_role: list[str] = field(default_factory=list)
    tier: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    regulatory: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    exhaustive: list[str] = field(default_factory=list)
    negators: list[str] = field(default_factory=list)
    causal: list[str] = field(default_factory=list)  # "led to", "because of": causation asserted
    subgroup: list[str] = field(default_factory=list)  # words of "adults with <subgroup>"
    event_count: int | None = None  # "801 of 6586 participants": the 801
    carried_arms: bool = False  # the arms were carried over from an earlier clause, not written in this one
    orientation_flipped: bool = False  # set by the verifier: comparator written as subject, with the comparative turned round
    residual_terms: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # structural ambiguity found while reading
    # filled by the verifier
    verdict: str | None = None
    bound_fact: str | None = None
    bound_card: str | None = None
    mismatches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_assertion(self) -> bool:
        return bool(
            self.arms or self.studies or self.unknown_studies or self.endpoints or self.magnitude is not None or self.magnitude_range or self.timepoints_weeks or self.population
            or self.estimand or self.ci or self.counts or self.p_value or self.phases or self.dates or self.numbers or self.statistical or self.endpoint_role or self.tier
            or self.verification or self.regulatory or self.missing or self.exhaustive or self.residual_terms or self.direction or self.threshold is not None or self.dispersion or self.unread
            or self.ni_margin is not None or self.parameter or self.negators or self.causal or self.subgroup or self.event_count is not None
        )

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["arms"] = [arm.as_dict() for arm in self.arms]
        data.pop("carried_arms", None)
        return {key: value for key, value in data.items() if value not in (None, [], {}, False, "")}


@dataclass
class SentenceReading:
    text: str
    cited: list[str]
    claims: list[StructuredClaim]
    instruction: str | None = None  # injected or imperative text found in the sentence
    anaphor: str | None = None  # "It", "Its", "That dose": refers to the previous sentence's subject
    study_anaphor: str | None = None  # "That trial", "The study"
    hypothetical: bool = False
    comparative: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def is_factual(self) -> bool:
        return any(claim.has_assertion for claim in self.claims)


class _Reader:
    def __init__(self, text: str):
        self.text = text
        self.taken = bytearray(len(text))
        self.mentions: list[Mention] = []

    def free(self, start: int, end: int) -> bool:
        return not any(self.taken[start:end])

    def add(self, kind: str, start: int, end: int, **data: Any) -> Mention:
        for i in range(start, end):
            self.taken[i] = 1
        mention = Mention(kind, start, end, data)
        self.mentions.append(mention)
        return mention

    def scan(self, pattern: re.Pattern, kind: str, build, spanning: bool = False) -> None:
        position = 0
        while position <= len(self.text):
            match = pattern.search(self.text, position)
            if match is None:
                break
            # a spanning pattern may run over names and numbers already read, but its first and last words must be unread
            free = self.free(match.start(), match.end()) or (spanning and self.free(match.start(), match.start() + 2) and self.free(match.end() - 4, match.end()))
            if match.end() > match.start() and free:
                data = build(match)
                if data is not None:
                    self.add(kind, match.start(), match.end(), **data)
                position = match.end()
            else:  # part of the match is already read as something else: look for a shorter reading further on
                position = match.start() + 1

    def of(self, *kinds: str) -> list[Mention]:
        return sorted((m for m in self.mentions if m.kind in kinds), key=lambda m: m.start)


def _float(text: str) -> float:
    return float(text.replace("+", ""))


def _date(match: re.Match) -> dict[str, Any]:
    groups = match.groupdict()
    month = groups.get("m")
    if not month and groups.get("mon"):
        key = groups["mon"].lower()
        month = f"{lx.MONTH_NUMBER['sept' if key.startswith('sept') else key[:3]]:02d}"
    day = groups.get("d")
    return {"date": "-".join(part for part in (groups["y"], month, f"{int(day):02d}" if day else None) if part)}


def _phase(token: str) -> str:
    return {"i": "1", "ii": "2", "iii": "3", "iv": "4"}.get(token.lower(), token)


def _scan_mentions(text: str, vocab: Vocabulary) -> _Reader:
    r = _Reader(text)
    for match in MARKER_RE.finditer(text):
        r.add("MARKER", match.start(), match.end(), ids=re.findall(r"C\d+", match.group(1)))

    # names first: digits inside them are not numbers
    r.scan(re.compile(r"\bNCT\d{8}\b", re.IGNORECASE), "STUDY", lambda m: {"nct": m.group(0).upper(), "name": m.group(0).upper()})
    study_re = vocab.study_pattern()
    if study_re is not None:
        r.scan(study_re, "STUDY", lambda m: {"nct": vocab.studies[m.group(1).lower()], "name": m.group(1)})
    r.scan(re.compile(r"\b[A-Z]{4,}[A-Za-z]*-(?:\d+|[A-Z]{2,}[A-Za-z0-9]*)\b"), "UNKNOWN_STUDY", lambda m: None if re.match(r"MACE-\d", m.group(0)) else {"name": m.group(0)})
    r.scan(re.compile(r"\bplacebo[- ](?:adjusted|subtracted|corrected)\b", re.IGNORECASE), "PLACEBO_ADJUSTED", lambda m: {})
    r.scan(vocab.drug_pattern(), "DRUG", lambda m: {"slug": vocab.drug_slug(m.group(1)), "alias": m.group(1)})

    # statements that contain digits or labels the numeric patterns would otherwise take
    early = ("CI_NOT_REPORTED", "N_NOT_REPORTED", "P_NOT_REPORTED", "ESTIMAND_NOT_STATED", "MULT_NOT_STATED", "TIME_NOT_STATED", "POP_NOT_STATED", "CI_INCLUDES_NULL", "CI_EXCLUDES_NULL", "POP_MIXED", "POP_NO_T2D", "POP_T1D", "POP_T2D",
             "MULT_NOT_CONTROLLED", "MULT_CONTROLLED")
    for name, pattern in lx.FEATURES:
        if name in early:
            r.scan(pattern, "FEATURE", lambda m, name=name: {"name": name, "text": m.group(0)}, spanning=pattern in lx.SPANNING)

    # numbers with a role
    r.scan(lx.CI_RE, "CI", lambda m: {"low": _float(m.group("low")), "high": _float(m.group("high")), "level": float(m.group("level")) if m.group("level") else None})
    r.scan(lx.INTERVAL_RE, "CI", lambda m: {"low": _float(m.group("low")), "high": _float(m.group("high")), "level": None})
    r.scan(lx.CI_LABEL_RE, "CI_LABEL", lambda m: {"level": float(m.group("level")) if m.group("level") else None})
    r.scan(lx.P_VALUE_RE, "P", lambda m: {"op": {"less than": "<", "below": "<", "under": "<", "greater than": ">", "≤": "<=", "≥": ">="}.get((m.group("op") or "=").lower(), m.group("op") or "="), "value": float(m.group("val"))})
    for pattern in lx.THRESHOLD_RES:
        r.scan(pattern, "THRESHOLD", lambda m: {"value": float(m.group("val")), "text": m.group(0)})
    for pattern, factor in lx.TIME_RES:
        r.scan(pattern, "TIME", lambda m, factor=factor: {"weeks": float(m.group("val")) * factor, "exact": factor == 1.0, "text": m.group(0)})
    for pattern, weeks in lx.WORDED_TIME_RES:
        r.scan(pattern, "TIME", lambda m, weeks=weeks: {"weeks": weeks, "exact": False, "text": m.group(0)})
    r.scan(lx.DOSE_RE, "DOSE", lambda m: {"values": [float(v) for v in re.findall(lx.UNUM, m.group("vals"))], "unit": m.group("unit").lower().replace("milligrams", "mg").replace("milligram", "mg").replace("μ", "µ")})
    r.scan(lx.COUNT_OF_RE, "COUNT_OF", lambda m: {"value": int(m.group("val")), "n": int(m.group("n"))})
    for pattern in lx.COUNT_RES:
        r.scan(pattern, "COUNT", lambda m: {"value": int(m.group("val"))})
    r.scan(lx.PHASE_RE, "PHASE", lambda m: {"phases": [_phase(p) for p in (m.group("p"), m.group("p2")) if p]})
    r.scan(lx.DISPERSION_RE, "DISPERSION", lambda m: {"measure": "SD" if (m.group("kind") or m.group("abbr")).lower() in ("standard deviation", "sd") else "SE", "value": float(m.group("val"))})
    r.scan(lx.MARGIN_RE, "MARGIN", lambda m: {"value": _float(m.group("val"))})
    for pattern in lx.DATE_RES:
        r.scan(pattern, "DATE", _date)
    for pattern in lx.RANGE_RES:
        r.scan(pattern, "RANGE", lambda m: {"low": _float(m.group("low")), "high": _float(m.group("high")), "unit": lx.sentence_unit_class(m.group("unit"))})
    def magnitude(m: re.Match) -> dict[str, Any]:
        unit = lx.sentence_unit_class(m.group("unit"))
        if unit == "PCT" and lx.PEOPLE_AFTER_RE.match(text, m.end()):
            unit = "PCT_PEOPLE"
        bound = lx.BOUND_RE.search(text[max(0, m.start() - 20) : m.start()])
        return {"value": _float(m.group("val")), "signed": m.group("val")[0] in "+-", "unit": unit, "param": None, "bound": bound.group(0).strip().lower() if bound else None}

    r.scan(lx.MAGNITUDE_RE, "MAG", magnitude)

    # everything else in the lexicon
    for name, pattern in lx.FEATURES:
        if name not in early:
            def build(m, name=name):
                data = {"name": name, "text": m.group(0)}
                if name == "EP_MACE" and m.groupdict().get("variant"):
                    data["variant"] = f"mace-{m.group('variant')}"
                return data
            r.scan(pattern, "FEATURE", build, spanning=pattern in lx.SPANNING)
    for pattern, family in lx.PARAM_RES:
        r.scan(pattern, "PARAM", lambda m, family=family: {"family": family})
    r.scan(lx.COMPARATOR_RE, "CMP", lambda m: {"text": m.group(0).lower()})
    r.scan(lx.PAIR_RE, "PAIR", lambda m: {})
    r.scan(lx.PAREN_INTERVAL_RE, "PAREN", lambda m: {"low": _float(m.group("low")), "high": _float(m.group("high"))})
    r.scan(lx.BARE_NUMBER_RE, "NUMBER", lambda m: {"value": _float(m.group("val")), "signed": m.group("val")[0] in "+-"})

    _promote_numbers(r)
    # Fail closed on digits nothing has read: "MACE-30", "Phase 20b", "0.790.99" must not pass as if the digits were not there.
    # (a digit inside a word nothing else has read, such as GLP-1, is checked later as part of that word)
    words = [(m.start(), m.end()) for m in _WORD_RE.finditer(text) if r.free(m.start(), m.end())]
    for match in re.finditer(r"\d[\d.,]*", text):
        if r.free(match.start(), match.end()) and not any(start <= match.start() and match.end() <= end for start, end in words):
            r.add("UNREAD", match.start(), match.end(), text=text[max(0, match.start() - 8) : match.end() + 4].strip())
    return r


def _promote_numbers(r: _Reader) -> None:
    """Give role-less numbers a role where the wording fixes it: 'hazard ratio 0.84', '-20.9 (95% CI ...)'."""
    anchors = lambda: r.of("MAG", "RANGE")  # noqa: E731
    for param in r.of("PARAM"):
        following = [n for n in r.of("NUMBER") if param.end <= n.start <= param.end + 90]
        if not following:
            continue
        number = following[0]
        between = r.text[param.end : number.start]
        if any(param.end <= a.start < number.start for a in anchors()) or re.search(r"[.;]", between):
            continue
        number.kind = "MAG"
        number.data = {"value": number.data["value"], "signed": number.data["signed"], "unit": None, "param": param.data["family"]}
    for number in r.of("NUMBER"):
        next_ci = next((c for c in r.of("CI") if 0 <= c.start - number.end <= 5), None)
        if next_ci is not None and re.fullmatch(r"[\s;,(\[]*", r.text[number.end : next_ci.start]):
            number.kind = "MAG"
            number.data = {"value": number.data["value"], "signed": number.data["signed"], "unit": None, "param": None}
    for paren in r.of("PAREN"):
        previous = [a for a in r.of("MAG") if 0 <= paren.start - a.end <= 2]
        if previous:
            paren.kind = "CI"
            paren.data = {"low": paren.data["low"], "high": paren.data["high"], "level": None}
        else:
            r.mentions.remove(paren)
            r.mentions.append(Mention("NUMBER", paren.start, paren.end, {"value": paren.data["low"], "signed": False}))
            r.mentions.append(Mention("NUMBER", paren.start, paren.end, {"value": paren.data["high"], "signed": False}))


def _build_arms(r: _Reader, vocab: Vocabulary) -> list[ArmMention]:
    """Arm mentions: a drug with the dose written next to it; a dose on its own continues the previous drug's list."""
    text = r.text
    drugs = r.of("DRUG")
    doses = r.of("DOSE")
    raw: list[dict[str, Any]] = [{"slug": d.data["slug"], "doses": [], "start": d.start, "end": d.end, "unit": "mg"} for d in drugs]
    orphans: list[Mention] = []
    for dose in doses:
        owner = None
        for entry in raw:  # "tirzepatide 15 mg", "tirzepatide (15 mg)", "tirzepatide at 15 mg"
            gap = text[entry["end"] : dose.start] if entry["end"] <= dose.start else None
            if gap is not None and len(gap) <= 24 and re.fullmatch(r"[\s(,]*(?:at|of|dosed at|at a dose of|at doses of|once[- ]weekly|once[- ]daily|oral|sc|subcutaneous)?[\s(]*", gap, re.IGNORECASE) and not entry["doses"]:
                owner = entry
        if owner is None:
            for entry in raw:  # "15 mg tirzepatide", "15 mg of tirzepatide"
                gap = text[dose.end : entry["start"]] if dose.end <= entry["start"] else None
                if gap is not None and len(gap) <= 16 and re.fullmatch(r"\s*(?:of\s+)?(?:or mtd\s*-\s*)?(?:once[- ]\w+\s+)?(?:oral\s+)?", gap, re.IGNORECASE) and not entry["doses"]:
                    owner = entry
                    break
        if owner is not None:
            owner["doses"] = list(dose.data["values"])
            owner["unit"] = dose.data["unit"]
            owner["start"], owner["end"] = min(owner["start"], dose.start), max(owner["end"], dose.end)
        else:
            orphans.append(dose)
    for dose in orphans:
        raw.append({"slug": None, "doses": list(dose.data["values"]), "start": dose.start, "end": dose.end, "orphan": True, "unit": dose.data["unit"]})
    raw.sort(key=lambda e: e["start"])
    for index, entry in enumerate(raw):  # "tirzepatide 5 mg, 10 mg and 15 mg"
        if entry.get("orphan") and index > 0 and raw[index - 1]["slug"] and _LIST_GAP.fullmatch(text[raw[index - 1]["end"] : entry["start"]]):
            entry["slug"] = raw[index - 1]["slug"]
            entry["listed"] = True
    for placebo in r.of("PLACEBO_ADJUSTED"):
        raw.append({"slug": "placebo", "doses": [], "start": placebo.start, "end": placebo.end, "forced_role": "COMPARATOR", "unit": "mg"})
    raw.sort(key=lambda e: e["start"])

    arms: list[ArmMention] = []
    markers = r.of("CMP")
    pairs = r.of("PAIR")
    previous_end, previous_start = 0, None
    for entry in raw:
        components = expand_drugs(entry["slug"]) if entry["slug"] else []
        if entry["slug"] in COMBINATION_COMPONENTS:
            dose_map = {"": entry["doses"]} if entry["doses"] else {}
        elif entry["slug"]:
            dose_map = {entry["slug"]: entry["doses"]} if entry["doses"] else {}
        else:
            dose_map = {"": entry["doses"]}
        role = entry.get("forced_role") or "TREATMENT"
        if not entry.get("forced_role"):
            if any(previous_end <= m.start and m.end <= entry["start"] for m in markers) or _COMPARATOR_CUE.search(text[max(previous_end, entry["start"] - 48) : entry["start"]]):
                role = "COMPARATOR"
            elif previous_start is not None and _COMPARISON_OF_GAP.fullmatch(text[previous_end : entry["start"]]) and _COMPARISON_OF.search(text[max(0, previous_start - 40) : previous_start]):
                role = "COMPARATOR"  # "comparison of orforglipron 12 mg with oral semaglutide 7 mg"
            elif any(previous_end <= p.start and p.end <= entry["start"] for p in pairs):
                role = "PAIR"
        unit = entry.get("unit", "mg")
        arms.append(ArmMention(components, dose_map, role, entry["start"], entry["end"], text[entry["start"] : entry["end"]], unit, {key: unit for key in dose_map}))
        previous_end, previous_start = entry["end"], entry["start"]
    return arms


def _merge_arms(arms: list[ArmMention], text: str) -> list[ArmMention]:
    """Join the components of a written combination ("eloralintide 9 mg + tirzepatide 15 mg") into one arm."""
    merged: list[ArmMention] = []
    for arm in arms:
        if merged:
            last = merged[-1]
            gap = text[last.end : arm.start] if arm.start >= last.end else None
            if gap is not None and lx.COMBINATION_CONNECTOR_RE.fullmatch(gap):
                doses = {key: list(value) for key, value in last.doses.items()}
                for key, value in arm.doses.items():
                    doses.setdefault(key, []).extend(v for v in value if v not in doses.get(key, []))
                units = {**{key: arm.unit_of(key) for key in arm.doses}, **{key: last.unit_of(key) for key in last.doses}}
                merged[-1] = ArmMention(list(dict.fromkeys([*last.drugs, *arm.drugs])), doses, last.role, last.start, arm.end, text[last.start : arm.end], last.dose_unit, units)
                continue
            if gap is not None and last.role == "PAIR" and lx.LOOSE_CONNECTOR_RE.fullmatch(gap):
                arm.role = "PAIR"  # "difference between X and Y": two sides, orientation not stated
        merged.append(arm)
    return merged


def _direction(features: list[Mention]) -> dict[str, Any]:
    """Direction of effect asserted by the direction words of one claim.

    ``base`` is what the verbs and nouns say (lost, reduction, rose); ``polarity`` what a comparative says
    (more / less). "A smaller reduction" turns the base round, so ``direction`` is the net reading.
    """
    names = [m.data["name"] for m in features]
    dec = "DIR_DECREASE_NOUN" in names or "DIR_DECREASE_VERB" in names
    inc = "DIR_INCREASE_NOUN" in names or "DIR_INCREASE_VERB" in names
    up, down = "DIR_UP_COMPARATIVE" in names, "DIR_DOWN_COMPARATIVE" in names
    out: dict[str, Any] = {
        "comparative": up or down,
        "polarity": None if up == down else ("UP" if up else "DOWN"),
        "base": None if dec == inc else ("DECREASE" if dec else "INCREASE"),
        "effect": "DIR_DECREASE_VERB" in names or "DIR_INCREASE_VERB" in names or "EFFECT_WORD" in names or up or down,
        "direction": None,
    }
    if (dec and inc) or (up and down):
        out["direction"] = "AMBIGUOUS"
    elif dec or inc:
        base = "DECREASE" if dec else "INCREASE"
        out["direction"] = {"DECREASE": "INCREASE", "INCREASE": "DECREASE"}[base] if down else base
    elif up or down:
        out["direction"] = "INCREASE" if up else "DECREASE"
    return out


_FEATURE_FIELDS = {
    "ESTIMAND_TREATMENT_REGIMEN": ("estimand", "TREATMENT_REGIMEN"), "ESTIMAND_EFFICACY": ("estimand", "EFFICACY"), "ESTIMAND_INFERRED": ("estimand", "INFERRED"),
    "ESTIMAND_ON_TREATMENT": ("estimand", "ON_TREATMENT"),
    "ESTIMAND_NOT_STATED": ("estimand", "NOT_STATED"), "TIME_NOT_STATED": ("missing", "TIME"), "POP_NOT_STATED": ("population", "NOT_STATED"),
    "POP_T2D": ("population", "T2D"), "POP_NO_T2D": ("population", "NO_T2D"), "POP_MIXED": ("population", "MIXED"), "POP_T1D": ("population", "T1D"),
    "TIER_PEER_REVIEWED": ("tier", "PEER_REVIEWED"), "TIER_NOT_PEER_REVIEWED": ("tier", "NOT_PEER_REVIEWED"), "TIER_PRESS_RELEASE": ("tier", "PRESS_RELEASE"),
    "TIER_REGISTRY_RESULTS": ("tier", "REGISTRY_RESULTS"), "TIER_REGULATORY_LABEL": ("tier", "REGULATORY_LABEL"), "TIER_REGULATORY_REVIEW": ("tier", "REGULATORY_REVIEW"),
    "TIER_SPONSOR_PIPELINE": ("tier", "SPONSOR_PIPELINE"), "TIER_CONFERENCE": ("tier", "CONFERENCE"),
    "VERIFICATION_HUMAN": ("verification", "HUMAN_VERIFIED"), "VERIFICATION_NOT_HUMAN": ("verification", "NOT_HUMAN_VERIFIED"),
    "VERIFICATION_QUOTE_ANCHORED": ("verification", "QUOTE_ANCHORED"), "VERIFICATION_MACHINE_EXTRACTED": ("verification", "MACHINE_EXTRACTED"),
    "REG_APPROVED": ("regulatory", "APPROVED"), "REG_NOT_APPROVED": ("regulatory", "NOT_APPROVED"), "REG_INVESTIGATIONAL": ("regulatory", "INVESTIGATIONAL"),
    "PRIMARY": ("endpoint_role", "PRIMARY"), "KEY_SECONDARY": ("endpoint_role", "KEY_SECONDARY"), "SECONDARY": ("endpoint_role", "SECONDARY"), "EXPLORATORY": ("endpoint_role", "EXPLORATORY"),
    "POST_HOC": ("endpoint_role", "POST_HOC"), "CONFIRMATORY": ("endpoint_role", "CONFIRMATORY"), "DEMONSTRATED": ("endpoint_role", "DEMONSTRATED"),
    "NEG_CONFIRMATORY": ("endpoint_role", "NOT_ESTABLISHED"), "MULT_CONTROLLED": ("endpoint_role", "MULTIPLICITY_CONTROLLED"),
    "MULT_NOT_CONTROLLED": ("endpoint_role", "NOT_MULTIPLICITY_CONTROLLED"), "MULT_NOT_STATED": ("missing", "MULTIPLICITY"),
    "CI_NOT_REPORTED": ("missing", "CI"), "N_NOT_REPORTED": ("missing", "N"), "P_NOT_REPORTED": ("missing", "P"),
}
_NEITHER_ITEMS = [("CI", r"confidence interval|\bCI\b"), ("N", r"number (?:of participants )?analy[sz]ed|sample size|number of participants|\bn\b"), ("P", r"p[- ]?value"), ("MULTIPLICITY", r"multiplicity")]
_STATISTICAL = {"SUPERIORITY", "NEG_SUPERIORITY", "HYPOTHETICAL_SUPERIORITY", "SIGNIFICANT", "NOT_SIGNIFICANT", "NOMINAL", "NO_DIFFERENCE", "NI_MET", "NI_NOT_MET", "NI_TEST", "CI_INCLUDES_NULL", "CI_EXCLUDES_NULL"}


def _claim_from(r: _Reader, mentions: list[Mention], arms: list[ArmMention], span_text: str, cited: list[str], free_words: list[tuple[int, int, str]], subgroup_at: frozenset[int] = frozenset()) -> StructuredClaim:
    claim = StructuredClaim(text=span_text.strip(), cited=cited, arms=arms)
    by_kind: dict[str, list[Mention]] = {}
    for mention in mentions:
        by_kind.setdefault(mention.kind, []).append(mention)
    claim.subject_drugs = list(dict.fromkeys(d for arm in arms if arm.role != "COMPARATOR" for d in arm.drugs))
    claim.comparator_drugs = list(dict.fromkeys(d for arm in arms if arm.role == "COMPARATOR" for d in arm.drugs))
    claim.studies = list(dict.fromkeys(m.data["nct"] for m in by_kind.get("STUDY", [])))
    claim.unknown_studies = list(dict.fromkeys(m.data["name"] for m in by_kind.get("UNKNOWN_STUDY", [])))

    anchors = sorted(by_kind.get("MAG", []) + by_kind.get("RANGE", []), key=lambda m: m.start)
    if len(anchors) > 1:
        claim.problems.append("more than one value in a clause that could not be separated")
    if anchors:
        anchor = anchors[0]
        if anchor.kind == "RANGE":
            claim.magnitude_range = (anchor.data["low"], anchor.data["high"])
            claim.unit = anchor.data["unit"]
        else:
            claim.magnitude, claim.magnitude_signed, claim.unit = anchor.data["value"], anchor.data["signed"], anchor.data["unit"]
            claim.bound = anchor.data.get("bound")
            if anchor.data.get("param"):
                claim.parameter = anchor.data["param"]
    params = list(dict.fromkeys(m.data["family"] for m in by_kind.get("PARAM", [])))
    if params and claim.parameter is None:
        claim.parameter = params[0]
    if len(set(params)) > 1:
        claim.problems.append("more than one effect measure named in one clause")

    intervals = by_kind.get("CI", [])
    if len(intervals) > 1:
        claim.problems.append("more than one confidence interval in one clause")
    if intervals:
        claim.ci = dict(intervals[0].data)
    levels = [m.data["level"] for m in by_kind.get("CI_LABEL", []) if m.data.get("level")]
    if levels and not claim.ci:
        claim.ci_level_only = levels[0]
    p_values = by_kind.get("P", [])
    if p_values:
        claim.p_value = dict(p_values[0].data)
        if len(p_values) > 1:
            claim.problems.append("more than one p-value in one clause")
    thresholds = list(dict.fromkeys(m.data["value"] for m in by_kind.get("THRESHOLD", [])))
    if thresholds:
        claim.threshold = thresholds[0]
        if len(thresholds) > 1:
            claim.problems.append("more than one responder threshold in one clause")
    claim.timepoints_weeks = list(dict.fromkeys(round(m.data["weeks"], 3) for m in sorted(by_kind.get("TIME", []), key=lambda m: m.start)))
    claim.counts = list(dict.fromkeys(m.data["value"] for m in by_kind.get("COUNT", [])))
    if by_kind.get("COUNT_OF"):
        claim.event_count = by_kind["COUNT_OF"][0].data["value"]
        claim.counts = list(dict.fromkeys([*claim.counts, *(m.data["n"] for m in by_kind["COUNT_OF"])]))
        if len(by_kind["COUNT_OF"]) > 1:
            claim.problems.append("more than one 'n of N' count in one clause")
    if by_kind.get("DISPERSION"):
        claim.dispersion = dict(by_kind["DISPERSION"][0].data)
    margins = [m.data["value"] for m in by_kind.get("MARGIN", [])]
    claim.ni_margin = margins[0] if margins else None
    claim.phases = list(dict.fromkeys(p for m in by_kind.get("PHASE", []) for p in m.data["phases"]))
    claim.dates = list(dict.fromkeys(m.data["date"] for m in by_kind.get("DATE", [])))
    claim.numbers = [m.data["value"] for m in by_kind.get("NUMBER", [])]

    features = by_kind.get("FEATURE", [])
    reading = _direction(features)
    claim.direction, claim.effect_asserted, claim.comparative = reading["direction"], reading["effect"], reading["comparative"]
    claim.comparative_polarity, claim.base_direction = reading["polarity"], reading["base"]
    for mention in features:
        name = mention.data["name"]
        if name in _FEATURE_FIELDS:
            target, value = _FEATURE_FIELDS[name]
            current = getattr(claim, target)
            if value not in current:
                current.append(value)
        elif name in _STATISTICAL or name == "EFFECT_WORD":
            claim.statistical.append({"name": name, "text": mention.data["text"].lower()})
        elif name.startswith("EP_"):
            claim.endpoints.append({"family": name[3:], "text": mention.data["text"].lower(), "variant": mention.data.get("variant")})
        elif name == "EXHAUSTIVE":
            claim.exhaustive.append(mention.data["text"].lower())
        elif name == "CAUSAL":
            claim.causal.append(mention.data["text"].lower())
        elif name == "RANKING":
            claim.statistical.append({"name": "RANKING", "text": mention.data["text"].lower()})
        elif name == "RESPONDER_PROPORTION":
            # "the proportion of participants ... was 20.9%": a share of people, never a mean change
            claim.endpoints.append({"family": "RESPONDER", "text": mention.data["text"].lower(), "variant": None})
            if claim.unit == "PCT":
                claim.unit = "PCT_PEOPLE"
        elif name == "NEITHER_REPORTED":
            named = [item for item, pattern in _NEITHER_ITEMS if re.search(pattern, mention.data["text"], re.IGNORECASE)]
            if len(named) < 2:
                claim.problems.append("a 'neither ... nor ... is reported' statement whose two items could not both be read")
            claim.missing.extend(item for item in named if item not in claim.missing)
    if by_kind.get("THRESHOLD"):
        claim.endpoints.append({"family": "RESPONDER", "text": by_kind["THRESHOLD"][0].data["text"].lower(), "variant": None})
    claim.unread = [m.data["text"] for m in by_kind.get("UNREAD", [])]
    for start, _, word in free_words:
        low = word.lower()
        if start in subgroup_at and low not in lx.NEUTRAL and low not in lx.NEGATORS:
            claim.subgroup.append(low)
        if low in lx.NEGATORS or low.endswith("n't"):
            claim.negators.append(low)
        else:
            bare = re.sub(r"'s$|'$", "", low)
            if bare not in lx.NEUTRAL and not all(part in lx.NEUTRAL or part in lx.NEGATORS for part in re.split(r"[-/']", bare) if part):
                claim.residual_terms.append(bare)
    claim.residual_terms = list(dict.fromkeys(claim.residual_terms))
    return claim


def _depths(text: str) -> list[int]:
    """Parenthesis depth at every character: a separator inside brackets does not end the clause around them."""
    depth, out = 0, []
    for ch in text:
        if ch in ")]":
            depth = max(0, depth - 1)
        out.append(depth)
        if ch in "([":
            depth += 1
    return out


def _split_points(r: _Reader, anchors: list[Mention]) -> list[int] | None:
    """Where one valued clause ends and the next begins; None when two values cannot be separated."""
    points: list[int] = []
    depths = _depths(r.text)
    for left, right in zip(anchors, anchors[1:], strict=False):
        gap_start, gap_end = left.end, right.start
        gap = r.text[gap_start:gap_end]
        strong = [(depths[gap_start + m.start()], gap_start + m.start()) for m in _STRONG_SEPARATOR.finditer(gap)]
        if strong:
            shallowest = min(depth for depth, _ in strong)
            points.append(next(position for depth, position in strong if depth == shallowest))
            continue
        commas = [(depths[gap_start + i], gap_start + i) for i, ch in enumerate(gap) if ch == ","]
        if not commas:
            return None
        shallowest = min(depth for depth, _ in commas)
        points.append([position for depth, position in commas if depth == shallowest][-1])
    return points


def read_sentence(sentence: str, vocab: Vocabulary | None = None) -> SentenceReading:
    """Structured claims asserted by one sentence (already normalised by ``split_sentences``)."""
    vocab = vocab or Vocabulary.default()
    r = _scan_mentions(sentence, vocab)
    text = r.text
    markers = r.of("MARKER")
    cited_all = list(dict.fromkeys(cid for m in markers for cid in m.data["ids"]))
    plain = MARKER_RE.sub(lambda m: " " * len(m.group(0)), text)
    instruction = None
    hit = lx.INSTRUCTION_RE.search(plain) or lx.IMPERATIVE_RE.search(plain) or lx.RECOMMENDATION_RE.search(plain)
    if hit:
        instruction = hit.group(0).strip()
    hypothetical = bool(lx.HYPOTHETICAL_FRAME_RE.search(plain))
    anaphor = lx.ANAPHOR_RE.search(plain)
    study_anaphor = lx.STUDY_ANAPHOR_RE.search(plain)

    arms_all = _merge_arms(_build_arms(r, vocab), text)
    content = [m for m in r.mentions if m.kind not in ("MARKER", "DRUG", "DOSE", "PLACEBO_ADJUSTED")]
    # Words nothing has read. A compound read only in part ("Reviewer-confirmed", "Expert-checked") leaves its other parts to check.
    words: list[tuple[int, int, str]] = []
    for m in _WORD_RE.finditer(plain):
        if r.free(m.start(), m.end()):
            words.append((m.start(), m.end(), m.group(0)))
            continue
        for part in re.finditer(r"[A-Za-z0-9]+", m.group(0)):
            start, end = m.start() + part.start(), m.start() + part.end()
            if r.free(start, end) and re.match(r"[A-Za-z]", part.group(0)) and len(part.group(0)) > 1:  # not the "s" of "ACHIEVE-3's"
                words.append((start, end, part.group(0)))
    subgroup_at: set[int] = set()
    for match in lx.SUBGROUP_RE.finditer(plain):
        for start, _end, word in (w for w in words if w[0] >= match.end()):
            between = plain[match.end() : start]
            if not r.free(match.end(), start) or re.search(r"[,;.()]", between):
                break
            low = word.lower()
            if low in ("and", "or", "a", "an"):
                continue
            if low in lx.NEUTRAL or low in lx.NEGATORS or len(subgroup_at) >= 6:
                break
            subgroup_at.add(start)
    subgroup_at_frozen = frozenset(subgroup_at)
    # "801 of 6586" is a value of its own clause too, unless it stands in brackets after a value ("8.4% (801 of 6586)")
    depth_at = _depths(text)
    anchors = sorted([*r.of("MAG", "RANGE"), *(m for m in r.of("COUNT_OF") if depth_at[m.start] == 0)], key=lambda m: m.start)

    def cited_for(start: int, end: int) -> list[str]:
        own = list(dict.fromkeys(cid for m in markers if start <= m.start < end for cid in m.data["ids"]))
        return own or cited_all

    claims: list[StructuredClaim] = []
    problems: list[str] = []
    if len(anchors) >= 2 and len(arms_all) != len(anchors):
        expanded = [single for arm in arms_all for single in arm.split_doses()]
        if len(expanded) == len(anchors):
            arms_all = expanded
    list_mode = (
        len(anchors) >= 2
        and len(arms_all) == len(anchors)
        and (max(a.start for a in arms_all) < min(a.start for a in anchors) or min(a.start for a in arms_all) > max(a.end for a in anchors))
    )
    if list_mode:
        # "X and Y produced a and b, respectively": pair by position; everything else is shared
        shared = [m for m in content if m.kind not in ("MAG", "RANGE", "COUNT_OF", "CI", "CMP", "PAIR")]
        times = [m for m in shared if m.kind == "TIME"]
        zip_times = len(times) == len(anchors) and len({round(t.data["weeks"], 3) for t in times}) > 1
        for index, (arm, anchor) in enumerate(zip(arms_all, anchors, strict=True)):
            arm.role = "TREATMENT"
            next_start = anchors[index + 1].start if index + 1 < len(anchors) else len(text)
            own_ci = [m for m in content if m.kind == "CI" and anchor.end <= m.start < next_start]
            unit_mentions = [anchor, *own_ci, *(m for m in shared if not (zip_times and m.kind == "TIME"))]
            if zip_times:
                unit_mentions.append(times[index])
            claims.append(_claim_from(r, unit_mentions, [arm], text, cited_all, words, subgroup_at_frozen))
    else:
        if len(anchors) >= 2:
            points = _split_points(r, anchors)
            if points is None:
                problems.append("two values in one sentence could not be separated into clauses")
                points = []
        else:
            # no second value: still separate contrasted subjects ("X is ..., whereas Y is ...")
            points = []
            depths = _depths(plain)
            for match in _CLAUSE_SEPARATOR.finditer(plain):
                if depths[match.start()] > 0:
                    continue
                left = [a for a in arms_all if a.end <= match.start()] or [m for m in r.of("STUDY") if m.end <= match.start()]
                right = [a for a in arms_all if a.start >= match.end()] or [m for m in r.of("STUDY") if m.start >= match.end()]
                if left and right and not any(a.start < match.start() < a.end for a in arms_all):
                    points.append(match.start())
        bounds = [0, *points, len(text)]
        previous_treatment: list[ArmMention] = []
        for start, end in zip(bounds, bounds[1:], strict=False):
            unit_mentions = [m for m in content if start <= m.start < end]
            unit_arms = [a for a in arms_all if start <= a.start < end]
            has_anchor = any(m.kind in ("MAG", "RANGE", "COUNT_OF") for m in unit_mentions)
            if start > 0 and has_anchor and unit_arms:
                # a contrast word opening a valued clause ("... versus 3.1% with placebo") separates two results; it is not a comparator relation
                first = min(unit_arms, key=lambda a: a.start)
                lead = [m for m in unit_mentions if m.kind == "CMP" and m.start <= start + 3]
                if lead and first.role == "COMPARATOR" and not any(m.kind == "CMP" and m.start > start + 3 and m.end <= first.start for m in unit_mentions):
                    first.role = "TREATMENT"
                    unit_mentions = [m for m in unit_mentions if m not in lead]
            for arm in unit_arms:  # a bare dose continues the drug named before it
                if not arm.drugs and previous_treatment and previous_treatment[-1].drugs and len(previous_treatment[-1].drugs) == 1:
                    drug = previous_treatment[-1].drugs[0]
                    arm.drugs = [drug]
                    arm.doses = {drug: arm.doses.get("", [])}
            own_treatment = [a for a in unit_arms if a.role != "COMPARATOR"]
            carried_arms = False
            if not own_treatment and previous_treatment and start > 0:
                unit_arms = [*previous_treatment, *unit_arms]
                carried_arms = True
            elif own_treatment:
                previous_treatment = own_treatment
            unit_words = [w for w in words if start <= w[0] < end]
            claim = _claim_from(r, unit_mentions, unit_arms, text[start:end], cited_for(start, end), unit_words, subgroup_at_frozen)
            claim.carried_arms = carried_arms
            carried = (claims[-1].base_direction or claims[-1].direction) if claims else None
            # the verb carries over only into a clause that has a value of its own ("..., 17.8 points more than placebo"),
            # never into a clause that states something else ("...; retatrutide remains investigational")
            if has_anchor and claim.base_direction is None and not claim.magnitude_signed and carried in ("DECREASE", "INCREASE"):
                # "lost 20.9% versus 3.1% with placebo", "reduced weight by 20.9%, 17.8 points more than placebo":
                # the verb carries over, and a comparative of this clause ("less than") still turns it round
                claim.base_direction = carried
                claim.direction = {"DECREASE": "INCREASE", "INCREASE": "DECREASE"}[carried] if claim.comparative_polarity == "DOWN" else carried
            claims.append(claim)
    comparative = any(c.comparative or any(s["name"] == "SUPERIORITY" for s in c.statistical) for c in claims) and any(m.data["text"] in ("than", "versus", "vs", "compared with", "compared to", "relative to", "against") for m in r.of("CMP"))
    return SentenceReading(text=sentence, cited=cited_all, claims=claims, instruction=instruction, hypothetical=hypothetical, comparative=comparative, problems=problems,
                           anaphor=anaphor.group(0).strip() if anaphor else None, study_anaphor=study_anaphor.group(0).strip() if study_anaphor else None)
