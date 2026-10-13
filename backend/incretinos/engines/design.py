"""Protocol design workbench: eligibility comparison, coverage, complexity, and design references.

Everything here is descriptive of registered protocols. Complexity indicators are counts,
not validated burden scores, and nothing forecasts the probability of trial success.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ChangeEvent, Study
from ..vocab import ELIGIBILITY_TAGS
from .findings import study_brief


def _study(session: Session, nct_id: str) -> Study:
    study = session.scalar(select(Study).where(Study.nct_id == nct_id.upper()))
    if study is None:
        raise LookupError(f"Study {nct_id} is not on record")
    return study


def eligibility(session: Session, nct_id: str) -> dict[str, Any]:
    study = _study(session, nct_id)
    return {
        "study": study_brief(study),
        "sex": study.population.sex if study.population else None,
        "min_age": study.population.min_age if study.population else None,
        "max_age": study.population.max_age if study.population else None,
        "healthy_volunteers": study.population.healthy_volunteers if study.population else None,
        "inclusion": [{"ordinal": c.ordinal, "text": c.text, "tags": c.tags} for c in study.inclusion],
        "exclusion": [{"ordinal": c.ordinal, "text": c.text, "tags": c.tags} for c in study.exclusion],
        "source": "ClinicalTrials.gov eligibilityModule (registry summary; the full protocol may contain further criteria).",
    }


def compare_eligibility(session: Session, nct_a: str, nct_b: str) -> dict[str, Any]:
    a, b = eligibility(session, nct_a), eligibility(session, nct_b)

    def by_tag(block: dict[str, Any], kind: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for criterion in block[kind]:
            for tag in criterion["tags"] or ["UNTAGGED"]:
                out.setdefault(tag, []).append(criterion["text"])
        return out

    rows = []
    for tag in [*ELIGIBILITY_TAGS.keys(), "UNTAGGED"]:
        row = {
            "tag": tag,
            "a_inclusion": by_tag(a, "inclusion").get(tag, []),
            "b_inclusion": by_tag(b, "inclusion").get(tag, []),
            "a_exclusion": by_tag(a, "exclusion").get(tag, []),
            "b_exclusion": by_tag(b, "exclusion").get(tag, []),
        }
        if any(row[k] for k in ("a_inclusion", "b_inclusion", "a_exclusion", "b_exclusion")):
            a_has = bool(row["a_inclusion"] or row["a_exclusion"])
            b_has = bool(row["b_inclusion"] or row["b_exclusion"])
            row["status"] = "BOTH" if a_has and b_has else "ONLY_A" if a_has else "ONLY_B"
            rows.append(row)
    return {
        "a": {k: a[k] for k in ("study", "sex", "min_age", "max_age")},
        "b": {k: b[k] for k in ("study", "sex", "min_age", "max_age")},
        "by_topic": rows,
        "counts": {"a_inclusion": len(a["inclusion"]), "a_exclusion": len(a["exclusion"]), "b_inclusion": len(b["inclusion"]), "b_exclusion": len(b["exclusion"])},
        "note": "Criteria are grouped by keyword topic for side-by-side reading. Topic tags are assigned by simple text rules and do not replace reading the criteria.",
    }


def _bmi_threshold(study: Study) -> str | None:
    for criterion in study.inclusion:
        match = re.search(r"(?:bmi|body mass index)[^.;]{0,60}?(≥|>=|greater than or equal to|>)\s*(\d{2}(?:\.\d)?)", criterion.text, re.IGNORECASE)
        if match:
            return f"≥{match.group(2)}"
    return None


def complexity(study: Study) -> dict[str, Any]:
    protocol_endpoints = [e for e in study.endpoints if e.kind == "PROTOCOL"]
    weeks = [e.timepoint_weeks for e in protocol_endpoints if e.timepoint_weeks]
    return {
        "arms": len([a for a in study.arms if a.arm_type not in ("RESULTS_GROUP", "CURATED")]) or None,
        "primary_endpoints": len([e for e in protocol_endpoints if e.role == "PRIMARY"]),
        "secondary_endpoints": len([e for e in protocol_endpoints if e.role == "SECONDARY"]),
        "other_endpoints": len([e for e in protocol_endpoints if e.role == "OTHER_PRESPECIFIED"]),
        "inclusion_criteria": len(study.inclusion),
        "exclusion_criteria": len(study.exclusion),
        "listed_sites": study.location_count,
        "countries": len(study.countries or []),
        "longest_registered_assessment_weeks": max(weeks) if weeks else None,
        "enrollment": study.enrollment,
        "enrollment_type": study.enrollment_type,
        "note": "Counts from the registry record — descriptive indicators, not a validated complexity or burden score.",
    }


def coverage_matrix(session: Session, drug_slug: str | None = None) -> dict[str, Any]:
    """Which populations and question types each registered study covers."""
    rows = []
    for study in session.scalars(select(Study).order_by(Study.program, Study.display_name)):
        slugs = [link.drug.slug for link in study.drug_links if link.role != "COMPARATOR"]
        if drug_slug and drug_slug not in slugs:
            continue
        feats = study.design_features or {}
        conditions = " ".join(study.conditions or []).lower()
        cats = {e.category for e in study.endpoints}
        population = study.population
        rows.append(
            {
                **study_brief(study),
                "min_age": population.min_age if population else None,
                "max_age": population.max_age if population else None,
                "bmi_inclusion": _bmi_threshold(study),
                "covers": {
                    "no_t2d": bool(population and population.diabetes_status in ("NO_T2D", "MIXED")),
                    "t2d": bool(population and population.diabetes_status in ("T2D", "MIXED")),
                    "pediatric": bool(population and population.min_age and not population.min_age.startswith(("18", "19", "2", "3", "4", "5", "6", "7"))),
                    "obstructive_sleep_apnea": "sleep apnea" in conditions,
                    "osteoarthritis": "osteoarthritis" in conditions,
                    "cardiovascular_disease": bool(re.search(r"cardiovascular|atherosclerotic", conditions)),
                    "kidney_disease": bool(re.search(r"kidney|renal", conditions)),
                    "active_comparator": feats.get("comparator_type") in ("ACTIVE", "PLACEBO_AND_ACTIVE"),
                    "outcomes_trial": bool(feats.get("outcomes_trial")),
                    "withdrawal_or_maintenance": bool(feats.get("randomized_withdrawal") or feats.get("maintenance") or feats.get("maintenance_after_incretin")),
                    "body_composition_endpoint": "BODY_COMPOSITION" in cats,
                    "physical_function_endpoint": "PHYSICAL_FUNCTION" in cats,
                    "mace_endpoint": "MACE" in cats,
                },
                "complexity": complexity(study),
            }
        )
    return {
        "rows": rows,
        "dimensions": [
            ("no_t2d", "Without T2D"), ("t2d", "With T2D"), ("pediatric", "Paediatric"), ("obstructive_sleep_apnea", "OSA"), ("osteoarthritis", "Osteoarthritis"),
            ("cardiovascular_disease", "CV disease"), ("kidney_disease", "Kidney disease"), ("active_comparator", "Active comparator"), ("outcomes_trial", "Outcomes trial"),
            ("withdrawal_or_maintenance", "Withdrawal / maintenance"), ("body_composition_endpoint", "Body composition"), ("physical_function_endpoint", "Physical function"), ("mace_endpoint", "MACE endpoint"),
        ],
        "note": "Derived from registry conditions, eligibility text and registered outcome titles. A blank cell means the registry record does not indicate coverage — not that the question was not studied.",
    }


def endpoint_review(session: Session, nct_id: str) -> dict[str, Any]:
    study = _study(session, nct_id)
    rows = [
        {"title": e.title, "role": e.role, "category": e.category, "timeframe": e.timeframe_text, "timepoint_weeks": e.timepoint_weeks, "has_posted_results": any(r.endpoint.title == e.title for r in study.results)}
        for e in study.endpoints
        if e.kind == "PROTOCOL"
    ]
    return {"study": study_brief(study), "endpoints": rows, "complexity": complexity(study)}


def amendment_history(session: Session, nct_id: str) -> dict[str, Any]:
    study = _study(session, nct_id)
    events = session.scalars(select(ChangeEvent).where(ChangeEvent.source_id == study.source_id).order_by(ChangeEvent.detected_at.desc())).all()
    return {
        "study": study_brief(study),
        "versions": [
            {"version_label": v.version_label, "overall_status": v.overall_status, "has_results": v.has_results, "retrieved_at": v.retrieved_at.isoformat(), "content_hash": v.content_hash, "is_synthetic": v.is_synthetic}
            for v in sorted(study.versions, key=lambda v: v.id)
        ],
        "changes": [
            {"id": e.id, "detected_at": e.detected_at.isoformat(), "summary": e.summary, "significance": e.significance, "review_status": e.review_status, "is_synthetic": e.is_synthetic, "changes": e.changes}
            for e in events
        ],
        "note": "Versions are the snapshots this platform has stored. The registry's own dated history is the authoritative amendment record.",
    }
