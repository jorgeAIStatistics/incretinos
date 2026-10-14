"""HTTP API.

Read endpoints are public (the product holds public data only). Endpoints that change
state — review decisions and gap triage — require a reviewer token and are disabled
entirely when no token is configured. Every external input is validated by a typed schema.

Three rules hold for every route, and tests/test_hardening.py checks them against the route table:

* No GET request and no request without the reviewer token can change the database: the session
  such a request receives refuses to commit (``db.get_db``). Usage worth recording goes to the
  ``incretinos.usage`` application log, never to the audit trail.
* A list endpoint returns ``{"total", "limit", "offset", "rows"}`` where ``total`` is the number of
  rows that match the filters, not the size of the page, and the order is deterministic.
* Identifiers are bounded (``DbId``), so an oversized number is a 422 and never reaches the driver.

Evidence a reviewer rejected is hidden from every query made through the request session
(``incretinos.db``), so the analytic endpoints below never use it. The two inspection surfaces
opt in explicitly: the evidence review API (``routers/review.py``) and a trial's own page.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__
from ..assistant.answer import ask
from ..assistant.providers import get_provider, provider_status
from ..config import describe_settings_sources, get_settings
from ..db import INCLUDE_REJECTED, get_db
from ..engines import benefit_risk, design, stats
from ..engines.comparability import compare_findings, harmonize_dose
from ..engines.estimands import ESTIMAND_EXPLAINER
from ..engines.findings import (
    AE_KIND_LABEL,
    CATEGORY_LABEL,
    DISPLAY_RULE,
    ae_dto,
    analysis_dto,
    arm_stratum,
    baseline_class,
    build_finding,
    derived_interval,
    display_lead,
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
from ..enums import TIER_LABEL, EndpointCategory, EstimandLabel, EvidenceTier
from ..ingestion.scheduler import start_scheduler
from ..ingestion.snapshots import audit
from ..models import (
    REVIEWABLE_MODELS,
    AdverseEvent,
    AuditEvent,
    ChangeEvent,
    DiscoveredTrial,
    Dose,
    Drug,
    Endpoint,
    Estimand,
    EvidenceClaim,
    EvidenceGap,
    EvidenceSource,
    ExposureEquivalence,
    IngestionRun,
    OutcomeResult,
    Publication,
    ReviewDecision,
    ReviewQueueItem,
    SourceSnapshot,
    StatisticalAnalysis,
    Study,
    StudyArm,
    StudyDrug,
    StudyPublication,
)
from ..reports.export import comparison_report_markdown, study_report_markdown
from ..review.policy import STATUS_LABEL, canonical_status, is_usable, reviewer_problem
from ..vocab import norm_arm_label
from .auth import require_reviewer
from .limits import limit_assistant, limit_simulation
from .routers.review import router as review_router

settings = get_settings()
log = logging.getLogger(__name__)

# Anonymous activity (comparisons run, questions asked, reports exported) is logged here and nowhere else.
# The audit trail in the database holds only reviewer decisions and ingestion events.
usage_log = logging.getLogger("incretinos.usage")
_log = logging.getLogger("incretinos.api")


def _configure_logging() -> None:
    """Send the application's own records (usage, scheduler, start-up) to stderr at INFO.

    A web server configures only its own loggers, so without this the usage log would be discarded. Nothing is
    changed when the host process has already set up logging.
    """
    logger = logging.getLogger("incretinos")
    if not logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = get_settings()
    _configure_logging()
    _log.info(describe_settings_sources())
    _log.info(
        "write access: %s; API documentation: %s",
        "reviewer token configured" if cfg.reviewer_token else "none (read-only: no reviewer token is configured)",
        "served at /docs" if cfg.docs_enabled else "not served (set INCRETINOS_DOCS_ENABLED=true to serve it)",
    )
    scheduler = start_scheduler(cfg)
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
    # The interactive documentation and the schema are served only when INCRETINOS_DOCS_ENABLED is true.
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "X-Reviewer-Token"],
)

# The API answers with JSON or a Markdown download, never with a page: it needs no scripts, frames or subresources.
_API_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-site",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}
_DOCS_PATHS = ("/docs", "/redoc")


class SecurityHeadersMiddleware:
    """Adds the headers above to every HTTP response, including errors and CORS preflights."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The documentation pages load their viewer from a CDN, so they keep every header except the content policy.
        is_docs = scope["path"].startswith(_DOCS_PATHS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _API_SECURITY_HEADERS.items():
                    if not (is_docs and name == "Content-Security-Policy"):
                        headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)

app.include_router(review_router)


def _json_safe(value: Any) -> Any:
    """A validation error echoes the rejected input. NaN and infinities are not valid JSON, so they are spelled out."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, BaseException):
        return str(value)
    return value


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    """422 in FastAPI's usual shape. Without this, a body containing NaN or Infinity is rejected by validation but the
    error report itself cannot be serialised, and the client receives a 500 instead of the reason."""
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(_json_safe(exc.errors()))})


Db = Annotated[Session, Depends(get_db)]
NCT = Annotated[str, Field(pattern=r"^[Nn][Cc][Tt]\d{8}$")]
# Largest identifier both databases can hold (PostgreSQL INTEGER). Use this type for every id that reaches a query:
# an unbounded int overflows the SQLite driver (OverflowError) and the PostgreSQL integer type, and both surface as 500.
MAX_DB_ID = 2**31 - 1
DbId = Annotated[int, Field(ge=1, le=MAX_DB_ID)]  # in request bodies
PathId = Annotated[int, Path(ge=1, le=MAX_DB_ID)]
QueryId = Annotated[int, Query(ge=1, le=MAX_DB_ID)]
MAX_OFFSET = 1_000_000
Offset = Annotated[int, Query(ge=0, le=MAX_OFFSET, description="Number of matching rows to skip.")]
Filter = Annotated[str | None, Query(max_length=120)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _page(rows: list[Any], total: int, limit: int, offset: int, **extra: Any) -> dict[str, Any]:
    """The one shape of a list response. ``total`` counts every row matching the filters, not the rows returned."""
    return {"total": total, "limit": limit, "offset": offset, "rows": rows, **extra}


def _known(value: str | None, allowed, what: str) -> None:
    """A misspelt filter value must not read as 'no evidence': it is an error, not an empty list."""
    if value is not None and value not in allowed:
        raise HTTPException(status_code=422, detail=f"Unknown {what} '{value[:60]}'. Known values: {', '.join(sorted(allowed))}")


# Every number a calculation endpoint accepts is finite and of plausible magnitude. NaN, Infinity and values
# beyond these bounds are rejected with a 422 before any arithmetic runs.
MAX_INPUT_MAGNITUDE = 1e6
Real = Annotated[float, Field(allow_inf_nan=False, ge=-MAX_INPUT_MAGNITUDE, le=MAX_INPUT_MAGNITUDE)]
PositiveReal = Annotated[float, Field(allow_inf_nan=False, gt=0, le=MAX_INPUT_MAGNITUDE)]
Proportion = Annotated[float, Field(allow_inf_nan=False, ge=0, lt=1)]
Share = Annotated[float, Field(allow_inf_nan=False, ge=0, le=1)]


@contextmanager
def calculation() -> Iterator[None]:
    """Turn every refusal or numerical failure of a calculation into a 422 with a clear message — never a 500."""
    try:
        yield
    except (StatsInputError, InvalidInferenceError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ArithmeticError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"The calculation is not defined for these inputs ({type(exc).__name__}). No result is returned.") from exc


def _finite_result(value: Any, path: str = "result") -> Any:
    """Refuse to return a calculation whose output contains NaN or an infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        raise HTTPException(status_code=422, detail=f"The calculation did not produce a finite number for these inputs ({path}). No result is returned.")
    if isinstance(value, dict):
        for key, item in value.items():
            _finite_result(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _finite_result(item, f"{path}[{index}]")
    return value


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
    data["derived_interval"] = derived_interval(f)
    return data


# Eager loading. These change how many queries a response costs, never what it contains.
_STUDY_BRIEF = (joinedload(Study.population), selectinload(Study.drug_links).joinedload(StudyDrug.drug))
_FINDING_PARENTS = (
    selectinload(OutcomeResult.study).joinedload(Study.population),
    # Every arm of the study: arm identity maps a reporting label to a registered arm group (findings.resolve_arm).
    selectinload(OutcomeResult.study).selectinload(Study.arms),
    selectinload(OutcomeResult.endpoint),
    selectinload(OutcomeResult.arm),
    selectinload(OutcomeResult.estimand).selectinload(Estimand.strategies),
    selectinload(OutcomeResult.source),
)
_ANALYSIS_PARENTS = (
    # The study's arm-level results too: the direction of a contrast is checked against them (findings.analysis_direction).
    selectinload(StatisticalAnalysis.study).selectinload(Study.results),
    selectinload(StatisticalAnalysis.endpoint),
    selectinload(StatisticalAnalysis.estimand),
    selectinload(StatisticalAnalysis.source),
)


def _prime_study(study: Study) -> tuple[Any, ...]:
    """Load one study's endpoints, arms and estimands in three queries.

    The engines look up ``result.endpoint``, ``result.arm`` and ``result.estimand`` row by row; once the parents
    are in the session those lookups are identity-map hits instead of one query each.
    """
    return (study.endpoints, study.arms, [e.strategies for e in study.estimands])


def _prime_studies(db: Session) -> list[Study]:
    """Every study with its population and drug links (the engines scan all studies to find head-to-head trials)."""
    return list(db.scalars(select(Study).options(*_STUDY_BRIEF).order_by(Study.id)))


def _prime_evidence(db: Session) -> tuple[Any, ...]:
    """The same for whole-database passes: the parent tables are small (a few hundred rows each).

    The caller must keep the returned value alive while it works, because the session only holds weak references.
    """
    return (
        _prime_studies(db),
        db.scalars(select(Endpoint)).all(),
        db.scalars(select(StudyArm)).all(),
        db.scalars(select(Estimand).options(selectinload(Estimand.strategies))).all(),
        db.scalars(select(EvidenceSource)).all(),
    )


# --------------------------------------------------------------------------- meta


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


def _review_counts(db: Session) -> dict[str, Any]:
    """Verification status of all reviewable evidence, by type and in total (rejected rows are counted here)."""
    by_type: dict[str, dict[str, int]] = {}
    total: Counter[str] = Counter()
    for evidence_type, model in REVIEWABLE_MODELS.items():
        rows = db.execute(select(model.verification_status, func.count()).group_by(model.verification_status).execution_options(**{INCLUDE_REJECTED: True})).all()
        by_type[evidence_type] = {canonical_status(status) or status: n for status, n in rows}
        total.update(by_type[evidence_type])
    return {
        "by_status": {status: total.get(status, 0) for status in STATUS_LABEL},
        "by_type": by_type,
        "decisions_recorded": db.scalar(select(func.count()).select_from(ReviewDecision).where(ReviewDecision.evidence_type.is_not(None))),
        "needs_review": total.get("PENDING_HUMAN_REVIEW", 0) + total.get("SOURCE_CHANGED_RECHECK_REQUIRED", 0),
    }


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
    review = _review_counts(db)
    counts["results_rejected"] = review["by_type"].get("result", {}).get("REJECTED", 0)
    tiers = Counter(t for (t,) in db.execute(select(OutcomeResult.tier)))
    return {
        "version": __version__,
        "mode": "OFFLINE (committed fixtures)" if cfg.offline else "LIVE",
        "last_ingestion": {"mode": last_run.mode, "finished_at": last_run.finished_at.isoformat() if last_run and last_run.finished_at else None} if last_run else None,
        "counts": counts,
        # Every stored result by verification status, rejected ones included (they are not in counts.results).
        "result_verification": review["by_type"].get("result", {}),
        "verification_labels": dict(STATUS_LABEL),
        "review": review,
        "result_tiers": dict(tiers),
        "tier_labels": {t.value: TIER_LABEL[t] for t in EvidenceTier},
        "llm": provider_status(cfg),
        "review_enabled": bool(cfg.reviewer_token),
        "notice": "Public data only. Research use by qualified researchers. Not medical advice, not a treatment recommendation, not a regulatory submission.",
    }


@app.get("/api/validation", tags=["meta"])
def validation(db: Db) -> dict[str, Any]:
    _loaded = _prime_evidence(db)  # noqa: F841  (kept alive for the duration of the rules)
    results = [r.as_dict() for r in run_all(db)]
    return {"passed": sum(r["passed"] for r in results), "total": len(results), "rules": results}


# --------------------------------------------------------------------------- drugs


def _drug_summary(db: Session, drug: Drug, n_studies: int | None = None) -> dict[str, Any]:
    cls = classify_drug([(m.target, m.action) for m in drug.mechanisms])
    if n_studies is None:
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


def _drug_rows(db: Session) -> list[dict[str, Any]]:
    """Every drug on record, core programme first."""
    order = {"CORE": 0, "ADJACENT": 1, "COMPARATOR": 2}
    drugs = sorted(db.scalars(select(Drug).options(selectinload(Drug.mechanisms), joinedload(Drug.regulatory_source))), key=lambda d: (order.get(d.tier_group, 9), d.id))
    n_studies = {drug_id: n for drug_id, n in db.execute(select(StudyDrug.drug_id, func.count()).group_by(StudyDrug.drug_id))}
    return [_drug_summary(db, d, n_studies.get(d.id, 0)) for d in drugs]


@app.get("/api/drugs", tags=["drugs"])
def list_drugs(db: Db, limit: int = Query(200, ge=1, le=500), offset: Offset = 0) -> dict[str, Any]:
    rows = _drug_rows(db)
    return _page(rows[offset : offset + limit], len(rows), limit, offset)


@app.get("/api/drugs/{slug}", tags=["drugs"])
def get_drug(slug: str, db: Db) -> dict[str, Any]:
    drug = _drug_or_404(db, slug)
    equivalences = []
    eq_dose = (joinedload(ExposureEquivalence.dose_a).joinedload(Dose.formulation), joinedload(ExposureEquivalence.dose_b).joinedload(Dose.formulation), joinedload(ExposureEquivalence.source))
    for eq in db.scalars(select(ExposureEquivalence).options(*eq_dose).order_by(ExposureEquivalence.id)):
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
    studies = db.scalars(select(Study).join(StudyDrug, StudyDrug.study_id == Study.id).where(StudyDrug.drug_id == drug.id).options(*_STUDY_BRIEF).order_by(StudyDrug.id)).all()
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
def list_studies(db: Db, drug: Filter = None, program: Filter = None, has_results: bool | None = None, q: Annotated[str | None, Query(max_length=120)] = None,
                 limit: int = Query(500, ge=1, le=1000), offset: Offset = 0) -> dict[str, Any]:
    matched = []
    options = (*_STUDY_BRIEF, selectinload(Study.publication_links).joinedload(StudyPublication.publication))
    # NULL programmes first on every database (SQLite's default; PostgreSQL would otherwise put them last).
    for study in db.scalars(select(Study).options(*options).order_by(Study.program.asc().nulls_first(), Study.display_name, Study.id)):
        brief = study_brief(study)
        if drug and drug not in [d["slug"] for d in brief["drugs"]]:
            continue
        if program and study.program != program:
            continue
        if has_results is not None and bool(study.has_results) != has_results:
            continue
        if q and q.lower() not in f"{study.display_name} {study.brief_title} {study.nct_id}".lower():
            continue
        matched.append((study, brief))
    page = matched[offset : offset + limit]
    ids = [study.id for study, _ in page]
    # Tier names and result counts come from two grouped queries, not from loading every result of every study.
    tiers: dict[int, set[str]] = {i: set() for i in ids}
    n_results: dict[int, int] = dict.fromkeys(ids, 0)
    for study_id, tier, n in db.execute(select(OutcomeResult.study_id, OutcomeResult.tier, func.count()).where(OutcomeResult.study_id.in_(ids)).group_by(OutcomeResult.study_id, OutcomeResult.tier)):
        tiers[study_id].add(tier)
        n_results[study_id] += n
    for study_id, tier in db.execute(select(StatisticalAnalysis.study_id, StatisticalAnalysis.tier).where(StatisticalAnalysis.study_id.in_(ids)).distinct()):
        tiers[study_id].add(tier)
    rows = []
    for study, brief in page:
        primary_pub = next((link.publication for link in study.publication_links if link.relation == "PRIMARY_RESULTS"), None)
        rows.append({**brief, "evidence_tiers": sorted(tiers[study.id]), "n_results": n_results[study.id], "n_publications": len(study.publication_links),
                     "primary_publication": {"pmid": primary_pub.pmid, "journal": primary_pub.journal, "year": primary_pub.year} if primary_pub else None})
    return _page(rows, len(matched), limit, offset)


@app.get("/api/discovered", tags=["studies"])
def discovered(db: Db, drug: Filter = None, q: Annotated[str | None, Query(max_length=120)] = None, limit: int = Query(300, ge=1, le=1000), offset: Offset = 0) -> dict[str, Any]:
    watched = {nct for (nct,) in db.execute(select(Study.nct_id))}
    rows = []
    # Trials without a completion date go last on every database (PostgreSQL would otherwise list them first).
    for trial in db.scalars(select(DiscoveredTrial).order_by(DiscoveredTrial.primary_completion_date.desc().nulls_last(), DiscoveredTrial.id)):
        if drug and drug not in (trial.drug_slugs or []):
            continue
        if q and q.lower() not in f"{trial.brief_title} {trial.acronym or ''} {trial.nct_id}".lower():
            continue
        rows.append({"nct_id": trial.nct_id, "brief_title": trial.brief_title, "acronym": trial.acronym, "phase": trial.phase, "overall_status": trial.overall_status, "has_results": trial.has_results,
                     "enrollment": trial.enrollment, "primary_completion_date": trial.primary_completion_date, "last_update_posted": trial.last_update_posted, "drug_slugs": trial.drug_slugs, "ingested_in_depth": trial.nct_id in watched})
    return _page(rows[offset : offset + limit], len(rows), limit, offset,
                 note="Registry index of Lilly-led trials matching tracked intervention terms (summary fields only). Trials marked 'ingested in depth' have full protocol and results extraction.")


@app.get("/api/studies/{nct}", tags=["studies"])
def get_study(nct: str, db: Db) -> dict[str, Any]:
    # Inspection surface: a trial's own tables list every stored row, rejected ones included, each with its verification status.
    db.info[INCLUDE_REJECTED] = True
    study = _study_or_404(db, nct)
    _loaded = _prime_study(study)  # noqa: F841
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
        "population": {"diabetes_status": population.diabetes_status, "diabetes_basis": population.diabetes_basis, "sex": population.sex, "min_age": population.min_age, "max_age": population.max_age, "summary": population.summary, "baseline": population.baseline[:120], "baseline_total": len(population.baseline)} if population else None,
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
    study: Filter = None,
    drug: Filter = None,
    category: Filter = None,
    estimand: Filter = None,
    tier: Filter = None,
    arm_level_only: bool = True,
    limit: int = Query(400, ge=1, le=2000),
    offset: Offset = 0,
) -> dict[str, Any]:
    _known(category, EndpointCategory._value2member_map_, "endpoint category")
    _known(estimand, EstimandLabel._value2member_map_, "estimand")
    _known(tier, EvidenceTier._value2member_map_, "evidence tier")
    # Pass 1: one narrow query decides which results match (the same rules as before, applied to a few columns
    # instead of to fully loaded rows), which gives the true total. Pass 2 loads only the requested page.
    query = (
        select(OutcomeResult.id, OutcomeResult.scope, OutcomeResult.arm_label, StudyArm.is_pooled, StudyArm.drug_slug, Estimand.id, Estimand.label)
        .join(Study, OutcomeResult.study_id == Study.id)
        .join(Endpoint, OutcomeResult.endpoint_id == Endpoint.id)
        .outerjoin(StudyArm, OutcomeResult.arm_id == StudyArm.id)
        .outerjoin(Estimand, OutcomeResult.estimand_id == Estimand.id)
    )
    if study:
        query = query.where(or_(Study.nct_id == study.upper(), Study.display_name == study))
    if category:
        query = query.where(Endpoint.category == category)
    if tier:
        query = query.where(OutcomeResult.tier == tier)
    matched: list[int] = []
    for result_id, scope, arm_label, arm_is_pooled, arm_drug_slug, estimand_id, estimand_label in db.execute(query.order_by(Study.display_name, Endpoint.id, OutcomeResult.id)):
        if arm_level_only and (scope != "ARM" or " minus " in arm_label or arm_is_pooled):
            continue
        if drug and arm_drug_slug != drug:
            continue
        if estimand and (estimand_label if estimand_id is not None else "NOT_STATED") != estimand:
            continue
        matched.append(result_id)
    page = matched[offset : offset + limit]
    loaded = {r.id: r for r in db.scalars(select(OutcomeResult).where(OutcomeResult.id.in_(page)).options(*_FINDING_PARENTS))}
    rows = [_compact(build_finding(db, loaded[result_id])) for result_id in page]
    return _page(rows, len(matched), limit, offset, category_labels=CATEGORY_LABEL)


@app.get("/api/findings/{result_id}", tags=["evidence"])
def finding_detail(result_id: PathId, db: Db) -> dict[str, Any]:
    finding = get_finding(db, result_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"finding": finding.as_dict(), "interpretation": interpret_finding(finding)}


@app.get("/api/analyses/{analysis_id}", tags=["evidence"])
def analysis_detail(analysis_id: PathId, db: Db) -> dict[str, Any]:
    analysis = db.get(StatisticalAnalysis, analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    dto = analysis_dto(analysis)
    return {"analysis": dto, "interpretation": interpret_analysis(dto)}


@app.get("/api/analyses", tags=["evidence"])
def list_analyses(db: Db, study: Filter = None, category: Filter = None, curated_only: bool = False, limit: int = Query(300, ge=1, le=1500), offset: Offset = 0) -> dict[str, Any]:
    _known(category, EndpointCategory._value2member_map_, "endpoint category")
    conditions = []
    if study:
        conditions.append(or_(Study.nct_id == study.upper(), Study.display_name == study))
    if category:
        conditions.append(Endpoint.category == category)
    if curated_only:
        conditions.append(StatisticalAnalysis.origin == "CURATED")
    joined = select(StatisticalAnalysis).join(Study, StatisticalAnalysis.study_id == Study.id).join(Endpoint, StatisticalAnalysis.endpoint_id == Endpoint.id).where(*conditions)
    total = db.scalar(select(func.count()).select_from(joined.subquery()))
    rows = []
    # The same order as /api/findings. Without an ORDER BY a page of a LIMIT query is whatever the database returns first.
    for a in db.scalars(joined.options(*_ANALYSIS_PARENTS).order_by(Study.display_name, Endpoint.id, StatisticalAnalysis.id).limit(limit).offset(offset)):
        dto = analysis_dto(a)
        rows.append({**dto, "interpretation": interpret_analysis(dto)})
    return _page(rows, total, limit, offset)


class CompareRequest(Strict):
    a: DbId
    b: DbId


@app.post("/api/compare", tags=["evidence"])
def compare(body: CompareRequest, db: Db) -> dict[str, Any]:
    if body.a == body.b:
        raise HTTPException(status_code=422, detail="Choose two different results: a result set beside itself is not a comparison.")
    a, b = get_finding(db, body.a), get_finding(db, body.b)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="One or both findings were not found")
    _loaded = _prime_studies(db)  # noqa: F841
    comparison = compare_findings(db, a, b)
    usage_log.info("comparison.run a=%s b=%s classification=%s", body.a, body.b, comparison.classification)
    return {"a": a.as_dict(), "b": b.as_dict(), "interpretation_a": interpret_finding(a), "interpretation_b": interpret_finding(b), "comparison": comparison.as_dict(),
            "analysis_interpretations": [interpret_analysis(x) for x in comparison.reported_analyses],
            "other_analysis_interpretations": [interpret_analysis(x) for x in comparison.other_analyses]}


# ---- worked comparison presets: resolved by scientific description, then checked against their own titles


@dataclass(frozen=True)
class PresetSide:
    """One result described scientifically. Every stated attribute must hold for the stored result."""

    nct: str
    drug: str | None = None  # drug slug of the arm
    dose: float | None = None  # parsed single dose of the arm, in its stored unit
    placebo: bool = False
    arm_label: str | None = None  # exact (normalised) arm label, for combination arms that have no single dose
    estimand: str | None = None
    tier: str | None = None
    weeks: float | None = None
    baseline: str | None = None  # baseline class
    role: str | None = None  # endpoint role
    diabetes: str | None = None  # diabetes status of the trial population
    category: str = "WEIGHT_PCT_CHANGE"


@dataclass(frozen=True)
class PresetSpec:
    title: str
    question: str
    a: PresetSide
    b: PresetSide
    relationship: str  # RANDOMIZED_ARMS_OF_ONE_TRIAL | SAME_ARM_TWO_REPORTS | DIFFERENT_TRIALS
    analysis: str | None = None  # "CI" = an exactly matching between-arm analysis with an interval; "CI_NO_P" = … and no p-value


def _side_problems(side: PresetSide, f: Any) -> list[str]:
    """What a resolved finding fails to match in its description ([] = it is what the title says)."""
    problems = []
    if f.nct_id != side.nct:
        problems.append(f"trial is {f.nct_id}, not {side.nct}")
    if side.placebo != bool(f.is_placebo):
        problems.append("placebo flag differs")
    if side.drug is not None and f.drug_slug != side.drug:
        problems.append(f"arm drug is {f.drug_slug}, not {side.drug}")
    if side.dose is not None and f.dose_amount != side.dose:
        problems.append(f"arm dose is {f.dose_amount}, not {side.dose}")
    if side.arm_label is not None and norm_arm_label(f.arm_label) != norm_arm_label(side.arm_label):
        problems.append(f"arm is '{f.arm_label}', not '{side.arm_label}'")
    if side.estimand is not None and f.estimand_label != side.estimand:
        problems.append(f"estimand is {f.estimand_label}, not {side.estimand}")
    if side.tier is not None and f.tier != side.tier:
        problems.append(f"tier is {f.tier}, not {side.tier}")
    if side.weeks is not None and f.timepoint_weeks != side.weeks:
        problems.append(f"week is {f.timepoint_weeks}, not {side.weeks}")
    if side.baseline is not None and f.baseline_class != side.baseline:
        problems.append(f"baseline is {f.baseline_class}, not {side.baseline}")
    if side.role is not None and f.role != side.role:
        problems.append(f"endpoint role is {f.role}, not {side.role}")
    if side.diabetes is not None and f.diabetes_status != side.diabetes:
        problems.append(f"diabetes status is {f.diabetes_status}, not {side.diabetes}")
    if f.category != side.category or f.scope != "ARM" or f.is_pooled or f.value is None or f.randomization_stratum:
        problems.append("not a single-arm value of the main randomization on the stated endpoint")
    return problems


def _preset_candidates(db: Session, side: PresetSide) -> list[OutcomeResult]:
    study = db.scalar(select(Study).where(Study.nct_id == side.nct))
    if study is None:
        return []
    out = []
    for r in sorted(study.results, key=lambda x: x.id):
        arm = r.arm
        if r.endpoint.category != side.category or r.value is None or r.scope != "ARM" or arm is None or arm.is_pooled or " minus " in r.arm_label or not is_usable(r.verification_status):
            continue
        if arm_stratum(arm.label) or bool(arm.is_placebo) != side.placebo:
            continue
        if side.drug is not None and arm.drug_slug != side.drug:
            continue
        if side.dose is not None and arm.dose_amount != side.dose:
            continue
        if side.arm_label is not None and norm_arm_label(r.arm_label) != norm_arm_label(side.arm_label):
            continue
        if side.estimand is not None and (r.estimand.label if r.estimand else "NOT_STATED") != side.estimand:
            continue
        if side.tier is not None and r.tier != side.tier:
            continue
        if side.weeks is not None and r.timepoint_weeks != side.weeks:
            continue
        if side.baseline is not None and baseline_class(study.design_features, r.baseline_definition) != side.baseline:
            continue
        if side.role is not None and r.endpoint.role != side.role:
            continue
        out.append(r)
    return out


def _resolve_preset(db: Session, spec: PresetSpec) -> dict[str, Any] | None:
    """Resolve a preset to two stored results and check that the pair is what its title says. None = not offered."""
    pairs = [(ra, rb) for ra in _preset_candidates(db, spec.a) for rb in _preset_candidates(db, spec.b) if ra.id != rb.id]
    if not pairs:
        log.warning("Comparison preset not offered (no stored results match its description): %s", spec.title)
        return None
    # Within one trial, two values of one outcome measure go together; otherwise the records stored first are used.
    ra, rb = min(pairs, key=lambda pair: (0 if pair[0].endpoint_id == pair[1].endpoint_id else 1, pair[0].id, pair[1].id))
    fa, fb = build_finding(db, ra), build_finding(db, rb)
    comparison = compare_findings(db, fa, fb)
    problems = [f"A: {x}" for x in _side_problems(spec.a, fa)] + [f"B: {x}" for x in _side_problems(spec.b, fb)]
    if fa.result_id == fb.result_id:
        problems.append("both sides resolve to the same result")
    if comparison.relationship != spec.relationship:
        problems.append(f"the two groups are {comparison.relationship}, not {spec.relationship}")
    if spec.analysis is not None:
        exact = comparison.reported_analyses
        if not exact or exact[0]["ci_low"] is None:
            problems.append("no exactly matching between-arm analysis with a confidence interval is stored")
        elif spec.analysis == "CI_NO_P" and exact[0]["p_value_text"]:
            problems.append("the matching analysis does report a p-value")
    if problems:
        log.warning("Comparison preset not offered (it is not what its title says): %s — %s", spec.title, "; ".join(problems))
        return None

    def described(f: Any) -> dict[str, Any]:
        return {"result_id": f.result_id, "study": f.study_name, "nct_id": f.nct_id, "arm": f.arm_label, "estimand": f.estimand_label, "timepoint_weeks": f.timepoint_weeks,
                "baseline_class": f.baseline_class, "tier": f.tier, "diabetes_status": f.diabetes_status, "value": f.value, "unit": f.unit}

    return {"title": spec.title, "question": spec.question, "a": fa.result_id, "b": fb.result_id, "classification": comparison.classification,
            "relationship": comparison.relationship, "verified": True, "resolved": {"a": described(fa), "b": described(fb)}}


_SM1, _ATTAIN1, _TRIUMPH1, _SM4, _ELORA, _SM5 = "NCT04184622", "NCT05869903", "NCT05929066", "NCT04660643", "NCT06603571", "NCT05822830"
_SM1_15_EFFICACY = PresetSide(_SM1, drug="tirzepatide", dose=15, estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72, diabetes="NO_T2D")
_ELORA_COMBO = PresetSide(_ELORA, arm_label="eloralintide 9 mg + tirzepatide 15 mg", estimand="EFFICACY", tier="PRESS_RELEASE", weeks=48, diabetes="T2D")
PRESETS: tuple[PresetSpec, ...] = (
    PresetSpec("Tirzepatide 15 mg vs placebo within SURMOUNT-1", "Randomized arms of one trial, registry-posted analysis with confidence interval.",
               _SM1_15_EFFICACY, PresetSide(_SM1, placebo=True, drug="placebo", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72),
               "RANDOMIZED_ARMS_OF_ONE_TRIAL", analysis="CI"),
    PresetSpec("Retatrutide 12 mg (TRIUMPH-1) vs tirzepatide 15 mg (SURMOUNT-1)", "Different trials, same estimand: is this head-to-head evidence?",
               PresetSide(_TRIUMPH1, drug="retatrutide", dose=12, estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=80),
               PresetSide(_SM1, drug="tirzepatide", dose=15, estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72), "DIFFERENT_TRIALS"),
    PresetSpec("ATTAIN-1 36 mg: efficacy estimand vs treatment-regimen estimand", "Same arm, two estimands — two answers to two different questions.",
               PresetSide(_ATTAIN1, drug="orforglipron", dose=36, estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72),
               PresetSide(_ATTAIN1, drug="orforglipron", dose=36, estimand="TREATMENT_REGIMEN", tier="PEER_REVIEWED", weeks=72), "SAME_ARM_TWO_REPORTS"),
    PresetSpec("Orforglipron 36 mg (ATTAIN-1) vs tirzepatide 15 mg (SURMOUNT-1)", "Two placebo-controlled Phase 3 trials without diabetes, same inferred estimand and duration.",
               PresetSide(_ATTAIN1, drug="orforglipron", dose=36, estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72, diabetes="NO_T2D"), _SM1_15_EFFICACY, "DIFFERENT_TRIALS"),
    PresetSpec("SURMOUNT-4 (from randomization at week 36) vs SURMOUNT-1 (from baseline)", "Randomized-withdrawal baseline versus study-entry baseline.",
               PresetSide(_SM4, drug="tirzepatide", estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=88, baseline="RANDOMIZATION_AFTER_LEAD_IN"),
               PresetSide(_SM1, drug="tirzepatide", dose=15, estimand="EFFICACY", tier="REGISTRY_RESULTS", weeks=72, baseline="STUDY_BASELINE"), "DIFFERENT_TRIALS"),
    PresetSpec("EloraTZP 9 mg + 15 mg vs tirzepatide 15 mg within the Phase 2b trial", "Randomized within one Phase 2 trial in type 2 diabetes; press-release topline only.",
               _ELORA_COMBO, PresetSide(_ELORA, drug="tirzepatide", dose=15, estimand="EFFICACY", tier="PRESS_RELEASE", weeks=48, diabetes="T2D"), "RANDOMIZED_ARMS_OF_ONE_TRIAL"),
    PresetSpec("EloraTZP Phase 2b (T2D, 48 weeks) vs tirzepatide 15 mg in SURMOUNT-1 (no T2D, 72 weeks)", "Different populations, durations, phases and evidence tiers.",
               _ELORA_COMBO, _SM1_15_EFFICACY, "DIFFERENT_TRIALS"),
    PresetSpec("Tirzepatide vs semaglutide within SURMOUNT-5", "Head-to-head randomized trial; registry reports a difference with CI but no p-value.",
               PresetSide(_SM5, drug="tirzepatide", tier="REGISTRY_RESULTS", role="PRIMARY"), PresetSide(_SM5, drug="semaglutide", tier="REGISTRY_RESULTS", role="PRIMARY"),
               "RANDOMIZED_ARMS_OF_ONE_TRIAL", analysis="CI_NO_P"),
)


@app.get("/api/compare/presets", tags=["evidence"])
def compare_presets(db: Db) -> list[dict[str, Any]]:
    """Worked examples. Each is resolved from the live database by its scientific description and offered only
    after the resolved pair has been checked to be what the title says (two different results, the stated arms,
    estimands, time points and tiers, and the stated relationship between the groups)."""
    _loaded = _prime_evidence(db)  # noqa: F841  (parents loaded once; the engines then look them up without a query each)
    return [preset for preset in (_resolve_preset(db, spec) for spec in PRESETS) if preset is not None]


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
def estimand_inspector(db: Db, study: Annotated[str, Query(max_length=40)]) -> dict[str, Any]:
    """Results for the same arm and endpoint laid out by estimand, so the two are never merged."""
    s = _study_or_404(db, study)
    _loaded = _prime_study(s)  # noqa: F841
    table: dict[tuple, dict[str, Any]] = {}
    for r in s.results:
        if r.endpoint.category not in ("WEIGHT_PCT_CHANGE", "HBA1C_CHANGE", "WEIGHT_ABS_CHANGE") or r.value is None or r.scope != "ARM" or " minus " in r.arm_label or r.arm is None or r.arm.is_pooled:
            continue
        arm_key = "placebo" if r.arm.is_placebo else (f"{r.arm.drug_slug} {r.arm.dose_amount:g} {r.arm.dose_unit or 'mg'}" if r.arm.dose_amount is not None else r.arm.label)
        key = (r.endpoint.category, arm_key, r.timepoint_weeks, baseline_class(s.design_features, r.baseline_definition))
        cell = table.setdefault(key, {"category": r.endpoint.category, "category_label": CATEGORY_LABEL.get(r.endpoint.category), "arm": arm_key, "timepoint_weeks": r.timepoint_weeks, "baseline_definition": r.baseline_definition, "by_estimand": {}})
        label = r.estimand.label if r.estimand else "NOT_STATED"
        cell["by_estimand"].setdefault(label, []).append(_compact(build_finding(db, r)))
    rows = sorted(table.values(), key=lambda c: (c["category"], c["timepoint_weeks"] or 0, c["arm"] != "placebo", c["arm"]))
    for cell in rows:
        # Where several sources report one arm, endpoint, time point and estimand, all stay listed; the result id that a
        # chart should draw first comes from the one shared display rule (never from the evidence tier).
        cell["display_lead"] = {}
        for label, findings in cell["by_estimand"].items():
            lead, _others = display_lead(findings)
            if lead is not None:
                cell["display_lead"][label] = lead["result_id"]
    return {
        "study": study_brief(s),
        "display_rule": DISPLAY_RULE,
        "estimands": [{"label": e.label, "basis": e.basis, "description": e.description, "quote": e.quote, "explainer": ESTIMAND_EXPLAINER.get(e.label), "strategies": [{"event": x.event, "strategy": x.strategy, "basis": x.basis, "quote": x.quote} for x in e.strategies], "source": source_ref(e.source)} for e in s.estimands],
        "rows": rows,
        "explainers": ESTIMAND_EXPLAINER,
        "rule": "Values under different estimands are shown in separate columns and are never averaged, differenced or substituted for one another.",
    }


# --------------------------------------------------------------------------- benefit–risk


@app.get("/api/benefit-risk/{nct}", tags=["benefit-risk"])
def benefit_risk_profiles(nct: str, db: Db, estimand: Literal["EFFICACY", "TREATMENT_REGIMEN"] | None = None, weeks: float | None = Query(None, gt=0, le=600)) -> dict[str, Any]:
    _study_or_404(db, nct)
    with calculation():
        return benefit_risk.arm_profiles(db, nct, estimand, weeks)


class Criterion(Strict):
    weight: float = Field(ge=0, le=100, allow_inf_nan=False)
    best: Real
    worst: Real


class UtilityRequest(Strict):
    nct: NCT
    estimand: Literal["EFFICACY", "TREATMENT_REGIMEN"] | None = None
    weeks: float | None = Field(None, gt=0, le=600, allow_inf_nan=False)
    criteria: dict[Annotated[str, Field(min_length=1, max_length=60)], Criterion] | None = Field(None, max_length=20)


@app.post("/api/benefit-risk/utility", tags=["benefit-risk"])
def utility(body: UtilityRequest, db: Db) -> dict[str, Any]:
    _study_or_404(db, body.nct)
    with calculation():
        profiles = benefit_risk.arm_profiles(db, body.nct, body.estimand, body.weeks)
        return _finite_result(benefit_risk.utility_scores(profiles, {k: v.model_dump() for k, v in body.criteria.items()} if body.criteria else None))


class DoseScenarioRequest(Strict):
    nct: NCT
    estimand: Literal["EFFICACY", "TREATMENT_REGIMEN"] | None = None
    weeks: float | None = Field(None, gt=0, le=600, allow_inf_nan=False)
    metric: Literal["weight_pct", "weight_abs", "hba1c"] = "weight_pct"
    shares: dict[Annotated[str, Field(min_length=1, max_length=400)], Share] = Field(max_length=50)
    non_persistent_share: Share = 0.0
    non_persistent_value: Real | None = None

    @model_validator(mode="after")
    def _something_to_mix(self) -> DoseScenarioRequest:
        if not any(share > 0 for share in self.shares.values()):
            raise ValueError("at least one arm needs a share above 0: a scenario made only of an assumed value says nothing about any studied dose")
        return self


@app.post("/api/benefit-risk/dose-scenario", tags=["benefit-risk"])
def dose_scenario(body: DoseScenarioRequest, db: Db) -> dict[str, Any]:
    _study_or_404(db, body.nct)
    with calculation():
        profiles = benefit_risk.arm_profiles(db, body.nct, body.estimand, body.weeks)
        return _finite_result(benefit_risk.dose_scenario(profiles, body.shares, body.metric, body.non_persistent_share, body.non_persistent_value))


class RiskContrastRequest(Strict):
    ae_a: DbId
    ae_b: DbId


@app.post("/api/stats/risk-contrast", tags=["benefit-risk"])
def risk_contrast(body: RiskContrastRequest, db: Db) -> dict[str, Any]:
    """Risk difference and ratio for ONE adverse event in TWO different randomized arms of one trial (A − B)."""
    a, b = db.get(AdverseEvent, body.ae_a), db.get(AdverseEvent, body.ae_b)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="Adverse-event row not found")
    with calculation():
        contrast = _finite_result(benefit_risk.adverse_event_contrast(db, a, b))
    return {"a": ae_dto(a), "b": ae_dto(b), "a_statement": describe_adverse_event(ae_dto(a)), "b_statement": describe_adverse_event(ae_dto(b)), **contrast}


# --------------------------------------------------------------------------- safety


@app.get("/api/safety/trial-ae", tags=["safety"])
def safety_trial_ae(db: Db, drug: Filter = None, nct: Filter = None, measure_kind: Filter = None, term: Annotated[str | None, Query(max_length=80)] = None,
                    limit: int = Query(1500, ge=1, le=5000), offset: Offset = 0) -> dict[str, Any]:
    if measure_kind and measure_kind not in AE_KIND_LABEL:
        raise HTTPException(status_code=422, detail="Unknown measure kind")
    table = trial_ae_table(db, drug, nct, measure_kind, term, limit, offset)
    return _page(table.pop("rows"), table.pop("total"), limit, offset, **table)


@app.get("/api/safety/terms", tags=["safety"])
def safety_terms(db: Db, nct: Annotated[str, Query(max_length=40)], limit: int = Query(300, ge=1, le=5000), offset: Offset = 0) -> dict[str, Any]:
    """One entry per term and measure kind. A count the source does not report is null, never 0.

    Every posted serious-event term is stored, so a study can have far more terms than ``limit``: the response
    says how many exist (``total``, also ``total_terms``) and whether terms remain after this page (``truncated``). ``tables`` tells whether the term comes from the registry's
    serious-event table, its non-serious table, or both (their rows are different counts and carry ``is_serious``).
    """
    study = _study_or_404(db, nct)
    terms: dict[tuple[str, str], dict[str, Any]] = {}
    for ae in study.adverse_events:
        entry = terms.setdefault((ae.term, ae.measure_kind), {"categories": set(), "serious": set(), "count": None})
        entry["categories"].add(ae.category)
        entry["serious"].add(bool(ae.is_serious))
        if ae.n_affected is not None:  # an unreported count stays unknown; it is not counted as zero
            entry["count"] = ae.n_affected if entry["count"] is None else max(entry["count"], ae.n_affected)
    rows = [
        {
            "term": t,
            "measure_kind": k,
            "measure_kind_label": AE_KIND_LABEL.get(k, k),
            "category": sorted(e["categories"])[0] if len(e["categories"]) == 1 else None,
            "categories": sorted(e["categories"]),
            "is_serious": next(iter(e["serious"])) if len(e["serious"]) == 1 else None,
            "tables": [name for flag, name in ((True, "SERIOUS"), (False, "NOT_SERIOUS")) if flag in e["serious"]],
            # None = no count is reported for this term in any arm (for example a term reported only as a percentage).
            "max_participants_affected": e["count"],
            "count_status": "REPORTED" if e["count"] is not None else "NOT_REPORTED",
        }
        for (t, k), e in terms.items()
    ]
    # Terms with a reported count first (largest first); terms without any reported count follow, by name. An absent
    # count is not ranked as a small count: such terms are a separate block, and when the list is longer than one page
    # the response says so (``truncated``) and the block is reached by paging.
    rows.sort(key=lambda r: (r["max_participants_affected"] is None, -(r["max_participants_affected"] or 0), r["term"].lower(), r["term"], r["measure_kind"]))
    page = rows[offset : offset + limit]
    # "terms" is the original name of the list; "rows" is the same list under the name every list endpoint uses.
    return _page(page, len(rows), limit, offset, study=study_brief(study), terms=page, total_terms=len(rows), truncated=offset + len(page) < len(rows))


@app.get("/api/safety/spontaneous/{slug}", tags=["safety"])
def safety_spontaneous(slug: str, db: Db) -> dict[str, Any]:
    return spontaneous_overview(db, _drug_or_404(db, slug))


MAX_REPORT_COUNT = 10**12


class DisproportionalityRequest(Strict):
    drug_event: int = Field(ge=0, le=MAX_REPORT_COUNT)
    drug_total: int = Field(ge=0, le=MAX_REPORT_COUNT)
    event_total: int = Field(ge=0, le=MAX_REPORT_COUNT)
    all_total: int = Field(ge=0, le=MAX_REPORT_COUNT)


@app.post("/api/safety/disproportionality", tags=["safety"])
def safety_disproportionality(body: DisproportionalityRequest) -> dict[str, Any]:
    with calculation():
        return _finite_result(disproportionality(body.drug_event, body.drug_total, body.event_total, body.all_total))


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
def list_gaps(db: Db, drug: Filter = None, category: Filter = None, nature: Filter = None,
              w_importance: float = Query(DEFAULT_WEIGHTS["clinical_importance"], ge=0, le=1), w_uncertainty: float = Query(DEFAULT_WEIGHTS["uncertainty"], ge=0, le=1),
              w_tractability: float = Query(DEFAULT_WEIGHTS["tractability"], ge=0, le=1), w_actionability: float = Query(DEFAULT_WEIGHTS["actionability"], ge=0, le=1),
              limit: int = Query(500, ge=1, le=1000), offset: Offset = 0) -> dict[str, Any]:
    _known(nature, NATURE_LABEL, "gap nature")
    weights = {"clinical_importance": w_importance, "uncertainty": w_uncertainty, "tractability": w_tractability, "actionability": w_actionability}
    rows = []
    for gap in db.scalars(select(EvidenceGap).options(joinedload(EvidenceGap.drug)).order_by(EvidenceGap.id)):
        if drug and (gap.drug is None or gap.drug.slug != drug):
            continue
        if category and gap.category != category:
            continue
        if nature and gap.gap_nature != nature:
            continue
        rows.append(_gap_dto(gap, weights))
    rows.sort(key=lambda g: -g["priority"]["score"])
    return _page(rows[offset : offset + limit], len(rows), limit, offset, weights=weights, default_weights=DEFAULT_WEIGHTS, nature_labels=NATURE_LABEL,
                 categories=sorted({g["category"] for g in rows}), note="A gap in the public record is not confirmation that the sponsor has no data. Each gap states why it is open.")


class GapReview(Strict):
    review_status: Literal["OPEN", "IN_REVIEW", "CONFIRMED", "DISMISSED", "RESOLVED"]
    reviewer_note: str | None = Field(None, max_length=2000)
    clinical_importance: int | None = Field(None, ge=1, le=5)
    reviewer: str = Field(min_length=2, max_length=120)


@app.patch("/api/gaps/{gap_id}", tags=["gaps"])
def review_gap(gap_id: PathId, body: GapReview, db: Db, _: Annotated[str, Depends(require_reviewer)]) -> dict[str, Any]:
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
def design_coverage(db: Db, drug: Filter = None) -> dict[str, Any]:
    # Everything the matrix reads, loaded in a handful of queries; the engine then finds it in the session.
    options = (*_STUDY_BRIEF, selectinload(Study.endpoints), selectinload(Study.arms), selectinload(Study.inclusion), selectinload(Study.exclusion))
    _loaded = db.scalars(select(Study).options(*options)).unique().all()  # noqa: F841
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
    alpha: float = Field(0.05, gt=0, lt=0.5, allow_inf_nan=False)
    power: float = Field(0.9, gt=0.5, lt=1, allow_inf_nan=False)
    ratio: float = Field(1.0, gt=0, le=10, allow_inf_nan=False)
    delta: Real | None = None
    sd: PositiveReal | None = None
    p1: float | None = Field(None, gt=0, lt=1, allow_inf_nan=False)
    p2: float | None = Field(None, gt=0, lt=1, allow_inf_nan=False)
    margin: PositiveReal | None = None
    true_difference: Real = 0.0
    hazard_ratio: float | None = Field(None, gt=0, le=1000, allow_inf_nan=False)
    true_hazard_ratio: float = Field(1.0, gt=0, le=1000, allow_inf_nan=False)
    attrition: Proportion = 0.0


@app.post("/api/design/power", tags=["design"])
def design_power(body: PowerRequest) -> dict[str, Any]:
    with calculation():
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
        if body.attrition and "n_total" in result:
            result["with_attrition"] = stats.inflate_for_attrition(result["n_total"], body.attrition)
    result["kind"] = "CALCULATION"
    result["disclaimer"] = "A design calculation under the stated assumptions. It is not a forecast of the probability that any trial will succeed."
    return _finite_result(result)


class SimulationRequest(Strict):
    """Two-arm simulation. Besides the per-field limits, n_per_arm x n_sims may not exceed ``stats.MAX_SIMULATION_CELLS``
    (5,000,000): the simulation runs in fixed-size blocks of about 12 MB, so the product bounds CPU time only."""

    n_per_arm: int = Field(ge=3, le=20000)
    mean_treatment: Real
    mean_control: Real
    sd: PositiveReal
    dropout_treatment: Proportion = 0.0
    dropout_control: Proportion = 0.0
    missing_handling: Literal["COMPLETERS", "JUMP_TO_REFERENCE"] = "COMPLETERS"
    n_sims: int = Field(2000, ge=stats.MIN_SIMULATIONS, le=stats.MAX_SIMULATIONS)
    alpha: float = Field(0.05, gt=0, lt=0.5, allow_inf_nan=False)
    seed: int = Field(20261013, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def _within_budget(self) -> SimulationRequest:
        stats.check_simulation_budget(self.n_per_arm, self.n_sims)  # StatsInputError is a ValueError: reported as a 422
        return self


@app.post("/api/design/simulate", tags=["design"], dependencies=[Depends(limit_simulation)])
def design_simulate(body: SimulationRequest) -> dict[str, Any]:
    with calculation():
        return _finite_result(stats.simulate_two_arm_trial(stats.SimulationSpec(**body.model_dump())))


class SensitivityRequest(Strict):
    delta: Real
    sd: PositiveReal
    n_randomized_per_group: int = Field(ge=3, le=100000)
    attrition_rates: list[Proportion] = Field(default=[0.0, 0.1, 0.2, 0.3, 0.4], max_length=20)
    alpha: float = Field(0.05, gt=0, lt=0.5, allow_inf_nan=False)
    mean_treatment: Real | None = None
    mean_control: Real | None = None
    missing_treatment: Proportion = 0.0
    missing_control: Proportion = 0.0
    deltas: list[Real] = Field(default=[0.0, 2.0, 4.0, 6.0, 8.0], max_length=20)


@app.post("/api/design/sensitivity", tags=["design"])
def design_sensitivity(body: SensitivityRequest) -> dict[str, Any]:
    with calculation():
        out: dict[str, Any] = {"attrition": stats.attrition_sensitivity(body.delta, body.sd, body.n_randomized_per_group, body.attrition_rates, body.alpha),
                               "attrition_note": "Completers-only simplification: power if only participants who complete contribute. Label: scenario."}
        if body.mean_treatment is not None and body.mean_control is not None:
            out["missing_data"] = stats.missing_data_delta_scenarios(body.mean_treatment, body.mean_control, body.missing_treatment, body.missing_control, body.deltas)
            out["missing_data_note"] = "Delta-adjustment scenarios on aggregate means: hypothetical, not estimates."
        return _finite_result(out)


# --------------------------------------------------------------------------- assistant


class AskRequest(Strict):
    question: str = Field(min_length=3, max_length=1000)


@app.post("/api/assistant/ask", tags=["assistant"], dependencies=[Depends(limit_assistant)])
def assistant_ask(body: AskRequest, db: Db) -> dict[str, Any]:
    answer = ask(db, body.question, get_provider(get_settings()))
    usage_log.info("assistant.question intent=%s llm_used=%s", answer.intent, answer.llm.get("used", False))
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
def list_sources(db: Db, source_type: Filter = None, tier: Filter = None, q: Annotated[str | None, Query(max_length=120)] = None, limit: int = Query(500, ge=1, le=2000), offset: Offset = 0) -> dict[str, Any]:
    _known(tier, EvidenceTier._value2member_map_, "evidence tier")
    conditions = []
    if source_type:
        conditions.append(EvidenceSource.source_type == source_type)
    if tier:
        conditions.append(EvidenceSource.tier == tier)
    if q:
        conditions.append(or_(EvidenceSource.title.ilike(f"%{q}%"), EvidenceSource.key.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(EvidenceSource).where(*conditions))
    sources = db.scalars(select(EvidenceSource).where(*conditions).order_by(EvidenceSource.source_type, EvidenceSource.key).limit(limit).offset(offset)).all()
    ids = [source.id for source in sources]
    # Counted in the database: loading the snapshots themselves would pull every stored document into memory.
    n_results = {sid: n for sid, n in db.execute(select(OutcomeResult.source_id, func.count()).where(OutcomeResult.source_id.in_(ids)).group_by(OutcomeResult.source_id))}
    n_snapshots = {sid: n for sid, n in db.execute(select(SourceSnapshot.source_id, func.count()).where(SourceSnapshot.source_id.in_(ids)).group_by(SourceSnapshot.source_id))}
    rows = [{**source_ref(source), "source_type": source.source_type, "license_note": source.license_note, "n_snapshots": n_snapshots.get(source.id, 0), "n_results": n_results.get(source.id, 0), "external_ids": source.external_ids}
            for source in sources]
    by_type = {source_type: n for source_type, n in db.execute(select(EvidenceSource.source_type, func.count()).group_by(EvidenceSource.source_type))}
    return _page(rows, total, limit, offset, by_type=by_type)


@app.get("/api/sources/{source_id}", tags=["provenance"])
def get_source(source_id: PathId, db: Db) -> dict[str, Any]:
    source = db.get(EvidenceSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    dependent = select(OutcomeResult).where(OutcomeResult.source_id == source.id)
    results = db.scalars(dependent.options(selectinload(OutcomeResult.study), selectinload(OutcomeResult.endpoint)).order_by(OutcomeResult.id).limit(60)).all()
    # Snapshot metadata only: whether the document is stored is asked of the database instead of loading the document.
    snapshots = db.execute(
        select(SourceSnapshot.version_no, SourceSnapshot.content_hash, SourceSnapshot.retrieved_at, SourceSnapshot.version_label, SourceSnapshot.byte_size,
               SourceSnapshot.content.is_not(None), SourceSnapshot.is_synthetic, SourceSnapshot.excerpts)
        .where(SourceSnapshot.source_id == source.id).order_by(SourceSnapshot.version_no)
    ).all()
    return {
        **source_ref(source), "source_type": source.source_type, "license_note": source.license_note, "external_ids": source.external_ids, "classification": source.classification,
        "snapshots": [{"version_no": version_no, "content_hash": content_hash, "retrieved_at": retrieved_at.isoformat(), "version_label": version_label, "byte_size": byte_size, "content_stored": bool(content_stored), "is_synthetic": is_synthetic, "anchored_excerpts": excerpts}
                      for version_no, content_hash, retrieved_at, version_label, byte_size, content_stored, is_synthetic, excerpts in snapshots],
        "dependent_findings": [{"result_id": r.id, "study": r.study.display_name, "arm": r.arm_label, "endpoint": r.endpoint.title, "value": r.value, "verification_status": r.verification_status, "locator_quote": r.locator_quote, "source_location": r.source_location} for r in results],
        # At most 60 dependent findings are listed; this is how many there are.
        "dependent_findings_total": db.scalar(select(func.count()).select_from(dependent.subquery())),
        "changes": [{"id": e.id, "detected_at": e.detected_at.isoformat(), "summary": e.summary, "significance": e.significance, "review_status": e.review_status, "is_synthetic": e.is_synthetic} for e in db.scalars(select(ChangeEvent).where(ChangeEvent.source_id == source.id).order_by(ChangeEvent.id))],
    }


def _change_rows(db: Session, limit: int, offset: int = 0) -> list[dict[str, Any]]:
    """Detected source changes, newest first."""
    events = db.scalars(select(ChangeEvent).options(joinedload(ChangeEvent.source)).order_by(ChangeEvent.detected_at.desc(), ChangeEvent.id.desc()).limit(limit).offset(offset))
    return [{"id": e.id, "source": source_ref(e.source), "detected_at": e.detected_at.isoformat(), "summary": e.summary, "significance": e.significance, "review_status": e.review_status, "is_synthetic": e.is_synthetic,
             "synthetic_notice": "SYNTHETIC DEMONSTRATION — the 'before' version was generated from the real record by labelled edits to demonstrate change review. It is not a real registry change." if e.is_synthetic else None, "changes": e.changes}
            for e in events]


@app.get("/api/changes", tags=["provenance"])
def list_changes(db: Db, limit: int = Query(200, ge=1, le=1000), offset: Offset = 0) -> dict[str, Any]:
    return _page(_change_rows(db, limit, offset), db.scalar(select(func.count()).select_from(ChangeEvent)), limit, offset)


@app.get("/api/audit", tags=["provenance"])
def list_audit(db: Db, action: Filter = None, limit: int = Query(200, ge=1, le=1000), offset: Offset = 0) -> dict[str, Any]:
    conditions = [AuditEvent.action == action] if action else []
    total = db.scalar(select(func.count()).select_from(AuditEvent).where(*conditions))
    rows = [{"id": e.id, "at": e.at.isoformat(), "actor": e.actor, "action": e.action, "target_type": e.target_type, "target_id": e.target_id, "detail": e.detail}
            for e in db.scalars(select(AuditEvent).where(*conditions).order_by(AuditEvent.id.desc()).limit(limit).offset(offset))]
    return _page(rows, total, limit, offset)


@app.get("/api/review-queue", tags=["provenance"])
def review_queue(db: Db, status: Filter = "OPEN", item_type: Filter = None, limit: int = Query(300, ge=1, le=1000), offset: Offset = 0) -> dict[str, Any]:
    """Items that are not evidence rows: source changes, reconciliation problems, ingestion failures.

    Evidence awaiting review is not here: a pending evidence row is its own review item (GET /api/review/evidence).
    """
    conditions = []
    if status:
        conditions.append(ReviewQueueItem.status == status)
    if item_type:
        conditions.append(ReviewQueueItem.item_type == item_type)
    total = db.scalar(select(func.count()).select_from(ReviewQueueItem).where(*conditions))
    by_priority = case({"HIGH": 0, "MEDIUM": 1, "LOW": 2}, value=ReviewQueueItem.priority, else_=9)
    items = db.scalars(select(ReviewQueueItem).where(*conditions).order_by(by_priority, ReviewQueueItem.id).limit(limit).offset(offset)).all()
    counts = db.execute(select(ReviewQueueItem.item_type, ReviewQueueItem.status, func.count()).group_by(ReviewQueueItem.item_type, ReviewQueueItem.status)).all()
    rows = [{"id": i.id, "item_type": i.item_type, "target_type": i.target_type, "target_id": i.target_id, "title": i.title, "reason": i.reason, "priority": i.priority, "status": i.status, "payload": i.payload, "created_at": i.created_at.isoformat()} for i in items]
    return _page(rows, total, limit, offset, counts=[{"item_type": t, "status": s, "n": n} for t, s, n in sorted(counts)], review_enabled=bool(get_settings().reviewer_token),
                 evidence_review="/api/review/evidence")


class DecisionRequest(Strict):
    decision: Literal["APPROVE", "REJECT", "NEEDS_INFO", "REOPEN"]
    reviewer: str = Field(min_length=2, max_length=120)
    rationale: str = Field(min_length=3, max_length=2000)


# Queue item state machine: (status, decision) -> next status. Anything else is refused with 409.
QUEUE_TRANSITIONS: dict[tuple[str, str], str] = {
    ("OPEN", "APPROVE"): "RESOLVED",
    ("OPEN", "REJECT"): "RESOLVED",
    ("OPEN", "NEEDS_INFO"): "IN_REVIEW",
    ("IN_REVIEW", "APPROVE"): "RESOLVED",
    ("IN_REVIEW", "REJECT"): "RESOLVED",
    ("IN_REVIEW", "NEEDS_INFO"): "IN_REVIEW",
    ("RESOLVED", "REOPEN"): "OPEN",
}


@app.post("/api/review-queue/{item_id}/decision", tags=["provenance"])
def decide(item_id: PathId, body: DecisionRequest, db: Db, _: Annotated[str, Depends(require_reviewer)]) -> dict[str, Any]:
    """Decide on a queue item (a source change, a reconciliation problem, an ingestion failure).

    This never changes the verification status of evidence: that is done per evidence row through
    POST /api/review/evidence/{type}/{id}/decisions. A resolved item accepts no further decision
    until it is explicitly reopened (decision REOPEN).
    """
    item = db.get(ReviewQueueItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Review item not found")
    if item.item_type == "CURATED_CLAIM" or item.status == "RETIRED":
        raise HTTPException(status_code=409, detail={
            "message": "This item belongs to the retired source-level curated queue and can no longer be decided. Review the evidence row itself: "
                       "a decision is recorded on one exact result, analysis, adverse event or claim.",
            "evidence_review": "/api/review/evidence?status=PENDING_HUMAN_REVIEW" + (f"&source_key={item.payload['source_key']}" if (item.payload or {}).get("source_key") else ""),
            "decision_endpoint": "POST /api/review/evidence/{evidence_type}/{id}/decisions",
        })
    problem = reviewer_problem(body.reviewer)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    next_status = QUEUE_TRANSITIONS.get((item.status, body.decision))
    if next_status is None:
        hint = "Send decision REOPEN to reopen it before deciding again." if item.status == "RESOLVED" else "REOPEN applies to resolved items only."
        raise HTTPException(status_code=409, detail=f"Item {item.id} is {item.status}; decision {body.decision} is not allowed in that state. {hint}")
    previous = item.status
    db.add(ReviewDecision(queue_item_id=item.id, target_type=item.target_type, target_id=item.target_id, decision=body.decision, reviewer=body.reviewer, rationale=body.rationale))
    item.status = next_status
    if item.item_type == "SOURCE_CHANGE" and item.target_id:
        event = db.get(ChangeEvent, item.target_id)
        if event is not None:
            event.review_status = item.status
    audit(db, "review.decision", actor=body.reviewer, target_type="review_queue_item", target_id=item.id, decision=body.decision, rationale=body.rationale,
          item_type=item.item_type, status_before=previous, status_after=item.status)
    db.commit()
    return {"id": item.id, "status": item.status, "decision": body.decision, "previous_status": previous}


# --------------------------------------------------------------------------- reports & overview


@app.get("/api/reports/comparison", tags=["reports"], response_class=PlainTextResponse)
def report_comparison(db: Db, a: QueryId, b: QueryId) -> PlainTextResponse:
    _loaded = _prime_studies(db)  # noqa: F841
    try:
        markdown = comparison_report_markdown(db, a, b)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    usage_log.info("report.exported kind=comparison a=%s b=%s", a, b)
    return PlainTextResponse(markdown, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="incretinos-comparison-{a}-{b}.md"'})


@app.get("/api/reports/study/{nct}", tags=["reports"], response_class=PlainTextResponse)
def report_study(nct: str, db: Db) -> PlainTextResponse:
    _study_or_404(db, nct)
    usage_log.info("report.exported kind=study nct=%s", nct.upper())
    return PlainTextResponse(study_report_markdown(db, nct), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="incretinos-{nct.upper()}.md"'})


@app.get("/api/overview", tags=["meta"])
def overview(db: Db) -> dict[str, Any]:
    drugs = [d for d in _drug_rows(db) if d["tier_group"] != "COMPARATOR"]
    _loaded = _prime_evidence(db)
    studies = _loaded[0]
    gaps = sorted((_gap_dto(g) for g in db.scalars(select(EvidenceGap).options(joinedload(EvidenceGap.drug)).order_by(EvidenceGap.id))), key=lambda g: -g["priority"]["score"])
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
        "changes": _change_rows(db, limit=5),
        "upcoming_readouts": upcoming,
        "completed_without_registry_results": unpublished,
        "presets": compare_presets(db),
    }
