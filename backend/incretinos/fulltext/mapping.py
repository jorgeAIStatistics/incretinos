"""Which registered study arm a table column (or a named group in a sentence) belongs to.

The column heading is read as printed, after expanding the abbreviations the table itself defines ("OFG" from
"Abbreviations: ... OFG=orforglipron"). Its drug, placebo status and single dose are read with the same helpers
that characterise registry arms (``vocab``), and compared with the registered arms of the study (pooled and
results-only groups are not candidates):

* UNIQUE              exactly one registered arm has this drug and this dose (or is the placebo arm)
* VIA_EQUIVALENCE     no arm has the printed dose, but exactly one arm's dose is documented as exposure-equivalent
                      to it (drugs.yaml equivalences); the equivalence is named in the basis
* UNIQUE_BY_DRUG_ONLY no dose is printed (or none can be read) and exactly one registered arm has the drug
* SUBSET_OF_ARM       one of the above, but the document reports a subgroup or a cohort of that arm
* AMBIGUOUS           several arms fit; the row is kept without an arm link and the candidates are listed
* NONE                no registered arm fits; the row is kept without an arm link

The N printed in the heading is compared with the arm's registered number randomized and the comparison is
reported; it never changes the match. Nothing here guesses: when the reading is not unique the status says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..vocab import detect_drugs, parse_single_dose
from .tables import clean, read_header_n

UNIQUE = "UNIQUE"
VIA_EQUIVALENCE = "VIA_EQUIVALENCE"
UNIQUE_BY_DRUG_ONLY = "UNIQUE_BY_DRUG_ONLY"
SUBSET_OF_ARM = "SUBSET_OF_ARM"
AMBIGUOUS = "AMBIGUOUS"
NONE = "NONE"
LINKED = frozenset({UNIQUE, VIA_EQUIVALENCE, UNIQUE_BY_DRUG_ONLY, SUBSET_OF_ARM})


@dataclass(frozen=True)
class ArmCandidate:
    id: int
    label: str
    drug_slug: str | None
    dose_amount: float | None
    dose_unit: str | None
    formulation_key: str | None
    is_placebo: bool
    n_randomized: int | None


@dataclass
class ArmMatch:
    header: str
    expanded: str
    status: str
    arm_id: int | None
    arm_label: str | None
    basis: str
    candidates: list[str] = field(default_factory=list)
    drug: str | None = None
    dose: float | None = None
    n_header: int | None = None
    n_registered: int | None = None
    n_agrees: bool | None = None
    expansions: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "header": self.header, "read_as": self.expanded, "status": self.status, "arm_label": self.arm_label, "basis": self.basis,
            "candidates": self.candidates, "drug": self.drug, "dose": self.dose, "n_in_header": self.n_header, "n_randomized_registered": self.n_registered,
            "n_agrees": self.n_agrees, "abbreviations_expanded": self.expansions,
        }


def expand(header: str, abbreviations: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Replace whole-word abbreviations the table defines by their definitions ("OFG 6 mg" -> "orforglipron 6 mg")."""
    used: dict[str, str] = {}
    text = header
    for short, long in abbreviations.items():
        if len(short) < 2 or short in ("N", "n"):
            continue
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(short)}(?![A-Za-z0-9])")
        if pattern.search(text):
            text = pattern.sub(long, text)
            used[short] = long
    return text, used


def match_arm(
    header: str,
    arms: list[ArmCandidate],
    *,
    abbreviations: dict[str, str] | None = None,
    equivalent_doses: dict[tuple[str | None, float], set[float]] | None = None,
    subset_label: str | None = None,
) -> ArmMatch:
    """Map one printed heading to a registered arm (see the module docstring for the statuses)."""
    header = clean(header)
    expanded, used = expand(header, abbreviations or {})
    drugs = [d for d in detect_drugs(expanded) if d != "placebo"]
    placebo = bool(re.search(r"\bplacebo\b|\bPBO\b", expanded, re.IGNORECASE)) and not drugs
    dose = parse_single_dose(expanded)
    n_header = read_header_n(header)
    out = ArmMatch(header=header, expanded=expanded, status=NONE, arm_id=None, arm_label=None, basis="", drug=drugs[0] if len(drugs) == 1 else None,
                   dose=dose[0] if dose else None, n_header=n_header, expansions=used)
    if placebo:
        pool = [a for a in arms if a.is_placebo]
        how = "the heading names placebo"
    elif len(drugs) == 1:
        pool = [a for a in arms if a.drug_slug == drugs[0] and not a.is_placebo]
        how = f"the heading names {drugs[0]}"
    else:
        out.basis = "the heading names no drug and no placebo" if not drugs else f"the heading names several drugs ({', '.join(drugs)})"
        out.candidates = [a.label for a in arms]
        return out
    if used:
        how += " (" + ", ".join(f"{k}={v}" for k, v in used.items()) + ", as defined in the table's abbreviations)"
    chosen: list[ArmCandidate] = []
    status = NONE
    if not pool:
        out.basis = f"{how}, but no registered arm of the study has it"
        out.candidates = [a.label for a in arms]
        return out
    if placebo:
        chosen, status, basis = pool, UNIQUE, how
    elif dose is not None:
        exact = [a for a in pool if a.dose_amount is not None and abs(a.dose_amount - dose[0]) < 1e-9 and (a.dose_unit or "mg") == dose[1]]
        if exact:
            chosen, status, basis = exact, UNIQUE, f"{how} and the dose {dose[0]:g} {dose[1]}, which the registered arm has"
        else:
            via: list[tuple[ArmCandidate, float]] = []
            for a in pool:
                for (formulation, amount), others in (equivalent_doses or {}).items():
                    if a.formulation_key == formulation and a.dose_amount is not None and abs(amount - a.dose_amount) < 1e-9 and any(abs(o - dose[0]) < 1e-9 for o in others):
                        via.append((a, amount))
            chosen = [a for a, _ in via]
            status = VIA_EQUIVALENCE
            basis = (f"{how}; no registered arm has {dose[0]:g} {dose[1]}, but a documented exposure equivalence relates it to "
                     + ", ".join(f"{a.label} ({amount:g} mg {a.formulation_key})" for a, amount in via)) if via else f"{how}; no registered arm has the dose {dose[0]:g} {dose[1]}"
    else:
        chosen, status, basis = pool, UNIQUE_BY_DRUG_ONLY, f"{how}; the heading prints no single dose, so the match rests on the drug alone"
    if len(chosen) == 1:
        arm = chosen[0]
        out.arm_id, out.arm_label = arm.id, arm.label
        out.n_registered = arm.n_randomized
        if n_header is not None and arm.n_randomized is not None:
            out.n_agrees = n_header == arm.n_randomized
            basis += f"; N={n_header} in the heading {'equals' if out.n_agrees else 'differs from'} the registered number randomized ({arm.n_randomized})"
        if subset_label:
            status = SUBSET_OF_ARM
            basis += f"; the column reports {subset_label}, a subset of this arm"
        out.status, out.basis = status, basis
    elif len(chosen) > 1:
        out.status, out.basis = AMBIGUOUS, f"{basis}; {len(chosen)} registered arms fit"
        out.candidates = [a.label for a in chosen]
    else:
        out.status, out.basis = NONE, basis
        out.candidates = [a.label for a in pool]
    return out
