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
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import AE_KIND_LABEL, DISCONTINUATION_KINDS
from ..models import AdverseEvent, Drug, LabelRecord, SpontaneousReportSeries, Study, StudyDrug
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


def trial_ae_table(session: Session, drug_slug: str | None = None, nct_id: str | None = None, measure_kind: str | None = None, term: str | None = None, limit: int = 2000) -> dict[str, Any]:
    """Structured trial adverse-event rows of ONE measure kind (the guard refuses mixtures)."""
    query = select(AdverseEvent).join(Study, AdverseEvent.study_id == Study.id)
    if nct_id:
        query = query.where(Study.nct_id == nct_id.upper())
    if drug_slug:
        query = query.join(StudyDrug, StudyDrug.study_id == Study.id).join(Drug, Drug.id == StudyDrug.drug_id).where(Drug.slug == drug_slug)
    if measure_kind:
        query = query.where(AdverseEvent.measure_kind == measure_kind)
    if term:
        query = query.where(AdverseEvent.term.ilike(f"%{term}%"))
    rows = list(session.scalars(query.order_by(Study.display_name, AdverseEvent.term, AdverseEvent.arm_label).limit(limit)))
    kinds = {r.measure_kind for r in rows}
    return {
        "rows": [ae_dto(r) for r in rows],
        "measure_kinds_present": sorted(kinds),
        "measure_kind_labels": {k: AE_KIND_LABEL.get(k, k) for k in sorted(kinds)},
        "single_kind": len(kinds) <= 1,
        "note": "Rows of different measure kinds are listed together here for inspection only; charts and contrasts require a single measure kind."
        if len(kinds) > 1
        else None,
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
