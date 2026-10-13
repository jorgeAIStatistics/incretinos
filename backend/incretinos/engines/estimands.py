"""Estimand recognition (ICH E9(R1)).

Registry records rarely name the estimand, but the analysis-population text usually
reveals how the main intercurrent event — stopping study treatment — was handled.
This module classifies that text with explicit patterns and always reports *why*
(the matched phrase) and *on what basis* (stated by the source vs inferred here).
An inferred label is an inference, and is displayed as one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..enums import EstimandBasis, EstimandLabel, IceEvent, IceStrategy

# Hypothetical strategy for treatment discontinuation -> "efficacy estimand" in Lilly programmes.
_HYPOTHETICAL = [
    r"excluding data after (?:prematurely )?(?:stopping|discontinu\w+)(?: of)?(?: the)? study (?:drug|treatment)",
    r"excluding data after (?:the )?(?:permanent )?discontinuation",
    r"up to the earliest date of discontinuation of study (?:drug|treatment)",
    r"(?:prior to|before|until) (?:the )?(?:permanent |premature )?discontinuation of (?:the )?study (?:drug|treatment)",
    r"\bon[- ]treatment\b",
    r"efficacy estimand",
    r"had (?:all )?(?:participants|patients) remained on",
    r"excluding data after (?:initiat\w+|start\w+) (?:of )?rescue",
]

# Treatment-policy strategy -> "treatment-regimen estimand".
_TREATMENT_POLICY = [
    r"treatment[- ]regimen estimand",
    r"regardless of (?:treatment |study drug )?(?:adherence|discontinuation)",
    r"irrespective of (?:treatment |study drug )?(?:adherence|discontinuation)",
    r"including (?:all )?data (?:collected |obtained )?(?:after|following) (?:treatment |study drug )?discontinuation",
    r"regardless of (?:the )?(?:use|initiation) of (?:rescue|prohibited)",
    r"treatment[- ]policy (?:strategy|estimand)",
]

_RESCUE_HYPOTHETICAL = [
    r"initiation of prohibited weight management treatments",
    r"excluding data after (?:initiat\w+|start\w+) (?:of )?(?:rescue|prohibited)",
    r"(?:prior to|before) (?:initiat\w+ )?rescue",
]


@dataclass(frozen=True)
class IceFinding:
    event: IceEvent
    strategy: IceStrategy
    quote: str


@dataclass(frozen=True)
class EstimandClassification:
    label: EstimandLabel
    basis: EstimandBasis
    quote: str | None = None
    strategies: tuple[IceFinding, ...] = field(default_factory=tuple)
    rationale: str = ""


def _first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            start = max(0, match.start() - 60)
            end = min(len(text), match.end() + 60)
            return text[start:end].strip()
    return None


def classify_estimand_text(*texts: str | None, stated: bool = False) -> EstimandClassification:
    """Classify the estimand implied by analysis-population / method text.

    ``stated=True`` is for sources that name the estimand explicitly (publications,
    releases); otherwise the result is labelled as inferred from the analysis description.
    """
    text = " ".join(t for t in texts if t)
    if not text.strip():
        return EstimandClassification(EstimandLabel.NOT_STATED, EstimandBasis.NOT_DETERMINABLE, rationale="No analysis description available.")

    hyp = _first_match(_HYPOTHETICAL, text)
    pol = _first_match(_TREATMENT_POLICY, text)
    explicit_eff = re.search(r"efficacy estimand", text, re.IGNORECASE)
    explicit_reg = re.search(r"treatment[- ]regimen estimand", text, re.IGNORECASE)
    basis = EstimandBasis.STATED_BY_SOURCE if (stated or explicit_eff or explicit_reg) else EstimandBasis.INFERRED_FROM_ANALYSIS_DESCRIPTION

    strategies: list[IceFinding] = []
    rescue = _first_match(_RESCUE_HYPOTHETICAL, text)
    if rescue:
        strategies.append(IceFinding(IceEvent.PROHIBITED_OR_RESCUE_THERAPY, IceStrategy.HYPOTHETICAL, rescue))

    if hyp and pol and not (explicit_eff or explicit_reg):
        return EstimandClassification(
            EstimandLabel.NOT_STATED,
            EstimandBasis.NOT_DETERMINABLE,
            quote=hyp,
            rationale="Text contains language consistent with both hypothetical and treatment-policy handling; not classified.",
        )
    if explicit_eff and not explicit_reg or (hyp and not pol):
        strategies.insert(0, IceFinding(IceEvent.TREATMENT_DISCONTINUATION, IceStrategy.HYPOTHETICAL, hyp or ""))
        return EstimandClassification(
            EstimandLabel.EFFICACY,
            basis,
            quote=hyp,
            strategies=tuple(strategies),
            rationale=(
                "Data after discontinuation of study treatment are excluded, i.e. a hypothetical strategy for "
                "treatment discontinuation — the construction Lilly publications call the efficacy estimand."
            ),
        )
    if explicit_reg and not explicit_eff or (pol and not hyp):
        strategies.insert(0, IceFinding(IceEvent.TREATMENT_DISCONTINUATION, IceStrategy.TREATMENT_POLICY, pol or ""))
        return EstimandClassification(
            EstimandLabel.TREATMENT_REGIMEN,
            basis,
            quote=pol,
            strategies=tuple(strategies),
            rationale=(
                "Outcomes are analysed regardless of treatment discontinuation, i.e. a treatment-policy strategy — "
                "the construction Lilly publications call the treatment-regimen estimand."
            ),
        )
    return EstimandClassification(
        EstimandLabel.NOT_STATED,
        EstimandBasis.NOT_DETERMINABLE,
        rationale="The analysis description does not say how data after treatment discontinuation were handled.",
    )


ESTIMAND_EXPLAINER: dict[str, str] = {
    EstimandLabel.EFFICACY.value: (
        "Efficacy estimand — the treatment effect had all participants remained on their randomized treatment "
        "(hypothetical strategy for treatment discontinuation and, typically, prohibited/rescue therapy). "
        "It depends on a hypothetical continuation of treatment and is usually larger in magnitude."
    ),
    EstimandLabel.TREATMENT_REGIMEN.value: (
        "Treatment-regimen estimand — the treatment effect regardless of treatment discontinuation "
        "(treatment-policy strategy). It reflects the randomized population as treated in practice within the trial."
    ),
    EstimandLabel.NOT_STATED.value: (
        "The source does not state the estimand and the analysis description does not allow it to be inferred."
    ),
    EstimandLabel.OTHER.value: "A different estimand construction; see the analysis description.",
}
