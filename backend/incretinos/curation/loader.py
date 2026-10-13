"""Load curated evidence files into the database with source resolution and quote anchoring.

Verification is decided here, mechanically:

* QUOTE_ANCHORED          — the locator quote was found in the stored source text (registry
                            record, FDA label) or among excerpts verified against the live
                            source when fixtures were recorded (PubMed abstracts, web pages).
* CURATED_PENDING_REVIEW  — the quote could not be checked automatically; a review-queue item
                            is opened. Nothing is ever marked human-verified by this loader.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..enums import (
    EstimandBasis,
    EvidenceTier,
    Origin,
    RegulatoryStatus,
    VerificationStatus,
)
from ..ingestion.snapshots import (
    enqueue_review,
    normalize_ws,
    quote_in_text,
    record_snapshot,
    sha256_text,
    source_text_for_anchoring,
)
from ..models import (
    AdverseEvent,
    Dose,
    Drug,
    DrugMechanism,
    Endpoint,
    Estimand,
    EvidenceClaim,
    EvidenceSource,
    ExposureEquivalence,
    Formulation,
    Indication,
    IntercurrentEventStrategy,
    OutcomeResult,
    Population,
    StatisticalAnalysis,
    Study,
    StudyArm,
    StudyDrug,
)
from ..vocab import COMBINATION_SLUGS, arm_drug_slug, norm_arm_label, parse_single_dose_mg
from .schema import (
    ClaimDef,
    ClaimsFile,
    DrugsFile,
    SourcesFile,
    StudyEvidenceFile,
    Watchlist,
    WatchStudy,
)

M = TypeVar("M", bound=BaseModel)

AUTHORITATIVE_APPROVAL_TIERS = {EvidenceTier.REGULATORY_LABEL.value, EvidenceTier.REGULATORY_REVIEW.value}


class CurationError(ValueError):
    pass


def load_yaml(path: Path, model: type[M]) -> M:
    with path.open(encoding="utf-8") as handle:
        return model.model_validate(yaml.safe_load(handle))


def read_watchlist(curated_dir: Path) -> Watchlist:
    return load_yaml(curated_dir / "watchlist.yaml", Watchlist)


def read_drugs(curated_dir: Path) -> DrugsFile:
    return load_yaml(curated_dir / "drugs.yaml", DrugsFile)


def read_sources(curated_dir: Path) -> list:
    """All curated source definitions (sources.yaml plus generated sources.*.yaml)."""
    out = []
    for path in sorted(curated_dir.glob("sources*.yaml")):
        out.extend(load_yaml(path, SourcesFile).sources)
    return out


def study_files(curated_dir: Path) -> list[StudyEvidenceFile]:
    folder = curated_dir / "studies"
    return [load_yaml(p, StudyEvidenceFile) for p in sorted(folder.glob("*.yaml"))] if folder.is_dir() else []


def collect_pubmed_anchors(curated_dir: Path) -> dict[str, list[str]]:
    """PMID -> every curated quote that cites that PubMed record (verified at fetch time)."""
    anchors: dict[str, list[str]] = {}

    def add(source: str | None, *quotes: str | None) -> None:
        if source and source.startswith("pubmed:"):
            bucket = anchors.setdefault(source.split(":", 1)[1], [])
            bucket.extend(q for q in quotes if q and q not in bucket)

    for drug in read_drugs(curated_dir).drugs:
        add(drug.regulatory.source, drug.regulatory.quote)
        for item in [*drug.mechanisms, *drug.indications, *drug.equivalences]:
            add(item.source, item.quote)
    for file in study_files(curated_dir):
        for item in [*file.estimands, *file.claims]:
            add(item.source, item.quote)
        for item in [*file.results, *file.analyses, *file.adverse_events]:
            add(item.source, item.quote, *item.extra_quotes)
    claims_path = curated_dir / "claims.yaml"
    if claims_path.is_file():
        for claim in load_yaml(claims_path, ClaimsFile).claims:
            add(claim.source, claim.quote)
    return anchors


# --------------------------------------------------------------------------- sources


def load_sources(session: Session, curated_dir: Path) -> int:
    """Register non-API sources (releases, guidance, pipeline pages). Their text is not stored."""
    anchors_path = curated_dir / "web_anchors.json"
    anchors = json.loads(anchors_path.read_text(encoding="utf-8")) if anchors_path.is_file() else {}
    count = 0
    for src in read_sources(curated_dir):
        anchor = anchors.get(src.key, {})
        retrieved = datetime.fromisoformat(src.retrieved_date).replace(tzinfo=UTC)
        record_snapshot(
            session,
            key=src.key,
            source_type=src.source_type,
            tier=src.tier.value,
            title=src.title,
            url=src.url,
            publisher=src.publisher,
            published_date=src.published_date,
            version_label=f"retrieved {src.retrieved_date}",
            content_hash=anchor.get("content_sha256") or sha256_text(f"{src.url}|{src.title}|{src.published_date}"),
            content=None,
            excerpts=anchor.get("verified_quotes", []),
            license_note=src.license_note or "Third-party web content: not redistributed. Only the URL, retrieval date and short locator quotes are stored.",
            content_type="text/html",
            retrieved_at=retrieved,
        )
        count += 1
    session.flush()
    return count


def resolve_source(session: Session, key: str) -> EvidenceSource:
    source = session.scalar(select(EvidenceSource).where(EvidenceSource.key == key))
    if source is None:
        raise CurationError(f"Curated entry cites unknown source '{key}'. Ingest it or add it to sources.yaml.")
    return source


def anchor_quote(session: Session, source: EvidenceSource, quote: str | None, *, review_key: str, title: str) -> str:
    """Decide the verification status of a curated statement from its locator quote."""
    if quote:
        text, excerpts = source_text_for_anchoring(session, source)
        if text and quote_in_text(quote, text):
            return VerificationStatus.QUOTE_ANCHORED.value
        normalized = normalize_ws(quote).lower()
        if any(normalized == normalize_ws(e).lower() or normalized in normalize_ws(e).lower() for e in excerpts):
            return VerificationStatus.QUOTE_ANCHORED.value
    enqueue_review(
        session,
        item_type="CURATED_CLAIM",
        target_type="evidence_source",
        target_id=source.id,
        title=title[:300],
        reason=(
            "Curated statement could not be anchored automatically to stored source text. "
            "A reviewer should confirm it against the original source."
            if quote
            else "Curated statement has no locator quote."
        ),
        priority="MEDIUM",
        payload={"source_key": source.key, "quote": quote},
        dedupe_key=f"curated:{review_key}"[:200],
    )
    return VerificationStatus.CURATED_PENDING_REVIEW.value


# --------------------------------------------------------------------------- drugs


def load_drug_identities(session: Session, curated_dir: Path) -> list[Drug]:
    """Create drug, formulation and dose rows (no cited evidence yet — sources may not be ingested)."""
    drugs: list[Drug] = []
    for d in read_drugs(curated_dir).drugs:
        drug = session.scalar(select(Drug).where(Drug.slug == d.slug)) or Drug(slug=d.slug, name=d.name)
        drug.name = d.name
        drug.code_name = d.code_name
        drug.brand_names = d.brand_names
        drug.sponsor = d.sponsor
        drug.modality = d.modality
        drug.route = d.route
        drug.tier_group = d.tier_group
        drug.is_combination = d.is_combination
        drug.component_slugs = d.component_slugs
        drug.notes = d.notes
        session.add(drug)
        session.flush()
        for f in d.formulations:
            formulation = session.scalar(select(Formulation).where(Formulation.key == f.key)) or Formulation(key=f.key, drug_id=drug.id, name=f.name, stage=f.stage)
            formulation.drug_id = drug.id
            formulation.name, formulation.dosage_form, formulation.route = f.name, f.dosage_form, f.route
            formulation.stage, formulation.notes = f.stage, f.notes
            session.add(formulation)
            session.flush()
            for dose_def in f.doses:
                dose = session.scalar(select(Dose).where(Dose.key == dose_def.key)) or Dose(key=dose_def.key, formulation_id=formulation.id, amount=dose_def.amount)
                dose.formulation_id = formulation.id
                dose.amount, dose.unit, dose.frequency, dose.role = dose_def.amount, dose_def.unit, dose_def.frequency, dose_def.role
                session.add(dose)
        drugs.append(drug)
    session.flush()
    return drugs


def load_drug_evidence(session: Session, curated_dir: Path) -> None:
    """Attach cited mechanism / regulatory / indication / equivalence statements to drugs."""
    for d in read_drugs(curated_dir).drugs:
        drug = session.scalar(select(Drug).where(Drug.slug == d.slug))
        if drug is None:
            raise CurationError(f"Drug {d.slug} has not been created")
        reg_source = resolve_source(session, d.regulatory.source)
        if d.regulatory.status == RegulatoryStatus.APPROVED and reg_source.tier not in AUTHORITATIVE_APPROVAL_TIERS:
            raise CurationError(
                f"{d.slug}: an APPROVED status must cite FDA labeling or an FDA review; '{reg_source.key}' is tier {reg_source.tier}."
            )
        drug.regulatory_status = d.regulatory.status.value
        drug.regulatory_summary = d.regulatory.summary
        drug.regulatory_as_of = d.regulatory.as_of
        drug.regulatory_source_id = reg_source.id
        _upsert_claim(
            session,
            ClaimDef(
                key=f"{d.slug}:regulatory-status",
                topic="REGULATORY_STATUS",
                statement=d.regulatory.summary,
                source=d.regulatory.source,
                quote=d.regulatory.quote,
                locator=d.regulatory.locator,
                drug=d.slug,
            ),
            drug=drug,
        )

        session.execute(delete(DrugMechanism).where(DrugMechanism.drug_id == drug.id))
        for m in d.mechanisms:
            source = resolve_source(session, m.source)
            session.add(
                DrugMechanism(
                    drug_id=drug.id,
                    target=m.target.value,
                    action=m.action.value,
                    description=m.description,
                    source_id=source.id,
                    quote=m.quote,
                    verification_status=anchor_quote(session, source, m.quote, review_key=f"{d.slug}:mech:{m.target.value}", title=f"{d.name}: {m.target.value} {m.action.value}"),
                )
            )

        session.execute(delete(Indication).where(Indication.drug_id == drug.id))
        for i, ind in enumerate(d.indications):
            source = resolve_source(session, ind.source)
            if ind.status == RegulatoryStatus.APPROVED and source.tier not in AUTHORITATIVE_APPROVAL_TIERS:
                raise CurationError(f"{d.slug}: approved indication must cite FDA labeling or review, not {source.tier}.")
            session.add(
                Indication(
                    drug_id=drug.id,
                    brand=ind.brand,
                    text=ind.text,
                    status=ind.status.value,
                    jurisdiction=ind.jurisdiction,
                    approval_date=ind.approval_date,
                    source_id=source.id,
                    quote=ind.quote,
                    verification_status=anchor_quote(session, source, ind.quote, review_key=f"{d.slug}:ind:{i}", title=f"{d.name} indication ({ind.brand or ind.jurisdiction})"),
                )
            )

        for eq in d.equivalences:
            dose_a = session.scalar(select(Dose).where(Dose.key == eq.a))
            dose_b = session.scalar(select(Dose).where(Dose.key == eq.b))
            if dose_a is None or dose_b is None:
                raise CurationError(f"{d.slug}: exposure equivalence references unknown dose ({eq.a}, {eq.b})")
            source = resolve_source(session, eq.source)
            row = session.scalar(
                select(ExposureEquivalence).where(ExposureEquivalence.dose_a_id == dose_a.id, ExposureEquivalence.dose_b_id == dose_b.id)
            ) or ExposureEquivalence(dose_a_id=dose_a.id, dose_b_id=dose_b.id, basis=eq.basis.value, statement=eq.statement)
            row.basis, row.statement, row.source_id, row.quote = eq.basis.value, eq.statement, source.id, eq.quote
            row.verification_status = anchor_quote(session, source, eq.quote, review_key=f"{d.slug}:eq:{eq.a}:{eq.b}", title=f"{d.name}: {eq.a} ↔ {eq.b}")
            session.add(row)
    session.flush()


# --------------------------------------------------------------------------- studies


def apply_watch_study(session: Session, study: Study, watch: WatchStudy) -> None:
    study.display_name = watch.display_name
    study.program = watch.program
    study.design_features = watch.design_features
    for link_def in watch.drugs:
        drug = session.scalar(select(Drug).where(Drug.slug == link_def.slug))
        if drug is None:
            raise CurationError(f"{watch.nct}: unknown drug '{link_def.slug}' in watchlist")
        link = session.scalar(select(StudyDrug).where(StudyDrug.study_id == study.id, StudyDrug.drug_id == drug.id))
        if link is None:
            link = StudyDrug(study_id=study.id, drug_id=drug.id)
            session.add(link)
        link.role = link_def.role
    if watch.population is not None:
        population = study.population or Population(study_id=study.id)
        population.diabetes_status = watch.population.diabetes_status.value
        population.diabetes_basis = f"Curated: {watch.population.basis}"
        session.add(population)
    session.flush()


def _find_or_create_arm(session: Session, study: Study, label: str) -> StudyArm:
    norm = norm_arm_label(label)
    arm = session.scalar(select(StudyArm).where(StudyArm.study_id == study.id, StudyArm.norm_label == norm))
    if arm is None:
        drug_slug, is_placebo = arm_drug_slug(label)
        formulation_key = None
        if drug_slug == "orforglipron":
            # Sources name the formulation in the arm description; nothing is assumed when they do not.
            if re.search(r"foundayo|tablet|label dosage", label, re.IGNORECASE):
                formulation_key = "foundayo-tablet"
            elif re.search(r"capsule|investigational", label, re.IGNORECASE):
                formulation_key = "orforglipron-capsule"
        dose =parse_single_dose_mg(label) if drug_slug and "+" not in drug_slug and drug_slug not in COMBINATION_SLUGS.values() else None
        arm = StudyArm(
            study_id=study.id,
            origin=Origin.CURATED.value,
            label=label,
            norm_label=norm,
            arm_type="CURATED",
            drug_slug=drug_slug,
            is_placebo=is_placebo,
            dose_amount=dose,
            dose_unit="mg" if dose is not None else None,
            dose_text=label if drug_slug in COMBINATION_SLUGS.values() else None,
            formulation_key=formulation_key,
            is_pooled=bool(re.search(r"pooled|all doses|combined", label, re.IGNORECASE)),
        )
        session.add(arm)
        session.flush()
    return arm


def _upsert_claim(session: Session, claim: ClaimDef, *, drug: Drug | None = None, study: Study | None = None) -> EvidenceClaim:
    source = resolve_source(session, claim.source)
    if drug is None and claim.drug:
        drug = session.scalar(select(Drug).where(Drug.slug == claim.drug))
    row = session.scalar(select(EvidenceClaim).where(EvidenceClaim.key == claim.key)) or EvidenceClaim(key=claim.key, topic=claim.topic, statement=claim.statement, tier=source.tier)
    row.topic, row.statement, row.claim_kind = claim.topic, claim.statement, claim.claim_kind.value
    row.tier, row.source_id, row.quote, row.locator = source.tier, source.id, claim.quote, claim.locator
    row.drug_id = drug.id if drug else None
    row.study_id = study.id if study else None
    row.verification_status = anchor_quote(session, source, claim.quote, review_key=f"claim:{claim.key}", title=claim.statement)
    session.add(row)
    return row


def _missing(pairs: list[tuple[str, object]]) -> list[str]:
    return [name for name, value in pairs if value is None]


def reset_curated_rows(session: Session, study: Study) -> None:
    """Remove previously loaded curated rows for a study (registry-derived rows are untouched)."""
    curated = Origin.CURATED.value
    session.execute(delete(OutcomeResult).where(OutcomeResult.study_id == study.id, OutcomeResult.origin == curated))
    session.execute(delete(StatisticalAnalysis).where(StatisticalAnalysis.study_id == study.id, StatisticalAnalysis.origin == curated))
    session.execute(delete(AdverseEvent).where(AdverseEvent.study_id == study.id, AdverseEvent.origin == curated))
    session.execute(delete(Endpoint).where(Endpoint.study_id == study.id, Endpoint.origin == curated))
    for old in session.scalars(select(Estimand).where(Estimand.study_id == study.id, Estimand.basis == EstimandBasis.STATED_BY_SOURCE.value)).all():
        session.delete(old)
    session.flush()


def load_study_evidence(session: Session, file: StudyEvidenceFile, *, reset: bool = True, tag: str = "") -> dict[str, int]:
    """Load one curated file. Several files may target one study: reset only on the first."""
    study = session.scalar(select(Study).where(Study.nct_id == file.study))
    if study is None:
        raise CurationError(f"Curated file references study {file.study}, which has not been ingested")
    curated = Origin.CURATED.value
    if reset:
        reset_curated_rows(session, study)

    for ann in file.arms:
        arm = _find_or_create_arm(session, study, ann.label)
        if ann.drug is not None:
            arm.drug_slug = ann.drug
        if ann.dose_amount is not None:
            arm.dose_amount, arm.dose_unit = ann.dose_amount, ann.dose_unit
        if ann.dose_text is not None:
            arm.dose_text = ann.dose_text
        if ann.formulation is not None:
            if session.scalar(select(Formulation).where(Formulation.key == ann.formulation)) is None:
                raise CurationError(f"{file.study}: unknown formulation '{ann.formulation}'")
            arm.formulation_key = ann.formulation
        if ann.is_placebo is not None:
            arm.is_placebo = ann.is_placebo
        if ann.n_randomized is not None and arm.n_randomized is None:
            arm.n_randomized = ann.n_randomized
        if ann.arm_type is not None:
            arm.arm_type = ann.arm_type

    if file.population_summary:
        population = study.population or Population(study_id=study.id)
        summary = dict(population.summary or {})
        for name, entry in file.population_summary.items():
            source = resolve_source(session, entry["source"])
            summary[name] = {
                "value": entry["value"],
                "origin": "CURATED",
                "source_key": source.key,
                "quote": entry.get("quote"),
                "verification": anchor_quote(session, source, entry.get("quote"), review_key=f"{file.study}:pop:{name}", title=f"{study.display_name} baseline {name}"),
            }
        population.summary = summary
        session.add(population)

    estimands: dict[str, Estimand] = {}
    for e in file.estimands:
        source = resolve_source(session, e.source)
        existing = session.scalar(
            select(Estimand).where(Estimand.study_id == study.id, Estimand.label == e.label.value, Estimand.basis == EstimandBasis.STATED_BY_SOURCE.value)
        )
        if existing is not None:
            if e.quote and not existing.quote:
                existing.quote, existing.source_id = e.quote, source.id
            estimands[e.key] = existing
            continue
        estimand = Estimand(
            study_id=study.id,
            label=e.label.value,
            basis=EstimandBasis.STATED_BY_SOURCE.value,
            description=e.description,
            source_id=source.id,
            quote=e.quote,
        )
        session.add(estimand)
        session.flush()
        for s in e.strategies:
            session.add(
                IntercurrentEventStrategy(
                    estimand_id=estimand.id, event=s.event.value, strategy=s.strategy.value, basis=EstimandBasis.STATED_BY_SOURCE.value, quote=s.quote
                )
            )
        estimands[e.key] = estimand

    endpoints: dict[str, Endpoint] = {}
    for ep in file.endpoints:
        endpoint = Endpoint(
            study_id=study.id,
            origin=curated,
            kind="CURATED",
            title=ep.title,
            role=ep.role.value,
            category=ep.category.value,
            threshold=ep.threshold,
            unit=ep.unit,
            timepoint_weeks=ep.timepoint_weeks,
            timeframe_text=ep.timeframe_text,
            description=ep.description,
            hierarchy_rank=ep.hierarchy_rank,
            multiplicity_controlled=ep.multiplicity_controlled.value,
        )
        session.add(endpoint)
        endpoints[ep.key] = endpoint
    session.flush()

    def endpoint_for(key: str) -> Endpoint:
        if key not in endpoints:
            raise CurationError(f"{file.study}: unknown endpoint key '{key}'")
        return endpoints[key]

    def estimand_for(key: str | None) -> Estimand | None:
        if key is None:
            return None
        if key not in estimands:
            raise CurationError(f"{file.study}: unknown estimand key '{key}'")
        return estimands[key]

    for i, r in enumerate(file.results):
        source = resolve_source(session, r.source)
        endpoint = endpoint_for(r.endpoint)
        estimand = estimand_for(r.estimand)
        arm = None if r.scope == "RANGE_ACROSS_ARMS" else _find_or_create_arm(session, study, r.arm)
        if endpoint.source_id is None:
            endpoint.source_id = source.id
        missing = _missing([("confidence_interval", r.ci_low), ("n_analyzed", r.n_analyzed)])
        if estimand is None:
            missing.append("estimand_label")
        session.add(
            OutcomeResult(
                study_id=study.id,
                endpoint_id=endpoint.id,
                arm_id=arm.id if arm else None,
                estimand_id=estimand.id if estimand else None,
                origin=curated,
                arm_label=r.arm,
                scope=r.scope,
                class_title=r.class_title,
                param_type=r.param_type,
                value=r.value,
                value_text=None if r.value is None else f"{r.value:g}",
                value_low=r.value_low,
                value_high=r.value_high,
                unit=r.unit or endpoint.unit,
                dispersion_type=r.dispersion_type,
                dispersion_value=r.dispersion_value,
                ci_low=r.ci_low,
                ci_high=r.ci_high,
                ci_level=r.ci_level if r.ci_low is not None else None,
                n_analyzed=r.n_analyzed,
                analysis_population=r.analysis_population,
                analysis_method=r.analysis_method,
                baseline_definition=r.baseline_definition,
                timepoint_weeks=r.timepoint_weeks if r.timepoint_weeks is not None else endpoint.timepoint_weeks,
                timeframe_text=endpoint.timeframe_text,
                source_id=source.id,
                source_location=r.locator,
                locator_quote=r.quote,
                tier=source.tier,
                verification_status=anchor_quote(
                    session, source, r.quote, review_key=f"{file.study}:{tag}result:{i}:{r.endpoint}:{r.arm}", title=f"{study.display_name} — {endpoint.title} — {r.arm}"
                ),
                missing_fields=missing,
                notes=r.notes,
            )
        )

    for i, a in enumerate(file.analyses):
        source = resolve_source(session, a.source)
        endpoint = endpoint_for(a.endpoint)
        estimand = estimand_for(a.estimand)
        treatment = _find_or_create_arm(session, study, a.treatment)
        comparator = _find_or_create_arm(session, study, a.comparator) if a.comparator else None
        if endpoint.source_id is None:
            endpoint.source_id = source.id
        missing = _missing([("estimate", a.estimate), ("confidence_interval", a.ci_low), ("p_value", a.p_value_text)])
        if a.multiplicity_controlled.value == "NOT_STATED":
            missing.append("multiplicity_status")
        session.add(
            StatisticalAnalysis(
                study_id=study.id,
                endpoint_id=endpoint.id,
                estimand_id=estimand.id if estimand else None,
                origin=curated,
                treatment_arm_id=treatment.id,
                comparator_arm_id=comparator.id if comparator else None,
                treatment_label=a.treatment,
                comparator_label=a.comparator,
                hypothesis_type=a.hypothesis_type.value,
                param_type=a.param_type,
                estimate=a.estimate,
                estimate_text=None if a.estimate is None else f"{a.estimate:g}",
                unit=a.unit,
                ci_low=a.ci_low,
                ci_high=a.ci_high,
                ci_level=a.ci_level if a.ci_low is not None else None,
                p_value_text=a.p_value_text,
                method=a.method,
                ni_margin=a.ni_margin,
                ni_margin_text=a.ni_margin_text,
                ni_met=a.ni_met.value,
                superiority_tested=a.superiority_tested.value,
                superiority_met=a.superiority_met.value,
                multiplicity_controlled=a.multiplicity_controlled.value,
                timepoint_weeks=a.timepoint_weeks if a.timepoint_weeks is not None else endpoint.timepoint_weeks,
                comment=a.comment,
                source_id=source.id,
                source_location=a.locator,
                locator_quote=a.quote,
                tier=source.tier,
                verification_status=anchor_quote(
                    session, source, a.quote, review_key=f"{file.study}:{tag}analysis:{i}:{a.endpoint}:{a.treatment}", title=f"{study.display_name} — {endpoint.title} — {a.treatment} vs {a.comparator}"
                ),
                missing_fields=missing,
                notes=a.notes,
            )
        )

    for i, ae in enumerate(file.adverse_events):
        source = resolve_source(session, ae.source)
        arm = None if ae.scope == "RANGE_ACROSS_ARMS" else _find_or_create_arm(session, study, ae.arm)
        session.add(
            AdverseEvent(
                study_id=study.id,
                arm_id=arm.id if arm else None,
                origin=curated,
                arm_label=ae.arm,
                scope=ae.scope,
                term=ae.term,
                category=ae.category.value,
                measure_kind=ae.measure_kind.value,
                is_serious=ae.is_serious,
                severity=ae.severity,
                n_affected=ae.n_affected,
                n_at_risk=ae.n_at_risk,
                percent=ae.percent,
                percent_low=ae.percent_low,
                percent_high=ae.percent_high,
                period=ae.period,
                source_id=source.id,
                source_location=ae.locator,
                locator_quote=ae.quote,
                tier=source.tier,
                verification_status=anchor_quote(
                    session, source, ae.quote, review_key=f"{file.study}:{tag}ae:{i}:{ae.term}:{ae.arm}", title=f"{study.display_name} — {ae.term} — {ae.arm}"
                ),
                missing_fields=_missing([("n_affected", ae.n_affected), ("n_at_risk", ae.n_at_risk)]),
                notes=ae.notes,
            )
        )

    for claim in file.claims:
        _upsert_claim(session, claim, study=study)
    session.flush()
    return {"results": len(file.results), "analyses": len(file.analyses), "adverse_events": len(file.adverse_events), "claims": len(file.claims)}


def load_claims(session: Session, curated_dir: Path) -> int:
    path = curated_dir / "claims.yaml"
    if not path.is_file():
        return 0
    claims = load_yaml(path, ClaimsFile).claims
    for claim in claims:
        _upsert_claim(session, claim)
    session.flush()
    return len(claims)
