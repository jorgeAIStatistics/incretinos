"""Convert research notes (research/web-notes/*.json) into curated evidence files.

The notes were gathered on 2026-10-13 by AI-assisted retrieval from primary sources; each
finding carries a source URL and a short verbatim locator quote. This script is the only
path from those notes into the product, and it is deliberately conservative:

* registry-sourced findings are skipped — the ClinicalTrials.gov adapter extracts those itself;
* a numeric finding is kept only if the curation schema accepts it: its digits appear in its own
  locator quote, its sign is the sign the quote prints (or the quote states the reduction in
  words), its unit follows the endpoint category's convention, and — for a responder proportion —
  the quote does not name a different threshold from the endpoint;
* every distinct endpoint gets its own key. The key is built from the complete endpoint text
  together with category, responder threshold, unit and time point, and the conversion FAILS if two
  different endpoints would share a key (``EndpointKeyCollision``). An earlier version cut the text to
  28 characters, which folded ">=10%", ">=15%" and ">=20%" responder values under ">=5%" and fat mass
  under lean mass;
* endpoint role and multiplicity status describe the endpoint, not one arm's value, so they are NOT part
  of the key: the placebo, lowest-dose and comparator values of one source table stay on the same endpoint
  as the active arms. When the notes disagree, the endpoint takes the value its active-arm rows share (or
  the role a quote states), otherwise NOT_STATED; each row whose note says otherwise keeps that in its notes
  and the disagreement is listed in the report;
* a row that a hand-curated file of the same study already holds (same source, quote and value) is not
  generated a second time;
* a ratio of geometric least-squares means is stored as such (param_type), not as a treatment difference;
* a non-inferiority conclusion (ni_met) is set only from a quote of the cited source that states it in words;
* the adverse-event measure kind is read from the endpoint's own wording with parenthetical
  disclaimers set aside, so "incidence of dysesthesia (... NOT discontinuation)" is an incidence;
* an estimand record is written per source. It is STATED_BY_SOURCE only when a quote from that
  same source names the estimand; otherwise it is INFERRED_BY_CURATOR;
* findings that restate a change as a positive "reduction" are skipped (sign ambiguity);
* everything rejected is listed, with the reason, in research/web-notes/conversion-report.md.

Nothing produced here is marked verified: the loader later anchors each quote against stored
source text and otherwise queues the statement for human review.

Usage:  python scripts/notes_to_curated.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from pydantic import ValidationError

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from incretinos.curation.schema import (  # noqa: E402
    SIGN_FROM_WORDING,
    SIGNED_CHANGE_CATEGORIES,
    AdverseEventDef,
    AnalysisDef,
    ClaimDef,
    EndpointDef,
    EstimandDef,
    ResultDef,
    StudyEvidenceFile,
    ae_kind_for_term,
    estimand_named_in,
    has_direction_wording,
    number_in_quote,
    printed_signs,
    result_identity,
    row_problem,
)
from incretinos.enums import AeMeasureKind, EndpointCategory, EstimandBasis, EstimandLabel  # noqa: E402
from incretinos.vocab import arm_drug_slug, categorize_endpoint  # noqa: E402

NOTES_DIR = BACKEND.parent / "research" / "web-notes"
DATA_DIR = BACKEND / "incretinos" / "curation" / "data"
RETRIEVED = "2026-10-13"

LABEL_BRANDS = ("zepbound", "mounjaro", "trulicity", "foundayo")
ESTIMAND = {"efficacy": "EFFICACY", "treatment-regimen": "TREATMENT_REGIMEN", "treatment regimen": "TREATMENT_REGIMEN"}
ROLE = {"primary": "PRIMARY", "key_secondary": "KEY_SECONDARY", "secondary": "SECONDARY", "exploratory": "EXPLORATORY"}
TRI = {"yes": "YES", "no": "NO"}
ESTIMAND_MEANING = {
    "EFFICACY": "the treatment effect had participants remained on study treatment (hypothetical strategy for treatment discontinuation).",
    "TREATMENT_REGIMEN": "the treatment effect regardless of treatment discontinuation (treatment-policy strategy).",
}
ESTIMAND_NAME = {"EFFICACY": "Efficacy estimand", "TREATMENT_REGIMEN": "Treatment-regimen estimand"}
ESTIMAND_ICE = {"EFFICACY": "HYPOTHETICAL", "TREATMENT_REGIMEN": "TREATMENT_POLICY"}
SIGN_NOTE = "Sign: the source prints this number without a minus sign and states the loss or reduction in words; it is stored as a negative change."
AE_ROUTE = re.compile(r"discontinu|non-completion|adverse event|nausea|vomiting|diarrh|constipation|dys[a]?esthesia|serious adverse|decreased appetite|fatigue")
DISCONTINUATION_KINDS = {
    AeMeasureKind.TREATMENT_DISCONTINUATION_DUE_TO_AE,
    AeMeasureKind.TREATMENT_DISCONTINUATION_ANY_REASON,
    AeMeasureKind.STUDY_WITHDRAWAL_DUE_TO_AE,
    AeMeasureKind.DISCONTINUATION_ANY_REASON,
}


class ConversionError(RuntimeError):
    """The notes cannot be converted without guessing; nothing is written."""


class EndpointKeyCollision(ConversionError):
    """Two different endpoints would be stored under one key."""


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


# --------------------------------------------------------------------------- endpoints


def endpoint_identity(definition: dict) -> tuple:
    """Everything that makes an endpoint the endpoint it is. Two definitions with different identities are different endpoints.

    Role and multiplicity status are not part of it: they describe how the endpoint is tested, which the notes record
    per comparison, and putting them in the identity split one source table (placebo, a lower dose) off its endpoint.
    """
    return (
        definition["title"],
        definition["category"],
        definition.get("threshold"),
        definition.get("unit"),
        definition.get("timepoint_weeks"),
    )


def endpoint_key(definition: dict) -> str:
    """A key that is different for every different endpoint.

    The readable part carries category, responder threshold, time point and the start of the title; the digest is
    taken over the complete identity, so two endpoints whose titles agree in their first words (">=5%" / ">=20%",
    "lean mass" / "fat mass", "AUC" / "Cmax") can never share a key.
    """
    weeks = definition.get("timepoint_weeks")
    threshold = definition.get("threshold")
    digest = hashlib.sha1("\x1f".join(repr(part) for part in endpoint_identity(definition)).encode("utf-8")).hexdigest()[:8]
    parts = [
        definition["category"],
        f"ge{threshold:g}pct" if threshold is not None else "",
        f"w{weeks:g}" if weeks is not None else "wna",
        slug(definition["title"], 44),
        digest,
    ]
    return slug("-".join(p for p in parts if p), 110)


def register_endpoint(endpoints: dict[str, dict], key: str, definition: dict) -> dict:
    """Record an endpoint under its key; refuse to put a different endpoint under a key already taken.

    Role and multiplicity are settled afterwards for the whole endpoint (``settle_endpoint_status``).
    """
    existing = endpoints.get(key)
    if existing is None:
        endpoints[key] = {"key": key, **definition}
        return endpoints[key]
    if endpoint_identity(existing) != endpoint_identity(definition):
        raise EndpointKeyCollision(
            f"endpoint key '{key}' would hold two different endpoints: "
            f"{endpoint_identity(existing)!r} and {endpoint_identity(definition)!r}. "
            "Distinct endpoints must have distinct keys; nothing was written."
        )
    return existing


_ROLE_IN_QUOTE = (
    ("PRIMARY", re.compile(r"\bprimary\s+(?:end\s?point|outcome|efficacy\s+(?:end\s?point|outcome))", re.IGNORECASE)),
    ("KEY_SECONDARY", re.compile(r"\bkey\s+secondary\b", re.IGNORECASE)),
)
STATUS_FIELDS = (("role", "endpoint role"), ("multiplicity_controlled", "multiplicity control"))


def settle_endpoint_status(nct: str, endpoint: dict, entries: list[dict]) -> list[str]:
    """Role and multiplicity status of ONE endpoint, from every row that uses it; report lines for disagreements.

    ``entries`` hold, per row, the status its research note records for that row's comparison, whether the row is an
    active-arm value, its quote and the row itself. The endpoint takes the value its active-arm rows share (placebo
    rows usually record nothing); if they disagree, a role that the quotes themselves name ("primary endpoint") and that
    is among the recorded values settles it; otherwise the endpoint is NOT_STATED. A row whose note records a different
    value keeps it in its own notes, so nothing the notes say is lost.
    """
    lines: list[str] = []
    for field, what in STATUS_FIELDS:
        stated = [e for e in entries if e[field] != "NOT_STATED"]
        values = {e[field] for e in stated if e["active"]} or {e[field] for e in stated}
        if len(values) <= 1:
            value = next(iter(values), "NOT_STATED")
        else:
            quoted = {role for e in entries for role, pattern in _ROLE_IN_QUOTE if pattern.search(e["quote"] or "")} if field == "role" else set()
            value = quoted.pop() if len(quoted) == 1 and quoted <= values else "NOT_STATED"
            per_value = "; ".join(f"{v} for {', '.join(sorted({e['arm'] for e in stated if e[field] == v}))}" for v in sorted(values))
            settled = f"stored {value} (named by the source quote)" if value != "NOT_STATED" else "stored NOT_STATED; each row keeps its own statement in its notes — needs human curation"
            lines.append(f"{nct}: endpoint '{endpoint['title'][:70]}' — the research notes record {what} per comparison ({per_value}); {settled}")
        endpoint[field] = value
        for e in entries:
            if e[field] not in ("NOT_STATED", value):
                shown = {"role": e[field], "multiplicity_controlled": {"YES": "controlled (YES)", "NO": "not controlled (NO)"}.get(e[field], e[field])}[field]
                note = f"The research note for this row records the {what} of this comparison as {shown}; the endpoint record holds {value}."
                e["row"]["notes"] = ((e["row"].get("notes") or "") + " " + note).strip()
    return lines


def _norm_quote(quote: str | None) -> str:
    return " ".join((quote or "").split())


def analysis_param_type(endpoint_text: str, unit_raw: str) -> str:
    """What the estimate of a between-arm quantity is, as the note's endpoint text and unit name it."""
    text = f"{endpoint_text} {unit_raw}".lower()
    if "hazard ratio" in text:
        return "Hazard ratio"
    if re.search(r"ratio of geometric (?:least[- ]squares |ls )?means?", text):
        return "Ratio of geometric least-squares means"
    return "Estimated treatment difference"


#: Words in which a source states that non-inferiority was shown (not a p-value or a design statement).
_NI_CONCLUSION = re.compile(
    r"\b(?:was|were|is|are|proved|shown to be|found to be)\s+non-?inferior\b"
    r"|\bdemonstrat\w*\s+(?:the\s+)?non-?inferiority\b"
    r"|\bnon-?inferiority\s+(?:was|were)\s+(?:met|achieved|demonstrated|shown|established|confirmed)\b"
    r"|\bmet\s+(?:the\s+)?(?:criteri\w+\s+for\s+)?non-?inferiority\b",
    re.IGNORECASE,
)
NI_NOT_STATED_NOTE = (
    "ni_met is left NOT_STATED: no quote from the cited source states in words that non-inferiority was met "
    "(the quote gives a non-inferiority p-value or the design, not the conclusion) — needs human curation."
)


def settle_ni_met(row: dict) -> None:
    """Set ni_met YES only when the row's own quote states the conclusion in words; otherwise say why it is not set.

    A quote from elsewhere in the source is not used: with several non-inferiority comparisons in one trial it could
    state the conclusion of another comparison.
    """
    if _NI_CONCLUSION.search(" ".join([row.get("quote") or "", *row.get("extra_quotes", [])])):
        row["ni_met"] = "YES"
    else:
        row["notes"] = ((row.get("notes") or "") + " " + NI_NOT_STATED_NOTE).strip()


def is_reduction_magnitude(endpoint_text: str, unit_raw: str, value: object) -> bool:
    """A positive number the source reports as an amount of loss ("average body weight loss of 20.9%")."""
    if not isinstance(value, int | float) or value <= 0:
        return False
    if re.search(r"reduction|loss", unit_raw, re.IGNORECASE):
        return True
    return bool(re.search(r"\b(?:loss|reduction)\b", endpoint_text, re.IGNORECASE)) and "change" not in endpoint_text.lower()


def endpoint_definition(endpoint_text: str, unit_raw: str, unit: str | None, weeks: float | None, role: str, mult: str, value: object) -> dict:
    category, threshold = categorize_endpoint(endpoint_text, unit)
    if category in SIGNED_CHANGE_CATEGORIES and is_reduction_magnitude(endpoint_text, unit_raw, value):
        # Reported as a positive amount lost, not as a signed change: kept exactly as reported, outside the
        # signed-change categories, instead of flipping its sign here.
        category = EndpointCategory.OTHER
    return {
        "title": endpoint_text[:300] or category.value,
        "role": role,
        "category": category.value,
        "threshold": threshold,
        "unit": unit,
        "timepoint_weeks": weeks,
        "multiplicity_controlled": mult,
    }


# --------------------------------------------------------------------------- signs


def apply_sign_basis(row: dict, fields: tuple[str, ...]) -> str | None:
    """Set ``sign_basis`` when negative numbers are printed unsigned with loss wording; return a reason to reject otherwise."""
    quote = row.get("quote") or ""
    unsigned = [name for name in fields if isinstance(row.get(name), int | float) and row[name] < 0 and printed_signs(row[name], quote) == {1}]
    if not unsigned:
        return None
    if not has_direction_wording(quote):
        shown = ", ".join(f"{name}={row[name]:g}" for name in unsigned)
        return f"sign not established by the quote ({shown} stored as negative; the quote prints the number without a minus sign and without loss/reduction wording) — needs human curation"
    row["sign_basis"] = SIGN_FROM_WORDING
    row["notes"] = ((row.get("notes") or "") + " " + SIGN_NOTE).strip()
    return None


def first_error(exc: ValidationError) -> str:
    return exc.errors()[0]["msg"].removeprefix("Value error, ")


def fit(model: type, row: dict, *, optional: tuple[str, ...]) -> tuple[dict | None, str | None]:
    """Validate a row. A confidence interval the quote does not contain is left out (and the row says which limit
    was not found); every other failure rejects the row."""
    try:
        model.model_validate(row)
        return row, None
    except ValidationError as exc:
        reason = first_error(exc)
    quote = row.get("quote") or ""
    absent = [name for name in ("ci_low", "ci_high") if row.get(name) is not None and not number_in_quote(row[name], quote)]
    if not absent:
        return None, reason
    trimmed = {k: v for k, v in row.items() if k not in optional}
    try:
        model.model_validate(trimmed)
    except ValidationError as exc:
        return None, first_error(exc)
    limits = ", ".join(f"{name} {row[name]:g}" for name in absent)
    trimmed["notes"] = ((trimmed.get("notes") or "") + f" The note records a confidence interval, but {limits} does not appear in the locator quote; the interval is not entered.").strip()
    return trimmed, None


# --------------------------------------------------------------------------- conversion


def convert(notes_dir: Path = NOTES_DIR, data_dir: Path = DATA_DIR) -> dict:
    """Build every generated document in memory. Raises ConversionError rather than writing something ambiguous."""
    manual_sources = {}
    existing = yaml.safe_load((data_dir / "sources.yaml").read_text())["sources"]
    for src in existing:
        if src.get("url"):
            manual_sources[src["url"]] = src["key"]
    watch = {s["nct"] for s in yaml.safe_load((data_dir / "watchlist.yaml").read_text())["studies"]}

    # Studies with a hand-curated file keep their contrasts there; do not also generate them heuristically.
    manual_studies = {p.name.split(".")[0] for p in (data_dir / "studies").glob("*.yaml") if ".generated." not in p.name}
    # Rows a hand-curated file already holds (same study, source, quote and value) are not generated a second time.
    manual_rows: dict[tuple, str] = {}
    for path in sorted((data_dir / "studies").glob("*.yaml")):
        if ".generated." in path.name:
            continue
        doc = yaml.safe_load(path.read_text()) or {}
        for kind, fields in (("results", ("value", "value_low")), ("analyses", ("estimate",)), ("adverse_events", ("percent", "n_affected", "percent_low"))):
            for item in doc.get(kind) or []:
                number = next((item.get(f) for f in fields if item.get(f) is not None), None)
                manual_rows[(doc.get("study"), kind, item.get("source"), _norm_quote(item.get("quote")), number)] = path.name
    statuses: dict[tuple[str, str], list[dict]] = defaultdict(list)  # (nct, endpoint key) -> per-row role / multiplicity
    generated_sources: dict[str, dict] = {}
    files: dict[str, dict] = defaultdict(lambda: {"estimands": {}, "endpoints": {}, "results": [], "analyses": [], "adverse_events": [], "claims": []})
    general_claims: list[dict] = []
    rejected: list[str] = []
    status_notes: list[str] = []  # endpoint role / multiplicity recorded differently per comparison
    kept = 0

    all_notes = []
    quotes_by_source: dict[str, list[tuple[str | None, str]]] = defaultdict(list)  # source key -> (nct, verbatim quote), in note order
    for notes_path in sorted(notes_dir.glob("*.json")):
        notes = json.loads(notes_path.read_text())
        sources = {s["id"]: s for s in notes.get("sources", [])}
        all_notes.append((notes_path.stem, notes, sources))
        for f in notes.get("findings", []):
            src = sources.get(f.get("source_id"))
            key = source_key(src, manual_sources) if src else None
            quote = (f.get("locator_quote") or "").strip()
            if key and quote:
                quotes_by_source[key].append((f.get("nct"), quote))

    def estimand_entry(bucket: dict, label: str, key: str, nct: str, own_quote: str | None) -> str:
        """One estimand record per (label, source). STATED_BY_SOURCE only with a quote from that source naming it."""
        est_key = f"{label.lower()}@{key}"
        if est_key in bucket["estimands"]:
            return est_key
        enum_label = EstimandLabel(label)
        candidates = [own_quote] if own_quote else []
        candidates += [q for q_nct, q in quotes_by_source[key] if q_nct in (nct, None)]
        quote = next((q for q in candidates if estimand_named_in(enum_label, q)), None)
        entry = {
            "key": est_key,
            "label": label,
            "basis": (EstimandBasis.STATED_BY_SOURCE if quote else EstimandBasis.INFERRED_BY_CURATOR).value,
            "description": (
                f"{ESTIMAND_NAME[label]} as named by the source: {ESTIMAND_MEANING[label]}"
                if quote
                else f"{ESTIMAND_NAME[label]} as recorded in the research note for this source; the note holds no verbatim quote from "
                f"the source that names the estimand, so the label is the curator's attribution: {ESTIMAND_MEANING[label]}"
            ),
            "source": key,
            "strategies": [{"event": "TREATMENT_DISCONTINUATION", "strategy": ESTIMAND_ICE[label]}],
        }
        if quote:
            entry["quote"] = quote
        EstimandDef.model_validate(entry)
        bucket["estimands"][est_key] = entry
        return est_key

    for stem, notes, sources in all_notes:
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
                    rejected.append(f"{fid}: claim rejected — {first_error(exc)}")
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
            if AE_ROUTE.search(lower):
                # The kind comes from the endpoint's own wording; a parenthetical "(NOT discontinuation)" says what
                # the measure is not and must not decide what it is.
                kind = ae_kind_for_term(endpoint_text)
                term = re.sub(r"^incidence of (adverse event: )?", "", endpoint_text, flags=re.IGNORECASE)
                term = re.sub(r"\s*\(.*$", "", term).strip() or endpoint_text
                if kind in DISCONTINUATION_KINDS:
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
                    "measure_kind": kind.value,
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
                    rejected.append(f"{fid}: AE rejected — {first_error(exc)}")
                    continue
                number = next((row.get(f) for f in ("percent", "n_affected", "percent_low") if row.get(f) is not None), None)
                held = manual_rows.get((nct, "adverse_events", key, _norm_quote(quote), number))
                if held:
                    rejected.append(f"{fid}: AE not generated — the hand-curated file {held} already holds this row (same source, quote and value) ({arm}; {endpoint_text[:50]})")
                    continue
                bucket["adverse_events"].append(row)
                kept += 1
                continue

            role = ROLE.get((f.get("endpoint_role") or "").lower(), "NOT_STATED")
            mult = TRI.get((f.get("multiplicity_controlled") or "").lower(), "NOT_STATED")
            definition = endpoint_definition(endpoint_text, unit_raw, unit, weeks, role, mult, value)
            try:
                endpoint_model = EndpointDef.model_validate({"key": "pending", **{k: v for k, v in definition.items() if v is not None}})
            except ValidationError as exc:
                rejected.append(f"{fid}: endpoint rejected — {first_error(exc)} ({arm}; {endpoint_text[:50]})")
                continue
            ep_key = endpoint_key(definition)

            arm_is_contrast = re.search(r"\s(?:vs\.?|versus|minus)\s", arm) is not None
            says_contrast = "hazard ratio" in unit_raw.lower() or re.search(r"difference|hazard ratio|time to first|ratio of geometric", lower) is not None
            if says_contrast and not arm_is_contrast:
                rejected.append(f"{fid}: between-arm quantity without an explicit 'A vs B' arm label — not entered ({arm}; {endpoint_text[:50]})")
                continue
            is_contrast = says_contrast and arm_is_contrast
            if is_contrast and nct in manual_studies:
                rejected.append(f"{fid}: between-arm contrast not generated — {nct} has a hand-curated file, which holds this study's contrasts ({arm}; {endpoint_text[:50]})")
                continue

            if is_contrast:
                parts = re.split(r"\s+(?:vs\.?|versus|minus)\s+", arm, maxsplit=1)
                treatment, comparator = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else None)
                statement = (f.get("statement") or "") + " " + (f.get("notes") or "")
                hypothesis = "NON_INFERIORITY" if re.search(r"non-?inferior", statement, re.IGNORECASE) else "SUPERIORITY" if re.search(r"superior", statement, re.IGNORECASE) else "NOT_STATED"
                row = {
                    "endpoint": ep_key,
                    "treatment": treatment,
                    "comparator": comparator,
                    "hypothesis_type": hypothesis,
                    "param_type": analysis_param_type(endpoint_text, unit_raw),
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
                model, optional, sign_fields, target = AnalysisDef, ("ci_low", "ci_high", "ci_level"), ("estimate", "ci_low", "ci_high"), "analyses"
            else:
                row = {
                    "endpoint": ep_key,
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
                model, optional, sign_fields, target = ResultDef, ("ci_low", "ci_high", "ci_level", "dispersion_type", "dispersion_value"), ("value", "value_low", "value_high", "ci_low", "ci_high"), "results"

            what = "analysis" if is_contrast else "result"
            reason = apply_sign_basis(row, sign_fields)
            if reason:
                rejected.append(f"{fid}: {what} rejected — {reason} ({arm}; {endpoint_text[:50]})")
                continue
            row, reason = fit(model, row, optional=optional)
            if row is None:
                rejected.append(f"{fid}: {what} rejected — {reason}")
                continue
            number = row.get("estimate") if is_contrast else (row.get("value") if row.get("value") is not None else row.get("value_low"))
            held = manual_rows.get((nct, target, key, _norm_quote(quote), number))
            if held:
                rejected.append(f"{fid}: {what} not generated — the hand-curated file {held} already holds this row (same source, quote and value) ({arm}; {endpoint_text[:50]})")
                continue
            if is_contrast and row.get("hypothesis_type") == "NON_INFERIORITY":
                settle_ni_met(row)
            est_key = estimand_entry(bucket, est_label, key, nct, quote) if est_label else None
            if est_key:
                row = {"endpoint": row["endpoint"], "estimand": est_key, **{k: v for k, v in row.items() if k != "endpoint"}}
            estimand_model = EstimandDef.model_validate(bucket["estimands"][est_key]) if est_key else None
            reason = row_problem(model.model_validate(row), endpoint_model, estimand_model)
            if reason:
                rejected.append(f"{fid}: {what} rejected — {reason} ({arm}; {endpoint_text[:50]}) — needs human curation")
                continue
            register_endpoint(bucket["endpoints"], ep_key, definition)
            bucket[target].append(row)
            statuses[(nct, ep_key)].append(
                {"role": role, "multiplicity_controlled": mult, "active": is_contrast or not arm_drug_slug(arm)[1], "quote": quote, "arm": arm, "row": row}
            )
            kept += 1

    # Role and multiplicity belong to the endpoint: settle them once over every row that uses it.
    for (nct, ep_key), entries in statuses.items():
        status_notes.extend(settle_endpoint_status(nct, files[nct]["endpoints"][ep_key], entries))

    # One endpoint, arm, estimand, source and time point cannot have two different values. If the notes say it
    # does, the notes do not settle which value belongs here: neither is entered.
    for nct, bucket in files.items():
        by_identity: dict[tuple, list[dict]] = defaultdict(list)
        for row in bucket["results"]:
            by_identity[result_identity(ResultDef.model_validate(row))].append(row)
        for rows in by_identity.values():
            values = {r.get("value") if r.get("value") is not None else r.get("value_low") for r in rows}
            if len(values) > 1:
                for r in rows:
                    bucket["results"].remove(r)
                    kept -= 1
                shown = ", ".join(f"{v:g}" for v in sorted(values))
                rejected.append(f"{nct}: results rejected — {len(rows)} different values ({shown}) for one endpoint, arm, estimand, source and time point ({rows[0]['arm']}; {rows[0]['endpoint']}) — needs human curation")

    header = (
        f"# GENERATED by scripts/notes_to_curated.py from research/web-notes/*.json (retrieved {RETRIEVED}).\n"
        "# Values are as reported by the cited source; each number appears in its own locator quote.\n"
        "# Verification status is assigned by the loader (quote anchoring) — nothing here is human-verified.\n"
    )
    documents: dict[str, dict] = {}
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
        try:
            StudyEvidenceFile.model_validate(doc)
        except ValidationError as exc:  # every row was checked above, so this is a defect in the converter itself
            raise ConversionError(f"{nct}: generated file does not pass the curation schema — {first_error(exc)}") from exc
        documents[nct] = doc

    return {
        "header": header,
        "documents": documents,
        "sources": sorted(generated_sources.values(), key=lambda s: s["key"]),
        "claims": general_claims,
        "kept": kept,
        "rejected": rejected,
        "status_notes": status_notes,
    }


def main() -> None:
    out = convert()
    header = out["header"]
    out_dir = DATA_DIR / "studies"
    out_dir.mkdir(exist_ok=True)
    for stale in out_dir.glob("*.generated.yaml"):
        if stale.name.split(".")[0] not in out["documents"]:
            stale.unlink()
    for nct, doc in out["documents"].items():
        (out_dir / f"{nct}.generated.yaml").write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=200), encoding="utf-8")
    (DATA_DIR / "sources.research.yaml").write_text(
        header + yaml.safe_dump({"sources": out["sources"]}, sort_keys=False, allow_unicode=True, width=200),
        encoding="utf-8",
    )
    (DATA_DIR / "claims.yaml").write_text(header + yaml.safe_dump({"claims": out["claims"]}, sort_keys=False, allow_unicode=True, width=200), encoding="utf-8")
    rejected = out["rejected"]
    report = [f"# Notes → curated conversion report ({RETRIEVED})", "", f"Kept {out['kept']} findings; rejected or skipped {len(rejected)}.", ""]
    report += [f"- {line}" for line in rejected]
    report += [
        "",
        "## Endpoint role and multiplicity recorded per comparison",
        "",
        "Role and multiplicity status are stored once per endpoint. Where the notes record them differently for the rows of one",
        "endpoint, the endpoint holds the value below and each row whose note differs keeps its own statement in its notes.",
        "",
    ]
    report += [f"- {line}" for line in out["status_notes"]]
    (NOTES_DIR / "conversion-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    summary = {nct: {k: len(v) for k, v in doc.items() if isinstance(v, list)} for nct, doc in out["documents"].items()}
    print(json.dumps({"kept": out["kept"], "rejected": len(rejected), "web_sources": len(out["sources"]), "general_claims": len(out["claims"]), "studies": summary}, indent=1))


if __name__ == "__main__":
    main()
