"""openFDA adapters: drug labeling (SPL) and FAERS spontaneous adverse-event reports.

Labeling is regulator-approved text and is stored in full (U.S. Government work).

FAERS data are *report counts*. They have no exposure denominator, are subject to
reporting bias, duplicates and notoriety effects, and the openFDA query matches a drug
in any role (suspect or concomitant). They are stored as counts and are never
converted to incidence anywhere in this codebase.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import EvidenceTier
from ..models import Drug, LabelRecord, SpontaneousReportSeries
from .snapshots import SnapshotOutcome, canonical_json, enqueue_review, record_change_event, record_snapshot, sha256_text
from .transport import FixtureMissing, ResourceNotFound, Transport

LABEL_LICENSE = "FDA-approved prescribing information via openFDA (U.S. Government work / CC0); stored in full."
FAERS_LICENSE = "FDA Adverse Event Reporting System data via openFDA (CC0). Report counts only; not validated; not incidence."

LABEL_SECTIONS = [
    "boxed_warning",
    "recent_major_changes",
    "indications_and_usage",
    "dosage_and_administration",
    "dosage_forms_and_strengths",
    "contraindications",
    "warnings_and_cautions",
    "adverse_reactions",
    "drug_interactions",
    "use_in_specific_populations",
    "description",
    "mechanism_of_action",
    "pharmacodynamics",
    "pharmacokinetics",
    "clinical_studies",
]

# MedDRA preferred terms tracked for class-relevant spontaneous-report review.
FAERS_TERMS_OF_INTEREST = [
    "NAUSEA", "VOMITING", "DIARRHOEA", "CONSTIPATION", "PANCREATITIS", "PANCREATITIS ACUTE", "CHOLELITHIASIS",
    "CHOLECYSTITIS", "GASTROPARESIS", "IMPAIRED GASTRIC EMPTYING", "INTESTINAL OBSTRUCTION", "ILEUS",
    "DEHYDRATION", "ACUTE KIDNEY INJURY", "HYPOGLYCAEMIA", "SUICIDAL IDEATION", "THYROID CANCER",
    "MEDULLARY THYROID CANCER", "ALOPECIA", "DYSAESTHESIA", "INJECTION SITE REACTION",
]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def label_text(record: dict, section: str) -> str:
    """Plain text of a label section (openFDA returns a list of strings; tables are separate HTML fields)."""
    value = record.get(section)
    if not value:
        return ""
    return "\n".join(v for v in value if isinstance(v, str))


@dataclass
class LabelIngest:
    record: LabelRecord
    outcome: SnapshotOutcome
    changed_sections: list[str]


class OpenFDALabelAdapter:
    def __init__(self, transport: Transport):
        self.transport = transport

    def fetch(self, brand: str, manufacturer: str | None = "Eli Lilly") -> dict | None:
        search = f'openfda.brand_name:"{brand}"'
        if manufacturer:
            search += f' AND openfda.manufacturer_name:"{manufacturer}"'
        try:
            raw = self.transport.get("openfda", "/drug/label.json", {"search": search, "limit": "1"}, fixture_key=f"openfda/label/{_slug(brand)}.json")
        except (ResourceNotFound, FixtureMissing):
            return None
        results = json.loads(raw).get("results") or []
        return results[0] if results else None

    def count_labels_for_generic(self, generic: str) -> int:
        """Number of FDA label records listing this generic name (0 when the database has none)."""
        try:
            raw = self.transport.get(
                "openfda", "/drug/label.json", {"search": f'openfda.generic_name:"{generic}"', "limit": "1"},
                fixture_key=f"openfda/label-search/{_slug(generic)}.json",
            )
        except ResourceNotFound:
            return 0
        return int(json.loads(raw).get("meta", {}).get("results", {}).get("total", 0))

    def watch_generic(self, session: Session, drug: Drug, generic: str) -> int:
        """Record a dated check of the label database for an investigational drug.

        Finding a label never changes the drug's status automatically; it opens a HIGH-priority
        review item so a person confirms the approval against the label itself.
        """
        total = self.count_labels_for_generic(generic)
        payload = {"query": f'openfda.generic_name:"{generic}"', "labels_found": total}
        outcome = record_snapshot(
            session,
            key=f"openfda-label-search:{drug.slug}",
            source_type="OPENFDA_LABEL_SEARCH",
            tier=EvidenceTier.REGULATORY_LABEL.value,
            title=f"openFDA label database search for generic name {generic}: {total} label record(s)",
            url="https://open.fda.gov/apis/drug/label/",
            publisher="U.S. Food and Drug Administration (openFDA)",
            version_label=f"{total} label(s)",
            content_hash=sha256_text(canonical_json(payload)),
            content=canonical_json(payload),
            license_note=LABEL_LICENSE,
        )
        if total > 0:
            enqueue_review(
                session,
                item_type="REGULATORY_STATUS",
                target_type="drug",
                target_id=drug.id,
                title=f"{drug.name}: {total} FDA label record(s) now exist — regulatory status may have changed",
                reason="The drug is curated as investigational but the FDA label database returned labeling. Confirm approval, indication and date against the label before updating the status.",
                priority="HIGH",
                payload=payload,
                dedupe_key=f"label-appeared:{drug.slug}:{total}",
            )
            if outcome.changed:
                record_change_event(
                    session, outcome,
                    changes=[{"field": "labels_found", "label": "FDA label records", "before": 0, "after": total, "significance": "HIGH", "note": "Possible new approval."}],
                    summary=f"FDA label database now returns {total} label record(s) for {generic}.",
                    significance="HIGH",
                    title=f"{drug.name}: FDA labeling detected",
                )
        return total

    def ingest(self, session: Session, drug: Drug | None, brand: str, manufacturer: str | None = "Eli Lilly") -> LabelIngest | None:
        record = self.fetch(brand, manufacturer)
        if record is None:
            return None
        # Hash only the text sections so cosmetic SPL packaging changes do not raise a label-change alert.
        section_text = {s: label_text(record, s) for s in LABEL_SECTIONS}
        content_hash = sha256_text(canonical_json(section_text))
        key = f"openfda-label:{_slug(brand)}"
        effective = record.get("effective_time")
        effective_iso = f"{effective[:4]}-{effective[4:6]}-{effective[6:8]}" if effective and len(effective) >= 8 else None

        previous_sections: dict[str, str] = {}
        existing = session.scalar(select(LabelRecord).where(LabelRecord.brand == brand))
        if existing is not None:
            previous_sections = dict(existing.sections or {})

        application = (record.get("openfda", {}).get("application_number") or [None])[0]
        outcome = record_snapshot(
            session,
            key=key,
            source_type="OPENFDA_LABEL",
            tier=EvidenceTier.REGULATORY_LABEL.value,
            title=f"{brand} ({', '.join(record.get('openfda', {}).get('generic_name', []) or ['?']).lower()}) prescribing information",
            url=f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={record.get('set_id')}" if record.get("set_id") else None,
            publisher="U.S. Food and Drug Administration (via openFDA / DailyMed)",
            published_date=effective_iso,
            version_label=f"v{record.get('version')} · effective {effective_iso}",
            content_hash=content_hash,
            content=canonical_json(section_text),
            license_note=LABEL_LICENSE,
            external_ids={"set_id": record.get("set_id"), "application_number": application, "spl_version": record.get("version")},
        )
        label = existing or LabelRecord(brand=brand)
        label.drug_id = drug.id if drug else None
        label.set_id = record.get("set_id")
        label.label_version = str(record.get("version")) if record.get("version") is not None else None
        label.effective_time = effective_iso
        label.sections = section_text
        label.source_id = outcome.source.id
        session.add(label)

        changed_sections: list[str] = []
        if outcome.changed:
            changed_sections = [s for s in LABEL_SECTIONS if previous_sections.get(s, "") != section_text.get(s, "")]
            changes = [
                {
                    "field": f"label.{s}",
                    "label": s.replace("_", " ").title(),
                    "before": f"{len(previous_sections.get(s, ''))} chars",
                    "after": f"{len(section_text.get(s, ''))} chars",
                    "significance": "HIGH" if s in ("boxed_warning", "indications_and_usage", "contraindications", "warnings_and_cautions") else "MEDIUM",
                    "note": "Label section text changed; review against 'Recent Major Changes'.",
                }
                for s in changed_sections
            ]
            record_change_event(
                session,
                outcome,
                changes=changes,
                summary=f"{brand} label text changed in {len(changed_sections)} section(s): {', '.join(changed_sections[:5])}.",
                significance="HIGH" if any(c["significance"] == "HIGH" for c in changes) else "MEDIUM",
                title=f"{brand} prescribing information updated ({label.effective_time})",
            )
        session.flush()
        return LabelIngest(label, outcome, changed_sections)


class OpenFDAFaersAdapter:
    def __init__(self, transport: Transport):
        self.transport = transport

    @staticmethod
    def _drug_search(generic: str) -> str:
        return f'patient.drug.openfda.generic_name.exact:"{generic.upper()}"'

    def _get(self, params: dict, fixture_key: str) -> dict | None:
        try:
            return json.loads(self.transport.get("openfda", "/drug/event.json", params, fixture_key=fixture_key))
        except (ResourceNotFound, FixtureMissing):
            return None  # openFDA answers 404 when a query matches no reports

    def total_reports(self, generic: str | None) -> tuple[int | None, str | None]:
        params = {"limit": "1"}
        if generic:
            params["search"] = self._drug_search(generic)
        data = self._get(params, f"openfda/faers/total-{_slug(generic) if generic else 'all'}.json")
        if not data:
            return None, None
        return data["meta"]["results"]["total"], data["meta"].get("last_updated")

    def reaction_counts(self, generic: str, limit: int = 500) -> list[dict]:  # openFDA rejects count limits of 1000+
        data = self._get(
            {"search": self._drug_search(generic), "count": "patient.reaction.reactionmeddrapt.exact", "limit": str(limit)},
            f"openfda/faers/reactions-{_slug(generic)}.json",
        )
        return (data or {}).get("results", [])

    def reports_by_quarter(self, generic: str) -> list[dict]:
        data = self._get({"search": self._drug_search(generic), "count": "receivedate"}, f"openfda/faers/receivedate-{_slug(generic)}.json")
        buckets: dict[str, int] = defaultdict(int)
        for row in (data or {}).get("results", []):
            day = row.get("time", "")
            if len(day) >= 6:
                quarter = (int(day[4:6]) - 1) // 3 + 1
                buckets[f"{day[:4]}-Q{quarter}"] += int(row.get("count", 0))
        return [{"period": period, "count": count} for period, count in sorted(buckets.items())]

    def background_reaction_total(self, term: str) -> int | None:
        data = self._get(
            {"search": f'patient.reaction.reactionmeddrapt.exact:"{term.upper()}"', "limit": "1"},
            f"openfda/faers/background-{_slug(term)}.json",
        )
        return data["meta"]["results"]["total"] if data else None

    def ingest_drug(self, session: Session, drug: Drug, generic: str) -> dict:
        total, last_updated = self.total_reports(generic)
        if total is None:
            return {"drug": drug.slug, "total": None}
        reactions = self.reaction_counts(generic)
        quarters = self.reports_by_quarter(generic)
        payload = {"total": total, "reactions": reactions, "quarters": quarters, "last_updated": last_updated}
        outcome = record_snapshot(
            session,
            key=f"openfda-faers:{drug.slug}",
            source_type="OPENFDA_FAERS",
            tier=EvidenceTier.SPONTANEOUS_REPORTS.value,
            title=f"FAERS spontaneous reports mentioning {generic.lower()} (openFDA drug/event)",
            url="https://open.fda.gov/apis/drug/event/",
            publisher="U.S. Food and Drug Administration (openFDA)",
            published_date=last_updated,
            version_label=f"FAERS data through {last_updated}",
            content_hash=sha256_text(canonical_json(payload)),
            content=canonical_json(payload),
            license_note=FAERS_LICENSE,
            external_ids={"generic_name": generic.upper()},
        )
        query = self._drug_search(generic)
        for kind, data, tot in (("TOTAL", [], total), ("BY_REACTION", reactions, total), ("BY_QUARTER", quarters, total)):
            series = session.scalar(
                select(SpontaneousReportSeries).where(SpontaneousReportSeries.drug_id == drug.id, SpontaneousReportSeries.kind == kind)
            ) or SpontaneousReportSeries(drug_id=drug.id, kind=kind, query=query)
            series.query = query
            series.total = tot
            series.data = data
            series.database_last_updated = last_updated
            series.retrieved_at = outcome.source.retrieved_at
            series.source_id = outcome.source.id
            session.add(series)
        session.flush()
        return {"drug": drug.slug, "total": total, "reactions": len(reactions)}

    def ingest_background(self, session: Session) -> dict:
        """Whole-database totals needed for disproportionality 2x2 tables."""
        all_total, last_updated = self.total_reports(None)
        if all_total is None:
            return {"background": None}
        rows = []
        for term in FAERS_TERMS_OF_INTEREST:
            count = self.background_reaction_total(term)
            if count is not None:
                rows.append({"term": term, "count": count})
        payload = {"all_reports": all_total, "terms": rows, "last_updated": last_updated}
        outcome = record_snapshot(
            session,
            key="openfda-faers:background",
            source_type="OPENFDA_FAERS",
            tier=EvidenceTier.SPONTANEOUS_REPORTS.value,
            title="FAERS all-report totals and per-reaction totals (openFDA drug/event)",
            url="https://open.fda.gov/apis/drug/event/",
            publisher="U.S. Food and Drug Administration (openFDA)",
            published_date=last_updated,
            version_label=f"FAERS data through {last_updated}",
            content_hash=sha256_text(canonical_json(payload)),
            content=canonical_json(payload),
            license_note=FAERS_LICENSE,
        )
        series = session.scalar(
            select(SpontaneousReportSeries).where(SpontaneousReportSeries.drug_id.is_(None), SpontaneousReportSeries.kind == "BACKGROUND_REACTION")
        ) or SpontaneousReportSeries(drug_id=None, kind="BACKGROUND_REACTION", query="all reports")
        series.total = all_total
        series.data = rows
        series.database_last_updated = last_updated
        series.retrieved_at = outcome.source.retrieved_at
        series.source_id = outcome.source.id
        session.add(series)
        session.flush()
        return {"background": all_total, "terms": len(rows)}
