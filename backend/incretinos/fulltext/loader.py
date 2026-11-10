"""The full-text step of the pipeline: fetch, gate by licence, parse, extract, record limitations.

For every document in ``curation/data/fulltext.yaml``:

1. Metadata and licence. Europe PMC records are looked up with the search API (``resultType=core``); the licence
   is stored exactly as returned, and the JATS ``<license>`` is compared with it. ``licence.classify`` decides
   whether the full text may be stored. The FDA review returns no licence; its basis is configured and stated.
2. Fetch. Offline, every resource comes from the committed fixtures (``fixtures/europepmc``, ``fixtures/fdadocs``),
   and ``fixtures/fulltext/manifest.json`` says when and from where each was retrieved and, for the FDA review, the
   hash, size and page count of the document as retrieved (the fixture is a page excerpt). A full text that may
   not be stored is never written to the fixtures.
3. Parse and extract (``jats``, ``pdf``, ``mapping``): tables are stored with their structure; configured places
   become evidence rows of origin FULLTEXT, each linked to the exact cells or sentence it was read from, with the
   parsing method and the quality assessment.
4. Anything that cannot be fetched, matched or parsed reliably is recorded as a limitation on the document and
   raised as a review-queue item; no row is produced for it.

All FULLTEXT rows, their endpoints and estimands, the documents, tables and extractions are rebuilt on every run.
Their evidence keys are derived from the document, table label, row label and column heading (or the paragraph and
the statistic's lead phrase), so a reseed gives the same keys. Every row is PENDING_HUMAN_REVIEW.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..enums import AeMeasureKind, EstimandBasis, EstimandLabel, Origin, TriState, VerificationStatus
from ..ingestion.snapshots import audit, enqueue_review, record_snapshot
from ..ingestion.transport import FixtureMissing, ResourceNotFound, Transport, TransportError
from ..models import (
    AdverseEvent,
    Dose,
    Endpoint,
    Estimand,
    EvidenceClaim,
    EvidenceSource,
    ExposureEquivalence,
    ExtractedTable,
    Formulation,
    FullTextDocument,
    FullTextExtraction,
    IntercurrentEventStrategy,
    OutcomeResult,
    StatisticalAnalysis,
    Study,
    StudyArm,
)
from ..vocab import DirectionBasis
from . import jats, mapping, pdf
from .licence import LicenceDecision, classify
from .tables import (
    BodyRow,
    ParsedTable,
    ci_level_in,
    clean,
    read_count_percent,
    read_estimate_ci,
    read_header_n,
    read_mean_dispersion,
    read_p_value,
)

FULLTEXT = Origin.FULLTEXT.value
PENDING = VerificationStatus.PENDING_HUMAN_REVIEW.value
MANIFEST = "fulltext/manifest.json"
CONFIG_FILE = "fulltext.yaml"
LIMITATION_ITEM = "FULLTEXT_LIMITATION"


def read_config(curated_dir: Path) -> list[dict[str, Any]]:
    path = Path(curated_dir) / CONFIG_FILE
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return list((yaml.safe_load(handle) or {}).get("documents") or [])


def read_manifest(fixture_dir: Path) -> dict[str, Any]:
    path = Path(fixture_dir) / MANIFEST
    if not path.is_file():
        return {"documents": {}, "resources": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _short(text: str | None, limit: int = 300) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- per-document state


@dataclass
class _Doc:
    spec: dict[str, Any]
    row: FullTextDocument
    source: EvidenceSource | None = None
    decision: LicenceDecision | None = None
    tables: dict[str, tuple[ParsedTable, ExtractedTable]] = field(default_factory=dict)
    excerpts: list[str] = field(default_factory=list)
    limitations: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {"tables": 0, "result": 0, "analysis": 0, "adverse_event": 0, "claim": 0})
    content: bytes | None = None
    content_hash: str | None = None
    retrieved_at: datetime | None = None
    estimands: dict[tuple[int, str], Estimand] = field(default_factory=dict)
    passages: dict[str, Any] = field(default_factory=dict)

    def limit(self, code: str, where: str, text: str) -> None:
        self.limitations.append({"code": code, "where": where, "text": text})


class FullTextStep:
    """One run of the full-text step (see the module docstring)."""

    def __init__(self, session: Session, transport: Transport, *, curated_dir: Path, fixture_dir: Path, record: bool = False):
        self.session = session
        self.transport = transport
        self.curated_dir = Path(curated_dir)
        self.fixture_dir = Path(fixture_dir)
        self.record = record and transport.mode == "LIVE"
        self.offline = transport.mode != "LIVE"
        self.manifest = read_manifest(self.fixture_dir)
        self._endpoints: dict[tuple[int, str], Endpoint] = {}
        self._arms: dict[int, list[mapping.ArmCandidate]] = {}
        self._equivalences: dict[tuple[str | None, float], set[float]] | None = None

    # ------------------------------------------------------------------ entry point
    def run(self) -> dict[str, Any]:
        self.clear()
        stats: dict[str, Any] = {"documents": {}, "limitations": 0}
        for spec in read_config(self.curated_dir):
            doc = self._document(spec)
            stats["documents"][spec["key"]] = {**doc.counts, "fetch": doc.row.fetch_status, "parse": doc.row.parse_status,
                                               "full_text_stored": doc.row.full_text_stored, "limitations": len(doc.limitations)}
            stats["limitations"] += len(doc.limitations)
        if self.record:
            self.transport.record(MANIFEST, (json.dumps(self.manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        return stats

    def clear(self) -> None:
        """Remove every FULLTEXT row and every document, table and extraction: the step rebuilds them all."""
        s = self.session
        for model in (OutcomeResult, StatisticalAnalysis, AdverseEvent, EvidenceClaim):
            s.execute(delete(model).where(model.origin == FULLTEXT))
        s.execute(delete(Endpoint).where(Endpoint.origin == FULLTEXT))
        estimand_ids = select(Estimand.id).where(Estimand.origin == FULLTEXT)
        s.execute(delete(IntercurrentEventStrategy).where(IntercurrentEventStrategy.estimand_id.in_(estimand_ids)))
        s.execute(delete(Estimand).where(Estimand.origin == FULLTEXT))
        s.execute(delete(FullTextExtraction))
        s.execute(delete(ExtractedTable))
        s.execute(delete(FullTextDocument))
        s.flush()

    # ------------------------------------------------------------------ one document
    def _document(self, spec: dict[str, Any]) -> _Doc:
        row = FullTextDocument(key=spec["key"], kind=spec["kind"], title=spec.get("title") or spec["key"], url=spec.get("url"), pmcid=spec.get("pmcid"),
                               application_number=spec.get("application_number"), extraction_scope=spec.get("extraction_scope"), limitations=[], details={},
                               full_text_storable=False, full_text_stored=False, fetch_status="NOT_FETCHED", parse_status="NOT_ATTEMPTED")
        self.session.add(row)
        self.session.flush()
        doc = _Doc(spec=spec, row=row)
        try:
            if spec["kind"] == "JATS":
                self._jats(doc)
            elif spec["kind"] == "PDF":
                self._pdf(doc)
            else:
                doc.limit("UNSUPPORTED_KIND", spec["key"], f"document kind {spec['kind']!r} is not supported")
        except (TransportError, ValueError, KeyError) as exc:
            # A document that fails part-way keeps what was established before the failure; nothing is invented after it.
            doc.limit("PROCESSING_FAILED", spec["key"], f"{type(exc).__name__}: {exc}")
            if row.parse_status in ("NOT_ATTEMPTED", "PARSED"):
                row.parse_status = "FAILED" if not any(doc.counts.values()) else "PARTIAL"
        for note in spec.get("not_extracted") or []:
            doc.limit("NOT_EXTRACTED", "configuration", note)
        self._finish(doc)
        return doc

    def _finish(self, doc: _Doc) -> None:
        row = doc.row
        row.limitations = doc.limitations
        if doc.limitations and row.parse_status == "PARSED" and any(lim["code"] not in ("NOT_EXTRACTED", "MIXED_AUTHORSHIP", "NO_EXTRACTION_CONFIGURED", "LICENCE_RESTRICTS_STORAGE") for lim in doc.limitations):
            row.parse_status = "PARTIAL"
        # The source snapshot is written last, with the short quotes the rows cite as its excerpts.
        if doc.source is None and doc.content_hash:
            doc.source = self._snapshot(doc)
            row.source_id = doc.source.id
        elif doc.source is not None and doc.excerpts:
            self._snapshot(doc)
        for lim in doc.limitations:
            enqueue_review(
                self.session, item_type=LIMITATION_ITEM, target_type="fulltext_document", target_id=row.id,
                title=f"Full text: {row.title[:120]} — {lim['code'].replace('_', ' ').lower()}", reason=f"{lim['where']}: {lim['text']}",
                priority="MEDIUM" if lim["code"] in ("NOT_EXTRACTED", "MIXED_AUTHORSHIP", "NO_EXTRACTION_CONFIGURED", "LICENCE_RESTRICTS_STORAGE") else "HIGH",
                payload={"document": row.key, **lim}, dedupe_key=f"fulltext:{row.key}:{lim['code']}:{sha256((lim['where'] + lim['text']).encode())[:16]}"[:200],
            )
        audit(self.session, "fulltext.document_processed", target_type="fulltext_document", target_id=row.key, fetch=row.fetch_status, parse=row.parse_status,
              full_text_stored=row.full_text_stored, counts=doc.counts, limitations=len(doc.limitations))
        self.session.flush()

    def _snapshot(self, doc: _Doc) -> EvidenceSource:
        spec, row = doc.spec, doc.row
        stored = row.full_text_stored and doc.content is not None
        outcome = record_snapshot(
            self.session,
            key=f"fulltext:{row.key}",
            source_type="FULLTEXT_JATS" if row.kind == "JATS" else "FULLTEXT_PDF",
            tier=spec.get("tier") or "SECONDARY",
            title=row.title,
            content_hash=doc.content_hash or "",
            content=doc.content.decode("utf-8") if stored else None,
            url=row.url,
            publisher=spec.get("publisher") or (row.details or {}).get("journal"),
            published_date=(row.details or {}).get("published_date"),
            version_label=row.version_label,
            license_note=(doc.decision.reason if doc.decision else None),
            content_type="application/jats+xml" if row.kind == "JATS" else "application/pdf",
            excerpts=sorted(set(doc.excerpts)),
            external_ids={k: v for k, v in {"pmcid": row.pmcid, "pmid": row.pmid, "doi": row.doi, "application_number": row.application_number}.items() if v},
            retrieved_at=doc.retrieved_at,
            byte_size=row.byte_size,
        )
        return outcome.source

    # ------------------------------------------------------------------ fetching
    def _get(self, service: str, path: str, params: dict | None, fixture_key: str, *, recordable: bool) -> bytes:
        """Live: fetch (and record only when the content may be redistributed). Offline: read the fixture."""
        if self.offline:
            return self.transport.get(service, path, params, fixture_key=fixture_key)
        return self.transport.get(service, path, params, fixture_key=fixture_key if (self.record and recordable) else None)

    def _resource_time(self, fixture_key: str) -> datetime | None:
        if self.offline:
            return _parse_time((self.manifest.get("resources") or {}).get(fixture_key, {}).get("retrieved_at"))
        now = datetime.now(UTC)
        return now

    def _note_resource(self, fixture_key: str, url: str, content: bytes, *, recorded: bool) -> None:
        if not self.record:
            return
        self.manifest.setdefault("resources", {})[fixture_key] = {
            "url": url, "status": 200, "sha256": sha256(content), "bytes": len(content), "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "recorded": recorded,
        }

    # ------------------------------------------------------------------ JATS
    def _jats(self, doc: _Doc) -> None:
        spec, row = doc.spec, doc.row
        pmcid = spec["pmcid"]
        meta_key = f"europepmc/search/{pmcid}.json"
        params = {"query": f"PMCID:{pmcid}", "resultType": "core", "format": "json"}
        raw = self._get("europepmc", "/search", params, meta_key, recordable=True)
        self._note_resource(meta_key, f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMCID:{pmcid}&resultType=core&format=json", raw, recorded=True)
        results = (json.loads(raw).get("resultList") or {}).get("result") or []
        meta = next((r for r in results if r.get("pmcid") == pmcid), None)
        if meta is None:
            doc.limit("METADATA_NOT_FOUND", "Europe PMC search", f"the search for PMCID:{pmcid} returned no record")
            return
        journal = ((meta.get("journalInfo") or {}).get("journal") or {}).get("title")
        row.title = meta.get("title") or row.title
        row.pmid, row.doi = meta.get("pmid"), meta.get("doi")
        row.url = row.url or f"https://europepmc.org/article/PMC/{pmcid}"
        row.licence = meta.get("license")
        row.open_access_flag = meta.get("isOpenAccess")
        row.details = {"journal": journal, "published_date": (meta.get("journalInfo") or {}).get("printPublicationDate") or meta.get("firstPublicationDate"),
                       "in_epmc": meta.get("inEPMC"), "has_pdf": meta.get("hasPDF"), "related_sources": [f"pubmed:{meta['pmid']}"] if meta.get("pmid") else [],
                       "population": spec.get("population")}
        decision = classify(row.licence)
        doc.decision = decision
        row.full_text_storable = decision.storable
        row.licence_basis = (f"Europe PMC search API (resultType=core): license={row.licence!r}, isOpenAccess={row.open_access_flag!r}. "
                             f"Reading: {decision.reason}. The isOpenAccess flag alone is not treated as permission to store.")
        row.storage_basis = decision.reason
        if not decision.storable:
            doc.limit("LICENCE_RESTRICTS_STORAGE", "licence", f"{decision.reason}. The full text is not stored and never written to the offline fixtures.")
        row.retrieved_at = self._resource_time(meta_key)
        wants_text = bool(spec.get("tables") or spec.get("sentences") or spec.get("claims") or spec.get("estimands"))
        if row.open_access_flag != "Y":
            doc.limit("NOT_OPEN_ACCESS", "Europe PMC", f"isOpenAccess={row.open_access_flag!r}: the full text is not available from Europe PMC; metadata only")
            return
        if not wants_text:
            doc.limit("NO_EXTRACTION_CONFIGURED", "configuration", "no table or sentence of this document is configured for extraction; metadata and licence only")
            return
        text_key = f"europepmc/fulltext/{pmcid}.xml"
        if self.offline and not decision.storable and not (self.fixture_dir / text_key).is_file():
            row.fetch_status = "NOT_FETCHED"
            doc.limit("LICENCE_BLOCKS_FIXTURE", "offline fixtures",
                      f"{decision.reason}. The full text may not be redistributed, so no offline fixture of it exists; its tables can be read only by a live run, "
                      "which keeps the extracted cells and short quotes and never the text. No rows in this build.")
            return
        try:
            xml = self._get("europepmc", f"/{pmcid}/fullTextXML", None, text_key, recordable=decision.storable)
        except (FixtureMissing, ResourceNotFound) as exc:
            row.fetch_status = "FAILED"
            doc.limit("FULL_TEXT_NOT_FETCHED", "Europe PMC fullTextXML", f"{type(exc).__name__}: {exc}")
            return
        self._note_resource(text_key, f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML", xml, recorded=decision.storable)
        row.fetch_status = "FETCHED"
        row.content_hash, row.byte_size = sha256(xml), len(xml)
        doc.content, doc.content_hash = xml, row.content_hash
        doc.retrieved_at = self._resource_time(text_key) or row.retrieved_at
        try:
            root = jats.parse(xml)
        except Exception as exc:  # malformed XML: recorded, nothing extracted
            row.parse_status = "FAILED"
            doc.limit("UNPARSEABLE", "JATS XML", f"the full text could not be parsed as XML ({type(exc).__name__}: {exc})")
            return
        lic = jats.licence(root)
        row.licence_url = lic.url
        jats_decision = classify(None, lic.url) if lic.url else classify(lic.text)
        row.details = {**row.details, "jats_licence": {"url": lic.url, "content_type": lic.content_type, "text": _short(lic.text, 400)}}
        if jats_decision.storable != decision.storable or (jats_decision.normalised and decision.normalised and jats_decision.normalised != decision.normalised):
            doc.limit("LICENCE_DISAGREEMENT", "licence", f"Europe PMC returns {row.licence!r} but the article's own <license> reads {lic.url or _short(lic.text, 120)!r}; "
                                                         "the stricter reading is applied")
            if not jats_decision.storable:
                decision = jats_decision
                doc.decision = decision
                row.full_text_storable = False
        row.full_text_stored = row.full_text_storable
        row.licence_basis += f" The article's JATS <license> names {lic.url or 'no licence URL'}."
        row.version_label = jats.article_ids(root).get("pmcid-ver")
        row.parse_status = "PARSED"
        doc.source = self._snapshot(doc)
        row.source_id = doc.source.id
        self.session.flush()
        configured = {t["label"] for t in spec.get("tables") or []}
        denominator_table = (spec.get("denominators") or {}).get("table")
        for wrap in jats.table_wraps(root):
            table = jats.parse_table(root, wrap)
            # Under a licence that does not allow storing the text, only the tables something is extracted from are kept.
            if not row.full_text_storable and table.label not in configured and table.label != denominator_table:
                continue
            self._store_table(doc, table)
        for table_spec in spec.get("tables") or []:
            self._table_extractions(doc, table_spec)
        self._jats_statements(doc, root)

    def _jats_statements(self, doc: _Doc, root: Any) -> None:
        spec = doc.spec
        study = self._study(spec.get("study")) if spec.get("study") else None
        if (spec.get("sentences") or spec.get("claims") or spec.get("estimands")) and study is None:
            doc.limit("STUDY_NOT_FOUND", "configuration", f"study {spec.get('study')} is not in the database")
            return
        for name, est in (spec.get("estimands") or {}).items():
            sentence = self._sentence(doc, root, est.get("sentence") or {}, where=f"estimand {name}")
            if sentence is not None:
                doc.passages[name] = sentence
        for claim in spec.get("claims") or []:
            found = self._sentence(doc, root, claim, where=f"claim {claim.get('topic')}")
            if found is None:
                continue
            paragraph, text = found
            self._claim(doc, study, topic=claim["topic"], ident=f"{claim['topic']}:{paragraph.paragraph_id or sha256(text.encode())[:8]}", statement=text,
                        locator=f"{paragraph.section}, paragraph {paragraph.paragraph_id or '(no id)'}", table=None,
                        passage={"section": paragraph.section, "paragraph_id": paragraph.paragraph_id, "sentence": text}, method=jats.SENTENCE_METHOD)
        denominators = self._denominators(doc, study)
        for sentence_spec in spec.get("sentences") or []:
            self._jats_statistic(doc, root, study, sentence_spec, denominators)

    def _sentence(self, doc: _Doc, root: Any, spec: dict[str, Any], *, where: str) -> tuple[jats.Paragraph, str] | None:
        anchor, section = spec.get("anchor"), spec.get("section")
        if not anchor:
            return None
        count = jats.count_sentences(root, anchor, section=section)
        if count != 1:
            doc.limit("ANCHOR_NOT_UNIQUE" if count else "ANCHOR_NOT_FOUND", where,
                      f"the phrase {anchor!r} occurs in {count} sentences of section {section!r}; exactly one is required")
            return None
        return jats.find_sentence(root, anchor, section=section)

    def _denominators(self, doc: _Doc, study: Study | None) -> dict[str, Any]:
        spec = doc.spec.get("denominators") or {}
        out: dict[str, Any] = {}
        if not spec or spec.get("table") not in doc.tables:
            return out
        table, stored = doc.tables[spec["table"]]
        headers = table.column_headers()
        for role in ("treatment", "comparator"):
            cols = [c for c, h in headers.items() if c >= 1 and re.search(spec[role], h, re.IGNORECASE)]
            if len(cols) != 1:
                doc.limit("DENOMINATOR_NOT_UNIQUE", f"{spec['table']} heading {spec[role]!r}", f"{len(cols)} column headings match; denominators are not read")
                continue
            header = headers[cols[0]]
            out[role] = {"table": stored, "parsed": table, "col": cols[0], "header": header, "n": read_header_n(header)}
        return out

    def _jats_statistic(self, doc: _Doc, root: Any, study: Study, sp: dict[str, Any], denominators: dict[str, Any]) -> None:
        where = f"sentence {sp['id']}"
        found = self._sentence(doc, root, sp, where=where)
        if found is None:
            return
        paragraph, sentence = found
        lead = sp["statistic"]["lead"]
        flat = clean(sentence)
        at = flat.lower().find(clean(lead).lower())
        if at < 0:
            doc.limit("STATISTIC_NOT_FOUND", where, f"the lead phrase {lead!r} is not in the sentence")
            return
        tail = flat[at + len(clean(lead)):]
        m = re.match(r"\s*:?\s*(-?\d+(?:\.\d+)?\s?%?\s?[\(\[]\s?-?\d+(?:\.\d+)?\s?,\s?-?\d+(?:\.\d+)?\s?[\)\]])\s?(?:;\s?(p\s?[<=>]\s?0?\.\d+))?", tail)
        interval = read_estimate_ci(m.group(1)) if m else None
        if interval is None:
            doc.limit("INTERVAL_NOT_PARSED", where, f"no 'estimate (low, high)' pattern follows {lead!r} in the sentence")
            return
        more = re.findall(r"-?\d+(?:\.\d+)?\s?%?\s?[\(\[]\s?-?\d+(?:\.\d+)?\s?,\s?-?\d+(?:\.\d+)?\s?[\)\]]", flat)
        unit = "%" if "%" in m.group(1) else (re.search(r"\b(cm|kg|mg/dL)\b", m.group(1)).group(1) if re.search(r"\b(cm|kg|mg/dL)\b", m.group(1)) else None)
        level = ci_level_in(flat[:at + len(lead)]) or ci_level_in(flat)
        p_text = read_p_value(m.group(2)) if m.group(2) else None
        population = (doc.spec.get("population") or {})
        treatment = denominators.get("treatment")
        comparator = denominators.get("comparator")
        if not treatment or not comparator:
            doc.limit("GROUPS_NOT_IDENTIFIED", where, "the compared groups could not be read from the denominator table headings; not extracted")
            return
        t_match = self._match(study, treatment["header"], {}, subset=population.get("label"))
        c_match = self._match(study, comparator["header"], {}, subset=population.get("label"))
        endpoint = self._endpoint(doc, study, sp["endpoint"], caption=None, locator=f"{paragraph.section}, paragraph {paragraph.paragraph_id}")
        estimand, estimand_quality = self._jats_estimand(doc, study)
        method_text = None
        if sp.get("method"):
            method_found = self._sentence(doc, root, sp["method"], where=f"{where} method")
            method_text = method_found[1] if method_found else None
        cells = [{"role": "sentence", "text": sentence, "section": paragraph.section, "paragraph_id": paragraph.paragraph_id}]
        for role, den in (("denominator_treatment", treatment), ("denominator_comparator", comparator)):
            if den:  # always true here; kept explicit
                cells.append({"role": role, "table": den["table"].label, "row_index": None, "col_index": den["col"], "column_header": den["header"], "text": den["header"]})
        quality = {
            "value_source": "running text (a sentence of the Results section), not a table",
            "statistic": {"lead": lead, "matched": m.group(0).strip(), "other_intervals_in_sentence": len(more) - 1},
            "interval": "PARSED" if not interval.irregular else f"PARSED_IRREGULAR: {interval.irregular}",
            "unit": {"value": unit, "found_in": ["the printed estimate"] if unit else []},
            "ci_level": {"value": level, "found_in": "the sentence" if level else "not stated"},
            "estimand": estimand_quality,
            "arm_match": t_match.as_dict(), "comparator_match": c_match.as_dict(),
            "population": population.get("label"), "subgroup": population.get("kind"),
            "denominators": {"treatment": treatment["n"], "comparator": comparator["n"], "source": f"column headings of {treatment['table'].label}"},
            "method": {"text": _short(method_text, 400), "source": f"{sp['method']['section']} paragraph" if method_text else "not stated"},
        }
        quote = sentence
        extraction = self._extraction(doc, None, "analysis", target=f"{sp['statistic']['param_type']} with {int(level) if level else '?'}% CI", row_label=None,
                                      column_header=treatment["header"], comparator_header=comparator["header"],
                                      cells=cells, passage={"section": paragraph.section, "paragraph_id": paragraph.paragraph_id, "sentence": sentence, "lead": lead},
                                      method=jats.SENTENCE_METHOD, version=jats.PARSE_VERSION, quality=quality)
        missing = [name for name, value in (("unit", unit), ("ci_level", level), ("p_value_text", p_text), ("method", method_text)) if value is None]
        analysis = StatisticalAnalysis(
            study_id=study.id, endpoint_id=endpoint.id, estimand_id=estimand.id if estimand else None, origin=FULLTEXT,
            treatment_arm_id=t_match.arm_id, comparator_arm_id=c_match.arm_id,
            treatment_label=self._subset_label(treatment["header"], population.get("label")),
            comparator_label=self._subset_label(comparator["header"], population.get("label")),
            group_labels=[treatment["header"], comparator["header"]], direction_basis=DirectionBasis.DOCUMENT.value,
            class_title=f"Subgroup: {population['label']}" if population.get("label") else None, hypothesis_type="NOT_STATED",
            param_type=sp["statistic"]["param_type"], estimate=interval.estimate, estimate_text=interval.text, unit=unit, ci_low=interval.low, ci_high=interval.high,
            ci_level=level, p_value_text=p_text, method=_short(method_text, 1000), timepoint_weeks=endpoint.timepoint_weeks,
            source_id=doc.source.id, source_location=f"{paragraph.section}, paragraph {paragraph.paragraph_id}; statistic after “{lead}”", locator_quote=quote,
            tier=doc.spec.get("tier"), verification_status=PENDING, missing_fields=missing, fulltext_extraction_id=extraction.id,
            notes=self._notes(population, extraction),
        )
        self.session.add(analysis)
        doc.excerpts.append(_short(sentence, 600))
        doc.counts["analysis"] += 1

    def _jats_estimand(self, doc: _Doc, study: Study) -> tuple[Estimand | None, dict[str, Any]]:
        specs = doc.spec.get("estimands") or {}
        if not specs:
            return None, {"label": None, "in_table": False, "stated_in": "not stated"}
        name, est = next(iter(specs.items()))
        found = doc.passages.get(name)
        if found is None:
            return None, {"label": None, "in_table": False, "stated_in": "not found (the configured sentence was not found)"}
        paragraph, sentence = found
        key = (study.id, name)
        if key not in doc.estimands:
            estimand = Estimand(study_id=study.id, origin=FULLTEXT, label=est["label"], basis=est["basis"], description=_short(est.get("description"), 1000),
                                population_text=(doc.spec.get("population") or {}).get("label"), source_id=doc.source.id, quote=sentence)
            self.session.add(estimand)
            self.session.flush()
            doc.estimands[key] = estimand
        return doc.estimands[key], {"label": est["label"], "basis": est["basis"], "in_table": False,
                                    "stated_in": f"{paragraph.section}, paragraph {paragraph.paragraph_id} (not in the sentence the value is read from)", "quote": sentence}

    # ------------------------------------------------------------------ PDF
    def _pdf(self, doc: _Doc) -> None:
        spec, row = doc.spec, doc.row
        decision = classify(spec.get("licence"), us_government_work=bool(spec.get("us_government_work")))
        doc.decision = decision
        row.licence = spec.get("licence")
        row.licence_basis = clean(spec.get("licence_basis") or decision.reason)
        row.full_text_storable = decision.storable
        row.storage_basis = ("Mixed authorship (FDA-authored and applicant-authored sections): the PDF is not stored. Kept: metadata, the SHA-256 of the "
                             "document as retrieved, the cells of the extracted FDA-analysis tables and short locator quotes.") if not decision.storable else decision.reason
        if not decision.storable:
            doc.limit("MIXED_AUTHORSHIP", "licence", row.storage_basis)
        row.details = {"publisher": spec.get("publisher"), "related_sources": spec.get("related_sources") or []}
        pages_wanted = sorted(set(spec.get("pages") or []))
        entry = (self.manifest.get("documents") or {}).get(spec["key"], {})
        try:
            if self.offline:
                if not entry:
                    raise FixtureMissing(f"no manifest entry for {spec['key']} in {MANIFEST}", service=spec.get("service", "fdadocs"))
                data = self.transport.get(spec.get("service", "fdadocs"), spec["path"], None, fixture_key=spec["fixture"])
                if sha256(data) != entry.get("fixture_sha256"):
                    raise ValueError(f"the excerpt fixture's SHA-256 does not match the manifest ({spec['fixture']})")
                pages, _ = pdf.read_pages(data, pages_wanted, numbering=entry["excerpt_pages"])
                row.content_hash, row.byte_size, row.page_count = entry["sha256"], entry["bytes"], entry["page_count"]
                row.retrieved_at = _parse_time(entry.get("retrieved_at"))
                row.details = {**row.details, "fixture": {"path": spec["fixture"], "sha256": entry["fixture_sha256"], "pages": entry["excerpt_pages"],
                                                          "note": "page excerpt; text outside the extracted tables, the configured FDA passages and the running header/footer is blanked"}}
            else:
                data = self._get(spec.get("service", "fdadocs"), spec["path"], None, spec["fixture"], recordable=False)
                pages, total = pdf.read_pages(data, pages_wanted)
                row.content_hash, row.byte_size, row.page_count = sha256(data), len(data), total
                row.retrieved_at = datetime.now(UTC)
        except (FixtureMissing, ResourceNotFound) as exc:
            row.fetch_status = "FAILED"
            doc.limit("NOT_FETCHED", spec.get("url") or spec["path"], f"{type(exc).__name__}: {exc}")
            return
        except Exception as exc:  # an unreadable PDF: recorded, nothing extracted
            row.fetch_status = "FETCHED"
            row.parse_status = "FAILED"
            doc.limit("UNPARSEABLE", "PDF", f"the PDF could not be read ({type(exc).__name__}: {exc})")
            return
        row.fetch_status = "FETCHED"
        doc.content_hash = row.content_hash
        doc.retrieved_at = row.retrieved_at
        missing_pages = [p for p in pages_wanted if p not in {pg.number for pg in pages}]
        if missing_pages:
            doc.limit("PAGES_NOT_READ", "PDF", f"configured pages not present in the document read: {missing_pages}")
        if not any(ln for pg in pages for ln in pg.lines):
            row.parse_status = "FAILED"
            doc.limit("NO_TEXT_LAYER", "PDF", "no positioned text on the configured pages (a scanned PDF?); nothing is extracted")
            return
        row.parse_status = "PARSED"
        doc.source = self._snapshot(doc)
        row.source_id = doc.source.id
        self.session.flush()
        for passage in spec.get("passages") or []:
            found = pdf.find_passage(pages, passage["page"], passage["starts_with"])
            if found is None:
                doc.limit("PASSAGE_NOT_FOUND", f"PDF page {passage['page']}", f"no sentence starting {passage['starts_with']!r}")
                continue
            if found.authored_by_fda is not True:
                doc.limit("PASSAGE_NOT_FDA_AUTHORED", f"PDF page {passage['page']}",
                          f"the sentence {_short(found.sentence, 80)!r} is not under an FDA's Assessment heading on the pages read (heading: {found.heading!r}); not extracted")
                continue
            doc.passages[passage["id"]] = found
            for nct in (passage.get("claim") or {}).get("studies") or []:
                study = self._study(nct)
                if study is None:
                    doc.limit("STUDY_NOT_FOUND", passage["id"], f"study {nct} is not in the database")
                    continue
                self._claim(doc, study, topic=passage["claim"]["topic"], ident=f"{passage['id']}", statement=found.sentence,
                            locator=f"PDF page {found.page}, under “{found.heading}” (heading on PDF page {found.heading_page})", table=None,
                            passage={"page": found.page, "heading": found.heading, "heading_page": found.heading_page, "sentence": found.sentence},
                            method=pdf.PARSE_METHOD + "/sentence")
        for table_spec in spec.get("tables") or []:
            table = pdf.extract_table(pages, table_spec["label"])
            if table is None:
                doc.limit("TABLE_NOT_FOUND", table_spec["label"], f"no caption '{table_spec['label']}.' on the pages read")
                continue
            if table.authorship != "FDA_ANALYSIS":
                doc.limit("TABLE_NOT_FDA_ANALYSIS", table.locator, f"source line {table.source_line!r} does not name an FDA analysis; the table is not extracted")
                continue
            self._store_table(doc, table)
            self._table_extractions(doc, table_spec)
        if self.record:
            self._record_excerpt(doc, data, pages)

    def _record_excerpt(self, doc: _Doc, data: bytes, pages: list[pdf.PageText]) -> None:
        """Write the page excerpt fixture: the configured pages, with only the extracted tables, the FDA passages and
        the running header and footer left printed, and its manifest entry (hash, size and page count of the original)."""
        spec, row = doc.spec, doc.row
        bands: dict[int, list[tuple[float, float]]] = {p.number: [] for p in pages}
        for label in doc.tables:
            for number, low, high in pdf.table_region(pages, label):
                bands.setdefault(number, []).append((low, high))
        for passage in doc.passages.values():
            page = next(p for p in pages if p.number == passage.page)
            top_line = passage.heading_line if passage.heading_page == passage.page and passage.heading_line is not None else passage.first_line
            bands[passage.page].append((page.lines[passage.last_line].y - 4.0, page.lines[top_line].y + 12.0))
            if passage.heading_page != passage.page and passage.heading_page is not None and passage.heading_line is not None:
                heading_page = next(p for p in pages if p.number == passage.heading_page)
                y = heading_page.lines[passage.heading_line].y
                bands.setdefault(passage.heading_page, []).append((y - 4.0, y + 12.0))
        excerpt = pdf.excerpt(data, bands)
        self.transport.record(spec["fixture"], excerpt)
        self.manifest.setdefault("documents", {})[spec["key"]] = {
            "url": spec.get("url"), "status": 200, "sha256": row.content_hash, "bytes": row.byte_size, "page_count": row.page_count,
            "retrieved_at": (row.retrieved_at or datetime.now(UTC)).isoformat(timespec="seconds"), "fixture": spec["fixture"], "fixture_sha256": sha256(excerpt),
            "excerpt_pages": sorted(bands), "kept_bands": {str(k): [[round(a, 1), round(b, 1)] for a, b in v] for k, v in sorted(bands.items())},
            "note": "Page excerpt of the document as retrieved. Every glyph run outside the kept bands (the extracted FDA-analysis tables, the configured "
                    "sentences under 'The FDA's Assessment' with their heading, and the running header and footer) is blanked, so the fixture carries "
                    "no applicant-authored text. Page numbers are those of the original document.",
        }

    # ------------------------------------------------------------------ tables
    def _store_table(self, doc: _Doc, table: ParsedTable) -> ExtractedTable:
        stored = ExtractedTable(
            document_id=doc.row.id, key=f"{doc.row.key}#{table.label}"[:200], label=table.label, caption=table.caption, locator=table.locator, section=table.section,
            element_id=table.element_id, page_start=table.page_start, page_end=table.page_end, printed_page=table.printed_page, authorship=table.authorship,
            source_line=table.source_line, header_rows=table.header_json(), body_rows=table.rows_json(), footnotes=table.footnotes, abbreviations=table.abbreviations,
            units=table.units, n_columns=table.n_columns, parse_method=table.parse_method, parse_version=table.parse_version,
            structural_notes=list(table.notes) + ([f"panels: {json.dumps([{k: v for k, v in p.items() if k != 'header_cells'} for p in table.panels], ensure_ascii=False)}"] if table.panels else []),
            content_hash=table.content_hash(),
        )
        self.session.add(stored)
        self.session.flush()
        doc.tables[table.label] = (table, stored)
        doc.counts["tables"] += 1
        if not table.rows and table.notes:
            doc.limit("TABLE_WITHOUT_CELLS", table.locator, "; ".join(table.notes))
        return stored

    def _table_extractions(self, doc: _Doc, table_spec: dict[str, Any]) -> None:
        label = table_spec["label"]
        if label not in doc.tables:
            if not any(lim["where"].startswith(label) for lim in doc.limitations):
                doc.limit("TABLE_NOT_FOUND", label, "the configured table was not found in the document")
            return
        table, stored = doc.tables[label]
        expect = table_spec.get("expect")
        if expect and clean(expect).lower() not in clean(table.caption).lower():
            doc.limit("CAPTION_MISMATCH", table.locator, f"the caption {table.caption!r} does not contain the expected {expect!r}; nothing is extracted")
            return
        study = self._study(table_spec.get("study") or doc.spec.get("study"))
        if study is None:
            doc.limit("STUDY_NOT_FOUND", table.locator, f"study {table_spec.get('study') or doc.spec.get('study')} is not in the database")
            return
        for item in table_spec.get("extract") or []:
            kind = item["kind"]
            if kind == "differences":
                self._differences(doc, study, table, stored, table_spec, item)
            elif kind == "arm_estimates":
                self._arm_estimates(doc, study, table, stored, table_spec, item)
            elif kind == "footnote_claims":
                self._footnote_claims(doc, study, table, stored, item)
            elif kind == "adverse_events":
                self._adverse_events(doc, study, table, stored, item)
            else:
                doc.limit("UNKNOWN_EXTRACTION", table.locator, f"extraction kind {kind!r} is not supported")

    def _rows(self, doc: _Doc, table: ParsedTable, pattern: str, group: str | None, *, unique: bool = True) -> list[tuple[int, BodyRow]]:
        hits = table.find_rows(pattern, group=group)
        if unique and len(hits) != 1:
            doc.limit("ROW_NOT_UNIQUE" if hits else "ROW_NOT_FOUND", table.locator,
                      f"{len(hits)} rows match {pattern!r}" + (f" under {group!r}" if group else "") + "; exactly one is required, nothing is extracted from it")
            return []
        return hits

    def _differences(self, doc: _Doc, study: Study, table: ParsedTable, stored: ExtractedTable, tspec: dict[str, Any], item: dict[str, Any]) -> None:
        group = item.get("group")
        hits = self._rows(doc, table, item["row"], group)
        if not hits:
            return
        ri, row = hits[0]
        p_hits = table.find_rows(item["p_row"], group=group) if item.get("p_row") else []
        p_hits = [(i, r) for i, r in p_hits if i > ri]
        p_index, p_row = (p_hits[0] if p_hits else (None, None))
        headers = table.column_headers(row.panel)
        ref_cols = [c for c, h in headers.items() if c >= 1 and re.search(item["reference"], h, re.IGNORECASE)]
        if len(ref_cols) != 1:
            doc.limit("REFERENCE_COLUMN_NOT_UNIQUE", table.locator, f"{len(ref_cols)} column headings match the reference {item['reference']!r}; nothing is extracted")
            return
        ref_col = ref_cols[0]
        espec = item.get("endpoint") or tspec.get("endpoint")
        endpoint = self._endpoint(doc, study, espec, caption=table.caption, locator=table.locator, table=table)
        estimand, est_quality = self._pdf_estimand(doc, study, table, tspec)
        unit, unit_found = self._unit_for(table, ri, row)
        method, method_source = self._method_for(table, ri, row)
        level = ci_level_in(row.stub)
        ref_header = headers[ref_col]
        ref_match = self._match(study, ref_header, table.abbreviations)
        for col, text in sorted(row.cells.items()):
            if col == ref_col:
                continue
            header = headers.get(col, "")
            where = f"{table.locator}; row “{row.stub}”" + (f" under “{row.group}”" if row.group else "") + f"; column “{header}”"
            interval = read_estimate_ci(text)
            if interval is None:
                doc.limit("CELL_NOT_PARSED", where, f"the cell {text!r} is not an 'estimate (low, high)' interval; not extracted")
                continue
            t_match = self._match(study, header, table.abbreviations)
            p_text = p_row.cells.get(col) if p_row else None
            cells = [
                {"role": "estimate_and_interval", "row_index": ri, "col_index": col, "row_label": row.stub, "column_header": header, "text": text},
                {"role": "comparator_heading", "row_index": None, "col_index": ref_col, "row_label": None, "column_header": ref_header, "text": ref_header},
            ]
            if p_row is not None and p_text is not None:
                cells.append({"role": "p_value", "row_index": p_index, "col_index": col, "row_label": p_row.stub, "column_header": header, "text": p_text})
            for marker in [*row.footnote_refs, *(p_row.footnote_refs if p_row else [])]:
                note = table.footnote(marker)
                if note:
                    cells.append({"role": "footnote", "marker": marker, "text": note})
            if method_source and method:
                cells.append({"role": "method_footnote", "text": method, "via": method_source})
            p_note = None
            if p_row is not None and p_row.footnote_refs:
                p_note = "; ".join(f"footnote {m}: {table.footnote(m)}" for m in p_row.footnote_refs if table.footnote(m))
            quality = self._cell_quality(table, t_match, ref_match, interval, unit, unit_found, est_quality, method, method_source, level, row, p_text)
            parsed_p = read_p_value(p_text) if p_text else None
            extraction = self._extraction(doc, stored, "analysis", target=f"{row.stub} — difference with its {int(level) if level else ''}% CI".replace(" % CI", " CI"),
                                          row_label=self._row_label(row), column_header=header, comparator_header=ref_header, cells=cells, passage=None,
                                          method=table.parse_method, version=table.parse_version, quality=quality)
            quote = " | ".join(x for x in (table.caption, row.group, row.stub, header, text, *( [p_row.stub, p_text] if p_row is not None and p_text else [])) if x)
            missing = [n for n, v in (("unit", unit), ("ci_level", level), ("method", method), ("p_value_text", p_text)) if v is None]
            analysis = StatisticalAnalysis(
                study_id=study.id, endpoint_id=endpoint.id, estimand_id=estimand.id if estimand else None, origin=FULLTEXT,
                treatment_arm_id=t_match.arm_id, comparator_arm_id=ref_match.arm_id, treatment_label=header, comparator_label=ref_header,
                group_labels=[header, ref_header], direction_basis=DirectionBasis.DOCUMENT.value, class_title=row.group, hypothesis_type="NOT_STATED",
                param_type=row.stub, estimate=interval.estimate, estimate_text=text, unit=unit, ci_low=interval.low, ci_high=interval.high, ci_level=level,
                p_value_text=p_text if parsed_p is None else parsed_p, method=method, multiplicity_controlled=TriState.NOT_STATED.value,
                timepoint_weeks=endpoint.timepoint_weeks, comment=p_note, source_id=doc.source.id, source_location=where, locator_quote=quote,
                tier=doc.spec.get("tier"), verification_status=PENDING, missing_fields=missing, fulltext_extraction_id=extraction.id,
                notes=self._notes(doc.spec.get("population") or {}, extraction),
            )
            self.session.add(analysis)
            doc.excerpts.append(quote)
            doc.counts["analysis"] += 1

    def _arm_estimates(self, doc: _Doc, study: Study, table: ParsedTable, stored: ExtractedTable, tspec: dict[str, Any], item: dict[str, Any]) -> None:
        hits = self._rows(doc, table, item["row"], item.get("group"))
        if not hits:
            return
        ri, row = hits[0]
        headers = table.column_headers(row.panel)
        endpoint = self._endpoint(doc, study, item.get("endpoint") or tspec.get("endpoint"), caption=table.caption, locator=table.locator, table=table)
        estimand, est_quality = self._pdf_estimand(doc, study, table, tspec)
        unit, unit_found = self._unit_for(table, ri, row)
        method, method_source = self._method_for(table, ri, row)
        dispersion = "SE" if re.search(r"\(SE\)", row.stub) else ("SD" if re.search(r"\(SD\)", row.stub) else None)
        for col, text in sorted(row.cells.items()):
            header = headers.get(col, "")
            where = f"{table.locator}; row “{row.stub}”" + (f" under “{row.group}”" if row.group else "") + f"; column “{header}”"
            read = read_mean_dispersion(text)
            if read is None:
                doc.limit("CELL_NOT_PARSED", where, f"the cell {text!r} is not a 'value (dispersion)' pair; not extracted")
                continue
            match = self._match(study, header, table.abbreviations)
            n_header = read_header_n(header)
            cells = [{"role": "value_and_dispersion", "row_index": ri, "col_index": col, "row_label": row.stub, "column_header": header, "text": text}]
            for marker in row.footnote_refs:
                if table.footnote(marker):
                    cells.append({"role": "footnote", "marker": marker, "text": table.footnote(marker)})
            quality = self._cell_quality(table, match, None, None, unit, unit_found, est_quality, method, method_source, None, row, None)
            quality["dispersion"] = {"type": dispersion, "found_in": "row label" if dispersion else "not stated"}
            quality["denominator"] = {"n_in_heading": n_header, "meaning": table.abbreviations.get("N"), "stored_as": "analysis_population text (not n_analyzed: the heading N is the number randomized)"}
            extraction = self._extraction(doc, stored, "result", target=f"{row.stub} — arm-level estimate", row_label=self._row_label(row), column_header=header,
                                          comparator_header=None, cells=cells, passage=None, method=table.parse_method, version=table.parse_version, quality=quality)
            quote = " | ".join(x for x in (table.caption, row.group, row.stub, header, text) if x)
            population = f"Column heading N={n_header} ({table.abbreviations.get('N') or 'meaning of N not defined in the table'})" if n_header is not None else None
            result = OutcomeResult(
                study_id=study.id, endpoint_id=endpoint.id, arm_id=match.arm_id, estimand_id=estimand.id if estimand else None, origin=FULLTEXT, arm_label=header,
                scope="ARM", class_title=row.group, param_type=row.stub, value=read[0], value_text=text, unit=unit, dispersion_type=dispersion, dispersion_value=read[1],
                analysis_population=population, analysis_method=method, timepoint_weeks=endpoint.timepoint_weeks, timeframe_text=endpoint.timeframe_text,
                source_id=doc.source.id, source_location=where, locator_quote=quote, tier=doc.spec.get("tier"), verification_status=PENDING,
                missing_fields=[n for n, v in (("n_analyzed", None), ("ci_low", None), ("ci_high", None), ("unit", unit)) if v is None],
                fulltext_extraction_id=extraction.id, notes=self._notes(doc.spec.get("population") or {}, extraction),
            )
            self.session.add(result)
            doc.excerpts.append(quote)
            doc.counts["result"] += 1

    def _footnote_claims(self, doc: _Doc, study: Study, table: ParsedTable, stored: ExtractedTable, item: dict[str, Any]) -> None:
        for pattern in item["rows"]:
            hits = [(i, r) for i, r in table.find_rows(pattern) if r.footnote_refs]
            if len(hits) != 1:
                doc.limit("ROW_NOT_UNIQUE" if hits else "ROW_NOT_FOUND", table.locator, f"{len(hits)} rows with a footnote marker match {pattern!r}; exactly one is required")
                continue
            ri, row = hits[0]
            parent = next((r.stub for r in reversed(table.rows[:ri]) if not r.footnote_refs and r.stub not in ("On treatment",) and r.kind == "data"), None)
            headers = table.column_headers(row.panel)
            for marker in row.footnote_refs:
                note = table.footnote(marker)
                if not note:
                    doc.limit("FOOTNOTE_NOT_FOUND", table.locator, f"row {row.stub!r} cites footnote {marker!r}, which the table does not print")
                    continue
                cells = [{"role": "footnote", "marker": marker, "text": note},
                         *({"role": "row_value", "row_index": ri, "col_index": c, "row_label": row.stub, "column_header": headers.get(c), "text": t} for c, t in sorted(row.cells.items()))]
                label = f"{parent} › {row.stub}" if parent else row.stub
                self._claim(doc, study, topic=item["topic"], ident=f"{table.label}:{marker}", statement=note,
                            locator=f"{table.locator}; footnote {marker} (cited by row “{label}”)", table=stored, cells=cells, row_label=label, method=table.parse_method,
                            quality={"footnote_cited_by_row": label, "row_values": {headers.get(c, str(c)): t for c, t in sorted(row.cells.items())},
                                     "table_structure": table.notes or ["no structural issue noted"]})

    def _adverse_events(self, doc: _Doc, study: Study, table: ParsedTable, stored: ExtractedTable, item: dict[str, Any]) -> None:
        hits = table.find_rows(item["row"])
        if not hits:
            doc.limit("ROW_NOT_FOUND", table.locator, f"no row matches {item['row']!r}")
            return
        panels_seen = {r.panel for _, r in hits}
        if len(hits) != len(panels_seen):
            doc.limit("ROW_NOT_UNIQUE", table.locator, f"{len(hits)} rows match {item['row']!r} in {len(panels_seen)} panels; exactly one per panel is required")
            return
        note_measure = next((f["text"] for f in table.footnotes if re.search(r"number of participants", f["text"], re.IGNORECASE)), None)
        for ri, row in hits:
            headers = table.column_headers(row.panel)
            panel_label = table.panel_label(row.panel)
            for pattern in item["columns"]:
                cols = [c for c, h in headers.items() if c >= 1 and re.search(pattern, h, re.IGNORECASE)]
                if len(cols) != 1:
                    doc.limit("COLUMN_NOT_UNIQUE", table.locator, f"{len(cols)} headings match {pattern!r} in panel {panel_label!r}")
                    continue
                col = cols[0]
                header = headers[col]
                text = row.cells.get(col)
                where = f"{table.locator}" + (f"; panel “{panel_label}”" if panel_label else "") + f"; row “{row.stub}”; column “{header}”"
                read = read_count_percent(text)
                if read is None:
                    doc.limit("CELL_NOT_PARSED", where, f"the cell {text!r} is not an 'n (%)' count; not extracted")
                    continue
                match = self._match(study, header, table.abbreviations, subset=panel_label)
                n_at_risk = read_header_n(header)
                cells = [{"role": "count_and_percent", "row_index": ri, "col_index": col, "row_label": row.stub, "column_header": header, "panel": panel_label, "text": text}]
                if note_measure:
                    cells.append({"role": "table_note", "text": note_measure})
                quality = {
                    "arm_match": match.as_dict(), "panel": panel_label,
                    "measure": {"kind": item["measure_kind"], "found_in": "table note" if note_measure else "configuration only", "note": note_measure},
                    "count": {"n": read[0], "percent": read[1], "percent_printed": read[1] is not None},
                    "denominator": {"n_in_heading": n_at_risk, "found_in": "column heading" if n_at_risk is not None else "not found"},
                    "table_structure": table.notes or ["no structural issue noted"],
                }
                extraction = self._extraction(doc, stored, "adverse_event", target=f"{row.stub} — participants with the event", row_label=self._row_label(row),
                                              column_header=(f"{panel_label} › {header}" if panel_label else header), comparator_header=None, cells=cells, passage=None,
                                              method=table.parse_method, version=table.parse_version, quality=quality)
                quote = " | ".join(x for x in (table.caption, panel_label, row.stub, header, text) if x)
                event = AdverseEvent(
                    study_id=study.id, arm_id=match.arm_id, origin=FULLTEXT, arm_label=(f"{header} — {panel_label}" if panel_label else header), scope="ARM",
                    term=row.stub, organ_system=None, category=item.get("category") or "OTHER", measure_kind=item.get("measure_kind") or AeMeasureKind.PARTICIPANTS_WITH_EVENT.value,
                    is_serious=bool(item.get("is_serious")), n_affected=read[0], n_at_risk=n_at_risk, percent=read[1], period=item.get("period"),
                    source_id=doc.source.id, source_location=where, locator_quote=quote, tier=doc.spec.get("tier"), verification_status=PENDING,
                    missing_fields=[n for n, v in (("n_at_risk", n_at_risk), ("percent", read[1])) if v is None], fulltext_extraction_id=extraction.id,
                    notes=self._notes(doc.spec.get("population") or {}, extraction),
                )
                self.session.add(event)
                doc.excerpts.append(quote)
                doc.counts["adverse_event"] += 1

    # ------------------------------------------------------------------ shared pieces
    def _claim(self, doc: _Doc, study: Study, *, topic: str, ident: str, statement: str, locator: str, table: ExtractedTable | None,
               passage: dict[str, Any] | None = None, cells: list[dict[str, Any]] | None = None, row_label: str | None = None, method: str = "",
               quality: dict[str, Any] | None = None) -> None:
        key = f"fulltext:{doc.row.key}:{topic}:{ident}:{study.nct_id}"
        if len(key) > 120:
            key = f"fulltext:{sha256(key.encode())[:40]}:{study.nct_id}"
        if self.session.scalar(select(EvidenceClaim.id).where(EvidenceClaim.key == key)) is not None:
            return
        extraction = self._extraction(doc, table, "claim", target=f"{topic.replace('_', ' ')} statement", row_label=row_label, column_header=None, comparator_header=None,
                                      cells=cells or [{"role": "sentence", "text": statement}], passage=passage, method=method,
                                      version=pdf.PARSE_VERSION if doc.row.kind == "PDF" else jats.PARSE_VERSION,
                                      quality=quality or {"value_source": "a sentence of the document, verbatim", "authorship": passage.get("heading") if passage else None})
        quote = _short(statement, 600)
        self.session.add(EvidenceClaim(
            key=key, study_id=study.id, topic=topic, statement=statement, claim_kind="DOCUMENTED", tier=doc.spec.get("tier"), source_id=doc.source.id,
            locator=locator, quote=quote, verification_status=PENDING, origin=FULLTEXT, fulltext_extraction_id=extraction.id,
        ))
        doc.excerpts.append(quote)
        doc.counts["claim"] += 1

    def _extraction(self, doc: _Doc, table: ExtractedTable | None, evidence_type: str, *, target: str, row_label: str | None, column_header: str | None,
                    comparator_header: str | None, cells: list[dict[str, Any]], passage: dict[str, Any] | None, method: str, version: str,
                    quality: dict[str, Any]) -> FullTextExtraction:
        confidence = _confidence(quality)
        extraction = FullTextExtraction(document_id=doc.row.id, table_id=table.id if table else None, evidence_type=evidence_type, target=target, row_label=row_label,
                                        column_header=column_header, comparator_header=comparator_header, cells=cells, passage=passage, parse_method=method,
                                        parse_version=version, quality=quality, confidence=confidence)
        self.session.add(extraction)
        self.session.flush()
        return extraction

    @staticmethod
    def _row_label(row: BodyRow) -> str:
        return f"{row.group} › {row.stub}" if row.group else row.stub

    @staticmethod
    def _subset_label(header: str, subset: str | None) -> str:
        return f"{header} — {subset}" if subset else header

    @staticmethod
    def _notes(population: dict[str, Any], extraction: FullTextExtraction) -> str:
        parts = ["Machine-extracted from a full-text document; pending human review. Extraction confidence describes the parse, not the value."]
        if population.get("kind") == "SUBGROUP_POST_HOC":
            parts.append(f"Post hoc subgroup result ({population.get('label')}); not the trial's primary result.")
        return " ".join(parts)

    def _study(self, nct: str | None) -> Study | None:
        return self.session.scalar(select(Study).where(Study.nct_id == nct)) if nct else None

    def _arm_candidates(self, study: Study) -> list[mapping.ArmCandidate]:
        if study.id not in self._arms:
            arms = self.session.scalars(select(StudyArm).where(StudyArm.study_id == study.id, StudyArm.origin == Origin.REGISTRY.value).order_by(StudyArm.id)).all()
            self._arms[study.id] = [mapping.ArmCandidate(a.id, a.label, a.drug_slug, a.dose_amount, a.dose_unit, a.formulation_key, bool(a.is_placebo), a.n_randomized)
                                    for a in arms if not a.is_pooled and a.arm_type not in ("RESULTS_GROUP", "CURATED", "SEQUENCE")]
        return self._arms[study.id]

    def _equivalent_doses(self) -> dict[tuple[str | None, float], set[float]]:
        if self._equivalences is None:
            out: dict[tuple[str | None, float], set[float]] = {}
            doses = {d.id: d for d in self.session.scalars(select(Dose))}
            forms = {f.id: f.key for f in self.session.scalars(select(Formulation))}
            for eq in self.session.scalars(select(ExposureEquivalence).where(ExposureEquivalence.basis == "DOCUMENTED_BIOEQUIVALENCE")):
                a, b = doses.get(eq.dose_a_id), doses.get(eq.dose_b_id)
                if a and b:
                    out.setdefault((forms.get(a.formulation_id), float(a.amount)), set()).add(float(b.amount))
                    out.setdefault((forms.get(b.formulation_id), float(b.amount)), set()).add(float(a.amount))
            self._equivalences = out
        return self._equivalences

    def _match(self, study: Study, header: str, abbreviations: dict[str, str], *, subset: str | None = None) -> mapping.ArmMatch:
        return mapping.match_arm(header, self._arm_candidates(study), abbreviations=abbreviations, equivalent_doses=self._equivalent_doses(), subset_label=subset)

    def _endpoint(self, doc: _Doc, study: Study, spec: dict[str, Any] | None, *, caption: str | None, locator: str, table: ParsedTable | None = None) -> Endpoint:
        spec = spec or {}
        title = spec.get("title") or caption or "Endpoint as described in the document"
        key = (study.id, title)
        if key in self._endpoints:
            return self._endpoints[key]
        weeks = spec.get("timepoint_weeks")
        timeframe = None
        if caption and weeks is not None:
            m = re.search(r"\bWeek\s+(\d+)\b", caption)
            timeframe = m.group(0) if m else None
            if m is None or float(m.group(1)) != float(weeks):
                doc.limit("TIMEPOINT_NOT_IN_CAPTION", locator, f"the caption {caption!r} does not print Week {weeks:g}; the time point rests on the configuration")
        endpoint = Endpoint(study_id=study.id, origin=FULLTEXT, kind="FULLTEXT", title=title, role=spec.get("role") or "NOT_STATED", category=spec.get("category") or "OTHER",
                            threshold=spec.get("threshold"), unit=None, timeframe_text=timeframe or (f"Week {weeks:g}" if weeks is not None else None), timepoint_weeks=weeks,
                            description=(f"{caption} ({locator})" if caption else f"As reported in {locator}"), multiplicity_controlled=TriState.NOT_STATED.value,
                            source_id=doc.source.id if doc.source else None, source_location=locator)
        self.session.add(endpoint)
        self.session.flush()
        self._endpoints[key] = endpoint
        return endpoint

    def _pdf_estimand(self, doc: _Doc, study: Study, table: ParsedTable, tspec: dict[str, Any]) -> tuple[Estimand | None, dict[str, Any]]:
        name = tspec.get("estimand")
        in_table = _estimand_in_table(table)
        if in_table:
            return None, {"label": in_table, "in_table": True, "stated_in": "caption or footnote of the table"}
        if not name:
            return None, {"label": None, "in_table": False, "stated_in": "not stated in the caption or footnotes; no estimand attributed"}
        est = (doc.spec.get("estimands") or {}).get(name) or {}
        passage = doc.passages.get(est.get("passage"))
        if passage is None:
            return None, {"label": None, "in_table": False, "stated_in": "not stated in the table; the configured passage was not found, so no estimand is attributed"}
        key = (study.id, name)
        if key not in doc.estimands:
            note = tspec.get("estimand_note")
            estimand = Estimand(study_id=study.id, origin=FULLTEXT, label=est.get("label") or EstimandLabel.NOT_STATED.value,
                                basis=est.get("basis") or EstimandBasis.NOT_DETERMINABLE.value,
                                description=clean(" ".join(x for x in (est.get("description"), note) if x)), population_text=None, source_id=doc.source.id, quote=passage.sentence)
            self.session.add(estimand)
            self.session.flush()
            doc.estimands[key] = estimand
        return doc.estimands[key], {"label": est.get("label"), "basis": est.get("basis"), "in_table": False,
                                    "stated_in": f"document text outside the table, PDF page {passage.page} (under “{passage.heading}”)",
                                    "quote": passage.sentence, "note": tspec.get("estimand_note")}

    @staticmethod
    def _unit_for(table: ParsedTable, ri: int, row: BodyRow) -> tuple[str | None, list[str]]:
        """The unit of a row's values, from the nearest place that prints one: the row's own label, its group label, the
        label of the estimate (or risk) row of the same group, then the caption. A baseline row is never a source: its
        unit is that of the measurement, not of the change estimated."""
        found: list[str] = []
        unit = None
        own = [u for u in table.units if u.get("where") == "row_label" and u.get("row") == ri]
        if own:
            unit, found = own[0]["unit"], [f"row label “{row.stub}”"]
        if unit is None and row.group:
            grp = [u for u in table.units if u.get("where") == "group_label" and u.get("text") == row.group]
            if grp:
                unit, found = grp[0]["unit"], [f"group label “{row.group}”"]
        if unit is None:
            siblings = [(u, table.rows[u["row"]]) for u in table.units if u.get("where") == "row_label" and u.get("row") != ri
                        and table.rows[u["row"]].group == row.group and re.match(r"^(estimate|risk)\b", table.rows[u["row"]].stub, re.IGNORECASE)]
            if siblings:
                unit, found = siblings[0][0]["unit"], [f"estimate row label “{siblings[0][1].stub}”"]
        captions = [u for u in table.units if u.get("where") == "caption"]
        if captions:
            if unit is None and not row.group:
                unit, found = captions[0]["unit"], ["caption"]
            elif captions[0]["unit"] == unit:
                found.append("caption")
        return unit, found

    @staticmethod
    def _method_for(table: ParsedTable, ri: int, row: BodyRow) -> tuple[str | None, str | None]:
        """The model footnote of the row, or of the nearest row above it in the same group that cites one."""
        for k in range(ri, -1, -1):
            r = table.rows[k]
            if r.group != row.group:
                break
            for marker in r.footnote_refs:
                note = table.footnote(marker)
                if note and re.search(r"model|regression|covariance|imputation", note, re.IGNORECASE):
                    return note, f"footnote {marker}, cited by row “{r.stub}”"
        return None, None

    @staticmethod
    def _cell_quality(table: ParsedTable, t_match: mapping.ArmMatch, c_match: mapping.ArmMatch | None, interval: Any, unit: str | None, unit_found: list[str],
                      est_quality: dict[str, Any], method: str | None, method_source: str | None, level: float | None, row: BodyRow, p_text: str | None) -> dict[str, Any]:
        quality: dict[str, Any] = {
            "value_source": f"table cell ({table.parse_method})",
            "arm_match": t_match.as_dict(),
            "unit": {"value": unit, "found_in": unit_found or ["not found"]},
            "estimand": est_quality,
            "method": {"text": method, "source": method_source or "not stated"},
            "table_authorship": table.authorship,
            "table_source_line": table.source_line,
            "table_structure": table.notes or ["no structural issue noted"],
            "row_merged_lines": row.lines,
        }
        if c_match is not None:
            quality["comparator_match"] = c_match.as_dict()
        if interval is not None:
            quality["interval"] = "PARSED" if not interval.irregular else f"PARSED_IRREGULAR: {interval.irregular}"
            quality["ci_level"] = {"value": level, "found_in": "row label" if level else "not stated"}
        if p_text is not None or interval is not None:
            quality["p_value"] = {"text": p_text, "read": read_p_value(p_text) if p_text else None}
        return quality


def _estimand_in_table(table: ParsedTable) -> str | None:
    texts = [table.caption, *(f.get("text", "") for f in table.footnotes)]
    joined = " ".join(t for t in texts if t).lower()
    if re.search(r"treatment[- ]regimen estimand", joined):
        return "TREATMENT_REGIMEN"
    if re.search(r"efficacy estimand", joined):
        return "EFFICACY"
    return None


def _confidence(quality: dict[str, Any]) -> str:
    """HIGH / MEDIUM / LOW: a summary of the quality assessment. It describes the parse, never the truth of the value."""
    statuses = [m.get("status") for m in (quality.get("arm_match"), quality.get("comparator_match")) if isinstance(m, dict)]
    unit = (quality.get("unit") or {}).get("value") if isinstance(quality.get("unit"), dict) else "n/a"
    interval = str(quality.get("interval") or "PARSED")
    estimand = quality.get("estimand") if isinstance(quality.get("estimand"), dict) else None
    if any(s in (mapping.AMBIGUOUS, mapping.NONE) for s in statuses) or unit is None or interval.startswith("PARSED_IRREGULAR"):
        return "LOW"
    if (any(s in (mapping.VIA_EQUIVALENCE, mapping.UNIQUE_BY_DRUG_ONLY, mapping.SUBSET_OF_ARM) for s in statuses)
            or (estimand is not None and not estimand.get("in_table")) or quality.get("value_source", "").startswith("running text")):
        return "MEDIUM"
    return "HIGH"
