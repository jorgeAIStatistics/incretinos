"""End-to-end ingestion orchestration.

One code path serves live ingestion, fixture recording and the offline demo; only the
transport differs. A failure in one source is recorded and queued for review — it
never aborts the run and never results in partially invented data.

Loads rebuild evidence rows (delete and re-insert), so the review lifecycle follows every step that
touches them, in the same transaction as the step:

* ``reconcile`` (full pass) re-keys the rebuilt rows, re-applies recorded human decisions, re-checks
  quote anchoring and reports rows whose content changed. It needs a complete state, so a full seed
  runs it once, after the curated load; a partial refresh (check-updates, the scheduler) runs it
  after each of its steps, because nothing else follows.
* ``hold_rejections`` (protective pass) runs after a step that rebuilt rows while more steps are
  still to come. It only puts rejected rows back to REJECTED.

Either way a rejected row is never committed with a mechanical status, not even between two steps.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..curation import loader
from ..db import rejected_visible
from ..models import Drug, EvidenceSource, IngestionRun, Publication, Study, StudyPublication, utcnow
from ..review.lifecycle import ReconcileReport, hold_rejections, reconcile
from .ctgov import CTGovAdapter
from .ctgov_diff import diff_studies, overall_significance, summarize_changes
from .openfda import OpenFDAFaersAdapter, OpenFDALabelAdapter
from .pubmed import PubMedAdapter
from .snapshots import audit, enqueue_review, record_change_event
from .transport import FixtureMissing, Transport, TransportError, build_transport

# The offline demo needs one registry change to show the review workflow. The public API
# exposes only the current record, so the "earlier version" below is SYNTHETIC: it is
# derived from the real record by the labelled edits in ``make_synthetic_prior_version``
# and is flagged is_synthetic everywhere it is stored or shown.
DEMO_CHANGE_NCT = "NCT07321886"

#: Steps after which evidence rows or their sources may differ, so the review lifecycle must reconcile.
RECONCILE_AFTER = frozenset({"ctgov", "openfda-label", "curated-evidence", "review-reconcile"})
#: Steps that read evidence analytically and must not see rejected rows.
ANALYTIC_STEPS = frozenset({"gap-engine"})


def make_synthetic_prior_version(raw: dict) -> dict:
    prior = copy.deepcopy(raw)
    protocol = prior["protocolSection"]
    status = protocol["statusModule"]
    design = protocol["designModule"]
    status["overallStatus"] = "NOT_YET_RECRUITING"
    status["lastUpdatePostDateStruct"] = {"date": "2026-01-15", "type": "ACTUAL"}
    if "enrollmentInfo" in design and isinstance(design["enrollmentInfo"].get("count"), int):
        design["enrollmentInfo"]["count"] = design["enrollmentInfo"]["count"] - 180
    secondary = protocol.get("outcomesModule", {}).get("secondaryOutcomes", [])
    if len(secondary) > 1:
        secondary.pop()
    criteria = protocol.get("eligibilityModule", {}).get("eligibilityCriteria", "")
    lines = criteria.splitlines()
    exclusion_at = next((i for i, line in enumerate(lines) if line.strip().lower().startswith("exclusion criteria")), None)
    if exclusion_at is not None:
        bullets = [i for i in range(exclusion_at + 1, len(lines)) if lines[i].strip().startswith("*")]
        if len(bullets) > 1:
            lines.pop(bullets[-1])
            protocol["eligibilityModule"]["eligibilityCriteria"] = "\n".join(lines)
    return prior


@dataclass
class IngestReport:
    mode: str
    studies: list[str] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    publications: int = 0
    labels: list[str] = field(default_factory=list)
    faers: list[dict[str, Any]] = field(default_factory=list)
    discovered: int = 0
    curated: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)
    # Review lifecycle: counts from the last reconcile of the run, and every stored row whose content changed.
    review: dict[str, Any] = field(default_factory=dict)
    evidence_changes: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__


class Pipeline:
    def __init__(self, session: Session, settings: Settings, transport: Transport | None = None, *, offline: bool | None = None, record: bool = False):
        self.session = session
        self.settings = settings
        is_offline = settings.offline if offline is None else offline
        self.transport = transport or build_transport(offline=is_offline, fixture_dir=settings.fixture_dir, record=record, settings=settings)
        self.ctgov = CTGovAdapter(self.transport)
        self.pubmed = PubMedAdapter(self.transport, cache_dir=settings.cache_dir)
        self.labels = OpenFDALabelAdapter(self.transport)
        self.faers = OpenFDAFaersAdapter(self.transport)
        self.report = IngestReport(mode=self.transport.mode)
        self._reconciled = False

    # -- helpers
    def reconcile_review(self) -> ReconcileReport:
        """Re-key rebuilt rows, re-apply review decisions and collect changed evidence (same transaction)."""
        outcome = reconcile(self.session)
        self.report.review = outcome.summary()
        self.report.evidence_changes.extend(outcome.changed)
        self._reconciled = True
        return outcome

    def _run(self, adapter: str, fn: Callable[[], dict | None], *, review: str | None = None) -> None:
        """Run one step in its own transaction, followed by the review pass it needs.

        ``review`` is "reconcile" (full pass), "hold" (protective pass: more steps follow) or "none";
        by default a step in RECONCILE_AFTER gets the full pass.
        """
        review = review or ("reconcile" if adapter in RECONCILE_AFTER else "none")
        run = IngestionRun(adapter=adapter, mode=self.transport.mode)
        self.session.add(run)
        self.session.flush()
        try:
            if adapter in ANALYTIC_STEPS:
                stats = fn() or {}
            else:
                # Loaders must see rejected rows (to delete or update them) even though analytic code never does.
                with rejected_visible(self.session):
                    stats = fn() or {}
            # Inside the try: if the review pass fails, the step is rolled back with it rather than committed unprotected.
            if review == "reconcile":
                stats = {**stats, "review": self.reconcile_review().summary()}
            elif review == "hold":
                self._reconciled = False
                stats = {**stats, "rejections_held": hold_rejections(self.session)}
            run.stats = stats
            run.status = "SUCCEEDED"
        except Exception as exc:  # recorded, surfaced, never swallowed silently
            self.session.rollback()
            run = IngestionRun(adapter=adapter, mode=self.transport.mode, status="FAILED", error=f"{type(exc).__name__}: {exc}")
            self.session.add(run)
            self._fail(adapter, exc)
        run.finished_at = utcnow()
        self.session.commit()

    def _fail(self, what: str, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        self.report.failures.append({"target": what, "error": message})
        enqueue_review(
            self.session,
            item_type="INGEST_FAILURE",
            target_type="ingestion",
            target_id=None,
            title=f"Ingestion failed: {what}",
            reason=message,
            priority="HIGH",
            dedupe_key=f"ingest-failure:{what}:{message[:80]}",
        )
        audit(self.session, "ingestion.failure", target_type="ingestion", target_id=what, error=message)

    # -- steps
    def ingest_studies(self, *, demo_change: bool = True, only: list[str] | None = None) -> dict:
        watchlist = loader.read_watchlist(self.settings.curated_dir)
        ingested = 0
        for watch in watchlist.studies:
            if only and watch.nct not in only:
                continue
            try:
                with self.session.begin_nested():
                    # The synthetic prior version is seeded once per database. After `seed --reset` the study row is gone
                    # but its source and snapshots (the synthetic one included) are preserved history: do not add it again.
                    already = self.session.scalar(select(EvidenceSource.id).where(EvidenceSource.key == f"ctgov:{watch.nct}")) is not None
                    synthetic_prior = False
                    if demo_change and watch.nct == DEMO_CHANGE_NCT and not already:
                        real = json.loads(self.ctgov.fetch_raw(watch.nct))
                        prior = json.dumps(make_synthetic_prior_version(real)).encode()
                        self.ctgov.ingest_study(self.session, watch.nct, raw_override=prior, is_synthetic=True)
                        synthetic_prior = True
                    result = self.ctgov.ingest_study(self.session, watch.nct)
                    loader.apply_watch_study(self.session, result.study, watch)
                    if result.outcome.changed and result.previous_parsed is not None:
                        changes = diff_studies(result.previous_parsed, result.parsed)
                        event = record_change_event(
                            self.session,
                            result.outcome,
                            changes=[c.as_dict() for c in changes],
                            summary=summarize_changes(watch.nct, changes),
                            significance=overall_significance(changes),
                            title=f"{watch.display_name} ({watch.nct}) registry record updated",
                            is_synthetic=synthetic_prior,
                        )
                        self.report.changes.append({"nct": watch.nct, "summary": event.summary, "significance": event.significance, "synthetic": synthetic_prior})
                ingested += 1
                self.report.studies.append(watch.nct)
            except (TransportError, ValueError, KeyError) as exc:
                self._fail(f"ctgov:{watch.nct}", exc)
        return {"studies": ingested, "changes": len(self.report.changes)}

    def ingest_publications(self) -> dict:
        watchlist = loader.read_watchlist(self.settings.curated_dir)
        anchors = loader.collect_pubmed_anchors(self.settings.curated_dir)
        total = 0
        for watch in watchlist.studies:
            study = self.session.scalar(select(Study).where(Study.nct_id == watch.nct))
            if study is None:
                continue
            try:
                si_pmids = self.pubmed.search_by_nct(watch.nct)
            except TransportError as exc:
                self._fail(f"pubmed-search:{watch.nct}", exc)
                si_pmids = []
            registry_pmids = [
                p for (p,) in self.session.execute(
                    select(Publication.pmid).join(StudyPublication).where(StudyPublication.study_id == study.id)
                ) if p
            ]
            wanted = list(dict.fromkeys([*( [watch.primary_pmid] if watch.primary_pmid else []), *si_pmids, *registry_pmids]))
            try:
                articles = self.pubmed.fetch_articles(wanted, anchors=anchors)
            except TransportError as exc:
                self._fail(f"pubmed-fetch:{watch.nct}", exc)
                continue
            fetched = {a.pmid for a in articles}
            for article in articles:
                publication = self.pubmed.upsert_article(self.session, article)
                self.pubmed.reconcile(self.session, study, article, publication, si_hit=article.pmid in si_pmids, primary_pmid=watch.primary_pmid)
                total += 1
            if watch.primary_pmid and watch.primary_pmid not in fetched:
                enqueue_review(
                    self.session,
                    item_type="RECONCILIATION",
                    target_type="study",
                    target_id=study.id,
                    title=f"{watch.display_name}: primary publication PMID {watch.primary_pmid} could not be retrieved",
                    reason="The curated primary publication was not returned by PubMed (or has no offline fixture).",
                    dedupe_key=f"missing-primary:{watch.nct}:{watch.primary_pmid}",
                )
            self.session.flush()
        # Publications cited only by curated evidence (not registry-linked to a watched study).
        known = {p for (p,) in self.session.execute(select(Publication.pmid)) if p}
        extra = [p for p in anchors if p not in known]
        if extra:
            for article in self.pubmed.fetch_articles(extra, anchors=anchors):
                self.pubmed.upsert_article(self.session, article)
                total += 1
        self.report.publications = total
        return {"articles": total}

    def ingest_labels(self) -> dict:
        watched: dict[str, int] = {}
        for d in loader.read_drugs(self.settings.curated_dir).drugs:
            drug = self.session.scalar(select(Drug).where(Drug.slug == d.slug))
            for brand in d.label_brands:
                try:
                    result = self.labels.ingest(self.session, drug, brand, d.label_manufacturer)
                except TransportError as exc:
                    self._fail(f"label:{brand}", exc)
                    continue
                if result is None:
                    self._fail(f"label:{brand}", FixtureMissing(f"No label record available for {brand}", service="openfda"))
                else:
                    self.report.labels.append(brand)
            if d.label_watch_generic:
                try:
                    watched[d.slug] = self.labels.watch_generic(self.session, drug, d.label_watch_generic)
                except TransportError as exc:
                    self._fail(f"label-watch:{d.slug}", exc)
        return {"labels": self.report.labels, "investigational_label_checks": watched}

    def ingest_faers(self) -> dict:
        for d in loader.read_drugs(self.settings.curated_dir).drugs:
            if not d.faers_generic:
                continue
            drug = self.session.scalar(select(Drug).where(Drug.slug == d.slug))
            try:
                self.report.faers.append(self.faers.ingest_drug(self.session, drug, d.faers_generic))
            except TransportError as exc:
                self._fail(f"faers:{d.slug}", exc)
        try:
            self.report.faers.append(self.faers.ingest_background(self.session))
        except TransportError as exc:
            self._fail("faers:background", exc)
        return {"series": len(self.report.faers)}

    def discover(self) -> dict:
        terms = loader.read_watchlist(self.settings.curated_dir).discovery_terms
        self.report.discovered = self.ctgov.discover(self.session, terms)
        return {"new_trials_indexed": self.report.discovered}

    def load_curated(self) -> dict:
        loader.load_drug_evidence(self.session, self.settings.curated_dir)
        stats: dict[str, Any] = {"studies": {}}
        seen: dict[str, int] = {}
        for file in loader.study_files(self.settings.curated_dir):
            n = seen.get(file.study, 0)
            counts = loader.load_study_evidence(self.session, file, reset=(n == 0), tag=f"f{n}:")
            seen[file.study] = n + 1
            previous = stats["studies"].get(file.study, {})
            stats["studies"][file.study] = {k: previous.get(k, 0) + v for k, v in counts.items()}
        stats["claims"] = loader.load_claims(self.session, self.settings.curated_dir)
        self.report.curated = stats
        return stats

    def refresh_gaps(self) -> dict:
        from ..engines.gaps import refresh_gaps

        return {"gaps": refresh_gaps(self.session)}

    # -- entry point
    def run(self, *, demo_change: bool = True, only: list[str] | None = None, skip: set[str] | None = None) -> IngestReport:
        skip = skip or set()
        self._run("curated-sources", lambda: {"sources": loader.load_sources(self.session, self.settings.curated_dir), "drugs": len(loader.load_drug_identities(self.session, self.settings.curated_dir))})
        # The curated load below completes the picture (arm annotations, curated rows), so the full reconcile waits for it.
        self._run("ctgov", lambda: self.ingest_studies(demo_change=demo_change, only=only), review="hold")
        if "pubmed" not in skip:
            self._run("pubmed", self.ingest_publications)
        if "labels" not in skip:
            self._run("openfda-label", self.ingest_labels, review="none")
        if "faers" not in skip:
            self._run("openfda-faers", self.ingest_faers)
        if "discover" not in skip:
            self._run("ctgov-discovery", self.discover)
        self._run("curated-evidence", self.load_curated)
        if not self._reconciled:
            # The curated load failed and was rolled back: reconcile what is there rather than leave rows unkeyed.
            self._run("review-reconcile", lambda: {})
        self._run("gap-engine", self.refresh_gaps)
        audit(self.session, "ingestion.completed", mode=self.transport.mode, studies=len(self.report.studies), failures=len(self.report.failures), review=self.report.review)
        self.session.commit()
        return self.report
