"""HTTP API.

Read endpoints are public (the product holds public data only). Endpoints that change
state — review decisions and gap triage — require a reviewer token and are disabled
entirely when no token is configured. Every external input is validated by a typed schema.
"""

from __future__ import annotations

import hmac
import re
from collections import Counter
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import __version__
from ..assistant.answer import ask
from ..assistant.providers import get_provider, provider_status
from ..config import get_settings
from ..db import get_db
from ..engines import benefit_risk, design, stats
from ..engines.comparability import compare_findings, harmonize_dose
from ..engines.estimands import ESTIMAND_EXPLAINER
from ..engines.findings import (
    AE_KIND_LABEL,
    CATEGORY_LABEL,
    ae_dto,
    analysis_dto,
    baseline_class,
    build_finding,
    get_finding,
    source_ref,
    study_brief,
)
from ..engines.gaps import DEFAULT_WEIGHTS, NATURE_LABEL, priority
from ..engines.interpretation import (
    classify_drug,
    describe_adverse_event,
    interpret_analysis,
    interpret_finding,
)
from ..engines.safety import (
    InvalidInferenceError,
    disproportionality,
    label_safety_sections,
    spontaneous_overview,
    trial_ae_table,
)
from ..engines.stats import StatsInputError
from ..engines.validation import run_all
from ..enums import TIER_LABEL, EvidenceTier
from ..ingestion.scheduler import start_scheduler
from ..ingestion.snapshots import audit
from ..models import (
    AdverseEvent,
    AuditEvent,
    ChangeEvent,
    DiscoveredTrial,
    Drug,
    Endpoint,
    EvidenceClaim,
    EvidenceGap,
    EvidenceSource,
    ExposureEquivalence,
    IngestionRun,
    OutcomeResult,
    Publication,
    ReviewDecision,
    ReviewQueueItem,
    StatisticalAnalysis,
    Study,
    StudyDrug,
)
from ..reports.export import comparison_report_markdown, study_report_markdown

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler = start_scheduler(get_settings())
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(
    title="IncretinOS API",
    version=__version__,
    lifespan=lifespan,
    description="Clinical evidence and trial intelligence for incretin and amylin therapeutics. Public data only. Not medical advice.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "X-Reviewer-Token"],
)

Db = Annotated[Session, Depends(get_db)]
NCT = Annotated[str, Field(pattern=r"^[Nn][Cc][Tt]\d{8}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def require_reviewer(x_reviewer_token: Annotated[str | None, Header()] = None) -> str:
    """Least privilege: mutations need a configured token; without one the API is read-only."""
    expected = get_settings().reviewer_token
    if not expected:
        raise HTTPException(status_code=403, detail="This deployment is read-only: no reviewer token is configured.")
    if not x_reviewer_token or not hmac.compare_digest(x_reviewer_token, expected):
        raise HTTPException(status_code=401, detail="A valid reviewer token is required for this action.")
    return "reviewer"


def _study_or_404(db: Session, nct: str) -> Study:
    if not re.fullmatch(r"[Nn][Cc][Tt]\d{8}", nct):
        raise HTTPException(status_code=422, detail="Not a valid NCT identifier")
    study = db.scalar(select(Study).where(Study.nct_id == nct.upper()))
    if study is None:
        raise HTTPException(status_code=404, detail=f"Study {nct.upper()} is not on record")
    return study


def _drug_or_404(db: Session, slug: str) -> Drug:
    drug = db.scalar(select(Drug).where(Drug.slug == slug))
    if drug is None:
        raise HTTPException(status_code=404, detail=f"Drug '{slug}' is not on record")
    return drug


def _compact(f) -> dict[str, Any]:
    """Finding without the long free-text fields, for tables."""
    data = f.as_dict()
    for key in ("analysis_population", "analysis_method", "design_features", "population_summary", "conditions", "diabetes_basis"):
        data.pop(key, None)
    # An interval derived here from a reported standard error is returned separately and flagged as an inference.
    data["derived_interval"] = None
    if f.ci_low is None and f.value is not None and f.dispersion_value is not None and f.dispersion_type and "standard error" in f.dispersion_type.lower():
        data["derived_interval"] = stats.ci_from_se(f.value, f.dispersion_value)
    return data


# --------------------------------------------------------------------------- meta


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/meta", tags=["meta"])
def meta(db: Db) -> dict[str, Any]:
    cfg = get_settings()
    counts = {
        "drugs": db.scalar(select(func.count()).select_from(Drug)),
        "studies": db.scalar(select(func.count()).select_from(Study)),
        "discovered_trials": db.scalar(select(func.count()).select_from(DiscoveredTrial)),
        "results": db.scalar(select(func.count()).select_from(OutcomeResult)),
        "analyses": db.scalar(select(func.count()).select_from(StatisticalAnalysis)),
        "adverse_event_rows": db.scalar(select(func.count()).select_from(AdverseEvent)),
        "publications": db.scalar(select(func.count()).select_from(Publication)),
        "sources": db.scalar(select(func.count()).select_from(EvidenceSource)),
        "gaps": db.scalar(select(func.count()).select_from(EvidenceGap)),
        "open_review_items": db.scalar(select(func.count()).select_from(ReviewQueueItem).where(ReviewQueueItem.status == "OPEN")),
    }
    last_run = db.scalar(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(1))
    verification = Counter(v for (v,) in db.execute(select(OutcomeResult.verification_status)))
    tiers = Counter(t for (t,) in db.execute(select(OutcomeResult.tier)))
    return {
        "version": __version__,
        "mode": "OFFLINE (committed fixtures)" if cfg.offline else "LIVE",
        "last_ingestion": {"mode": last_run.mode, "finished_at": last_run.finished_at.isoformat() if last_run and last_run.finished_at else None} if last_run else None,
        "counts": counts,
        "result_verification": dict(verification),
        "result_tiers": dict(tiers),
        "tier_labels": {t.value: TIER_LABEL[t] for t in EvidenceTier},
        "llm": provider_status(cfg),
        "review_enabled": bool(cfg.reviewer_token),
        "notice": "Public data only. Research use by qualified researchers. Not medical advice, not a treatment recommendation, not a regulatory submission.",
    }


@app.get("/api/validation", tags=["meta"])
def validation(db: Db) -> dict[str, Any]:
    results = [r.as_dict() for r in run_all(db)]
    return {"passed": sum(r["passed"] for r in results), "total": len(results), "rules": results}


# --------------------------------------------------------------------------- drugs


def _drug_summary(db: Session, drug: Drug) -> dict[str, Any]:
    cls = classify_drug([(m.target, m.action) for m in drug.mechanisms])
    n_studies = db.scalar(select(func.count()).select_from(StudyDrug).where(StudyDrug.drug_id == drug.id))
    return {
        "slug": drug.slug,
        "name": drug.name,
        "code_name": drug.code_name,
        "brand_names": drug.brand_names,
        "sponsor": drug.sponsor,
        "modality": drug.modality,
        "route": drug.route,
        "tier_group": drug.tier_group,
        "is_combination": drug.is_combination,
        "component_slugs": drug.component_slugs,
        "regulatory_status": drug.regulatory_status,
        "regulatory_summary": drug.regulatory_summary,
        "regulatory_as_of": drug.regulatory_as_of,
        "regulatory_source": source_ref(drug.regulatory_source),
        "classification": cls,
        "n_studies": n_studies,
        "notes": drug.notes,
    }


@app.get("/api/drugs", tags=["drugs"])
def list_drugs(db: Db) -> list[dict[str, Any]]:
    order = {"CORE": 0, "ADJACENT": 1, "COMPARATOR": 2}
    drugs = sorted(db.scalars(select(Drug)), key=lambda d: (order.get(d.tier_group, 9), d.id))
    return [_drug_summary(db, d) for d in drugs]


@app.get("/api/drugs/{slug}", tags=["drugs"])
def get_drug(slug: str, db: Db) -> dict[str, Any]:
    drug = _drug_or_404(db, slug)
    equivalences = []
    for eq in db.scalars(select(ExposureEquivalence)):
        if eq.dose_a.formulation.drug_id == drug.id:
            equivalences.append(
                {
                    "a": {"formulation": eq.dose_a.formulation.key, "formulation_name": eq.dose_a.formulation.name, "amount": eq.dose_a.amount, "unit": eq.dose_a.unit},
                    "b": {"formulation": eq.dose_b.formulation.key, "formulation_name": eq.dose_b.formulation.name, "amount": eq.dose_b.amount, "unit": eq.dose_b.unit},
                    "basis": eq.basis,
                    "statement": eq.statement,
                    "quote": eq.quote,
                    "verification_status": eq.verification_status,
                    "source": source_ref(eq.source),
                }
            )
    studies = [link.study for link in db.scalars(select(StudyDrug).where(StudyDrug.drug_id == drug.id))]
    claims = db.scalars(select(EvidenceClaim).where(EvidenceClaim.drug_id == drug.id)).all()
    return {
        **_drug_summary(db, drug),
        "mechanisms": [{"target": m.target, "action": m.action, "description": m.description, "quote": m.quote, "verification_status": m.verification_status, "source": source_ref(m.source)} for m in drug.mechanisms],
        "indications": [{"brand": i.brand, "text": i.text, "status": i.status, "jurisdiction": i.jurisdiction, "approval_date": i.approval_date, "quote": i.quote, "verification_status": i.verification_status, "source": source_ref(i.source)} for i in drug.indications],
        "formulations": [{"key": f.key, "name": f.name, "dosage_form": f.dosage_form, "route": f.route, "stage": f.stage, "notes": f.notes, "doses": [{"key": d.key, "amount": d.amount, "unit": d.unit, "frequency": d.frequency, "role": d.role} for d in sorted(f.doses, key=lambda d: d.amount)]} for f in drug.formulations],
        "equivalences": equivalences,
        "equivalence_rule": "Doses of different formulations are related only through the documented pairs listed here. No milligram conversion factor exists or is applied.",
        "studies": [study_brief(s) for s in sorted(studies, key=lambda s: (s.program or "", s.display_name))],
        "claims": [{"topic": c.topic, "statement": c.statement, "quote": c.quote, "tier": c.tier, "verification_status": c.verification_status, "source": source_ref(c.source)} for c in claims],
    }


@app.get("/api/harmonize", tags=["drugs"])
def harmonize(db: Db, formulation: str = Query(max_length=80), amount: float = Query(gt=0, le=10000), target: str = Query(max_length=80)) -> dict[str, Any]:
    return harmonize_dose(db, formulation, amount, target)


# --------------------------------------------------------------------------- studies


@app.get("/api/studies", tags=["studies"])
def list_studies(db: Db, drug: str | None = None, program: str | None = None, has_results: bool | None = None, q: Annotated[str | None, Query(max_length=120)] = None) -> list[dict[str, Any]]:
    out = []
    for study in db.scalars(select(Study).order_by(Study.program, Study.display_name)):
        brief = study_brief(study)
        if drug and drug not in [d["slug"] for d in brief["drugs"]]:
            continue
        if program and study.program != program:
            continue
        if has_results is not None and bool(study.has_results) != has_results:
            continue
        if q and q.lower() not in f"{study.display_name} {study.brief_title} {study.nct_id}".lower():
            continue
        tiers = sorted({r.tier for r in study.results} | {a.tier for a in study.analyses})
        primary_pub = next((link.publication for link in study.publication_links if link.relation == "PRIMARY_RESULTS"), None)
        out.append({**brief, "evidence_tiers": tiers, "n_results": len(study.results), "n_publications": len(study.publication_links),
                    "primary_publication": {"pmid": primary_pub.pmid, "journal": primary_pub.journal, "year": primary_pub.year} if primary_pub else None})
    return out


@app.get("/api/discovered", tags=["studies"])
def discovered(db: Db, drug: str | None = None, q: Annotated[str | None, Query(max_length=120)] = None, limit: int = Query(300, ge=1, le=1000)) -> dict[str, Any]:
    watched = {nct for (nct,) in db.execute(select(Study.nct_id))}
    rows = []
    for trial in db.scalars(select(DiscoveredTrial).order_by(DiscoveredTrial.primary_completion_date.desc())):
        if drug and drug not in (trial.drug_slugs or []):
            continue
        if q and q.lower() not in f"{trial.brief_title} {trial.acronym or ''} {trial.nct_id}".lower():
            continue
        rows.append({"nct_id": trial.nct_id, "brief_title": trial.brief_title, "acronym": trial.acronym, "phase": trial.phase, "overall_status": trial.overall_status, "has_results": trial.has_results,
                     "enrollment": trial.enrollment, "primary_completion_date": trial.primary_completion_date, "last_update_posted": trial.last_update_posted, "drug_slugs": trial.drug_slugs, "ingested_in_depth": trial.nct_id in watched})
    return {"total": len(rows), "rows": rows[:limit], "note": "Registry index of Lilly-led trials matching tracked intervention terms (summary fields only). Trials marked 'ingested in depth' have full protocol and results extraction."}


@app.get("/api/studies/{nct}", tags=["studies"])
def get_study(nct: str, db: Db) -> dict[str, Any]:
    study = _study_or_404(db, nct)
    population = study.population
    by_endpoint: dict[int, dict[str, Any]] = {}
    for result in study.results:
        endpoint = result.endpoint
        group = by_endpoint.setdefault(endpoint.id, {"endpoint_id": endpoint.id, "title": endpoint.title, "role": endpoint.role, "category": endpoint.category, "category_label": CATEGORY_LABEL.get(endpoint.category), "timeframe": endpoint.timeframe_text, "origin": endpoint.origin, "kind": endpoint.kind, "multiplicity_controlled": endpoint.multiplicity_controlled, "results": [], "analyses": []})
        group["results"].append(_compact(build_finding(db, result)))
    for analysis in study.analyses:
        endpoint = analysis.endpoint
        group = by_endpoint.setdefault(endpoint.id, {"endpoint_id": endpoint.id, "title": endpoint.title, "role": endpoint.role, "category": endpoint.category, "category_label": CATEGORY_LABEL.get(endpoint.category), "timeframe": endpoint.timeframe_text, "origin": endpoint.origin, "kind": endpoint.kind, "multiplicity_controlled": endpoint.multiplicity_controlled, "results": [], "analyses": []})
        group["analyses"].append(analysis_dto(analysis))
    role_order = {"PRIMARY": 0, "KEY_SECONDARY": 1, "SECONDARY": 2, "OTHER_PRESPECIFIED": 3, "EXPLORATORY": 4, "POST_HOC": 5, "NOT_STATED": 6}
    groups = sorted(by_endpoint.values(), key=lambda g: (role_order.get(g["role"], 9), g["origin"], g["endpoint_id"]))
    links = sorted(study.publication_links, key=lambda link: (link.relation != "PRIMARY_RESULTS", -(link.publication.year or 0)))
    return {
        **study_brief(study),
        "official_title": study.official_title,
        "brief_summary": study.brief_summary,
        "lead_sponsor": study.lead_sponsor,
        "intervention_model": study.intervention_model,
        "primary_purpose": study.primary_purpose,
        "completion_date": study.completion_date,
        "results_first_posted": study.results_first_posted,
        "location_count": study.location_count,
        "countries": study.countries,
        "source": source_ref(study.source),
        "population": {"diabetes_status": population.diabetes_status, "diabetes_basis": population.diabetes_basis, "sex": population.sex, "min_age": population.min_age, "max_age": population.max_age, "summary": population.summary, "baseline": population.baseline[:120]} if population else None,
        "arms": [{"id": a.id, "label": a.label, "arm_type": a.arm_type, "drug_slug": a.drug_slug, "dose_amount": a.dose_amount, "dose_unit": a.dose_unit, "dose_text": a.dose_text, "formulation_key": a.formulation_key, "is_placebo": a.is_placebo, "is_pooled": a.is_pooled, "n_randomized": a.n_randomized, "description": a.description, "origin": a.origin} for a in study.arms],
        "protocol_endpoints": [{"title": e.title, "role": e.role, "category": e.category, "timeframe": e.timeframe_text, "description": e.description} for e in study.endpoints if e.kind == "PROTOCOL"],
        "estimands": [{"id": e.id, "label": e.label, "basis": e.basis, "description": e.description, "quote": e.quote, "explainer": ESTIMAND_EXPLAINER.get(e.label), "strategies": [{"event": s.event, "strategy": s.strategy, "basis": s.basis, "quote": s.quote} for s in e.strategies], "source": source_ref(e.source)} for e in study.estimands],
        "endpoint_groups": groups,
        "publications": [{"pmid": link.publication.pmid, "doi": link.publication.doi, "title": link.publication.title, "journal": link.publication.journal, "year": link.publication.year, "authors": link.publication.authors[:6], "relation": link.relation, "link_bases": link.bases, "link_confidence": link.confidence, "source_id": link.publication.source_id} for link in links],
        "claims": [{"topic": c.topic, "statement": c.statement, "quote": c.quote, "tier": c.tier, "verification_status": c.verification_status, "source": source_ref(c.source)} for c in db.scalars(select(EvidenceClaim).where(EvidenceClaim.study_id == study.id))],
        "complexity": design.complexity(study),
        "versions": design.amendment_history(db, study.nct_id)["versions"],
    }


@app.get("/api/studies/{nct}/eligibility", tags=["studies"])
def study_eligibility(nct: str, db: Db) -> dict[str, Any]:
    _study_or_404(db, nct)
    return design.eligibility(db, nct)


# --------------------------------------------------------------------------- findings & comparison


@app.get("/api/findings", tags=["evidence"])
def list_findings(
    db: Db,
    study: str | None = None,
    drug: str | None = None,
    category: str | None = None,
    estimand: str | None = None,
    tier: str | None = None,
    arm_level_only: bool = True,
    limit: int = Query(400, ge=1, le=2000),
) -> dict[str, Any]:
    query = select(OutcomeResult).join(Study, OutcomeResult.study_id == Study.id).join(Endpoint, OutcomeResult.endpoint_id == Endpoint.id)
    if study:
        query = query.where(or_(Study.nct_id == study.upper(), Study.display_name == study))
    if category:
        query = query.where(Endpoint.category == category)
    if tier:
        query = query.where(OutcomeResult.tier == tier)
    rows = []
    for result in db.scalars(query.order_by(Study.display_name, Endpoint.id, OutcomeResult.id)):
        if arm_level_only and (result.scope != "ARM" or " minus " in result.arm_label or (result.arm and result.arm.is_pooled)):
            continue
        if drug and not (result.arm and result.arm.drug_slug == drug):
            continue
        if estimand and (result.estimand.label if result.estimand else "NOT_STATED") != estimand:
            continue
        rows.append(_compact(build_finding(db, result)))
        if len(rows) >= limit:
            break
    return {"total": len(rows), "rows": rows, "category_labels": CATEGORY_LABEL}


@app.get("/api/findings/{result_id}", tags=["evidence"])
def finding_detail(result_id: int, db: Db) -> dict[str, Any]:
    finding = get_finding(db, result_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"finding": finding.as_dict(), "interpretation": interpret_finding(finding)}


@app.get("/api/analyses/{analysis_id}", tags=["evidence"])
def analysis_detail(analysis_id: int, db: Db) -> dict[str, Any]:
    analysis = db.get(StatisticalAnalysis, analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    dto = analysis_dto(analysis)
    return {"analysis": dto, "interpretation": interpret_analysis(dto)}


@app.get("/api/analyses", tags=["evidence"])
def list_analyses(db: Db, study: str | None = None, category: str | None = None, curated_only: bool = False, limit: int = Query(300, ge=1, le=1500)) -> dict[str, Any]:
    query = select(StatisticalAnalysis).join(Study, StatisticalAnalysis.study_id == Study.id).join(Endpoint, StatisticalAnalysis.endpoint_id == Endpoint.id)
    if study:
        query = query.where(or_(Study.nct_id == study.upper(), Study.display_name == study))
    if category:
        query = query.where(Endpoint.category == category)
    if curated_only:
        query = query.where(StatisticalAnalysis.origin == "CURATED")
    rows = []
    for a in db.scalars(query.limit(limit)):
        dto = analysis_dto(a)
        rows.append({**dto, "interpretation": interpret_analysis(dto)})
    return {"total": len(rows), "rows": rows}


class CompareRequest(Strict):
    a: int = Field(gt=0)
    b: int = Field(gt=0)


@app.post("/api/compare", tags=["evidence"])
def compare(body: CompareRequest, db: Db) -> dict[str, Any]:
    a, b = get_finding(db, body.a), get_finding(db, body.b)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="One or both findings were not found")
    comparison = compare_findings(db, a, b)
    audit(db, "comparison.run", actor="api", target_type="comparison", target_id=f"{body.a}:{body.b}", classification=comparison.classification)
    db.commit()
    return {"a": a.as_dict(), "b": b.as_dict(), "interpretation_a": interpret_finding(a), "interpretation_b": interpret_finding(b), "comparison": comparison.as_dict(),
            "analysis_interpretations": [interpret_analysis(x) for x in comparison.reported_analyses]}


def _pick(db: Session, nct: str, arm_contains: str, estimand: str | None, tier: str | None = None, weeks: float | None = None, category: str = "WEIGHT_PCT_CHANGE", baseline_contains: str | None = None) -> int | None:
    study = db.scalar(select(Study).where(Study.nct_id == nct))
    if study is None:
        return None
    for r in study.results:
        if r.endpoint.category != category or r.value is None or r.scope != "ARM" or " minus " in r.arm_label or " to " in r.arm_label.lower():
            continue
        if arm_contains.lower() not in r.arm_label.lower():
            continue
        if estimand and (r.estimand.label if r.estimand else "NOT_STATED") != estimand:
            continue
        if tier and r.tier != tier:
            continue
        if weeks and r.timepoint_weeks != weeks:
            continue
        if baseline_contains and baseline_contains.lower() not in (r.baseline_definition or "").lower():
            continue
        return r.id
    return None


@app.get("/api/compare/presets", tags=["evidence"])
def compare_presets(db: Db) -> list[dict[str, Any]]:
    """Worked examples that exercise each classification (ids resolved from the live database)."""
    specs = [
        ("Tirzepatide 15 mg vs placebo within SURMOUNT-1", "Randomized arms of one trial, registry-posted analysis with confidence interval.",
         _pick(db, "NCT04184622", "15 mg", "EFFICACY", "REGISTRY_RESULTS", 72), _pick(db, "NCT04184622", "placebo", "EFFICACY", "REGISTRY_RESULTS", 72)),
        ("Retatrutide 12 mg (TRIUMPH-1) vs tirzepatide 15 mg (SURMOUNT-1)", "Different trials, same estimand: is this head-to-head evidence?",
         _pick(db, "NCT05929066", "12 mg", "TREATMENT_REGIMEN", "PEER_REVIEWED", 80), _pick(db, "NCT04184622", "15 mg", "TREATMENT_REGIMEN", "PEER_REVIEWED", 72)),
        ("ATTAIN-1 36 mg: efficacy estimand vs treatment-regimen estimand", "Same arm, two estimands — two answers to two different questions.",
         _pick(db, "NCT05869903", "36 mg", "EFFICACY", "REGISTRY_RESULTS", 72), _pick(db, "NCT05869903", "36 mg", "TREATMENT_REGIMEN", "PEER_REVIEWED", 72)),
        ("Orforglipron 36 mg (ATTAIN-1) vs tirzepatide 15 mg (SURMOUNT-1)", "Two placebo-controlled Phase 3 trials without diabetes, same inferred estimand and duration.",
         _pick(db, "NCT05869903", "36 mg", "EFFICACY", "REGISTRY_RESULTS", 72), _pick(db, "NCT04184622", "15 mg", "EFFICACY", "REGISTRY_RESULTS", 72)),
        ("SURMOUNT-4 (from randomization at week 36) vs SURMOUNT-1 (from baseline)", "Randomized-withdrawal baseline versus study-entry baseline.",
         _pick(db, "NCT04660643", "tirzepatide", "EFFICACY", "REGISTRY_RESULTS", 88, baseline_contains="randomization"), _pick(db, "NCT04184622", "15 mg", "EFFICACY", "REGISTRY_RESULTS", 72)),
        ("EloraTZP 9 mg + 15 mg vs tirzepatide 15 mg within the Phase 2b trial", "Randomized within one Phase 2 trial in type 2 diabetes; press-release topline only.",
         _pick(db, "NCT06603571", "9 mg + tirzepatide 15", "EFFICACY"), _pick(db, "NCT06603571", "tirzepatide 15 mg", "EFFICACY")),
        ("EloraTZP Phase 2b (T2D, 48 weeks) vs tirzepatide 15 mg in SURMOUNT-1 (no T2D, 72 weeks)", "Different populations, durations, phases and evidence tiers.",
         _pick(db, "NCT06603571", "9 mg + tirzepatide 15", "EFFICACY"), _pick(db, "NCT04184622", "15 mg", "EFFICACY", "REGISTRY_RESULTS", 72)),
        ("Tirzepatide vs semaglutide within SURMOUNT-5", "Head-to-head randomized trial; registry reports a difference with CI but no p-value.",
         _pick(db, "NCT05822830", "tirzepatide", None, "REGISTRY_RESULTS"), _pick(db, "NCT05822830", "semaglutide", None, "REGISTRY_RESULTS")),
    ]
    out = []
    for title, question, a, b in specs:
        if a and b:
            fa, fb = get_finding(db, a), get_finding(db, b)
            out.append({"title": title, "question": question, "a": a, "b": b, "classification": compare_findings(db, fa, fb).classification})
    return out


@app.get("/api/compare/studies", tags=["evidence"])
def compare_studies(db: Db, a: str, b: str) -> dict[str, Any]:
    sa, sb = _study_or_404(db, a), _study_or_404(db, b)

    def row(label: str, fn) -> dict[str, Any]:
        va, vb = fn(sa), fn(sb)
        return {"label": label, "a": va, "b": vb, "same": va == vb}

    def pop(s: Study, key: str) -> Any:
        entry = (s.population.summary or {}).get(key) if s.population else None
        return entry.get("value") if entry else None

    def primaries(s: Study) -> list[str]:
        return [f"{e.title} [{e.timeframe_text}]" for e in s.endpoints if e.kind == "PROTOCOL" and e.role == "PRIMARY"]

    rows = [
        row("Phase", lambda s: s.phase), row("Status", lambda s: s.overall_status), row("Allocation", lambda s: s.allocation), row("Masking", lambda s: s.masking),
        row("Intervention model", lambda s: s.intervention_model), row("Control type (curated)", lambda s: (s.design_features or {}).get("comparator_type")),
        row("Planned treatment duration, weeks (curated)", lambda s: (s.design_features or {}).get("treatment_weeks")),
        row("Randomized-withdrawal design", lambda s: bool((s.design_features or {}).get("randomized_withdrawal"))),
        row("Enrollment", lambda s: f"{s.enrollment} ({(s.enrollment_type or '').lower()})"), row("Diabetes status", lambda s: s.population.diabetes_status if s.population else None),
        row("Basis for diabetes status", lambda s: s.population.diabetes_basis if s.population else None),
        row("Age eligibility", lambda s: f"{s.population.min_age or '—'} to {s.population.max_age or 'no upper limit stated'}" if s.population else None),
        row("Mean age, years (registry baseline)", lambda s: pop(s, "age_mean_years")), row("Mean baseline weight, kg", lambda s: pop(s, "weight_mean_kg")), row("Mean baseline BMI", lambda s: pop(s, "bmi_mean")),
        row("Arms", lambda s: [arm.label for arm in s.arms if arm.arm_type not in ("RESULTS_GROUP", "CURATED")]),
        row("Registered primary endpoint(s)", primaries),
        row("Estimands on record", lambda s: sorted({f"{e.label} ({e.basis.replace('_', ' ').lower()})" for e in s.estimands})),
        row("Inclusion / exclusion criteria (count)", lambda s: f"{len(s.inclusion)} / {len(s.exclusion)}"),
        row("Countries", lambda s: len(s.countries or [])), row("Registry results posted", lambda s: bool(s.has_results)),
        row("Evidence tiers on record", lambda s: sorted({r.tier for r in s.results} | {x.tier for x in s.analyses})),
        row("Primary completion", lambda s: f"{s.primary_completion_date} ({(s.primary_completion_type or '').lower()})"),
    ]
    return {"a": study_brief(sa), "b": study_brief(sb), "rows": rows, "eligibility": design.compare_eligibility(db, sa.nct_id, sb.nct_id)}


@app.get("/api/estimands", tags=["evidence"])
def estimand_inspector(db: Db, study: str) -> dict[str, Any]:
    """Results for the same arm and endpoint laid out by estimand, so the two are never merged."""
    s = _study_or_404(db, study)
    table: dict[tuple, dict[str, Any]] = {}
    for r in s.results:
        if r.endpoint.category not in ("WEIGHT_PCT_CHANGE", "HBA1C_CHANGE", "WEIGHT_ABS_CHANGE") or r.value is None or r.scope != "ARM" or " minus " in r.arm_label or r.arm is None or r.arm.is_pooled:
            continue
        arm_key = "placebo" if r.arm.is_placebo else (f"{r.arm.drug_slug} {r.arm.dose_amount:g} mg" if r.arm.dose_amount is not None else r.arm.label)
        key = (r.endpoint.category, arm_key, r.timepoint_weeks, baseline_class(s.design_features, r.baseline_definition))
        cell = table.setdefault(key, {"category": r.endpoint.category, "category_label": CATEGORY_LABEL.get(r.endpoint.category), "arm": arm_key, "timepoint_weeks": r.timepoint_weeks, "baseline_definition": r.baseline_definition, "by_estimand": {}})
        label = r.estimand.label if r.estimand else "NOT_STATED"
        cell["by_estimand"].setdefault(label, []).append(_compact(build_finding(db, r)))
    rows = sorted(table.values(), key=lambda c: (c["category"], c["timepoint_weeks"] or 0, c["arm"] != "placebo", c["arm"]))
    return {
        "study": study_brief(s),
        "estimands": [{"label": e.label, "basis": e.basis, "description": e.description, "quote": e.quote, "explainer": ESTIMAND_EXPLAINER.get(e.label), "strategies": [{"event": x.event, "strategy": x.strategy, "basis": x.basis, "quote": x.quote} for x in e.strategies], "source": source_ref(e.source)} for e in s.estimands],
        "rows": rows,
        "explainers": ESTIMAND_EXPLAINER,
        "rule": "Values under different estimands are shown in separate columns and are never averaged, differenced or substituted for one another.",
    }


# --------------------------------------------------------------------------- benefit–risk


@app.get("/api/benefit-risk/{nct}", tags=["benefit-risk"])
def benefit_risk_profiles(nct: str, db: Db, estimand: Literal["EFFICACY", "TREATMENT_REGIMEN"] | None = None, weeks: float | None = Query(None, gt=0, le=600)) -> dict[str, Any]:
    _study_or_404(db, nct)
    return benefit_risk.arm_profiles(db, nct, estimand, weeks)


class Criterion(Strict):
    weight: float = Field(ge=0, le=100)
    best: float
    worst: float


class UtilityRequest(Strict):
    nct: NCT
    estimand: Literal["EFFICACY", "TREATMENT_REGIMEN"] | None = None
    criteria: dict[str, Criterion] | None = None


@app.post("/api/benefit-risk/utility", tags=["benefit-risk"])
def utility(body: UtilityRequest, db: Db) -> dict[str, Any]:
    _study_or_404(db, body.nct)
    try:
        profiles = benefit_risk.arm_profiles(db, body.nct, body.estimand)
        return benefit_risk.utility_scores(profiles, {k: v.model_dump() for k, v in body.criteria.items()} if body.criteria else None)
    except StatsInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class DoseScenarioRequest(Strict):
    nct: NCT
    estimand: Literal["EFFICACY", "TREATMENT_REGIMEN"] | None = None
    metric: Literal["weight_pct", "weight_abs", "hba1c"] = "weight_pct"
    shares: dict[str, float]
    non_persistent_share: float = Field(0.0, ge=0, le=1)
    non_persistent_value: float | None = None


@app.post("/api/benefit-risk/dose-scenario", tags=["benefit-risk"])
def dose_scenario(body: DoseScenarioRequest, db: Db) -> dict[str, Any]:
    _study_or_404(db, body.nct)
    try:
        profiles = benefit_risk.arm_profiles(db, body.nct, body.estimand)
        return benefit_risk.dose_scenario(profiles, body.shares, body.metric, body.non_persistent_share, body.non_persistent_value)
    except StatsInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class RiskContrastRequest(Strict):
    ae_a: int = Field(gt=0)
    ae_b: int = Field(gt=0)


@app.post("/api/stats/risk-contrast", tags=["benefit-risk"])
def risk_contrast(body: RiskContrastRequest, db: Db) -> dict[str, Any]:
    a, b = db.get(AdverseEvent, body.ae_a), db.get(AdverseEvent, body.ae_b)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="Adverse-event row not found")
    try:
        contrast = benefit_risk.arm_contrast(a.n_affected, a.n_at_risk, b.n_affected, b.n_at_risk, a.measure_kind, b.measure_kind, a.study_id == b.study_id)
    except (StatsInputError, InvalidInferenceError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"a": ae_dto(a), "b": ae_dto(b), "a_statement": describe_adverse_event(ae_dto(a)), "b_statement": describe_adverse_event(ae_dto(b)), **contrast}


# --------------------------------------------------------------------------- safety


@app.get("/api/safety/trial-ae", tags=["safety"])
def safety_trial_ae(db: Db, drug: str | None = None, nct: str | None = None, measure_kind: str | None = None, term: Annotated[str | None, Query(max_length=80)] = None, limit: int = Query(1500, ge=1, le=5000)) -> dict[str, Any]:
    if measure_kind and measure_kind not in AE_KIND_LABEL:
        raise HTTPException(status_code=422, detail="Unknown measure kind")
    return trial_ae_table(db, drug, nct, measure_kind, term, limit)


@app.get("/api/safety/terms", tags=["safety"])
def safety_terms(db: Db, nct: str) -> dict[str, Any]:
    study = _study_or_404(db, nct)
    counts: dict[tuple[str, str, str], int] = {}
    for ae in study.adverse_events:
        key = (ae.term, ae.measure_kind, ae.category)
        counts[key] = max(counts.get(key, 0), ae.n_affected or 0)
    rows = [{"term": t, "measure_kind": k, "measure_kind_label": AE_KIND_LABEL.get(k, k), "category": c, "max_participants_affected": n} for (t, k, c), n in counts.items()]
    return {"study": study_brief(study), "terms": sorted(rows, key=lambda r: -r["max_participants_affected"])[:300]}


@app.get("/api/safety/spontaneous/{slug}", tags=["safety"])
def safety_spontaneous(slug: str, db: Db) -> dict[str, Any]:
    return spontaneous_overview(db, _drug_or_404(db, slug))


class DisproportionalityRequest(Strict):
    drug_event: int = Field(ge=0)
    drug_total: int = Field(ge=0)
    event_total: int = Field(ge=0)
    all_total: int = Field(ge=0)


@app.post("/api/safety/disproportionality", tags=["safety"])
def safety_disproportionality(body: DisproportionalityRequest) -> dict[str, Any]:
    try:
        return disproportionality(body.drug_event, body.drug_total, body.event_total, body.all_total)
    except StatsInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/safety/labels/{slug}", tags=["safety"])
def safety_labels(slug: str, db: Db) -> dict[str, Any]:
    drug = _drug_or_404(db, slug)
    return {"drug": drug.slug, "labels": label_safety_sections(db, drug)}


# --------------------------------------------------------------------------- gaps


def _gap_dto(gap: EvidenceGap, weights: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "id": gap.id, "key": gap.key, "rule_id": gap.rule_id, "drug": {"slug": gap.drug.slug, "name": gap.drug.name} if gap.drug else None, "program": gap.program, "category": gap.category,
        "question": gap.question, "available_evidence": gap.available_evidence, "missing_evidence": gap.missing_evidence, "gap_nature": gap.gap_nature, "gap_nature_label": NATURE_LABEL.get(gap.gap_nature),
        "clinical_importance": gap.clinical_importance, "evidence_certainty": gap.evidence_certainty, "tractability": gap.tractability, "analytic_approach": gap.analytic_approach, "data_required": gap.data_required,
        "source_ids": gap.source_ids, "related_ncts": gap.related_ncts, "review_status": gap.review_status, "reviewer_note": gap.reviewer_note, "priority": priority(gap, weights),
    }


@app.get("/api/gaps", tags=["gaps"])
def list_gaps(db: Db, drug: str | None = None, category: str | None = None, nature: str | None = None,
              w_importance: float = Query(DEFAULT_WEIGHTS["clinical_importance"], ge=0, le=1), w_uncertainty: float = Query(DEFAULT_WEIGHTS["uncertainty"], ge=0, le=1),
              w_tractability: float = Query(DEFAULT_WEIGHTS["tractability"], ge=0, le=1), w_actionability: float = Query(DEFAULT_WEIGHTS["actionability"], ge=0, le=1)) -> dict[str, Any]:
    weights = {"clinical_importance": w_importance, "uncertainty": w_uncertainty, "tractability": w_tractability, "actionability": w_actionability}
    rows = []
    for gap in db.scalars(select(EvidenceGap)):
        if drug and (gap.drug is None or gap.drug.slug != drug):
            continue
        if category and gap.category != category:
            continue
        if nature and gap.gap_nature != nature:
            continue
        rows.append(_gap_dto(gap, weights))
    rows.sort(key=lambda g: -g["priority"]["score"])
    return {"total": len(rows), "rows": rows, "weights": weights, "default_weights": DEFAULT_WEIGHTS, "nature_labels": NATURE_LABEL,
            "categories": sorted({g["category"] for g in rows}), "note": "A gap in the public record is not confirmation that the sponsor has no data. Each gap states why it is open."}


class GapReview(Strict):
    review_status: Literal["OPEN", "IN_REVIEW", "CONFIRMED", "DISMISSED", "RESOLVED"]
    reviewer_note: str | None = Field(None, max_length=2000)
    clinical_importance: int | None = Field(None, ge=1, le=5)
    reviewer: str = Field(min_length=2, max_length=120)


@app.patch("/api/gaps/{gap_id}", tags=["gaps"])
def review_gap(gap_id: int, body: GapReview, db: Db, _: Annotated[str, Depends(require_reviewer)]) -> dict[str, Any]:
    gap = db.get(EvidenceGap, gap_id)
    if gap is None:
        raise HTTPException(status_code=404, detail="Gap not found")
    before = {"review_status": gap.review_status, "clinical_importance": gap.clinical_importance}
    gap.review_status = body.review_status
    gap.reviewer_note = body.reviewer_note
    if body.clinical_importance is not None:
        gap.clinical_importance = body.clinical_importance
    db.add(ReviewDecision(target_type="evidence_gap", target_id=gap.id, decision=body.review_status, reviewer=body.reviewer, rationale=body.reviewer_note or ""))
    audit(db, "gap.reviewed", actor=body.reviewer, target_type="evidence_gap", target_id=gap.id, before=before, after={"review_status": gap.review_status, "clinical_importance": gap.clinical_importance})
    db.commit()
    return _gap_dto(gap)


# --------------------------------------------------------------------------- design workbench


@app.get("/api/design/coverage", tags=["design"])
def design_coverage(db: Db, drug: str | None = None) -> dict[str, Any]:
    return design.coverage_matrix(db, drug)


@app.get("/api/design/eligibility-compare", tags=["design"])
def design_eligibility_compare(db: Db, a: str, b: str) -> dict[str, Any]:
    _study_or_404(db, a), _study_or_404(db, b)
    return design.compare_eligibility(db, a, b)


@app.get("/api/design/endpoints/{nct}", tags=["design"])
def design_endpoints(nct: str, db: Db) -> dict[str, Any]:
    _study_or_404(db, nct)
    return design.endpoint_review(db, nct)


@app.get("/api/design/amendments/{nct}", tags=["design"])
def design_amendments(nct: str, db: Db) -> dict[str, Any]:
    _study_or_404(db, nct)
    return design.amendment_history(db, nct)


class PowerRequest(Strict):
    design: Literal["two_means", "two_proportions", "noninferiority_means", "logrank_events", "noninferiority_hr_events"]
    alpha: float = Field(0.05, gt=0, lt=0.5)
    power: float = Field(0.9, gt=0.5, lt=1)
    ratio: float = Field(1.0, gt=0, le=10)
    delta: float | None = None
    sd: float | None = Field(None, gt=0)
    p1: float | None = Field(None, gt=0, lt=1)
    p2: float | None = Field(None, gt=0, lt=1)
    margin: float | None = Field(None, gt=0)
    true_difference: float = 0.0
    hazard_ratio: float | None = Field(None, gt=0)
    true_hazard_ratio: float = Field(1.0, gt=0)
    attrition: float = Field(0.0, ge=0, lt=1)


@app.post("/api/design/power", tags=["design"])
def design_power(body: PowerRequest) -> dict[str, Any]:
    try:
        if body.design == "two_means":
            if body.delta is None or body.sd is None:
                raise StatsInputError("delta and sd are required")
            result = stats.n_two_means(body.delta, body.sd, body.alpha, body.power, body.ratio)
        elif body.design == "two_proportions":
            if body.p1 is None or body.p2 is None:
                raise StatsInputError("p1 and p2 are required")
            result = stats.n_two_proportions(body.p1, body.p2, body.alpha, body.power, body.ratio)
        elif body.design == "noninferiority_means":
            if body.margin is None or body.sd is None:
                raise StatsInputError("margin and sd are required")
            result = stats.n_noninferiority_means(body.margin, body.sd, body.true_difference, body.alpha / 2, body.power, body.ratio)
        elif body.design == "logrank_events":
            if body.hazard_ratio is None:
                raise StatsInputError("hazard_ratio is required")
            result = stats.events_logrank(body.hazard_ratio, body.alpha, body.power, body.ratio)
        else:
            if body.margin is None:
                raise StatsInputError("margin is required")
            result = stats.events_noninferiority_hr(body.margin, body.true_hazard_ratio, body.alpha / 2, body.power, body.ratio)
    except StatsInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.attrition and "n_total" in result:
        result["with_attrition"] = stats.inflate_for_attrition(result["n_total"], body.attrition)
    result["kind"] = "CALCULATION"
    result["disclaimer"] = "A design calculation under the stated assumptions. It is not a forecast of the probability that any trial will succeed."
    return result


class SimulationRequest(Strict):
    n_per_arm: int = Field(ge=3, le=20000)
    mean_treatment: float
    mean_control: float
    sd: float = Field(gt=0)
    dropout_treatment: float = Field(0.0, ge=0, lt=1)
    dropout_control: float = Field(0.0, ge=0, lt=1)
    missing_handling: Literal["COMPLETERS", "JUMP_TO_REFERENCE"] = "COMPLETERS"
    n_sims: int = Field(2000, ge=100, le=20000)
    alpha: float = Field(0.05, gt=0, lt=0.5)
    seed: int = Field(20261013, ge=0, le=2**31 - 1)


@app.post("/api/design/simulate", tags=["design"])
def design_simulate(body: SimulationRequest) -> dict[str, Any]:
    try:
        return stats.simulate_two_arm_trial(stats.SimulationSpec(**body.model_dump()))
    except StatsInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class SensitivityRequest(Strict):
    delta: float
    sd: float = Field(gt=0)
    n_randomized_per_group: int = Field(ge=3, le=100000)
    attrition_rates: list[Annotated[float, Field(ge=0, lt=1)]] = Field(default=[0.0, 0.1, 0.2, 0.3, 0.4], max_length=20)
    alpha: float = Field(0.05, gt=0, lt=0.5)
    mean_treatment: float | None = None
    mean_control: float | None = None
    missing_treatment: float = Field(0.0, ge=0, lt=1)
    missing_control: float = Field(0.0, ge=0, lt=1)
    deltas: list[float] = Field(default=[0.0, 2.0, 4.0, 6.0, 8.0], max_length=20)


@app.post("/api/design/sensitivity", tags=["design"])
def design_sensitivity(body: SensitivityRequest) -> dict[str, Any]:
    try:
        out: dict[str, Any] = {"attrition": stats.attrition_sensitivity(body.delta, body.sd, body.n_randomized_per_group, body.attrition_rates, body.alpha),
                               "attrition_note": "Completers-only simplification: power if only participants who complete contribute. Label: scenario."}
        if body.mean_treatment is not None and body.mean_control is not None:
            out["missing_data"] = stats.missing_data_delta_scenarios(body.mean_treatment, body.mean_control, body.missing_treatment, body.missing_control, body.deltas)
            out["missing_data_note"] = "Delta-adjustment scenarios on aggregate means: hypothetical, not estimates."
        return out
    except StatsInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --------------------------------------------------------------------------- assistant


class AskRequest(Strict):
    question: str = Field(min_length=3, max_length=1000)


@app.post("/api/assistant/ask", tags=["assistant"])
def assistant_ask(body: AskRequest, db: Db) -> dict[str, Any]:
    answer = ask(db, body.question, get_provider(get_settings()))
    audit(db, "assistant.question", actor="api", target_type="assistant", target_id=answer.intent, llm_used=answer.llm.get("used", False))
    db.commit()
    return answer.as_dict()


@app.get("/api/assistant/examples", tags=["assistant"])
def assistant_examples() -> list[str]:
    return [
        "What evidence differentiates retatrutide from tirzepatide?",
        "Which outcomes in EloraTZP are exploratory?",
        "Which clinical questions are still unresolved for eloralintide?",
        "What would be required to compare SURMOUNT-1 and TRIUMPH-1 rigorously?",
        "Which results depend on hypothetical treatment continuation?",
        "Which programs have prospective cardiovascular outcomes trials?",
    ]


# --------------------------------------------------------------------------- sources, audit, review


@app.get("/api/sources", tags=["provenance"])
def list_sources(db: Db, source_type: str | None = None, tier: str | None = None, q: Annotated[str | None, Query(max_length=120)] = None, limit: int = Query(500, ge=1, le=2000)) -> dict[str, Any]:
    query = select(EvidenceSource)
    if source_type:
        query = query.where(EvidenceSource.source_type == source_type)
    if tier:
        query = query.where(EvidenceSource.tier == tier)
    if q:
        query = query.where(or_(EvidenceSource.title.ilike(f"%{q}%"), EvidenceSource.key.ilike(f"%{q}%")))
    usage = Counter(sid for (sid,) in db.execute(select(OutcomeResult.source_id)))
    rows = []
    for source in db.scalars(query.order_by(EvidenceSource.source_type, EvidenceSource.key).limit(limit)):
        rows.append({**source_ref(source), "source_type": source.source_type, "license_note": source.license_note, "n_snapshots": len(source.snapshots), "n_results": usage.get(source.id, 0), "external_ids": source.external_ids})
    types = Counter(t for (t,) in db.execute(select(EvidenceSource.source_type)))
    return {"total": len(rows), "rows": rows, "by_type": dict(types)}


@app.get("/api/sources/{source_id}", tags=["provenance"])
def get_source(source_id: int, db: Db) -> dict[str, Any]:
    source = db.get(EvidenceSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    results = db.scalars(select(OutcomeResult).where(OutcomeResult.source_id == source.id).limit(60)).all()
    return {
        **source_ref(source), "source_type": source.source_type, "license_note": source.license_note, "external_ids": source.external_ids, "classification": source.classification,
        "snapshots": [{"version_no": s.version_no, "content_hash": s.content_hash, "retrieved_at": s.retrieved_at.isoformat(), "version_label": s.version_label, "byte_size": s.byte_size, "content_stored": s.content is not None, "is_synthetic": s.is_synthetic, "anchored_excerpts": s.excerpts} for s in source.snapshots],
        "dependent_findings": [{"result_id": r.id, "study": r.study.display_name, "arm": r.arm_label, "endpoint": r.endpoint.title, "value": r.value, "verification_status": r.verification_status, "locator_quote": r.locator_quote, "source_location": r.source_location} for r in results],
        "changes": [{"id": e.id, "detected_at": e.detected_at.isoformat(), "summary": e.summary, "significance": e.significance, "review_status": e.review_status, "is_synthetic": e.is_synthetic} for e in db.scalars(select(ChangeEvent).where(ChangeEvent.source_id == source.id))],
    }


@app.get("/api/changes", tags=["provenance"])
def list_changes(db: Db) -> list[dict[str, Any]]:
    return [{"id": e.id, "source": source_ref(e.source), "detected_at": e.detected_at.isoformat(), "summary": e.summary, "significance": e.significance, "review_status": e.review_status, "is_synthetic": e.is_synthetic,
             "synthetic_notice": "SYNTHETIC DEMONSTRATION — the 'before' version was generated from the real record by labelled edits to demonstrate change review. It is not a real registry change." if e.is_synthetic else None, "changes": e.changes}
            for e in db.scalars(select(ChangeEvent).order_by(ChangeEvent.detected_at.desc()))]


@app.get("/api/audit", tags=["provenance"])
def list_audit(db: Db, action: str | None = None, limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    query = select(AuditEvent)
    if action:
        query = query.where(AuditEvent.action == action)
    return [{"id": e.id, "at": e.at.isoformat(), "actor": e.actor, "action": e.action, "target_type": e.target_type, "target_id": e.target_id, "detail": e.detail} for e in db.scalars(query.order_by(AuditEvent.id.desc()).limit(limit))]


@app.get("/api/review-queue", tags=["provenance"])
def review_queue(db: Db, status: str | None = "OPEN", item_type: str | None = None, limit: int = Query(300, ge=1, le=1000)) -> dict[str, Any]:
    query = select(ReviewQueueItem)
    if status:
        query = query.where(ReviewQueueItem.status == status)
    if item_type:
        query = query.where(ReviewQueueItem.item_type == item_type)
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items = sorted(db.scalars(query), key=lambda i: (order.get(i.priority, 9), i.id))
    counts = Counter((i.item_type, i.status) for i in db.scalars(select(ReviewQueueItem)))
    return {"total": len(items), "rows": [{"id": i.id, "item_type": i.item_type, "target_type": i.target_type, "target_id": i.target_id, "title": i.title, "reason": i.reason, "priority": i.priority, "status": i.status, "payload": i.payload, "created_at": i.created_at.isoformat()} for i in items[:limit]],
            "counts": [{"item_type": t, "status": s, "n": n} for (t, s), n in sorted(counts.items())], "review_enabled": bool(get_settings().reviewer_token)}


class DecisionRequest(Strict):
    decision: Literal["APPROVE", "REJECT", "NEEDS_INFO"]
    reviewer: str = Field(min_length=2, max_length=120)
    rationale: str = Field(min_length=3, max_length=2000)


@app.post("/api/review-queue/{item_id}/decision", tags=["provenance"])
def decide(item_id: int, body: DecisionRequest, db: Db, _: Annotated[str, Depends(require_reviewer)]) -> dict[str, Any]:
    item = db.get(ReviewQueueItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Review item not found")
    db.add(ReviewDecision(queue_item_id=item.id, target_type=item.target_type, target_id=item.target_id, decision=body.decision, reviewer=body.reviewer, rationale=body.rationale))
    item.status = "RESOLVED" if body.decision in ("APPROVE", "REJECT") else "IN_REVIEW"
    if item.item_type == "SOURCE_CHANGE" and item.target_id:
        event = db.get(ChangeEvent, item.target_id)
        if event is not None:
            event.review_status = item.status
    audit(db, "review.decision", actor=body.reviewer, target_type="review_queue_item", target_id=item.id, decision=body.decision, rationale=body.rationale)
    db.commit()
    return {"id": item.id, "status": item.status, "decision": body.decision}


# --------------------------------------------------------------------------- reports & overview


@app.get("/api/reports/comparison", tags=["reports"], response_class=PlainTextResponse)
def report_comparison(db: Db, a: int = Query(gt=0), b: int = Query(gt=0)) -> PlainTextResponse:
    try:
        markdown = comparison_report_markdown(db, a, b)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit(db, "report.exported", actor="api", target_type="comparison", target_id=f"{a}:{b}")
    db.commit()
    return PlainTextResponse(markdown, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="incretinos-comparison-{a}-{b}.md"'})


@app.get("/api/reports/study/{nct}", tags=["reports"], response_class=PlainTextResponse)
def report_study(nct: str, db: Db) -> PlainTextResponse:
    _study_or_404(db, nct)
    return PlainTextResponse(study_report_markdown(db, nct), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="incretinos-{nct.upper()}.md"'})


@app.get("/api/overview", tags=["meta"])
def overview(db: Db) -> dict[str, Any]:
    drugs = [d for d in list_drugs(db) if d["tier_group"] != "COMPARATOR"]
    studies = list(db.scalars(select(Study)))
    gaps = sorted((_gap_dto(g) for g in db.scalars(select(EvidenceGap))), key=lambda g: -g["priority"]["score"])
    upcoming = sorted(
        [study_brief(s) for s in studies if not s.has_results and (s.overall_status or "") in ("ACTIVE_NOT_RECRUITING", "RECRUITING") and s.primary_completion_date],
        key=lambda s: s["primary_completion_date"],
    )[:8]
    unpublished = [study_brief(s) for s in studies if (s.overall_status or "") == "COMPLETED" and not s.has_results]
    return {
        "meta": meta(db),
        "drugs": drugs,
        "pipeline_by_status": dict(Counter(s.overall_status for s in studies)),
        "studies_by_program": dict(Counter(s.program or "Other" for s in studies)),
        "top_gaps": gaps[:6],
        "gaps_by_nature": dict(Counter(g["gap_nature"] for g in gaps)),
        "changes": list_changes(db)[:5],
        "upcoming_readouts": upcoming,
        "completed_without_registry_results": unpublished,
        "presets": compare_presets(db),
    }
