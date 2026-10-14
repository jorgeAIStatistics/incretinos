"""Safety intelligence.

Two data families are kept apart by construction:

* Trial adverse events — participants with an event out of participants at risk in a
  randomized arm. Incidence proportions and between-arm contrasts are valid within a trial.
* Spontaneous reports (FAERS) — counts of voluntary reports. There is no denominator of
  exposed patients, so they support neither incidence nor causality. The only quantitative
  summary offered is disproportionality, which compares reporting patterns within the
  database and is a signal-screening statistic.

No function in this module (or anywhere else) turns report counts into a rate, and mixing
the two families on one scale raises an error.
"""

from __future__ import annotations

import math
from typing import Any

from scipy import stats as sps
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..enums import AE_KIND_LABEL, DISCONTINUATION_KINDS
from ..models import AdverseEvent, Drug, LabelRecord, SpontaneousReportSeries, Study, StudyArm, StudyDrug
from ..vocab import arm_drug_slug
from .findings import ae_dto, source_ref
from .stats import StatsInputError


class InvalidInferenceError(ValueError):
    """Raised when a calculation would misuse the data (e.g. incidence from spontaneous reports)."""


SPONTANEOUS_REPORT_CAVEATS = [
    "FAERS contains voluntary reports. The number of people exposed to the drug is unknown, so report counts cannot be converted into incidence or risk.",
    "A report does not establish that the drug caused the event; reports reflect suspicion, coincidence, underlying disease and co-medication.",
    "Reporting is incomplete and uneven: it rises with publicity, litigation, time on market and prescription volume, and duplicate reports exist.",
    "The openFDA query matches the drug in any role (suspect or concomitant) and counts a report once per reaction term it lists.",
    "Counts must not be compared with, added to, or plotted on the same scale as adverse-event percentages from clinical trials.",
]

DISPROPORTIONALITY_CAVEATS = [
    "A proportional reporting ratio or reporting odds ratio compares how often an event is mentioned in reports for this drug with how often it is mentioned for all other drugs. It measures reporting disproportion, not risk.",
    "The comparator is every other drug in FAERS, whose users differ systematically from users of this drug (indication, age, comorbidity).",
    "A disproportionality signal is a prompt for clinical review and denominator-based study; it is neither an incidence nor evidence of causation.",
]


def incidence_from_spontaneous_reports(*_: Any, **__: Any) -> None:
    """There is deliberately no implementation. Spontaneous reports have no exposure denominator."""
    raise InvalidInferenceError(
        "Incidence cannot be calculated from spontaneous reports: the number of exposed patients is unknown and reporting is incomplete. "
        "Use adverse-event data from a trial arm (events / participants at risk) for incidence."
    )


def causality_from_spontaneous_reports(*_: Any, **__: Any) -> None:
    raise InvalidInferenceError("Spontaneous reports and disproportionality statistics cannot establish that a drug causes an event.")


def assert_single_data_family(families: set[str]) -> None:
    """Guard used by charting/aggregation code paths."""
    if {"TRIAL", "SPONTANEOUS"} <= families:
        raise InvalidInferenceError(
            "Trial adverse-event rates and spontaneous-report counts do not share a sampling denominator and cannot be combined or shown on one scale."
        )


def assert_single_measure_kind(kinds: set[str]) -> None:
    """Discontinuation due to adverse events is not the incidence of an adverse event."""
    has_discontinuation = bool(kinds & DISCONTINUATION_KINDS)
    has_incidence = "PARTICIPANTS_WITH_EVENT" in kinds
    if has_discontinuation and has_incidence:
        raise InvalidInferenceError(
            "Discontinuation due to adverse events counts participants who stopped treatment; adverse-event incidence counts participants who experienced an event. "
            "They are different quantities and cannot share an axis or be summed."
        )
    if len(kinds & DISCONTINUATION_KINDS) > 1:
        raise InvalidInferenceError(
            "Treatment discontinuation due to adverse events (publications) and study non-completion due to adverse events (registry participant flow) are different quantities."
        )


def disproportionality(drug_event: int, drug_total: int, event_total: int, all_total: int, level: float = 0.95) -> dict[str, Any]:
    """PRR and ROR from a 2×2 reporting table.

    a = reports with drug and event; b = drug without event;
    c = event without drug;          d = neither.
    """
    for name, value in (("drug_event", drug_event), ("drug_total", drug_total), ("event_total", event_total), ("all_total", all_total)):
        if value is None or isinstance(value, bool) or not isinstance(value, int | float):
            raise StatsInputError(f"{name} must be a non-negative integer (a missing count is not zero)")
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise StatsInputError(f"{name} must be a finite non-negative integer")
        if int(value) != value or value < 0:
            raise StatsInputError(f"{name} must be a non-negative integer (a missing count is not zero)")
    level = float(level)
    if not 0 < level < 1:
        raise StatsInputError("confidence level must be strictly between 0 and 1")
    a = int(drug_event)
    b = int(drug_total) - a
    c = int(event_total) - a
    d = int(all_total) - int(drug_total) - c
    if min(b, c, d) < 0:
        raise StatsInputError("inconsistent totals: the 2×2 reporting table has a negative cell")
    result: dict[str, Any] = {
        "table": {"a_drug_and_event": a, "b_drug_without_event": b, "c_event_without_drug": c, "d_neither": d},
        "ci_level": level,
        "is_incidence": False,
        "supports_causality": False,
        "kind": "INFERENCE",
        "caveats": [*DISPROPORTIONALITY_CAVEATS, *SPONTANEOUS_REPORT_CAVEATS[:3]],
    }
    if a < 3 or b == 0 or c == 0 or d == 0:
        return {**result, "prr": None, "ror": None, "not_estimable": "Fewer than 3 reports for the drug–event pair, or an empty cell: disproportionality is not calculated."}
    z = float(sps.norm.ppf(1 - (1 - level) / 2))
    prr = (a / (a + b)) / (c / (c + d))
    se_prr = math.sqrt(1 / a - 1 / (a + b) + 1 / c - 1 / (c + d))
    ror = (a * d) / (b * c)
    se_ror = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return {
        **result,
        "prr": prr,
        "prr_ci": [math.exp(math.log(prr) - z * se_prr), math.exp(math.log(prr) + z * se_prr)],
        "ror": ror,
        "ror_ci": [math.exp(math.log(ror) - z * se_ror), math.exp(math.log(ror) + z * se_ror)],
        "method": "Proportional reporting ratio and reporting odds ratio with log-scale Wald intervals",
    }


def spontaneous_overview(session: Session, drug: Drug) -> dict[str, Any]:
    series = {s.kind: s for s in session.scalars(select(SpontaneousReportSeries).where(SpontaneousReportSeries.drug_id == drug.id))}
    background = session.scalar(select(SpontaneousReportSeries).where(SpontaneousReportSeries.drug_id.is_(None), SpontaneousReportSeries.kind == "BACKGROUND_REACTION"))
    if "TOTAL" not in series:
        return {
            "drug": drug.slug,
            "available": False,
            "explanation": "No FAERS series is stored for this drug (not marketed, or not ingested). Absence of reports here is not evidence of safety.",
            "caveats": SPONTANEOUS_REPORT_CAVEATS,
        }
    total = series["TOTAL"].total
    reactions = series.get("BY_REACTION")
    quarters = series.get("BY_QUARTER")
    by_term = {row["term"]: row["count"] for row in (reactions.data if reactions else [])}
    screening = []
    if background is not None and total:
        for row in background.data:
            a = by_term.get(row["term"])
            if a is None:
                screening.append({"term": row["term"], "drug_reports": None, "note": "Not among the 500 most-reported terms retrieved for this drug; count unavailable (not zero)."})
                continue
            try:
                stats = disproportionality(a, total, row["count"], background.total)
            except StatsInputError as exc:
                screening.append({"term": row["term"], "drug_reports": a, "note": str(exc)})
                continue
            screening.append({"term": row["term"], "drug_reports": a, "all_reports_with_term": row["count"], "prr": stats.get("prr"), "prr_ci": stats.get("prr_ci"), "ror": stats.get("ror"), "ror_ci": stats.get("ror_ci"), "not_estimable": stats.get("not_estimable")})
    return {
        "drug": drug.slug,
        "available": True,
        "unit": "reports (not patients, not incidence)",
        "total_reports": total,
        "database_last_updated": series["TOTAL"].database_last_updated,
        "query": series["TOTAL"].query,
        "top_reactions": (reactions.data[:40] if reactions else []),
        "reports_by_quarter": (quarters.data if quarters else []),
        "all_database_reports": background.total if background else None,
        "disproportionality_screen": screening,
        "caveats": SPONTANEOUS_REPORT_CAVEATS,
        "disproportionality_caveats": DISPROPORTIONALITY_CAVEATS,
        "source": source_ref(series["TOTAL"].source),
        "is_incidence": False,
        "supports_causality": False,
    }


ARM_ROLE_LABEL = {
    "PLACEBO": "Placebo",
    "ACTIVE_COMPARATOR": "Active comparator",
    "STUDY_DRUG": "Study drug",
    "UNCLASSIFIED": "Role not classified",
}


def arm_role(arm: StudyArm | None, drug_roles: dict[str, str]) -> dict[str, Any]:
    """What an arm is in its trial, read from stored data — never from the wording of its label.

    Inputs are the arm's parsed content (``is_placebo``, ``drug_slug``), the arm type the sponsor
    registered, and the role each drug has in this trial (``StudyDrug.role``). The registered type
    "Experimental" does not decide the role (in factorial trials every arm is registered that way);
    a registered comparator type does. When the registered type and the content disagree about a
    placebo arm, the role is left unclassified and the reason is given, so a switch or add-on arm
    is never presented as a pure placebo arm.

    The registry adapter stores every arm the sponsor registered as a placebo comparator as a placebo
    arm, including one that switches to an active drug later ("Placebo/30 mg/kg Bimagrumab"). For an
    arm stored as placebo the content is therefore read once more with the vocabulary's own arm
    parser (``vocab.arm_drug_slug``, the parser ingestion uses): if it finds an active drug among the
    components of the label, the registered type and the content disagree and the arm is unclassified.
    """
    fields: dict[str, Any] = {
        "arm_type": arm.arm_type if arm else None,
        "arm_drug_slug": arm.drug_slug if arm else None,
        "arm_is_placebo": bool(arm.is_placebo) if arm else None,
        "arm_dose_amount": arm.dose_amount if arm else None,
        "arm_dose_unit": arm.dose_unit if arm else None,
    }

    def out(role: str, basis: str) -> dict[str, Any]:
        return {"arm_role": role, "arm_role_label": ARM_ROLE_LABEL[role], "arm_role_basis": basis, **fields}

    if arm is None:
        return out("UNCLASSIFIED", "This row is not linked to an arm record.")
    registered = arm.arm_type if arm.arm_type in ("PLACEBO_COMPARATOR", "ACTIVE_COMPARATOR", "EXPERIMENTAL") else None
    if arm.is_placebo:
        named_drug, pure_placebo = arm_drug_slug(getattr(arm, "label", None) or "")
        if named_drug and not pure_placebo:
            return out(
                "UNCLASSIFIED",
                "Stored as a placebo arm (the registry lists it as a placebo comparator), but its label also names an active treatment, "
                "as for a switch or add-on arm. It is not treated as placebo.",
            )
        if registered in ("ACTIVE_COMPARATOR", "EXPERIMENTAL"):
            return out("PLACEBO", f"Stored as a placebo arm; the registry lists the arm type as {registered.replace('_', ' ').lower()}.")
        return out("PLACEBO", "Stored as a placebo arm.")
    if registered == "PLACEBO_COMPARATOR":
        return out(
            "UNCLASSIFIED",
            "Registered as a placebo comparator, but the arm is not stored as a pure placebo arm (its label names an active treatment, "
            "for example a switch or add-on arm). It is not treated as placebo.",
        )
    if registered == "ACTIVE_COMPARATOR":
        return out("ACTIVE_COMPARATOR", "Registered by the sponsor as an active comparator arm.")
    components = [c for c in (arm.drug_slug or "").split("+") if c]
    roles = [drug_roles.get(c) for c in components]
    if components and any(r == "INVESTIGATIONAL" for r in roles):
        return out("STUDY_DRUG", "The arm includes a drug recorded as investigational in this trial.")
    if components and all(r == "COMPARATOR" for r in roles):
        return out("ACTIVE_COMPARATOR", "The arm's only active treatment is recorded as a comparator drug in this trial.")
    return out("UNCLASSIFIED", "The arm's treatment is not linked to a drug with a recorded role in this trial.")


def _ae_filters(query, drug_slug: str | None, nct_id: str | None):
    if nct_id:
        query = query.where(Study.nct_id == nct_id.upper())
    if drug_slug:
        query = query.join(StudyDrug, StudyDrug.study_id == Study.id).join(Drug, Drug.id == StudyDrug.drug_id).where(Drug.slug == drug_slug)
    return query


def ae_coverage(session: Session, drug_slug: str | None = None, nct_id: str | None = None) -> list[dict[str, Any]]:
    """Which trials hold adverse-event rows of which measure kind (counts of rows and of distinct terms).

    Lets a client open on a trial and measure that actually have data. The counts ignore any
    measure-kind, term or row-limit filter of the table request.
    """
    query = _ae_filters(
        select(Study.nct_id, Study.display_name, AdverseEvent.measure_kind, AdverseEvent.scope, func.count(AdverseEvent.id), func.count(func.distinct(AdverseEvent.term)))
        .select_from(AdverseEvent)
        .join(Study, AdverseEvent.study_id == Study.id),
        drug_slug,
        nct_id,
    ).group_by(Study.nct_id, Study.display_name, AdverseEvent.measure_kind, AdverseEvent.scope)
    studies: dict[str, dict[str, Any]] = {}
    for nct, name, kind, scope, n_rows, n_terms in session.execute(query):
        study = studies.setdefault(nct, {"nct_id": nct, "display_name": name, "rows": 0, "by_measure_kind": {}})
        entry = study["by_measure_kind"].setdefault(kind, {"label": AE_KIND_LABEL.get(kind, kind), "rows": 0, "arm_level_rows": 0, "arm_level_terms": 0})
        entry["rows"] += n_rows
        study["rows"] += n_rows
        if scope == "ARM":
            entry["arm_level_rows"] += n_rows
            entry["arm_level_terms"] += n_terms
    return sorted(studies.values(), key=lambda s: s["display_name"])


def trial_ae_table(session: Session, drug_slug: str | None = None, nct_id: str | None = None, measure_kind: str | None = None, term: str | None = None, limit: int = 2000, offset: int = 0) -> dict[str, Any]:
    """Structured trial adverse-event rows of ONE measure kind (the guard refuses mixtures)."""
    query = _ae_filters(select(AdverseEvent).join(Study, AdverseEvent.study_id == Study.id), drug_slug, nct_id)
    if measure_kind:
        query = query.where(AdverseEvent.measure_kind == measure_kind)
    if term:
        query = query.where(AdverseEvent.term.ilike(f"%{term}%"))
    # "total" is every row matching the filters; "rows" is one page of them in a fully determined order.
    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = list(session.scalars(query.order_by(Study.display_name, AdverseEvent.term, AdverseEvent.arm_label, AdverseEvent.id).limit(limit).offset(offset)))
    kinds = {r.measure_kind for r in rows}
    drug_roles: dict[int, dict[str, str]] = {}

    def row(event: AdverseEvent) -> dict[str, Any]:
        if event.study_id not in drug_roles:
            drug_roles[event.study_id] = {link.drug.slug: link.role for link in event.study.drug_links}
        dto = ae_dto(event)
        # A percentage is either the source's own figure or a calculation made here from its counts — never both, and never unlabelled.
        basis = "REPORTED" if dto["percent_reported"] is not None else ("CALCULATED_FROM_COUNTS" if dto["percent_calculated"] is not None else None)
        return {**dto, "percent_basis": basis, **arm_role(event.arm, drug_roles[event.study_id])}

    return {
        "total": total,
        "rows": [row(r) for r in rows],
        "measure_kinds_present": sorted(kinds),
        "measure_kind_labels": {k: AE_KIND_LABEL.get(k, k) for k in sorted(kinds)},
        "single_kind": len(kinds) <= 1,
        "note": "Rows of different measure kinds are listed together here for inspection only; charts and contrasts require a single measure kind."
        if len(kinds) > 1
        else None,
        "arm_role_labels": ARM_ROLE_LABEL,
        "arm_role_rule": "Arm roles come from stored arm data (placebo flag, drug, registered arm type) and the drug roles recorded for the trial; the wording of an arm label is never used.",
        "coverage": ae_coverage(session, drug_slug, nct_id),
    }


def label_safety_sections(session: Session, drug: Drug) -> list[dict[str, Any]]:
    out = []
    for label in session.scalars(select(LabelRecord).where(LabelRecord.drug_id == drug.id)):
        sections = label.sections or {}
        out.append(
            {
                "brand": label.brand,
                "label_version": label.label_version,
                "effective_time": label.effective_time,
                "boxed_warning": sections.get("boxed_warning") or None,
                "recent_major_changes": sections.get("recent_major_changes") or None,
                "contraindications": sections.get("contraindications") or None,
                "warnings_and_cautions": sections.get("warnings_and_cautions") or None,
                "source": source_ref(label.source),
            }
        )
    return out
