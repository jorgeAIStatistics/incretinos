"""Structured comparison of two versions of a registry record.

Produces a reviewable change list (field, before, after, significance, why it matters)
instead of a raw text diff, so a clinical scientist can triage protocol amendments.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .ctgov import ParsedStudy

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"


@dataclass
class Change:
    field: str
    label: str
    before: Any
    after: Any
    significance: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_SCALAR_FIELDS: list[tuple[str, str, str, str]] = [
    ("overall_status", "Overall recruitment status", HIGH, "Status changes affect when evidence can be expected and whether the trial was stopped."),
    ("has_results", "Results posted", HIGH, "Newly posted results must be extracted and reconciled with earlier disclosures."),
    ("phase", "Phase", HIGH, "A phase change alters the evidentiary weight of the study."),
    ("enrollment", "Enrollment", MEDIUM, "Sample-size changes alter precision and may indicate a protocol amendment."),
    ("enrollment_type", "Enrollment type (anticipated/actual)", LOW, "Actual enrollment replaces the anticipated figure once recruitment closes."),
    ("primary_completion_date", "Primary completion date", MEDIUM, "Shifts the expected timing of primary results."),
    ("completion_date", "Study completion date", LOW, "Shifts the end of follow-up."),
    ("start_date", "Start date", LOW, ""),
    ("allocation", "Allocation", HIGH, "A change in randomization changes the design class."),
    ("masking", "Masking", HIGH, "A change in blinding affects risk of bias."),
    ("intervention_model", "Intervention model", MEDIUM, ""),
    ("lead_sponsor", "Lead sponsor", MEDIUM, ""),
    ("results_first_posted", "Results first posted date", LOW, ""),
    ("last_update_posted", "Last update posted", LOW, "Registry revision marker."),
    ("min_age", "Minimum age", MEDIUM, "Eligibility change."),
    ("max_age", "Maximum age", MEDIUM, "Eligibility change."),
    ("sex", "Eligible sex", MEDIUM, "Eligibility change."),
    ("location_count", "Number of listed sites", LOW, ""),
]


def _outcome_key(item: dict) -> tuple[str, str]:
    return (item.get("measure", "").strip(), item.get("timeFrame", "").strip())


def _list_changes(field: str, label: str, before: list[str], after: list[str], significance: str, note: str) -> list[Change]:
    removed = [x for x in before if x not in after]
    added = [x for x in after if x not in before]
    changes: list[Change] = []
    if added:
        changes.append(Change(f"{field}.added", f"{label} added", None, added, significance, note))
    if removed:
        changes.append(Change(f"{field}.removed", f"{label} removed", removed, None, significance, note))
    return changes


def diff_studies(old: ParsedStudy, new: ParsedStudy) -> list[Change]:
    changes: list[Change] = []
    for field, label, significance, note in _SCALAR_FIELDS:
        before, after = getattr(old, field), getattr(new, field)
        if before != after:
            changes.append(Change(field, label, before, after, significance, note))

    for attr, label, significance in (
        ("primary_outcomes", "Primary outcome", HIGH),
        ("secondary_outcomes", "Secondary outcome", MEDIUM),
        ("other_outcomes", "Other pre-specified outcome", LOW),
    ):
        before = [" | ".join(_outcome_key(o)) for o in getattr(old, attr)]
        after = [" | ".join(_outcome_key(o)) for o in getattr(new, attr)]
        changes += _list_changes(
            attr, label, before, after, significance,
            "Outcome changes after study start can indicate outcome switching; compare against the dated registry history.",
        )

    changes += _list_changes(
        "arms", "Arm", [a.get("label", "") for a in old.arms], [a.get("label", "") for a in new.arms], HIGH,
        "Arm changes alter the randomized comparisons available.",
    )
    changes += _list_changes(
        "inclusion", "Inclusion criterion", old.inclusion, new.inclusion, MEDIUM, "Changes the studied population and generalizability."
    )
    changes += _list_changes(
        "exclusion", "Exclusion criterion", old.exclusion, new.exclusion, MEDIUM, "Changes the studied population and generalizability."
    )
    changes += _list_changes("conditions", "Condition", old.conditions, new.conditions, LOW, "")
    changes += _list_changes("countries", "Country", old.countries, new.countries, LOW, "")

    old_pmids = sorted({r.get("pmid") for r in old.references if r.get("pmid")})
    new_pmids = sorted({r.get("pmid") for r in new.references if r.get("pmid")})
    changes += _list_changes(
        "references", "Linked publication (PMID)", old_pmids, new_pmids, MEDIUM, "A newly linked publication may contain results to extract."
    )

    old_oms = [m.get("title", "") for m in old.raw.get("resultsSection", {}).get("outcomeMeasuresModule", {}).get("outcomeMeasures", [])]
    new_oms = [m.get("title", "") for m in new.raw.get("resultsSection", {}).get("outcomeMeasuresModule", {}).get("outcomeMeasures", [])]
    changes += _list_changes(
        "results.outcome_measures", "Posted outcome measure", old_oms, new_oms, HIGH, "Posted results changed; re-extract and re-verify dependent findings."
    )
    return changes


def overall_significance(changes: list[Change]) -> str:
    levels = {c.significance for c in changes}
    # A bare "last update posted" bump with no substantive field change is LOW.
    return HIGH if HIGH in levels else MEDIUM if MEDIUM in levels else LOW


def summarize_changes(nct_id: str, changes: list[Change]) -> str:
    if not changes:
        return f"{nct_id}: record content changed but no tracked field differs (formatting or untracked fields)."
    substantive = [c for c in changes if c.field != "last_update_posted"]
    head = ", ".join(c.label.lower() for c in substantive[:4]) or "registry revision date"
    more = f" and {len(substantive) - 4} more" if len(substantive) > 4 else ""
    return f"{nct_id}: {len(substantive)} tracked change(s) — {head}{more}."
