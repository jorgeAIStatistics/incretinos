"""Convert research notes (research/web-notes/*.json) into curated evidence files.

The notes were gathered on 2026-10-13 by AI-assisted retrieval from primary sources; each
finding carries a source URL and a short verbatim locator quote. This script is the only
path from those notes into the product, and it is deliberately conservative:

* registry-sourced findings are skipped — the ClinicalTrials.gov adapter extracts those itself;
* a numeric finding is kept only if the curation schema accepts it, i.e. its digits appear in
  its own locator quote;
* findings that restate a change as a positive "reduction" are skipped (sign ambiguity);
* everything rejected is listed in research/web-notes/conversion-report.md.

Nothing produced here is marked verified: the loader later anchors each quote against stored
source text and otherwise queues the statement for human review.

Usage:  python scripts/notes_to_curated.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from pydantic import ValidationError

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from incretinos.curation.schema import AdverseEventDef, AnalysisDef, ClaimDef, ResultDef  # noqa: E402
from incretinos.vocab import categorize_endpoint  # noqa: E402

NOTES_DIR = BACKEND.parent / "research" / "web-notes"
DATA_DIR = BACKEND / "incretinos" / "curation" / "data"
RETRIEVED = "2026-10-13"

LABEL_BRANDS = ("zepbound", "mounjaro", "trulicity", "foundayo")
ESTIMAND = {"efficacy": "EFFICACY", "treatment-regimen": "TREATMENT_REGIMEN", "treatment regimen": "TREATMENT_REGIMEN"}
ROLE = {"primary": "PRIMARY", "key_secondary": "KEY_SECONDARY", "secondary": "SECONDARY", "exploratory": "EXPLORATORY"}
TRI = {"yes": "YES", "no": "NO"}
ESTIMAND_TEXT = {
    "EFFICACY": "Efficacy estimand as named by the source: the treatment effect had participants remained on study treatment (hypothetical strategy for treatment discontinuation).",
    "TREATMENT_REGIMEN": "Treatment-regimen estimand as named by the source: the treatment effect regardless of treatment discontinuation (treatment-policy strategy).",
}
ESTIMAND_ICE = {"EFFICACY": "HYPOTHETICAL", "TREATMENT_REGIMEN": "TREATMENT_POLICY"}


def slug(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def source_key(src: dict, manual: dict[str, str]) -> str | None:
    tier = (src.get("tier") or "").upper()
    if src.get("pmid"):
        return f"pubmed:{src['pmid']}"
    url = src.get("url") or ""
    if url in manual:
        return manual[url]
    title = (src.get("title") or "").lower()
    if tier.startswith("REGULATORY_LABEL") or "prescribing information" in title:
        for brand in LABEL_BRANDS:
            if brand in title or brand in url.lower():
                return f"openfda-label:{brand}"
    if tier.startswith("REGISTRY") or tier.startswith("SECONDARY") or "clinicaltrials.gov" in url or "eutils.ncbi" in url:
        return None
    date = src.get("published_date") or "undated"
    return f"web:{slug((src.get('publisher') or 'src')[:12])}-{date}-{slug(src.get('title') or url, 40)}"


def web_tier(src: dict) -> str:
    tier = (src.get("tier") or "").upper()
    if tier.startswith("PRESS"):
        return "PRESS_RELEASE"
    if tier.startswith("SPONSOR"):
        return "SPONSOR_PIPELINE"
    if tier.startswith("REGULATOR"):
        return "REGULATORY_REVIEW"
    if tier.startswith("CONFERENCE"):
        return "CONFERENCE"
    if tier.startswith("PEER"):
        return "PEER_REVIEWED"
    return "SECONDARY"


def clean_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    unit = re.sub(r"\(.*?\)", "", unit).strip()
    return {"% of participants": "% of participants", "%": "%"}.get(unit, unit) or None


def main() -> None:
    manual_sources = {}
    existing = yaml.safe_load((DATA_DIR / "sources.yaml").read_text())["sources"]
    for src in existing:
        if src.get("url"):
            manual_sources[src["url"]] = src["key"]
    watch = {s["nct"] for s in yaml.safe_load((DATA_DIR / "watchlist.yaml").read_text())["studies"]}

    # Studies with a hand-curated file keep their contrasts there; do not also generate them heuristically.
    manual_studies = {p.name.split(".")[0] for p in (DATA_DIR / "studies").glob("*.yaml") if ".generated." not in p.name}
    generated_sources: dict[str, dict] = {}
    files: dict[str, dict] = defaultdict(lambda: {"estimands": {}, "endpoints": {}, "results": [], "analyses": [], "adverse_events": [], "claims": []})
    general_claims: list[dict] = []
    rejected: list[str] = []
    kept = 0

    for notes_path in sorted(NOTES_DIR.glob("*.json")):
        notes = json.loads(notes_path.read_text())
        stem = notes_path.stem
        sources = {s["id"]: s for s in notes.get("sources", [])}
        for f in notes.get("findings", []):
            fid = f"{stem}:{f.get('id')}"
            src = sources.get(f.get("source_id"))
            if src is None:
                rejected.append(f"{fid}: unknown source id {f.get('source_id')}")
                continue
            key = source_key(src, manual_sources)
            if key is None:
                continue  # registry / secondary: handled by adapters or not used
            if key.startswith("web:") and key not in manual_sources.values():
                generated_sources[key] = {
                    "key": key,
                    "tier": web_tier(src),
                    "title": src.get("title") or key,
                    "publisher": src.get("publisher"),
                    "url": src.get("url"),
                    "published_date": src.get("published_date"),
                    "retrieved_date": RETRIEVED,
                }
            nct = f.get("nct")
            quote = (f.get("locator_quote") or "").strip() or None
            value = f.get("value")
            low, high = f.get("range_low"), f.get("range_high")
            endpoint_text = (f.get("endpoint") or "").strip()
            numeric = isinstance(value, int | float) or low is not None or high is not None

            if not numeric or nct not in watch:
                statement = (f.get("statement") or "").strip()
                if not statement:
                    continue
                claim = {
                    "key": f"note:{slug(stem, 24)}:{f.get('id')}",
                    "topic": slug(f.get("question") or "note", 40).upper().replace("-", "_") or "NOTE",
                    "statement": statement,
                    "source": key,
                    "quote": quote,
                }
                if f.get("drug") and "+" not in f["drug"] and " " not in f["drug"]:
                    claim["drug"] = f["drug"]
                try:
                    ClaimDef.model_validate(claim)
                except ValidationError as exc:
                    rejected.append(f"{fid}: claim rejected — {exc.errors()[0]['msg']}")
                    continue
                if nct in watch:
                    claim.pop("drug", None)
                    files[nct]["claims"].append(claim)
                else:
                    general_claims.append(claim)
                kept += 1
                continue

            arm = (f.get("arm") or "").strip()
            if not arm:
                rejected.append(f"{fid}: numeric finding without an arm — {endpoint_text[:60]}")
                continue
            if re.search(r"reported as (a )?(positive|loss|reduction)", endpoint_text, re.IGNORECASE):
                rejected.append(f"{fid}: skipped (value restated as a positive reduction; signed value kept elsewhere) — {arm}")
                continue
            if not isinstance(value, int | float) and low is None:
                continue
            est_label = ESTIMAND.get((f.get("estimand") or "").lower())
            bucket = files[nct]
            unit_raw = f.get("unit") or ""
            unit = clean_unit(unit_raw)
            weeks = f.get("timepoint_weeks") if isinstance(f.get("timepoint_weeks"), int | float) else None
            is_range = low is not None or high is not None
            lower = endpoint_text.lower()

            # ---- adverse events
            if re.search(r"discontinu|non-completion|adverse event|nausea|vomiting|diarrh|constipation|dys[a]?esthesia|serious adverse|decreased appetite|fatigue", lower):
                if "non-completion" in lower:
                    kind = "STUDY_WITHDRAWAL_DUE_TO_AE"
                elif "discontinu" in lower:
                    kind = "TREATMENT_DISCONTINUATION_DUE_TO_AE"
                else:
                    kind = "PARTICIPANTS_WITH_EVENT"
                term = re.sub(r"^incidence of (adverse event: )?", "", endpoint_text, flags=re.IGNORECASE)
                term = re.sub(r"\s*\(.*$", "", term).strip() or endpoint_text
                if kind != "PARTICIPANTS_WITH_EVENT":
                    category = "DISCONTINUATION"
                elif re.search(r"nausea|vomiting|diarrh|constipation", lower):
                    category = "GASTROINTESTINAL"
                elif "serious" in lower:
                    category = "SERIOUS"
                else:
                    category = "OTHER"
                row = {
                    "arm": arm,
                    "scope": "RANGE_ACROSS_ARMS" if is_range else "ARM",
                    "term": term[:200],
                    "category": category,
                    "measure_kind": kind,
                    "is_serious": "serious" in lower,
                    "source": key,
                    "quote": quote,
                    "period": f"{weeks:g} weeks" if weeks else None,
                    "notes": f.get("notes"),
                }
                if is_range:
                    row["percent_low"], row["percent_high"] = low, high
                elif "count" in unit_raw or "participants (" in unit_raw:
                    row["n_affected"] = int(value)
                    if isinstance(f.get("n"), int):
                        row["n_at_risk"] = f["n"]
                else:
                    row["percent"] = value
                    if isinstance(f.get("n"), int):
                        row["n_at_risk"] = f["n"]
                row = {k: v for k, v in row.items() if v is not None}
                try:
                    AdverseEventDef.model_validate(row)
                except ValidationError as exc:
                    rejected.append(f"{fid}: AE rejected — {exc.errors()[0]['msg']}")
                    continue
                bucket["adverse_events"].append(row)
                kept += 1
                continue

            category, threshold = categorize_endpoint(endpoint_text, unit)
            role = ROLE.get((f.get("endpoint_role") or "").lower(), "NOT_STATED")
            mult = TRI.get((f.get("multiplicity_controlled") or "").lower(), "NOT_STATED")
            ep_key = slug(f"{category.value}-{threshold or ''}-w{weeks if weeks is not None else 'na'}-{slug(endpoint_text, 28)}", 80)
            endpoint = bucket["endpoints"].setdefault(
                ep_key,
                {
                    "key": ep_key,
                    "title": endpoint_text[:300] or category.value,
                    "role": role,
                    "category": category.value,
                    "threshold": threshold,
                    "unit": unit,
                    "timepoint_weeks": weeks,
                    "multiplicity_controlled": mult,
                },
            )
            if endpoint["role"] == "NOT_STATED" and role != "NOT_STATED":
                endpoint["role"] = role
            if endpoint["multiplicity_controlled"] == "NOT_STATED" and mult != "NOT_STATED":
                endpoint["multiplicity_controlled"] = mult
            est_key = None
            if est_label:
                est_key = est_label.lower()
                bucket["estimands"].setdefault(
                    est_key,
                    {
                        "key": est_key,
                        "label": est_label,
                        "description": ESTIMAND_TEXT[est_label],
                        "source": key,
                        "quote": quote if quote and "estimand" in quote.lower() else None,
                        "strategies": [{"event": "TREATMENT_DISCONTINUATION", "strategy": ESTIMAND_ICE[est_label]}],
                    },
                )

            is_contrast = "hazard ratio" in unit_raw.lower() or re.search(r"difference|hazard ratio|time to first|\bvs\b|versus", lower) is not None and " vs " in f" {arm} ".replace("versus", "vs")
            if is_contrast and nct in manual_studies:
                continue
            if is_contrast:
                parts = re.split(r"\s+(?:vs\.?|versus)\s+", arm, maxsplit=1)
                treatment, comparator = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else None)
                statement = (f.get("statement") or "") + " " + (f.get("notes") or "")
                hypothesis = "NON_INFERIORITY" if re.search(r"non-?inferior", statement, re.IGNORECASE) else "SUPERIORITY" if re.search(r"superior", statement, re.IGNORECASE) else "NOT_STATED"
                row = {
                    "endpoint": ep_key,
                    "estimand": est_key,
                    "treatment": treatment,
                    "comparator": comparator,
                    "hypothesis_type": hypothesis,
                    "param_type": "Hazard ratio" if "hazard ratio" in unit_raw.lower() else "Estimated treatment difference",
                    "estimate": value,
                    "unit": None if "hazard ratio" in unit_raw.lower() else unit,
                    "ci_low": f.get("ci_low"),
                    "ci_high": f.get("ci_high"),
                    "ci_level": f.get("ci_level"),
                    "p_value_text": None if f.get("p_value") in (None, "") else str(f.get("p_value")),
                    "multiplicity_controlled": mult,
                    "timepoint_weeks": weeks,
                    "source": key,
                    "quote": quote,
                    "notes": f.get("notes"),
                }
                row = {k: v for k, v in row.items() if v is not None}
                if (row.get("ci_low") is None) != (row.get("ci_high") is None):
                    row.pop("ci_low", None), row.pop("ci_high", None), row.pop("ci_level", None)
                try:
                    AnalysisDef.model_validate(row)
                except ValidationError:
                    # CI limits may sit outside the short locator quote; keep the estimate, drop the unanchored CI.
                    for k in ("ci_low", "ci_high", "ci_level"):
                        row.pop(k, None)
                    try:
                        AnalysisDef.model_validate(row)
                        row["notes"] = ((row.get("notes") or "") + " Confidence interval reported by the source but not contained in the locator quote; not entered.").strip()
                    except ValidationError as exc:
                        rejected.append(f"{fid}: analysis rejected — {exc.errors()[0]['msg']}")
                        continue
                bucket["analyses"].append(row)
                kept += 1
                continue

            row = {
                "endpoint": ep_key,
                "estimand": est_key,
                "arm": arm,
                "scope": "RANGE_ACROSS_ARMS" if is_range else "ARM",
                "value": value if not is_range else None,
                "value_low": low,
                "value_high": high,
                "unit": unit,
                "ci_low": f.get("ci_low"),
                "ci_high": f.get("ci_high"),
                "ci_level": f.get("ci_level"),
                "dispersion_type": "Standard Error" if f.get("se") is not None else None,
                "dispersion_value": f.get("se"),
                "n_analyzed": f.get("n") if isinstance(f.get("n"), int) else None,
                "baseline_definition": f.get("baseline_definition"),
                "timepoint_weeks": weeks,
                "source": key,
                "quote": quote,
                "notes": f.get("notes"),
            }
            row = {k: v for k, v in row.items() if v is not None}
            try:
                ResultDef.model_validate(row)
            except ValidationError:
                for k in ("ci_low", "ci_high", "ci_level", "dispersion_type", "dispersion_value"):
                    row.pop(k, None)
                try:
                    ResultDef.model_validate(row)
                    if f.get("ci_low") is not None:
                        row["notes"] = ((row.get("notes") or "") + " Confidence interval reported by the source but not contained in the locator quote; not entered.").strip()
                except ValidationError as exc:
                    rejected.append(f"{fid}: result rejected — {exc.errors()[0]['msg']}")
                    continue
            bucket["results"].append(row)
            kept += 1

    out_dir = DATA_DIR / "studies"
    out_dir.mkdir(exist_ok=True)
    header = (
        "# GENERATED by scripts/notes_to_curated.py from research/web-notes/*.json (retrieved {date}).\n"
        "# Values are as reported by the cited source; each number appears in its own locator quote.\n"
        "# Verification status is assigned by the loader (quote anchoring) — nothing here is human-verified.\n"
    ).format(date=RETRIEVED)
    for nct, bucket in sorted(files.items()):
        used_endpoints = {r["endpoint"] for r in bucket["results"]} | {a["endpoint"] for a in bucket["analyses"]}
        used_estimands = {r.get("estimand") for r in bucket["results"]} | {a.get("estimand") for a in bucket["analyses"]}
        doc = {
            "study": nct,
            "estimands": [e for k, e in bucket["estimands"].items() if k in used_estimands],
            "endpoints": [{k: v for k, v in e.items() if v is not None} for key, e in bucket["endpoints"].items() if key in used_endpoints],
            "results": bucket["results"],
            "analyses": bucket["analyses"],
            "adverse_events": bucket["adverse_events"],
            "claims": bucket["claims"],
        }
        for estimand in doc["estimands"]:
            if estimand.get("quote") is None:
                estimand.pop("quote", None)
        (out_dir / f"{nct}.generated.yaml").write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=200), encoding="utf-8")

    (DATA_DIR / "sources.research.yaml").write_text(
        header + yaml.safe_dump({"sources": sorted(generated_sources.values(), key=lambda s: s["key"])}, sort_keys=False, allow_unicode=True, width=200),
        encoding="utf-8",
    )
    (DATA_DIR / "claims.yaml").write_text(header + yaml.safe_dump({"claims": general_claims}, sort_keys=False, allow_unicode=True, width=200), encoding="utf-8")
    report = [f"# Notes → curated conversion report ({RETRIEVED})", "", f"Kept {kept} findings; rejected or skipped {len(rejected)}.", ""]
    report += [f"- {line}" for line in rejected]
    (NOTES_DIR / "conversion-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    summary = {nct: {k: len(v) for k, v in b.items()} for nct, b in sorted(files.items())}
    print(json.dumps({"kept": kept, "rejected": len(rejected), "web_sources": len(generated_sources), "general_claims": len(general_claims), "studies": summary}, indent=1))


if __name__ == "__main__":
    main()
