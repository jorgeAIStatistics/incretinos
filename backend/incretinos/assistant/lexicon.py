"""Controlled vocabulary of the claim extractor.

Everything the extractor can read out of a sentence is listed here: number roles, direction words,
units, estimand names, statistical interpretations, evidence tiers, verification wording, population
wording and endpoint families. A word that is neither in this vocabulary nor in ``NEUTRAL`` is a
*residual term*: the verifier only lets it through when the cited fact itself uses it. That is the
fail-closed rule - the extractor never has to understand a sentence to refuse it.

Patterns are applied in the order given; an earlier pattern consumes its text, so the specific
(negated, multi-word) forms come before the general ones.
"""

from __future__ import annotations

import re

NUM = r"[-+]?\d+(?:\.\d+)?"
UNUM = r"\d+(?:\.\d+)?"

# --------------------------------------------------------------------------- number words

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_UNIT_WORD = r"(?:percent(?:age)?|per cent|%|kg|kilograms?|weeks?|months?|years?|mg|milligrams?|points?|participants|patients|adults|people|-?fold)"
NUMBER_WORD_RE = re.compile(
    r"\b(?:(?P<tens>" + "|".join(_TENS) + r")(?:[- ](?P<ones1>" + "|".join(list(_ONES)[1:10]) + r"))?|(?P<ones2>" + "|".join(_ONES) + r")|(?P<hundred>(?:a|one) hundred))"
    r"(?=[- ]?\s*" + _UNIT_WORD + r"\b)",
    re.IGNORECASE,
)


# "at week eighty", "Phase three": a number word that names a time point or a phase, whatever follows it
_NUMBER_WORD = (
    r"(?:(?P<tens>" + "|".join(_TENS) + r")(?:[- ](?P<ones1>" + "|".join(list(_ONES)[1:10]) + r"))?|(?P<ones2>" + "|".join(_ONES) + r")|(?P<hundred>(?:a|one) hundred))"
)
NUMBER_WORD_AFTER_RE = re.compile(r"\b(?P<lead>weeks?|months?|years?|days?|phase)\s+" + _NUMBER_WORD + r"\b", re.IGNORECASE)


def number_word_value(match: re.Match) -> int:
    if match.group("hundred"):
        return 100
    if match.group("tens"):
        return _TENS[match.group("tens").lower()] + (_ONES[match.group("ones1").lower()] if match.group("ones1") else 0)
    return _ONES[match.group("ones2").lower()]


# --------------------------------------------------------------------------- numeric roles (applied in this order)

_CI_LABEL = r"(?:\bCI\b|\bconfidence intervals?\b|\bconfidence limits?\b|\bcredible interval\b)"
CI_RE = re.compile(
    rf"(?:(?P<level>\d{{2}}(?:\.\d+)?)\s*(?:%|percent)\s*)?{_CI_LABEL}(?:\s*(?:of|was|were|is|=|:|,|ranged from|ranging from|from|spanning|spanned|limits))*\s*[\[(]?\s*"
    rf"(?P<low>{NUM})\s*%?\s*(?:to|,|;|and|-)\s*(?P<high>{NUM})\s*%?\s*[\])]?",
    re.IGNORECASE,
)
INTERVAL_RE = re.compile(rf"\binterval\s+(?:spanning|of|from)\s+(?P<low>{NUM})\s*%?\s*(?:to|and|,)\s*(?P<high>{NUM})\s*%?", re.IGNORECASE)
CI_LABEL_RE = re.compile(rf"(?:(?P<level>\d{{2}}(?:\.\d+)?)\s*(?:%|percent)\s*)?{_CI_LABEL}", re.IGNORECASE)
PAREN_INTERVAL_RE = re.compile(rf"[\[(]\s*(?P<low>{NUM})\s*%?\s*(?:to|,|;)\s*(?P<high>{NUM})\s*%?\s*[\])]")
P_VALUE_RE = re.compile(
    r"(?<![A-Za-z])[pP](?:[- ]?values?)?\s*(?:of|was|is|:)?\s*(?P<op><=|>=|<|>|=|≤|≥|less than|greater than|below|under)?\s*(?P<val>0?\.\d+|\d+(?:\.\d+)?(?:e-?\d+)?)(?![A-Za-z0-9])"
)
_WEIGHT_WORDS = r"(?:(?:body[- ]?)?weight[- ](?:reduction|loss)|reduction in (?:body[- ]?)?weight|loss of (?:body[- ]?)?weight)"
THRESHOLD_RES = [
    re.compile(rf"(?:{_WEIGHT_WORDS}|lost|losing|lose|loss|reduction)\s*(?:of\s*)?(?P<val>{UNUM})\s*(?:%|percent)\s+or\s+(?:more|greater)(?:\s*of\s+(?:their\s+|baseline\s+)?(?:body[- ]?)?weight)?", re.IGNORECASE),
    re.compile(rf"(?:{_WEIGHT_WORDS}|lost|losing|lose|loss|reduction)\s*(?:of\s*)?(?:≥|>=|=>|at least|of at least)\s*(?P<val>{UNUM})\s*(?:%|percent)(?:\s*(?:or (?:more|greater)))?(?:\s*of\s+(?:their\s+|baseline\s+)?(?:body[- ]?)?weight)?", re.IGNORECASE),
    re.compile(
        rf"(?:≥|>=|=>|at least|greater than or equal to)\s*(?P<val>{UNUM})\s*(?:%|percent)(?:\s*(?:or (?:more|greater)))?"
        rf"(?:\s*(?:of\s+(?:their\s+|baseline\s+)?(?:body[- ]?)?weight|{_WEIGHT_WORDS}|(?:body[- ]?)?weight))?",
        re.IGNORECASE,
    ),
    re.compile(rf"(?P<val>{UNUM})\s*(?:%|percent)\s+or\s+(?:more|greater)(?:\s*(?:of\s+(?:their\s+)?(?:body[- ]?)?weight|{_WEIGHT_WORDS}))?", re.IGNORECASE),
]
DOSE_RE = re.compile(
    r"(?<![\d.A-Za-z])(?P<vals>(?:\d+(?:\.\d+)?(?:\s*mg)?\s*(?:/|-|,|,?\s*\bor\b|,?\s*\band\b)\s*)*\d+(?:\.\d+)?)\s*-?\s*(?P<unit>mg/kg|mg|milligrams?|mcg|µg|μg)\b(?!\s*/\s*d[lL])",
    re.IGNORECASE,
)
TIME_RES = [
    (re.compile(rf"\bweeks?[\s-]*(?P<val>{UNUM})\b(?!\s*(?:%|percent|mg|kg))", re.IGNORECASE), 1.0),
    (re.compile(rf"(?<![\d.])(?P<val>{UNUM})[\s-]*(?:weeks?|wks?)\b", re.IGNORECASE), 1.0),
    (re.compile(rf"(?<![\d.])(?P<val>{UNUM})[\s-]*months?\b", re.IGNORECASE), 52.0 / 12.0),
    (re.compile(rf"(?<![\d.])(?P<val>{UNUM})[\s-]*(?:years?|yrs?)\b(?!\s*(?:of age|old|and older|or older))", re.IGNORECASE), 52.0),
]
# "after a year of treatment": a time point written without a digit
WORDED_TIME_RES = [
    (re.compile(r"\b(?:a|one|per|each) year\b|\bannual(?:ly)?\b|\b12 months\b", re.IGNORECASE), 52.0),
    (re.compile(r"\bhalf a year\b|\bsix months\b", re.IGNORECASE), 26.0),
    (re.compile(r"\ba month\b", re.IGNORECASE), 52.0 / 12.0),
]
# "801 of 6586 participants": events (or responders) out of the number analysed
COUNT_OF_RE = re.compile(
    r"(?<![\d.])(?P<val>\d+)\s+(?:of|out of)\s+(?P<n>\d+)\b(?!\s*(?:%|percent|mg|kg|weeks?|months?))(?:\s+(?:randomi[sz]ed\s+|enrolled\s+|treated\s+|analy[sz]ed\s+)?(?:participants|patients|adults|people|individuals|subjects))?",
    re.IGNORECASE,
)
COUNT_RES = [
    re.compile(r"\b[nN]\s*=\s*(?P<val>\d+)\b"),
    re.compile(r"(?<![\d.])(?P<val>\d+)\s+(?:randomi[sz]ed\s+|enrolled\s+|treated\s+|analy[sz]ed\s+|adult\s+)?(?:participants|patients|adults|people|individuals|subjects|persons|volunteers)\b", re.IGNORECASE),
    re.compile(r"\b(?:enrol(?:l)?ment|enrolled|sample size)\s*(?:of|was|is|:)?\s*(?P<val>\d+)\b", re.IGNORECASE),
]
PHASE_RE = re.compile(r"\bphase\s*(?P<p>[1-4]|iv|i{1,3})(?:\s*[ab])?\b(?:\s*/\s*(?P<p2>[1-4]|iv|i{1,3})\b)?", re.IGNORECASE)
DISPERSION_RE = re.compile(rf"(?:\b(?P<kind>standard error|standard deviation)\b|\b(?P<abbr>SEM|SE|SD)\b)\s*(?:of|was|is|=|:)?\s*(?P<val>{UNUM})", re.IGNORECASE)
# an inexact bound in front of a value changes the claim: "up to 20.9%", "more than 25%"
BOUND_RE = re.compile(r"\b(?:up to|as much as|as high as|as low as|as little as|at most|more than|greater than|over|above|under|below|less than|fewer than|exceeding|in excess of|upwards of|around|about|approximately|roughly|nearly|almost|circa|some)\s*$|~\s*$", re.IGNORECASE)
MARGIN_RE = re.compile(rf"\bmargin\s*(?:of|was|is|=|:)?\s*(?P<val>{NUM})", re.IGNORECASE)
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RES = [
    re.compile(r"\b(?P<y>(?:19|20)\d{2})-(?P<m>\d{2})(?:-(?P<d>\d{2}))?\b"),
    re.compile(rf"\b(?P<mon>{_MONTHS})[a-z]*\.?\s+(?:(?P<d>\d{{1,2}}),?\s+)?(?P<y>(?:19|20)\d{{2}})\b", re.IGNORECASE),
    re.compile(r"(?<![\d.\-])(?P<y>(?:19|20)\d{2})(?![\d.]|\s*(?:%|percent|mg|kg|participants|patients|adults))"),
]
MONTH_NUMBER = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}

_UNIT = (
    r"(?P<unit>%|percent(?:age)?[- ]points?|percent\b|pp\b|points?\b|kg\s*/\s*m(?:2|\^2|²)|kg\b|kilograms?\b|mm\s?hg\b|mg\s*/\s*dl\b|mmol\s*/\s*mol\b|mmol\s*/\s*l\b"
    r"|cm\b|centimet(?:er|re)s?\b|lbs?\b|pounds?\b|-?fold\b|kilos?\b|events?\s*(?:per|/)\s*(?:hour|hr|h)\b"
    # units no stored value uses: read so that they are refused, never ignored ("19.9 in" is not 19.9 cm)
    r"|inch(?:es)?\b|in\b(?=\s*(?:$|[,;.)]|with\b|at\b|for\b|from\b|after\b|over\b|by\b|versus\b|vs\b|than\b|compared\b|among\b|and\b|or\b))"
    r"|ft\b|feet\b|stones?\b|grams?\b|g\b|oz\b|ounces?\b|mm\b|millimet(?:er|re)s?\b|met(?:er|re)s?\b)"
)
RANGE_RES = [
    re.compile(rf"\b(?:from|between)\s+(?P<low>{NUM})\s*(?:%|percent)?\s*(?:to|and)\s*(?P<high>{NUM})\s*{_UNIT}", re.IGNORECASE),
    re.compile(rf"(?<![\d.])(?P<low>{NUM})\s*(?:%|percent)\s*to\s*(?P<high>{NUM})\s*{_UNIT}", re.IGNORECASE),
]
MAGNITUDE_RE = re.compile(rf"(?<![A-Za-z0-9.])(?P<val>{NUM})\s*{_UNIT}", re.IGNORECASE)
# A number standing on its own: not glued to letters ("MACE-4", "GLP-1", "HbA1c", "TRIUMPH-1" are names, not numbers).
BARE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.\-/^])(?P<val>[-+]?\d+(?:\.\d+)?)(?![A-Za-z0-9_^]|\.\d|-[A-Za-z])")

PEOPLE_AFTER_RE = re.compile(r"\s*of\s+(?:the\s+|all\s+|those\s+|these\s+)?(?:\w+\s+)?(?:participants|patients|adults|people|individuals|subjects|persons|volunteers|recipients)\b", re.IGNORECASE)

UNIT_CLASS = [
    (re.compile(r"percent(?:age)?[- ]points?|^pp$|^points?$", re.IGNORECASE), "PP"),
    (re.compile(r"^%$|^percent$", re.IGNORECASE), "PCT"),
    (re.compile(r"kg\s*/\s*m", re.IGNORECASE), "KG_M2"),
    (re.compile(r"^kg$|kilogram|^kilos?$", re.IGNORECASE), "KG"),
    (re.compile(r"^events?\s*(?:per|/)\s*(?:hour|hr|h)$", re.IGNORECASE), "EVENTS_H"),
    (re.compile(r"mm\s?hg", re.IGNORECASE), "MMHG"),
    (re.compile(r"mg\s*/\s*dl", re.IGNORECASE), "MG_DL"),
    (re.compile(r"^cm$|centimet", re.IGNORECASE), "CM"),
    (re.compile(r"fold", re.IGNORECASE), "FOLD"),
]


def sentence_unit_class(unit: str | None) -> str | None:
    if not unit:
        return None
    for pattern, name in UNIT_CLASS:
        if pattern.search(unit.strip()):
            return name
    return "UNSUPPORTED"  # pounds, mmol/mol, ... : a unit no stored fact uses


#: Which stored unit classes (facts.unit_class) a sentence unit may describe.
UNIT_COMPATIBLE = {
    "PCT": {"PCT", "PCT_PARTICIPANTS", "PCT_HBA1C", "PCT_POINTS"},
    "PCT_PEOPLE": {"PCT_PARTICIPANTS"},  # "25% of participants" is a proportion of people, never a mean change
    "PP": {"PCT_POINTS", "PCT_HBA1C"},
    "KG": {"KG"},
    "KG_M2": {"KG_M2"},
    "MMHG": {"MMHG"},
    "MG_DL": {"MG_DL"},
    "CM": {"CM"},
    "EVENTS_H": {"EVENTS_H"},
}

# --------------------------------------------------------------------------- parameters

PARAM_RES = [
    (re.compile(r"(?i:\bhazard ratios?\b)|\bHR\b"), "HR"),
    (re.compile(r"(?i:\bodds ratios?\b)|\bOR\b(?=\s*(?:=|of|was|,|:)?\s*\d)"), "OR"),
    (re.compile(r"(?i:\b(?:risk|rate) ratios?\b|\brelative risks?\b)|\bRR\b"), "RR"),
    (re.compile(r"(?i:\b(?:estimated |mean |ls[- ]mean |least[- ]squares mean |absolute |net |between[- ]group |between[- ]arm )*(?:treatment |risk )?differences?\b)|\bETD\b"), "DIFFERENCE"),
    (re.compile(r"(?i:\bplacebo[- ](?:adjusted|subtracted|corrected)\b)"), "DIFFERENCE"),
]

# --------------------------------------------------------------------------- comparator markers

COMPARATOR_RE = re.compile(
    r"\bversus\b|\bvs\b\.?|\bcompared (?:with|to)\b|\bin comparison (?:with|to)\b|\bcomparison (?:with|to)\b|\brelative to\b|\bthan\b|\bagainst\b|\bover (?=placebo|the comparator)"
    r"|\b(?:difference|differences) (?:from|versus|vs\.?)\b|\bminus\b",
    re.IGNORECASE,
)
PAIR_RE = re.compile(r"\bbetween\b", re.IGNORECASE)
COMBINATION_CONNECTOR_RE = re.compile(r"^\s*(?:\+|plus|combined with|in combination with|added to|on top of|co-?administered with|together with)\s*$", re.IGNORECASE)
LOOSE_CONNECTOR_RE = re.compile(r"^\s*(?:and|with)\s+(?:[\w-]+\s+){0,2}$|^\s*(?:and|with)\s*$", re.IGNORECASE)
#: Quantifiers that claim completeness, by what they claim. "every combination" restates "all combinations".
EXHAUSTIVE_CLASSES = [{"all", "every", "always", "everyone"}, {"only", "exclusively", "solely", "sole"}, {"none", "no other", "nobody"}]

# --------------------------------------------------------------------------- feature lexicon

# (feature, pattern). Order matters: first match consumes the text.
FEATURES: list[tuple[str, re.Pattern]] = []
#: Patterns that reach across other words ("do not state ... that X 12 mg is superior"): they may run over a drug name or a number.
SPANNING: set[re.Pattern] = set()


def _f(name: str, pattern: str, flags: int = re.IGNORECASE, spanning: bool = False) -> None:
    compiled = re.compile(pattern, flags)
    FEATURES.append((name, compiled))
    if spanning:
        SPANNING.add(compiled)


_NULL = r"(?:no difference|1(?:\.0+)?|one|zero|0(?:\.0+)?|unity|the null(?: value)?)"
_NEG = r"(?:not|neither|nor|never|cannot be|can't be|cannot|no evidence (?:of|that|for)|no(?! (?:doubt|question|wonder|surprise))|without(?! (?:question|doubt|a doubt|exception))|rather than|instead of)"
_NEG_VERB = (
    r"(?:(?:does|do|did|would|could|can) not|doesn't|didn't|cannot|can't|must not|should not|may not|not be|never|fail(?:s|ed)? to|failed to|unable to)"
    r"\s+(?:be\s+)?(?:establish(?:ed)?|show(?:n|ed)?|demonstrate[d]?|prove[dn]?|support(?:ed)?|indicate[d]?|imply|implied|state[d]?|say|claim(?:ed)?|conclude[d]?|describe[d]?|call(?:ed)?|read|rank(?:ed)?|interpret(?:ed)?|taken?|meet|met|reach(?:ed)?|achieve[d]?|confirm(?:ed)?)"
)
_GAP = r"(?:\W+[\w-]+){0,7}?\W+"

# missing information stated as missing
_f("CI_NOT_REPORTED", r"\b(?:no|without(?: an?y?| a)?)\s+(?:\d{2}\s*%\s*)?(?:confidence intervals?|CI)\b(?:\s+(?:is|was|were|has been|being))?(?:\s+(?:reported|available|given|provided|stated|disclosed))?")
_f("CI_NOT_REPORTED", r"\b(?:\d{2}\s*%\s*)?(?:confidence intervals?|CI)\b[^.;,()]{0,40}?\b(?:not|never|n't)\s+(?:been\s+)?(?:reported|available|given|provided|stated|disclosed|published)\b", spanning=True)
_f("CI_NOT_REPORTED", r"\b(?:confidence intervals?|CI)\s+(?:is|was|were)\s+(?:missing|unavailable|absent|unreported|lacking)\b")
_f("N_NOT_REPORTED", r"\b(?:n|sample size|number of participants|number analy[sz]ed|group size)\s+(?:is |was |were )?(?:not|never)\s+(?:been\s+)?(?:reported|stated|given|available|disclosed)\b|\bno sample size\b")
_f("N_NOT_REPORTED", r"\b(?:sample size|number of participants|number analy[sz]ed|group size)\b[^.;,()]{0,40}?\b(?:not|never)\s+(?:been\s+)?(?:reported|stated|given|available|disclosed)\b", spanning=True)
# "neither a confidence interval nor the number analysed is reported": read by the extractor into the items it names
_f("NEITHER_REPORTED", r"\bneither\b[^.;]{0,90}?\bnor\b[^.;]{0,70}?\b(?:is|was|were|are)\s+(?:reported|stated|given|available|disclosed)\b", spanning=True)
_f("P_NOT_REPORTED", r"\bp[- ]?values?\s+(?:is |was |were )?(?:not|never)\s+(?:been\s+)?(?:reported|stated|given|available|disclosed)\b|\bno p[- ]?value\b|\bwithout a p[- ]?value\b")
_f("ESTIMAND_NOT_STATED", r"\bestimand\s+(?:is |was )?not\s+(?:stated|reported|specified|named|given)\b|\bno estimand\s+(?:is |was )?(?:stated|reported|specified|named)\b|\bunstated estimand\b")
_f("MULT_NOT_STATED", r"\bmultiplicity(?:[- ]control)?(?: status)?\s+(?:is |was )?not\s+(?:stated|reported|specified)\b")
_f("TIME_NOT_STATED", r"\b(?:assessment )?time(?: ?point)?\s+(?:is |was )?not\s+(?:stated|reported|specified)\b")
_f("POP_NOT_STATED", r"\bdiabetes status\s+(?:is |was )?not\s+(?:stated|reported|specified)\b")
_NOT_GIVEN = r"(?:(?:does|did|do) not|doesn't|didn't)\s+(?:report|give|provide|state|include|disclose|contain)\s+(?:a |an |any |the )?"
_f("CI_NOT_REPORTED", rf"\b{_NOT_GIVEN}(?:\d{{2}}\s*%\s*)?(?:confidence intervals?|CI)\b")
_f("P_NOT_REPORTED", rf"\b{_NOT_GIVEN}p[- ]?values?\b")
_f("N_NOT_REPORTED", rf"\b{_NOT_GIVEN}(?:sample size|number of participants|group size|n)\b")
_f("ESTIMAND_NOT_STATED", rf"\b{_NOT_GIVEN}estimand\b")

# interval read against the null
_f("CI_INCLUDES_NULL", rf"\b(?:confidence )?interval\s+(?:includes|included|spans|spanned|crosses|crossed|contains|contained|covers|covered|overlaps|overlapped)\s+{_NULL}(?![\w.])")
_f("CI_INCLUDES_NULL", rf"\bCI\s+(?:includes|included|spans|crosses|crossed|contains|covers)\s+{_NULL}(?![\w.])")
_f("CI_INCLUDES_NULL", rf"\b(?:includes|included|spans|crosses|crossed|contains|covers)\s+{_NULL}(?![\w.])")
_f("CI_EXCLUDES_NULL", rf"\b(?:(?:confidence )?interval|CI)\s+(?:excludes|excluded|does not include|did not include|does not cross|did not cross)\s+{_NULL}(?![\w.])")
_f("CI_EXCLUDES_NULL", rf"\b(?:excludes|excluded|excluding)\s+{_NULL}(?![\w.])")

# statistical interpretation
_POST_NEG = (
    r"(?:(?:was|were|is|are|has|have|had|could|can|did|does)\s+(?:not|never)|wasn't|weren't|isn't|hasn't|haven't|hadn't|couldn't|cannot|can't|did not|does not)"
    r"\s+(?:been\s+|be\s+|yet\s+)*(?:shown|demonstrated|established|met|achieved|proven|proved|reached|concluded|confirmed|declared|claimed|tested|attained|supported)"
)
_f("NEG_SUPERIORITY", rf"\bsuperiority\b[^.;]{{0,80}}?\b{_POST_NEG}\b", spanning=True)
_f("NEG_SUPERIORITY", r"\bsuperiority\b[^.;]{0,80}?\b(?:failed|fell short|was rejected|not met|not shown|not demonstrated|not established)\b", spanning=True)
_f("NEG_SUPERIORITY", rf"\b{_NEG_VERB}{_GAP}superior(?:ity)?\b", spanning=True)
_f("NEG_SUPERIORITY", r"\b(?:must|should|can|may)(?:\s+not|n't)\s+be\s+(?:read|described|interpreted|called|taken|presented)\s+as\s+superior(?:ity)?\b")
_f("NEG_SUPERIORITY", rf"\b{_NEG}{_GAP}superior(?:ity)?\b", spanning=True)
_f("NEG_SUPERIORITY", r"\bno\s+(?:test of |test for |claim of |conclusion of )?superiority\b(?:\s+(?:test|claim|conclusion|analysis))?")
_f("HYPOTHETICAL_SUPERIORITY", r"\b(?:showing|establishing|demonstrating|proving|claiming|to (?:show|establish|demonstrate|prove|claim|test)|a claim of|claims? of|(?:a )?test(?:ing)? (?:of|for))\s+(?:that\s+)?(?:[\w-]+\s+){0,5}?superior(?:ity)?\b", spanning=True)
_f("SUPERIORITY", r"\bsuperior(?:ity)?\b|\boutperform(?:ed|s|ing)?\b|\bbeat(?:s|ing)?\b|\bbetter than\b|\bmore effective\b|\bmore efficacious\b|\bmore potent\b|\bbest[- ]in[- ]class\b")
_f("SUPERIORITY", r"\bsurpass(?:ed|es|ing)\b|\bexceed(?:ed|s|ing)?\b|\bworks? better\b|\bstronger\b|\bmost effective\b|\bthe better (?:drug|agent|option|treatment|choice)\b|\bpreferable\b|\bdominat(?:ed|es)\b|\boutdid\b|\bbest\b|\bbetter\b|\bworse\b|(?<!non-)(?<!non)(?<!non )\binferior to\b")
_f("NOT_SIGNIFICANT", r"\b(?:not|non|no|without|never)[- ]+(?:a |any |been |reach(?:ed|ing)? |achiev(?:ed|ing|e) |attain(?:ed)? |statistically |statistical |nominally |formally )*significan(?:t|ce|tly)\b")
_f("NOT_SIGNIFICANT", r"\b(?:must|should|can|may|cannot)(?:\s+not|n't)?\s+be\s+(?:described|called|read|interpreted|presented|taken|reported)\s+as\s+(?:statistically\s+|nominally\s+)?significan(?:t|ce)\b")
_f("NOT_SIGNIFICANT", r"\b(?:do not|don't|never|must not|should not|cannot)\s+(?:describe|call|read|present|report|interpret)\b[^.;]{0,60}?\bas\s+(?:statistically\s+|nominally\s+)?significan(?:t|ce)\b", spanning=True)
_f("NOT_SIGNIFICANT", r"\b(?:did not|does not|didn't|failed to|fell short of|short of)\s+(?:reach|achieve|attain|meet|show)?\s*(?:statistical |nominal )?significan(?:t|ce)\b|\bnon-?significant\b|\binsignificant\b")
_f("NO_DIFFERENCE", r"\bno\s+(?:clinically |meaningful |real |relevant |material |apparent |evidence of (?:a |any )?|clear )*(?:difference|differences|effect|effects|change|benefit|impact|reduction|increase|improvement|advantage)\b")
_f("NO_DIFFERENCE", r"\b(?:did|does|do) not (?:differ|change)\b|\bnot differ(?:ent)?\b|\bequivalen(?:t|ce)\b|\bsimilar\b|\bcomparable\b|\b(?:was|were|is|are|remained) the same\b|\bthe same as\b|\bidentical\b|\bindistinguishable\b|\bunchanged\b")
_f("NO_DIFFERENCE", r"\bno better\b|\bno worse\b|\bnull (?:result|effect|finding)\b|\bzero (?:effect|difference|change)\b|\bineffective\b|\bno (?:body[- ]?)?weight (?:loss|change|reduction)\b|\bhad no\b|\bno events?\b")
_f("SIGNIFICANT", r"\b(?:statistically |highly |clinically |nominally )?significan(?:t|ce|tly)\b")
_f("NOMINAL", r"\bnominal(?:ly)?\b")
_f("NI_NOT_MET", r"\b(?:not|failed to (?:show|demonstrate|meet|establish)|did not (?:show|demonstrate|meet|establish|achieve))\b[^.;]{0,30}?\bnon-?inferior(?:ity)?\b", spanning=True)
_f("NI_MET", r"\b(?:was|were|is|proved|shown to be|found to be|demonstrated to be|deemed|judged)\s+non-?inferior\b|\bnon-?inferior to\b")
_f("NI_MET", r"\b(?:met|meeting|meets|satisf(?:ied|ies|ying)|fulfill?(?:ed|s|ing)|achieved|reached)\s+(?:the\s+|its\s+|a\s+)?(?:pre-?specified\s+)?non-?inferiority\s+(?:criteri(?:on|a)|margin|threshold|bound|boundary)\b")
_f("NI_MET", r"\bnon-?inferiority\s+(?:was |were |is |has been |had been )?(?:met|shown|demonstrated|established|achieved|confirmed|concluded|declared|satisfied)\b|\bnon-?inferiority criteri(?:on|a)\b")
_f("NI_MET", r"\b(?:met|meeting|meets|satisf(?:ied|ies|ying)|fulfill?(?:ed|s|ing)|achieved|demonstrated|established|showed|shown|confirmed|reached)\b[^.;]{0,40}?\bnon-?inferiority\b", spanning=True)
_f("NI_TEST", r"\bnon-?inferior(?:ity)?\b")

# "confirmed against the source" is a claim about checking, not about the trial: read it before the role words below
_f("VERIFICATION_HUMAN", r"\b(?:confirmed|checked|verified|validated|audited|cross-checked|reconciled)\s+(?:against|with|by|in)\s+(?:the\s+|its\s+|an?\s+)?(?:original\s+)?(?:source|publication|paper|article|record|registry|label|reviewers?|humans?|experts?|curators?)\b")
_f("VERIFICATION_HUMAN", r"\b(?:has|have|had) been (?:independently |manually |fully )?(?:confirmed|checked|audited|certified|verified|validated)\b|\b(?:source|manually|human|hand)[- ](?:checked|confirmed)\b")

# endpoint role and multiplicity
_f("MULT_NOT_CONTROLLED", r"\bnot\s+(?:been\s+)?(?:controlled|adjusted|corrected)\s+for\s+(?:multiplicity|multiple (?:comparisons|testing)|type (?:i|1) error)\b|\bnot multiplicity[- ](?:controlled|adjusted)\b")
_f("MULT_NOT_CONTROLLED", r"\b(?:without|no)\s+(?:multiplicity|type (?:i|1) error)[- ]?(?:control|adjustment|correction)?\b|\b(?:uncontrolled|unadjusted) for multiplicity\b|\boutside (?:the )?(?:type (?:i|1) error|multiplicity)[- ]control\w*\b")
_f("MULT_CONTROLLED", r"\bmultiplicity[- ](?:controlled|adjusted)\b|\b(?:controlled|adjusted|corrected)\s+for\s+(?:multiplicity|multiple (?:comparisons|testing)|type (?:i|1) error)\b|\btype (?:i|1) error[- ]controlled\b")
_f("NEG_CONFIRMATORY", rf"\b{_NEG_VERB}\b|\bnot\s+(?:been\s+|yet\s+)*(?:established|confirmed|proven|proved|demonstrated)\b|\bnot\s+(?:a\s+)?confirmatory\b|\bnon-?confirmatory\b")
_f("KEY_SECONDARY", r"\bkey[- ]secondary\b")
_f("PRIMARY", r"\b(?:co-?)?primary\b")
_f("SECONDARY", r"\bsecondary\b")
_f("EXPLORATORY", r"\bexploratory\b|\bhypothesis[- ]generating\b")
_f("POST_HOC", r"\bpost[- ]hoc\b")
_f("CONFIRMATORY", r"\bconfirm(?:ed|s|ing)?\s+(?:the\s+|a\s+|its\s+)?(?:non-?inferiority|superiority|efficacy|primary hypothesis|hypothesis|(?:cardiovascular )?safety)\b"
   r"|\b(?:non-?inferiority|superiority|efficacy|(?:cardiovascular )?safety)\s+(?:was|were|is|has been|had been)\s+confirmed\b")
_f("CONFIRMATORY", r"\bconfirmatory\b|\bprove[dns]?\b|\bproof\b|\bestablish(?:ed|es|ing)?\b|\bdefinitive(?:ly)?\b|\bconclusive(?:ly)?\b|\bunequivocal(?:ly)?\b|\bpivotal\b")
_f("DEMONSTRATED", r"\bdemonstrat(?:ed|es|ing|e)\b")

# estimands
_f("ESTIMAND_TREATMENT_REGIMEN", r"\btreatment[- ]regimen(?:[- ]estimand)?\b|\btreatment[- ]policy(?: estimand| strategy)?\b|\b(?:regardless|irrespective) of (?:treatment |study[- ]drug )?(?:discontinuation|adherence)\b")
_f("ESTIMAND_EFFICACY", r"\befficacy[- ]estimand\b|\bhypothetical (?:estimand|strategy)\b|\bon[- ]treatment estimand\b|\bhad (?:all |every )?participants? remained on (?:the assigned |study )?treatment\b|\bif (?:all )?participants (?:had )?(?:stayed|remained) on treatment\b")
_f("ESTIMAND_ON_TREATMENT", r"\b(?:while[- ])?on[- ]treatment\b|\bper[- ]protocol\b|\bcompleters?\b")
_f("ESTIMAND_INFERRED", r"\binferred\b")

# evidence tier
_f("TIER_NOT_PEER_REVIEWED", r"\b(?:not|never|non|rather than|instead of|without)[- ]+(?:yet |been |a |being |undergone |undergoing )*peer[- ]review(?:ed)?\b|\bunpublished\b|\bnot (?:yet )?(?:been )?published\b")
_f("TIER_PEER_REVIEWED", r"\bpeer[- ]review(?:ed)?\b|\bjournal (?:article|publication|paper)\b|\bpublished (?:in|paper|article|study|trial|report|results|data)\b|\bpublication\b|\bpublished\b")
_f("TIER_PEER_REVIEWED", r"\b(?:nejm|new england journal of medicine|the lancet|lancet|jama|nature medicine)\b")
_f("TIER_PRESS_RELEASE", r"\bpress[- ]release\b|\btop-?line\b|\bsponsor (?:announcement|disclosure|statement|release)\b|\bannounce(?:d|s|ment)\b|\bcompany (?:announcement|release)\b|\bnews release\b")
_f("TIER_REGISTRY_RESULTS", r"\bregistry[- ]posted\b|\bposted (?:results|on (?:the )?registry|to clinicaltrials\.gov)\b|\bclinicaltrials\.gov (?:results|record|posting|entry)\b|\bregistry (?:results|record|data|entry|posting)\b|\bsponsor[- ]submitted\b")
_f("TIER_REGULATORY_LABEL", r"\bprescribing information\b|\b(?:product |approved |fda |us |drug )(?:label(?:ing)?)\b|\bpackage insert\b|(?<![-\w])label(?:ing)?\b(?! dosage)")
_f("TIER_REGULATORY_REVIEW", r"\bfda review\b|\bregulatory review\b|\breview document\b|\bstatistical review(?:er)?\b")
_f("TIER_SPONSOR_PIPELINE", r"\bpipeline (?:page|disclosure)\b")
_f("TIER_CONFERENCE", r"\bconference\b|\bcongress\b|\bposter\b|\bpreprint\b")

# verification status
_f("VERIFICATION_NOT_HUMAN", r"\b(?:not|never)\s+(?:yet\s+|been\s+)*(?:human[- ])?(?:verified|validated|reviewed)\b(?:\s+by\s+(?:a |an )?(?:human|reviewer|person))?|\bunverified\b|\bunreviewed\b")
_f("VERIFICATION_NOT_HUMAN", r"\b(?:awaiting|awaits|pending)\s+(?:human\s+)?(?:review|verification)\b|\bno human (?:review|verification)\b|\bwithout human (?:review|verification)\b")
_f("VERIFICATION_HUMAN", r"\bhuman[- ](?:verified|reviewed|validated|checked|approved|confirmed)\b|\b(?:verified|validated|checked|confirmed|approved|reviewed)\s+by\s+(?:a |an |the )?(?:human|reviewer|expert|person|clinician|curator|scientist)s?\b")
_f("VERIFICATION_HUMAN", r"\b(?:reviewer|expert|manually|independently|clinically|fully)[- ](?:verified|validated|approved|confirmed|checked|reviewed)\b|\bverified\b|\bvalidated\b|\bdouble[- ]checked\b|\bfact[- ]checked\b|\bverification\b")
_f("VERIFICATION_HUMAN", r"\breviewed\b|\baudited\b|\bcertified\b|\bcross-checked\b|\bsigned off\b")
_f("VERIFICATION_HUMAN", r"\b(?:[a-z]+-)?(?:confirm(?:ed|s|ing)?|checked|vetted|verified|validated|audited|certified|corroborated|authenticated)\b|\bconfirmation\b|\bvalidation\b")
_f("VERIFICATION_QUOTE_ANCHORED", r"\bquote[- ]anchored\b")
_f("VERIFICATION_MACHINE_EXTRACTED", r"\bmachine[- ]extracted\b")

# population (diabetes status)
_f("POP_MIXED", r"\bwith or without (?:type 2 |type ii )?diabetes(?: mellitus)?\b|\bregardless of diabetes status\b")
_f("POP_NO_T2D", r"\bwithout (?:type 2 |type ii |known )*diabetes(?: mellitus)?\b|\bnon[- ]?diabetic\b|\b(?:not|no|free of|did not have|do not have|does not have) (?:type 2 |type ii )?diabetes(?: mellitus)?\b|\bwithout t2dm?\b")
_f("POP_T1D", r"\btype 1 diabetes\b|\bt1dm?\b")
_f("POP_T2D", r"\btype (?:2|ii) diabetes(?: mellitus)?\b|\bt2dm?\b|\bdiabetic\b|\bwith diabetes\b|\bdiabetes\b")

# regulatory status
_f("REG_NOT_APPROVED", r"\bnot\s+(?:yet\s+|currently\s+|been\s+|fda[- ])*approved\b|\bunapproved\b|\bno (?:fda[- ])?approved (?:label(?:ing)?|indication|product)\b|\bhas not (?:been|received) (?:fda )?approv(?:ed|al)\b|\bnot (?:authori[sz]ed|licensed)\b")
_f("REG_APPROVED", r"\b(?:fda|ema|regulator)[- ]approved\b|\bapproved\b|\bapproval\b|\blicensed\b|\bauthori[sz]ed\b|\bmarketed\b|\bsold (?:as|under)\b|\bon the market\b|\bcommercially available\b")
_f("REG_INVESTIGATIONAL", r"\binvestigational\b|\bexperimental\b|\bin (?:clinical )?development\b|\bunder investigation\b")

# endpoint families
_f("EP_CV_DEATH", r"\bcardiovascular deaths?\b|\bcv deaths?\b|\b(?:deaths?|died|dying) (?:from|of|due to) (?:a )?cardiovascular causes?\b|\bcardiovascular mortality\b")
_f("EP_MORTALITY", r"\ball[- ]cause (?:death|mortality)\b|\bdeath from any cause\b|\bdeaths?\b|\bmortality\b|\bdied\b")
_f("EP_MACE", r"\bmace(?:-(?P<variant>\d))?\b(?!-\d)|\bmajor adverse cardiovascular events?\b")
_f("EP_HBA1C", r"\bhba1c\b|\ba1c\b|\bglycated ha?emoglobin\b|\bha?emoglobin a1c\b")
_f("EP_LEAN", r"\blean (?:body |soft tissue )?mass\b|\bfat[- ]free mass\b|\blean soft tissue\b")
_f("EP_FAT", r"\bfat mass\b|\bbody fat\b")
_f("EP_WAIST", r"\bwaist(?: circumference)?\b")
_f("EP_BMI", r"\bbmi\b|\bbody[- ]mass index\b")
_f("EP_BP", r"\b(?:systolic |diastolic )?blood pressure\b")
_f("EP_GLUCOSE", r"\bfasting (?:serum |plasma |blood )?glucose\b")
_f("EP_WEIGHT", r"\bbody[- ]?weight\b|\bweight\b")

# "the proportion of participants ... was 20.9%" is a responder proportion, never a mean change
_f("RESPONDER_PROPORTION", r"\b(?:proportions?|percentages?|shares?|fractions?|percent|rates?)\s+of\s+(?:the\s+|all\s+|those\s+)?[^.;]{0,40}?\b(?:participants|patients|adults|people|individuals|subjects|persons|recipients|responders)\b", spanning=True)
# causal wording: a within-arm change is not a treatment effect
_f("CAUSAL", r"\b(?:led|leads|leading|lead) to\b|\b(?:resulted|results|resulting|result) in\b|\bcaus(?:ed|es|ing|e)\b|\bbecause of\b|\bdue to\b|\battributable to\b|\battributed to\b|\bowing to\b|\bas a (?:result|consequence) of\b"
   r"|\bthanks to\b|\binduc(?:ed|es|ing|e)\b|\bdr(?:ove|ives|iven|ive) by\b|\bresponsible for\b|\bproduced by\b|\bbrought about\b")
# rankings: an order of treatments asserted as an effect
_f("RANKING", r"\brank(?:s|ed|ing)?\s+(?:first|second|third|highest|lowest|above|below|ahead|top|best)\b|\bahead of\b|\btop[- ]ranked\b|\bhighest[- ]ranked\b|\bnumber one\b|\bfirst place\b|\blead(?:s|ing)? the (?:field|class|pack)\b")

# exhaustive quantifiers
_f("EXHAUSTIVE", r"\bonly\b|\ball\b|\bevery\b|\bnone\b|\bno other\b|\bexclusively\b|\bsolely\b|\bsole\b|\balways\b|\beveryone\b|\bnobody\b")

# direction of effect
_f("DIR_DECREASE_VERB", r"\b(?:reduc(?:ed|es|e|ing)|lower(?:ed|s|ing)|decreas(?:ed|es|e|ing)|fell|fall(?:s|ing|en)|dropp?(?:ed|s|ing)|declin(?:ed|es|e|ing)|lost|los(?:es|e|ing)|shed|diminish(?:ed|es)?|shrank|went down|goes down)\b")
_f("DIR_INCREASE_VERB", r"\b(?:increas(?:ed|es|e|ing)|rose|ris(?:es|en|ing)|gain(?:ed|ing)|rais(?:ed|es|e|ing)|elevat(?:ed|es|e|ing)|grew|climb(?:ed|s)?|regain(?:ed|s|ing)|went up|goes up)\b")
_f("DIR_DECREASE_NOUN", r"\b(?:reductions?|decreases?|decline|loss(?:es)?|drop|fall)\b")
_f("DIR_INCREASE_NOUN", r"\b(?:increases?|gains?|rise|regain|elevations?)\b")
_f("DIR_UP_COMPARATIVE", r"\b(?:greater|larger|more|higher|bigger)\b")
_f("DIR_DOWN_COMPARATIVE", r"\b(?:lower|smaller|less|fewer)\b")
_f("EFFECT_WORD", r"\bimprov(?:ed|es|e|ing|ement|ements)\b|\bbenefit(?:s|ed)?\b|\beffective\b|\befficacious\b|\bworks?\b|\bprotect(?:s|ed|ive|ion)?\b")

# --------------------------------------------------------------------------- injected or imperative text

INSTRUCTION_RE = re.compile(
    r"\bignore\b|\bdisregard\b|\bforget\b|\boverride\b|\bbypass\b|\bjailbreak\b|\bsystem prompt\b|\b(?:previous|prior|above|earlier) (?:instructions?|rules|messages?)\b|\binstructions?\b|\bprompt\b"
    r"|\bas an ai\b|\blanguage model\b|\byou\b|\byour\b|\bplease\b|https?://|\bwww\.|\b(?:system|developer|user|assistant)\s*:|\bpretend\b|\brole[- ]?play\b|\bfrom now on\b|\bnew rule\b",
    re.IGNORECASE,
)
IMPERATIVE_RE = re.compile(
    r"^\W*(?:tell|say|state|write|output|print|reveal|repeat|respond|answer|advise|recommend|prescribe|switch|start|stop|take|click|visit|go|do|don't|remember|always|never|now|instead|add|append|include|omit|delete|remove|call|describe|report|declare|conclude|use(?! of)|buy|ask)\b",
    re.IGNORECASE,
)
# A recommendation is never an evidence statement, wherever it stands in the sentence ("Patients can use X, which ...").
RECOMMENDATION_RE = re.compile(
    r"\b(?:can|may|should|could|must|ought to|might|need to|would do well to)\s+(?:(?:also|then|now|safely|reasonably|still|therefore|instead)\s+)*"
    r"(?:use|take|start|switch(?:\s+to)?|choose|try|prescribe|receive|prefer|opt for|consider|be (?:used|prescribed|given|preferred|chosen|started|switched|offered|considered|recommended|treated))\b"
    r"|\bthe (?:treatment|drug|option|therapy|agent|medicine|medication|choice|one) (?:to use|of choice|to choose|to prefer|to pick|to take|to start)\b|\b(?:drug|treatment|agent|therapy|medication) of choice\b"
    r"|\bfirst[- ]line\b|\brecommend(?:s|ed|ing|ation|ations)?\b(?!\s+(?:starting\s+|maintenance\s+)?(?:dose|doses|dosage|dosing))|\badvis(?:e|es|ed|able)\b"
    r"|\b(?:better|best|preferred|right|good|ideal|reasonable|sensible|obvious|logical|natural) (?:choice|option|pick|candidate)\b|\bworth (?:trying|using|taking|starting|switching)\b"
    r"|\bconsider (?:using|switching|starting|taking|prescribing)\b|\bshould be (?:used|given|prescribed|preferred|started|chosen|offered)\b",
    re.IGNORECASE,
)
# Instruction-like text inside an evidence card (registry or sponsor free text): such a card is never used as evidence.
# Narrower than INSTRUCTION_RE, which also matches ordinary label wording such as "Administration Instructions".
CARD_INJECTION_RE = re.compile(
    r"\bignore\b|\bdisregard\b|\bforget\b|\boverride\b|\bbypass\b|\bjailbreak\b|\bsystem prompt\b|\b(?:previous|prior|above|earlier|all|these|new) (?:instructions?|rules|messages?)\b"
    r"|\bas an ai\b|\blanguage model\b|\byou\b|\byour\b|\bplease\b|https?://|\bwww\.|\b(?:system|developer|user|assistant)\s*:|\bpretend\b|\brole[- ]?play\b|\bfrom now on\b|\bnew rule\b"
    r"|\b(?:verifier|citation check|summary|model)\b[^.]{0,40}\b(?:must|should|will|is to)\b",
    re.IGNORECASE,
)
HYPOTHETICAL_FRAME_RE = re.compile(r"\bwould (?:require|need|take|be (?:needed|required|necessary))\b|\b(?:is|are|be) (?:required|needed|necessary) (?:to|for)\b|\brequirements? for\b", re.IGNORECASE)

# A sentence that refers back to the previous sentence's subject ("It also ...", "Its 95% CI ...", "That dose ..."). Singular
# references only: "they" or "these results" usually point inside their own sentence.
_ARM_NOUN = r"(?:dose|drug|agent|arm|group|compound|medicine|medication|regimen)"
ANAPHOR_RE = re.compile(
    rf"^\W*(?:it|its|it's)\b|\bits\b|(?:^|[,;:]\s*)it\b|\b(?:that|this|the same|the latter|the former|the said)\s+{_ARM_NOUN}\b|^\W*the\s+{_ARM_NOUN}\b",
    re.IGNORECASE,
)
ANAPHOR_START_RE = re.compile(rf"^\W*(?:it|its|it's|(?:that|this|the same|the said)\s+{_ARM_NOUN}|the\s+{_ARM_NOUN})\b", re.IGNORECASE)
STUDY_ANAPHOR_RE = re.compile(r"\b(?:that|this|the same|the said)\s+(?:trial|study)\b|^\W*(?:the|this|that)\s+(?:trial|study)\b", re.IGNORECASE)
# "adults with knee osteoarthritis", "patients who had heart failure": a subgroup the stored population must name
SUBGROUP_RE = re.compile(r"\b(?:adults|participants|patients|people|individuals|subjects|persons|women|men|those|children|adolescents|recipients)\s+(?:with|who had|who have|having|suffering from)\s+", re.IGNORECASE)

# The only sentences that may stand without evidence: framing and hedges that assert nothing about a trial, a drug or a record.
# Matched against the whole sentence (citation markers removed). Anything else is a factual sentence and must be supported.
META_SENTENCE_RES = [
    re.compile(r"(?:in )?(?:summary|overview|brief|short|conclusion)|to summari[sz]e|summary of (?:the )?evidence|key findings", re.IGNORECASE),
    re.compile(r"(?:here is|below is|the following is|this is) (?:a |the )?(?:brief |short )?(?:summary|overview)(?: of (?:the |this )?(?:evidence|evidence cards|cards|findings|results|stored evidence))?", re.IGNORECASE),
    re.compile(r"(?:this|the) summary (?:covers|describes|draws on|is based on|uses|summari[sz]es) (?:only )?(?:part|some|a subset|a part) of the (?:available |stored )?(?:evidence|evidence cards|cards)(?: (?:shown|offered|provided|available))?", re.IGNORECASE),
    re.compile(r"(?:this|the) summary (?:covers|describes|uses) only the (?:evidence )?cards (?:shown|offered|provided)", re.IGNORECASE),
    re.compile(r"(?:all )?(?:values|figures|numbers|results) are (?:given |stated |shown )?as reported (?:by|in) the cited (?:sources?|cards?|evidence)", re.IGNORECASE),
    re.compile(r"see the cited (?:cards|evidence|sources)(?: for (?:details|the full record|context))?", re.IGNORECASE),
    re.compile(r"(?:these|the) (?:results|findings|values) should be (?:interpreted|read) (?:with caution|cautiously|in context)", re.IGNORECASE),
]


def is_meta_sentence(text: str) -> bool:
    """True only for a sentence that is wholly one of the framing / hedge templates (or empty)."""
    plain = re.sub(r"\[\s*C\d+(?:\s*[,;]\s*C\d+)*\s*\]", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip().strip(" .:;!-").strip()
    return not plain or any(pattern.fullmatch(plain) for pattern in META_SENTENCE_RES)


#: Words that contradict each other when one is asserted and the cited text says the other ("the same trial" / "different trials").
ANTONYMS = {
    "same": {"different", "differ", "separate", "distinct"}, "single": {"different", "separate", "distinct"}, "identical": {"different", "differ"},
    "different": {"same", "identical", "single"}, "separate": {"same", "single"}, "direct": {"indirect"}, "indirect": {"direct"},
    "randomized": {"nonrandomized", "non-randomized", "observational"}, "blinded": {"open-label", "unblinded"}, "double-blind": {"open-label"}, "open-label": {"double-blind", "blinded"},
    "prespecified": {"post-hoc"}, "pre-specified": {"post-hoc"},
}

# --------------------------------------------------------------------------- neutral words

NEGATORS = frozenset({"no", "not", "never", "none", "neither", "nor", "cannot", "without", "n't", "non"})

NEUTRAL = frozenset(
    """
    a an the this that these those it its their they them there here which who whom whose what when where while whereas although though however
    and or but nor so yet both either each per as at by for from in into of on onto to with within across among amid between through throughout
    during after before until upon over under above below about around approximately roughly nearly almost circa such other another further
    also too then thus hence therefore because since if whether than
    is are was were be been being am has have had having do does did done will would can could may might
    reported report reports reporting observed observe observes recorded record records measured measure measures measurement assessed assess
    evaluated evaluate analysed analyzed analyse analyze estimated estimate estimates calculated seen found noted note notes gave give gives given
    yielded yield yields produced produce produces achieved achieve achieves achieving reached reach reaches attained attain experienced experience
    amounted corresponded corresponding stood came come comes ranged range ranging received receive receiving receives assigned randomized randomised
    randomly treated enrolled enrolling included include includes including involving involved comprised comprising
    according based using used use via respectively versus compared comparison comparing
    group groups arm arms cohort cohorts dose doses dosage dosing level levels value values result results finding findings data datum figure figures
    number numbers percentage percentages proportion proportions share mean means average averages least squares ls lsm adjusted point points
    change changes changed baseline week weeks wk month months year years day days time timepoint timepoints duration period
    end endpoint endpoints outcome outcomes analysis analyses trial trials study studies phase program programme programs
    participant participants patient patients adult adults people individual individuals subject subjects person persons
    treatment treatments treated therapy drug drugs agent agents medication medications compound compounds product
    shown showed show shows showing had called termed considered labelled labeled named regarded deemed described describe describes
    stated state states said say says indicated indicate indicates
    source sources evidence card cards record records statement statements stored
    estimand estimands interval intervals confidence ci limit limits upper lower bound bounds
    hazard ratio ratios odds risk difference differences
    kg mg kilogram kilograms milligram milligrams
    studied tested evaluated investigated examined assessed
    one two first second
    overall total respective corresponding
    made taking took administered administration dosed associated plus alongside together
    approximately roughly about around nearly
    following follow follows followed
    among amongst
    design designed remains remain remained list lists listed occurred occur occurs often
    gives give given had have having yielded
    whose where when
    assigned allocated receiving received
    on off out up down
    its his her our my
    report reports reporting
    led lead leads leading resulted resulting result
    any alone defined definition define corresponds
    investigator investigators author authors researcher researchers
    combined combination combinations coadministered
    indicate indicates indicated indicating
    similarly likewise meanwhile notably specifically namely overall
    whereas while
    case cases context terms respect regard regarding concerning
    conducted conduct still itself themselves composite continued continuing objective objectives recipient recipients brand brands name names same
    """.split()
)

# Tokens that mean the same thing in a card and in a sentence.
SYNONYMS = {
    "cv": "cardiovascular",
    "bodyweight": "weight",
    "t2d": "diabetes",
    "t2dm": "diabetes",
    "mi": "infarction",
    "sc": "subcutaneous",
    "qw": "weekly",
    "qd": "daily",
    "wk": "week",
    "pts": "patients",
    "glp1": "glp-1",
    "randomised": "randomized",
    "analysed": "analyzed",
    "haemoglobin": "hemoglobin",
    "labelling": "labeling",
    "programme": "program",
    "tumour": "tumor",
    "noninferiority": "non-inferiority",
    "noninferior": "non-inferior",
}
