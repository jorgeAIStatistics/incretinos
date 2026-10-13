"""PubMed E-utilities adapter.

Bibliographic metadata (PMID, DOI, title, journal, authors, publication types and the
DataBank accession list that carries NCT numbers) is stored. Abstract text is handled
conservatively because publishers may hold copyright: the full abstract is kept only
in the local, git-ignored cache; committed fixtures and the database keep its SHA-256,
its length, and the short excerpts used to anchor curated claims.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree as ET
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import EvidenceTier, LinkBasis
from ..models import EvidenceSource, Publication, Study, StudyPublication
from .ctgov import NCT_RE
from .snapshots import canonical_json, normalize_ws, quote_in_text, record_snapshot, sha256_text
from .transport import FixtureMissing, Transport

PUBMED_LICENSE = (
    "PubMed bibliographic metadata courtesy of the U.S. National Library of Medicine. Abstract text may be "
    "copyrighted by the publisher: only its hash and short anchoring excerpts are stored; full text is cached locally only."
)


def article_fixture_key(pmid: str) -> str:
    return f"pubmed/articles/{pmid}.json"


def esearch_fixture_key(nct_id: str) -> str:
    return f"pubmed/esearch/{nct_id.upper()}.json"


@dataclass
class Article:
    pmid: str
    title: str
    journal: str | None = None
    pub_date: str | None = None
    year: int | None = None
    doi: str | None = None
    authors: list[str] = field(default_factory=list)
    pub_types: list[str] = field(default_factory=list)
    databank_ncts: list[str] = field(default_factory=list)
    abstract_sha256: str | None = None
    abstract_chars: int | None = None
    abstract_ncts: list[str] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)
    abstract: str | None = None  # present only when fetched live or cached locally; never committed

    def to_fixture(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if k != "abstract"}
        data["_note"] = "Abstract text intentionally omitted; see abstract_sha256 and excerpts."
        return data

    @classmethod
    def from_fixture(cls, data: dict) -> Article:
        return cls(**{k: v for k, v in data.items() if not k.startswith("_")})


def _text(node) -> str:
    return normalize_ws("".join(node.itertext())) if node is not None else ""


def parse_efetch_xml(xml_bytes: bytes) -> list[Article]:
    """Parse an efetch PubmedArticleSet (defusedxml guards against entity-expansion attacks)."""
    root = ET.fromstring(xml_bytes)
    articles: list[Article] = []
    for node in root.findall(".//PubmedArticle"):
        pmid = _text(node.find("./MedlineCitation/PMID"))
        art = node.find("./MedlineCitation/Article")
        if not pmid or art is None:
            continue
        journal = _text(art.find("./Journal/Title")) or None
        pub = art.find("./Journal/JournalIssue/PubDate")
        year_text = _text(pub.find("Year")) if pub is not None else ""
        medline_date = _text(pub.find("MedlineDate")) if pub is not None else ""
        year_match = re.search(r"\d{4}", year_text or medline_date)
        month = _text(pub.find("Month")) if pub is not None else ""
        day = _text(pub.find("Day")) if pub is not None else ""
        pub_date = " ".join(x for x in [year_text or medline_date, month, day] if x) or None
        doi = None
        for aid in node.findall("./PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = _text(aid).lower()
        if doi is None:
            for eloc in art.findall("./ELocationID"):
                if eloc.get("EIdType") == "doi":
                    doi = _text(eloc).lower()
        authors = []
        for author in art.findall("./AuthorList/Author"):
            last, initials = _text(author.find("LastName")), _text(author.find("Initials"))
            collective = _text(author.find("CollectiveName"))
            authors.append(f"{last} {initials}".strip() if last else collective)
        abstract_parts = []
        for part in art.findall("./Abstract/AbstractText"):
            label = part.get("Label")
            body = _text(part)
            abstract_parts.append(f"{label}: {body}" if label else body)
        abstract = " ".join(p for p in abstract_parts if p) or None
        databank = sorted(
            {
                acc.upper()
                for acc in (_text(a) for a in art.findall("./DataBankList/DataBank/AccessionNumberList/AccessionNumber"))
                if NCT_RE.fullmatch(acc)
            }
        )
        articles.append(
            Article(
                pmid=pmid,
                title=_text(art.find("./ArticleTitle")),
                journal=journal,
                pub_date=pub_date,
                year=int(year_match.group(0)) if year_match else None,
                doi=doi,
                authors=[a for a in authors if a],
                pub_types=[_text(pt) for pt in art.findall("./PublicationTypeList/PublicationType")],
                databank_ncts=databank,
                abstract_sha256=sha256_text(normalize_ws(abstract)) if abstract else None,
                abstract_chars=len(abstract) if abstract else None,
                abstract_ncts=sorted({m.upper() for m in NCT_RE.findall(abstract or "")}),
                abstract=abstract,
            )
        )
    return articles


class PubMedAdapter:
    def __init__(self, transport: Transport, *, cache_dir: Path | None = None):
        self.transport = transport
        self.cache_dir = cache_dir

    # -- search
    def search_by_nct(self, nct_id: str) -> list[str]:
        """PMIDs whose PubMed record lists this NCT number as a secondary source identifier."""
        try:
            raw = self.transport.get(
                "eutils",
                "/esearch.fcgi",
                {"db": "pubmed", "term": f"{nct_id}[si]", "retmode": "json", "retmax": "200"},
                fixture_key=esearch_fixture_key(nct_id),
            )
        except FixtureMissing:
            return []
        return list(json.loads(raw).get("esearchresult", {}).get("idlist", []))

    # -- fetch
    def _cache_path(self, pmid: str) -> Path | None:
        return (self.cache_dir / "pubmed" / f"{pmid}.abstract.txt") if self.cache_dir else None

    def cached_abstract(self, pmid: str) -> str | None:
        path = self._cache_path(pmid)
        return path.read_text(encoding="utf-8") if path and path.is_file() else None

    def fetch_articles(self, pmids: list[str], *, anchors: dict[str, list[str]] | None = None) -> list[Article]:
        """Fetch article records. ``anchors`` maps PMID -> quotes to verify against the abstract."""
        pmids = [p for p in dict.fromkeys(pmids) if re.fullmatch(r"\d{1,9}", p)]
        anchors = anchors or {}
        articles: list[Article] = []
        if self.transport.mode == "OFFLINE":
            for pmid in pmids:
                try:
                    data = json.loads(self.transport.get("eutils", "/efetch.fcgi", {"id": pmid}, fixture_key=article_fixture_key(pmid)))
                except FixtureMissing:
                    continue
                article = Article.from_fixture(data)
                article.abstract = self.cached_abstract(pmid)
                articles.append(article)
            return articles

        for start in range(0, len(pmids), 150):
            batch = pmids[start : start + 150]
            xml = self.transport.get("eutils", "/efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"})
            for article in parse_efetch_xml(xml):
                # Verify requested anchor quotes against the live abstract; keep only those that match.
                wanted = anchors.get(article.pmid, [])
                article.excerpts = [q for q in wanted if article.abstract and quote_in_text(q, article.abstract)]
                cache = self._cache_path(article.pmid)
                if cache and article.abstract:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(article.abstract, encoding="utf-8")
                self.transport.record(article_fixture_key(article.pmid), json.dumps(article.to_fixture(), indent=1, ensure_ascii=False).encode())
                articles.append(article)
        return articles

    # -- persistence & reconciliation
    def upsert_article(self, session: Session, article: Article) -> Publication:
        publication = session.scalar(select(Publication).where(Publication.pmid == article.pmid))
        if publication is None:
            publication = Publication(pmid=article.pmid, title=article.title)
            session.add(publication)
        publication.title = article.title or publication.title
        publication.journal = article.journal
        publication.pub_date = article.pub_date
        publication.year = article.year
        publication.doi = article.doi
        publication.authors = article.authors
        publication.pub_types = article.pub_types
        publication.databank_ncts = article.databank_ncts
        publication.abstract_sha256 = article.abstract_sha256
        publication.abstract_chars = article.abstract_chars
        publication.has_abstract_text_cached = article.abstract is not None

        is_journal_article = any(pt.lower() == "journal article" for pt in article.pub_types)
        fingerprint = canonical_json({k: v for k, v in article.to_fixture().items() if k not in ("excerpts", "_note")})
        outcome = record_snapshot(
            session,
            key=f"pubmed:{article.pmid}",
            source_type="PUBMED",
            tier=EvidenceTier.PEER_REVIEWED.value if is_journal_article else EvidenceTier.SECONDARY.value,
            title=article.title,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{article.pmid}/",
            publisher=article.journal,
            published_date=article.pub_date,
            version_label=article.abstract_sha256[:12] if article.abstract_sha256 else None,
            content_hash=sha256_text(fingerprint),
            content=None,  # abstract text is not redistributed
            excerpts=article.excerpts,
            license_note=PUBMED_LICENSE,
            content_type="application/x-pubmed-metadata",
            external_ids={"pmid": article.pmid, "doi": article.doi, "abstract_sha256": article.abstract_sha256},
            byte_size=article.abstract_chars,
        )
        publication.source_id = outcome.source.id
        session.flush()
        return publication

    def reconcile(self, session: Session, study: Study, article: Article, publication: Publication, *, si_hit: bool, primary_pmid: str | None) -> StudyPublication | None:
        """Tie a publication to a study and record every independent basis for the link."""
        bases: list[str] = []
        if study.nct_id in article.databank_ncts or si_hit:
            bases.append(LinkBasis.PUBMED_DATABANK.value)
        if study.nct_id in article.abstract_ncts:
            bases.append(LinkBasis.ABSTRACT_MENTION.value)
        link = session.scalar(
            select(StudyPublication).where(StudyPublication.study_id == study.id, StudyPublication.publication_id == publication.id)
        )
        if link is None:
            if not bases and article.pmid != primary_pmid:
                return None
            link = StudyPublication(study_id=study.id, publication_id=publication.id, bases=[])
            session.add(link)
        merged = list(dict.fromkeys([*link.bases, *bases]))
        if article.pmid == primary_pmid:
            link.relation = "PRIMARY_RESULTS"
            if LinkBasis.CURATED.value not in merged:
                merged.append(LinkBasis.CURATED.value)
        link.bases = merged
        independent = {b for b in merged if b != LinkBasis.CURATED.value}
        link.confidence = "HIGH" if len(independent) >= 2 else "MEDIUM" if independent else "LOW"
        return link


def anchored_excerpts(session: Session, source: EvidenceSource) -> list[str]:
    return list(source.snapshots[-1].excerpts) if source.snapshots else []
