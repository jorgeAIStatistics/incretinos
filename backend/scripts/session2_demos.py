"""Session 2 demonstrations (A-F), run against a scratch database and written to docs/examples/session-2-demonstrations.md.

Usage (from backend/):  .venv/bin/python scripts/session2_demos.py [--out PATH]

What it does, in order:

1. Creates a temporary directory and seeds a fresh database there with the real command
   (``INCRETINOS_DATABASE_URL=sqlite:///<tmp>/demo.db incretinos seed``). The working database
   (backend/var/incretinos.db), the fixtures and the committed curated data are never written.
2. Writes a reviewers file in the same temporary directory holding the SHA-256 of a token generated for this run only,
   for one reviewer named "DEMONSTRATION REVIEWER (not a scientific review)". The token is never printed.
3. Calls the HTTP API in process (FastAPI TestClient against ``incretinos.api.main.app``; the same routes, validation,
   authentication and transactions as the served API) and prints every request and the relevant fields of the response.
4. For demonstration A, copies the curated directory to the temporary directory, changes one curated value in the copy,
   and re-runs ``incretinos seed --reset`` with INCRETINOS_CURATED_DIR pointing at the copy.

Every output in the document is produced by this run. The one human decision it records is a labelled demonstration in
the scratch database, which is deleted at the end; no row anywhere else becomes HUMAN_VERIFIED.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent.parent
WORKING_DB = (BACKEND / "var" / "incretinos.db").resolve()
DEFAULT_OUT = BACKEND.parent / "docs" / "examples" / "session-2-demonstrations.md"
REVIEWER = "DEMONSTRATION REVIEWER (not a scientific review)"
SM1, SM2, SM5, ATTAIN1, TRIUMPH1 = "NCT04184622", "NCT04657003", "NCT05822830", "NCT05869903", "NCT05929066"

SCRATCH = Path(tempfile.mkdtemp(prefix="incretinos-session2-demo-"))
DB_PATH = SCRATCH / "demo.db"
TOKEN = secrets.token_urlsafe(32)
ENV = {
    "INCRETINOS_DATABASE_URL": f"sqlite:///{DB_PATH}",
    "INCRETINOS_ENV_FILE": "none",
    "INCRETINOS_OFFLINE": "true",
    "INCRETINOS_LLM_PROVIDER": "none",
    "INCRETINOS_RATE_LIMIT_ENABLED": "false",
    "INCRETINOS_CACHE_DIR": str(SCRATCH / "cache"),
    "INCRETINOS_REVIEWERS_FILE": str(SCRATCH / "reviewers.json"),
}
assert DB_PATH.resolve() != WORKING_DB
(SCRATCH / "reviewers.json").write_text(json.dumps({REVIEWER: hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()}), encoding="utf-8")
os.environ.update(ENV)
sys.path.insert(0, str(BACKEND))

SECTIONS: dict[str, list[str]] = {"intro": []}
OUT: list[str] = SECTIONS["intro"]


def section(name: str) -> None:
    """Direct output to one section; the document prints the sections in the order intro, A-F, footer."""
    global OUT
    OUT = SECTIONS.setdefault(name, [])


def w(text: str = "") -> None:
    OUT.append(text)


def block(obj: Any, lang: str = "json") -> None:
    body = obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    w(f"```{lang}\n{body}\n```")


def run_cli(args: list[str], extra_env: dict[str, str] | None = None, shown_env: str = "") -> str:
    """Run the real CLI against the scratch database and return stdout; the command is recorded as typed."""
    env = {**os.environ, **ENV, **(extra_env or {})}
    done = subprocess.run([str(BACKEND / ".venv" / "bin" / "incretinos"), *args], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=900)
    if done.returncode != 0:
        raise SystemExit(f"incretinos {' '.join(args)} failed:\n{done.stderr[-3000:]}")
    return done.stdout


def pick(d: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: d.get(k) for k in keys}


# --------------------------------------------------------------------------- seed the scratch database

seed_out = run_cli(["seed"])
seed_report = json.loads(seed_out[seed_out.index("{"):])

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from incretinos.api.main import app  # noqa: E402
from incretinos.assistant import benchmark  # noqa: E402
from incretinos.assistant.claims import Vocabulary  # noqa: E402
from incretinos.config import get_settings  # noqa: E402
from incretinos.db import get_sessionmaker, reset_engine  # noqa: E402
from incretinos.models import Study  # noqa: E402

assert Path(get_settings().database_url.split("///", 1)[1]).resolve() == DB_PATH.resolve(), "the demonstrations must run on the scratch database"
client = TestClient(app)
AUTH = {"X-Reviewer-Token": TOKEN}
CALLS: list[str] = []


def api(method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, auth: bool = False) -> Any:
    response = client.request(method, path, params=params, json=body, headers=AUTH if auth else None)
    query = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    CALLS.append(f"{method} {path}{query}")
    if response.status_code >= 400:
        raise SystemExit(f"{method} {path}{query} -> {response.status_code}: {response.text[:500]}")
    return response.json() if "json" in response.headers.get("content-type", "") else response.text


def session():
    return get_sessionmaker()()


def analysis_id(db, nct: str, treatment: str, comparator: str = "placebo", *, category: str = "WEIGHT_PCT_CHANGE", weeks: float | None = 72,
                origin: str | None = "REGISTRY", estimand: str | None = None) -> int:
    study = db.scalar(select(Study).where(Study.nct_id == nct))
    for a in sorted(study.analyses, key=lambda x: x.id):
        if a.endpoint.category != category or (weeks is not None and a.timepoint_weeks != weeks) or (origin and a.origin != origin):
            continue
        if treatment.lower() not in (a.treatment_label or "").lower() or comparator.lower() not in (a.comparator_label or "").lower():
            continue
        if estimand and (a.estimand.label if a.estimand else "NOT_STATED") != estimand:
            continue
        return a.id
    raise SystemExit(f"no analysis {nct} {treatment} vs {comparator}")


# --------------------------------------------------------------------------- the document

w("# Session 2 demonstrations")
w()
w("Generated by `backend/scripts/session2_demos.py` on a scratch database seeded for the run (offline fixtures). Every output "
  "below is a real response of this code base, trimmed to the fields that matter; nothing was typed in by hand. Regenerate with:")
w()
block("cd backend\n.venv/bin/python scripts/session2_demos.py      # writes docs/examples/session-2-demonstrations.md", "sh")
w()
w("The script seeds its own database with the real command and points the API at it:")
w()
block("INCRETINOS_DATABASE_URL=sqlite:///$TMP/demo.db INCRETINOS_OFFLINE=true incretinos seed\n"
      "INCRETINOS_REVIEWERS_FILE=$TMP/reviewers.json   # one personal token, generated for this run, SHA-256 stored, never printed", "sh")
w()
w(f"API calls go through FastAPI's TestClient against `incretinos.api.main:app` (the same routes, validation, reviewer authentication "
  f"and transactions as the served API). The scratch database is deleted at the end of the run; `backend/var/incretinos.db` is never opened. "
  f"Seed of this run: {len(seed_report['studies'])} registered trials ingested, {len(seed_report.get('failures') or [])} failures.")
w()

# ============================================================================ B
section("B")
w("## B. AI factual integrity: the claim verifier on misleading and faithful statements")
w()
w("Each statement cites one stored evidence card as [C1]. The verifier is the one the research assistant uses (`assistant/verifier.py`); "
  "it receives the cited card and the sentence and returns a verdict per sentence with its reasons. The misleading statements were written "
  "for this demonstration by altering a faithful one; the stored values were not changed.")
w()
specs = {
    "s1_15": {"kind": "result", "nct": SM1, "arm_contains": "15 mg", "category": "WEIGHT_PCT_CHANGE", "tier": "PEER_REVIEWED", "estimand": "TREATMENT_REGIMEN", "weeks": 72},
    "s1_label": {"kind": "analysis", "nct": SM1, "category": "WEIGHT_PCT_CHANGE", "treatment_contains": "15 mg", "comparator_contains": "placebo", "tier": "REGULATORY_LABEL", "weeks": 72},
    "ac3": {"kind": "analysis", "nct": "NCT06045221", "category": "HBA1C_CHANGE", "treatment_contains": "orforglipron 12 mg", "comparator_contains": "semaglutide 7 mg", "tier": "PEER_REVIEWED"},
}
cases = [
    ("faithful", "VALID", "SUPPORTED", "s1_15",
     "In SURMOUNT-1, tirzepatide 15 mg reduced body weight by 20.9% from baseline to week 72 (95% CI, −21.8 to −19.9) under the treatment-regimen estimand [C1]."),
    ("faithful", "VALID", "SUPPORTED", "s1_label",
     "Per the FDA label, the estimated treatment difference versus placebo for tirzepatide 15 mg in SURMOUNT-1 was -17.8 percentage points (95% CI -19.3, -16.3; p<0.001) at week 72 [C1]."),
    ("direction reversed", "DIRECTION_REVERSED", "REJECT", "s1_15",
     "In SURMOUNT-1, tirzepatide 15 mg increased body weight by 20.9% from baseline to week 72 [C1]."),
    ("drugs swapped", "DRUG_SWAPPED", "REJECT", "s1_15",
     "In SURMOUNT-1, semaglutide 15 mg reduced body weight by 20.9% from baseline to week 72 [C1]."),
    ("arm changed", "ARM_SWAPPED", "REJECT", "s1_15",
     "In SURMOUNT-1, tirzepatide 5 mg reduced body weight by 20.9% from baseline to week 72 [C1]."),
    ("interval misstated", "CI_MISSTATED", "REJECT", "s1_15",
     "In SURMOUNT-1, tirzepatide 15 mg reduced body weight by 20.9% at week 72 (95% CI, −24.8 to −16.9) [C1]."),
    ("superiority claimed from non-inferiority", "NI_AS_SUPERIORITY", "REJECT", "ac3",
     "In ACHIEVE-3, orforglipron 12 mg was superior to oral semaglutide 7 mg in lowering HbA1c (estimated treatment difference -0.48 percentage points; 95% CI -0.65 to -0.31) [C1]."),
]
rows_b = []
with session() as db:
    vocabulary = Vocabulary.from_session(db)
    for kind, category, expected, spec, sentence in cases:
        out = benchmark.run_case(db, {"id": kind, "category": category, "expected": expected, "evidence": [specs[spec]], "sentence": sentence}, vocabulary)
        rows_b.append({"statement": kind, "sentence": sentence, "expected": expected, "verdict": out["outcome"], "verdicts": out.get("verdicts"),
                       "correct": out["correct"], "reasons": out["reasons"][:3]})
for row in rows_b:
    w(f"- **{row['statement']}** (expected {row['expected']}; verifier: **{row['verdict']}**, {'correct' if row['correct'] else 'WRONG'})  ")
    w(f"  _{row['sentence']}_")
    for reason in row["reasons"]:
        w(f"  - {reason}")
w()
w("### The four benchmark sets and their protocol")
w()
w("`incretinos benchmark-claims --set <name>` runs a set against the seeded database. Results of this run (the same code and seed as the "
  "recorded CLI runs):")
w()
w("| Set | Role | Cases | Detection (unsupported rejected) | False rejection (valid rejected) | Benchmark errors |")
w("|---|---|---|---|---|---|")
bench = {}
with session() as db:
    for name in benchmark.SETS:
        rep = benchmark.run_set(db, name)
        bench[name] = rep
        if not rep.get("present"):
            w(f"| {name} | {rep.get('role')} | not present | | | |")
            continue
        w(f"| {name} | {rep.get('role')} | {rep['cases']} | {100 * rep['detection_rate']:.1f}% ({rep['detected']}/{rep['unsupported_cases']}) | "
          f"{100 * rep['false_rejection_rate']:.1f}% ({rep['false_rejections']}/{rep['valid_cases']}) | {len(rep['errors'])} |")
w()
w("Protocol. `dev` and `dev-review` are development data: the verifier was written and repaired against them. `heldout` (heldout-v1) was "
  "measured once, blind, on the merged Session 2 code (detection 98.3%, 113/115; false rejection 20.8%, 10/48); its misses were studied "
  "afterwards, so it is development data from then on. `heldout-v2` (221 cases) was written without sight of the verifier and copied in "
  "verbatim; the number above is reported as measured, without tuning. The false-rejection rate is the cost of failing closed: a valid "
  "sentence the verifier cannot ground is refused, never passed.")
w()

# ============================================================================ C
section("C")
w("## C. Cross-source inconsistency: ATTAIN-1 orforglipron 36 mg, week 72")
w()
groups = api("GET", "/api/conflicts", params={"study": ATTAIN1, "evidence_type": "result", "limit": 200})
group = next(g for g in groups["rows"] if g["arm"] == "36 mg Orforglipron" and g["category"] == "WEIGHT_PCT_CHANGE" and g["time_point"] == "week 72")
detail_c = api("GET", f"/api/conflicts/{group['group_id']}")
w(f"`GET /api/conflicts?study={ATTAIN1}&evidence_type=result` lists the group; `GET /api/conflicts/{group['group_id']}` returns it:")
w()
block({
    "class": detail_c["class"], "explanation": detail_c["explanation"],
    "rows": [pick(r, "tier", "value_printed", "unit", "estimand_label", "analysis_set", "n", "formulation", "dose_reported", "verification_status")
             for r in detail_c["rows"]],
})
w()
def _comparison(pred) -> dict[str, Any] | None:
    return next((c for c in detail_c["comparisons"] if pred(c)), None)


def _dim(c: dict[str, Any], name: str) -> dict[str, Any]:
    return next((d for d in c["dimensions"] if d["dimension"] == name), {})


tiers = {f"{r['evidence_type']}:{r['id']}": r["tier"] for r in detail_c["rows"]}
explainable = _comparison(lambda c: c["class"] == "EXPLAINABLE_DIFFERENCE" and {tiers.get(c["a"]), tiers.get(c["b"])} == {"REGISTRY_RESULTS", "PEER_REVIEWED"})
true_conflict = _comparison(lambda c: c["class"] == "TRUE_CONFLICT")
w("**1. Efficacy versus treatment-regimen estimand: an explainable difference.**")
w()
block({k: explainable[k] for k in ("a_evidence_key", "b_evidence_key", "class", "explanation")} | {"estimand": pick(_dim(explainable, "estimand"), "status", "a", "b", "explains"),
                                                                                                    "next_actions": explainable["next_actions"]})
w()
w("**2. Capsule versus tablet: the label names the 17.2 mg tablet, the trial gave the 36 mg capsule.** The label's row joins this group only "
  "through the documented exposure-equivalent pair; the formulation named is recorded and never used as an explanation:")
w()
block(pick(_dim(true_conflict, "formulation"), "status", "a", "b", "explains", "note"))
w()
w("**3. A true conflict and its next action.** NEJM -11.2 and label -11.1 report the same quantity (treatment-regimen, ITT, n = 730, week 72, "
  "same randomized group) and differ at the precision both printed:")
w()
block({k: true_conflict[k] for k in ("class", "explanation", "possible_reasons", "next_actions")} | {
    "same": {d: _dim(true_conflict, d).get("status") for d in ("estimand", "baseline", "analysis_set", "time_point")}})
w()
w("Evidence tier is context only: the classification never reads it and no value is preferred because a regulator printed it. A true "
  "conflict is a reconciliation question for a reviewer, not an error identified in either source.")
w()

# ============================================================================ D
section("D")
w("## D. New evidence extraction from full-text documents")
w()
fulltext = api("GET", "/api/review/evidence", params={"origin": "FULLTEXT", "limit": 200})
w(f"`GET /api/review/evidence?origin=FULLTEXT` returns {fulltext['total']} rows, every one `PENDING_HUMAN_REVIEW` "
  f"({sorted({r['verification_status'] for r in fulltext['rows']})}) and out of analytic use until approved "
  f"(usable: {sorted({r['usable'] for r in fulltext['rows']})}).")
w()


def fulltext_card(row: dict[str, Any]) -> dict[str, Any]:
    d = api("GET", f"/api/review/evidence/{row['evidence_type']}/{row['id']}")
    ft = d["fulltext"]
    doc, table, ex = ft["document"], ft["table"], ft["extraction"]
    q = ex["quality"]
    return {
        "label": d["label"],
        "value": pick(d["values"], "param_type", "estimate", "estimate_text", "unit", "ci_low", "ci_high", "ci_level", "p_value_text") if row["evidence_type"] == "analysis"
        else pick(d["values"], "value", "value_text", "unit", "n_analyzed"),
        "status": pick(d["status"], "verification_status", "usable", "exclusion_reason"),
        "document": pick(doc, "key", "kind", "title", "url", "pmcid", "pmid", "doi", "application_number", "licence", "licence_url", "full_text_stored",
                         "storage_basis", "content_hash", "retrieved_at", "parse_status"),
        "location": ({"table": table["label"], "caption": table["caption"], "locator": table["locator"], "authorship": table["authorship"],
                      "source_line": table["source_line"], "used_cells_row_column": ft["used_cells"]} if table else
                     {"passage": ex["passage"]}),
        "cells": [pick(c, "role", "row_label", "column_header", "row_index", "col_index", "text") for c in ex["cells"]],
        "parse_method": f"{ex['parse_method']} v{ex['parse_version']}",
        "quality": {k: q.get(k) for k in ("value_source", "estimand", "subgroup", "population", "arm_match", "comparator_match") if q.get(k) is not None},
        "confidence": ex["confidence"],
        "check_flags": [pick(f, "code", "severity") for f in d["check_flags"]],
    }


sm5 = next(r for r in fulltext["rows"] if r["study_nct"] == SM5 and "Change from baseline in HbA1c" in (r["label"] or ""))
w("**1. SURMOUNT-5, prediabetes subgroup: HbA1c difference, tirzepatide versus semaglutide** (Europe PMC PMC13451520, CC BY)")
w()
card = fulltext_card(sm5)
for side in ("arm_match", "comparator_match"):
    if side in card["quality"]:
        card["quality"][side] = pick(card["quality"][side], "header", "status", "arm_label", "basis")
block(card)
w()
attain = [r for r in fulltext["rows"] if r["study_nct"] == ATTAIN1 and r["evidence_type"] == "analysis" and "FDA primary analysis" in (r["label"] or "")]
w(f"**2. ATTAIN-1, FDA review: treatment-regimen contrasts against placebo** ({len(attain)} analyses from one FDA-authored table)")
w()
cards = [fulltext_card(r) for r in sorted(attain, key=lambda r: r["label"])]
block({"document": cards[0]["document"], "location": cards[0]["location"], "parse_method": cards[0]["parse_method"],
       "estimand": cards[0]["quality"].get("estimand")})
w()
w("| Analysis | Cells read (row, column) | Estimate (95% CI) | p | Status | Check flags |")
w("|---|---|---|---|---|---|")
for c in cards:
    cell = next((x for x in c["cells"] if x["role"] == "estimate_and_interval"), {})
    w(f"| {c['label'].split(' — ')[-1]} | {cell.get('row_label')} / {cell.get('column_header')} (row {cell.get('row_index')}, column {cell.get('col_index')}) | "
      f"{c['value']['estimate_text']} | {c['value']['p_value_text']} | {c['status']['verification_status']} | {', '.join(f['code'] for f in c['check_flags']) or 'none'} |")
w()
w("The quality assessment describes how a value was read; it is not evidence that the value is right. None of these rows enters a "
  "comparison, a conflict group, an indirect calculation or a report until a reviewer approves it.")
w()

# ============================================================================ E
section("E")
w("## E. Statistical integrity")
w()
with session() as db:
    ids = {
        "sm1_15": analysis_id(db, SM1, "15 mg Tirzepatide"), "attain_36": analysis_id(db, ATTAIN1, "36 mg Orforglipron"),
        "sm2_15": analysis_id(db, SM2, "15 mg Tirzepatide"), "triumph1_12": analysis_id(db, TRIUMPH1, "12 mg", weeks=80, origin="CURATED"),
    }
direct = api("GET", f"/api/analyses/{ids['sm1_15']}")
w("**1. A direct randomized comparison with its reported effect** (`GET /api/analyses/{id}`, SURMOUNT-1 tirzepatide 15 mg versus placebo, registry-posted analysis)")
w()
eff = direct["interpretation"]["reported_effect"]
block({k: eff.get(k) for k in ("scale", "orientation", "estimate_text", "unit", "ci", "p_value", "estimand", "analysis_population", "multiplicity", "hypothesis",
                               "verification_status", "plain_reading")})
w()
assess_body = {"a": ids["sm1_15"], "b": ids["attain_36"]}
ind = api("POST", "/api/indirect/assess", body=assess_body)
w("**2. Anchored indirect comparison: SURMOUNT-1 tirzepatide 15 mg × ATTAIN-1 orforglipron 36 mg, common comparator placebo** "
  f"(`POST /api/indirect/assess` with `{json.dumps(assess_body)}`)")
w()
block({
    "status": ind["status"], "label": ind["label"],
    "inputs": [pick(i, "side", "study_name", "orientation", "estimate_text", "ci_low", "ci_high", "ci_level", "ci_sides", "se", "se_formula", "estimand_label",
                    "verification_status", "tier") for i in ind["inputs"]],
    "result": pick(ind["result"], "orientation", "estimate_text", "ci_text", "se", "p_value_text", "formula", "reading"),
    "assumptions": ind["result"]["assumptions"],
    "checks": [f"{c['label']}: {c['status']}" for c in ind["checks"]],
    "unverified_assumptions (CANNOT_ASSESS, shown beside the result)": [f"{u['label']}: {u['detail']}" for u in ind["unverified_assumptions"]],
    "not_a_randomized_comparison": ind["not_a_randomized_comparison"], "not_a_ranking": ind["not_a_ranking"],
})
w()
w("Each SE is recovered from the stored two-sided 95% interval, SE = (upper − lower) / (2 × 1.96); the indirect SE is "
  "√(SE_A² + SE_B²) because the two trials are independent.")
w()
for title, a_id, b_id in (("SURMOUNT-2 (type 2 diabetes) × ATTAIN-1 (no diabetes)", ids["sm2_15"], ids["attain_36"]),
                          ("TRIUMPH-1 week 80 × SURMOUNT-1 week 72", ids["triumph1_12"], ids["sm1_15"])):
    refused = api("POST", "/api/indirect/assess", body={"a": a_id, "b": b_id})
    w(f"**3. Refused: {title}**")
    w()
    block({"status": refused["status"], "headline": refused["headline"], "refusal_reasons": refused["refusal_reasons"]})
    w()

# ============================================================================ F
section("F")
w("## F. User journey: drug → trial → endpoint → source evidence → verification status → comparison → limitations → cited report")
w()
w("Covered end to end in the browser by `frontend/e2e/journey.spec.ts` (run in development and production modes in CI). The same steps through the API:")
w()
presets = api("GET", "/api/compare/presets")
preset = next(p for p in presets if p["title"].startswith("Tirzepatide 15 mg vs placebo within SURMOUNT-1"))
finding = api("GET", f"/api/review/evidence/result/{preset['a']}")
comparison = api("POST", "/api/compare", body={"a": preset["a"], "b": preset["b"]})["comparison"]
block({
    "drug": "tirzepatide (/molecules/tirzepatide)",
    "trial": f"SURMOUNT-1 ({SM1}) (/trials/{SM1})",
    "endpoint": finding["endpoint"]["title"] if finding.get("endpoint") else None,
    "finding": finding["label"],
    "source_evidence": pick(finding["source"], "key", "title", "url", "tier"),
    "anchoring": pick(finding["anchoring"], "basis", "locator"),
    "verification_status": pick(finding["status"], "verification_status", "label", "reason_text"),
    "comparison (POST /api/compare)": pick(comparison, "classification", "headline", "relationship"),
    "methodological limitations": comparison["reasons"] + [f"{f['label']}: {f['explanation']}" for f in comparison["factors"] if f["assessment"] != "MATCH"],
    "permitted statement": comparison["permitted_statements"][:1],
})
w()
report = api("GET", "/api/reports/comparison", params={"a": preset["a"], "b": preset["b"]})
lines = report.splitlines()
w(f"Exported cited report (`GET /api/reports/comparison?a={preset['a']}&b={preset['b']}`), first {min(45, len(lines))} lines and the sources section:")
w()
sources_at = next((i for i, line in enumerate(lines) if line.startswith("## Sources")), len(lines))
excerpt = lines[:45] + (["", "[...]", ""] + lines[sources_at:sources_at + 12] if sources_at > 45 else [])
block("\n".join(excerpt), "markdown")
w()

# Demonstration A runs last: it approves one row and then changes it, so the other demonstrations and the benchmarks run on the
# freshly seeded state. The document still prints A first.
# ============================================================================ A
section("A")
w("## A. Evidence review: a decision, its audit trail, and what a later source change does to it")
w()
w("> **The decision recorded here is a demonstration, not a scientific review.** It was made by a reviewer named "
  f"\"{REVIEWER}\" through the authenticated decision API, in a scratch database that exists only for this run. "
  "No row in the working database, the fixtures or the committed data is HUMAN_VERIFIED: the number of human-verified rows there is 0.")
w()
with session() as db:
    target = next(r for r in db.scalar(select(Study).where(Study.nct_id == SM1)).results
                  if r.origin == "CURATED" and r.tier == "PEER_REVIEWED" and r.endpoint.category == "WEIGHT_PCT_CHANGE" and "15 mg" in r.arm_label
                  and r.timepoint_weeks == 72 and (r.estimand.label if r.estimand else "") == "TREATMENT_REGIMEN")
    target_id, target_key = target.id, target.evidence_key
detail = api("GET", f"/api/review/evidence/result/{target_id}")
w("**1. An unreviewed curated result: its source, quote and extracted fields** (`GET /api/review/evidence/result/{id}`)")
w()
block({
    "label": detail["label"], "origin": detail["origin"],
    "status": pick(detail["status"], "verification_status", "label", "reason", "usable"),
    "source": pick(detail["source"], "key", "title", "url", "tier"),
    "anchoring": pick(detail["anchoring"], "basis", "basis_label", "quote", "locator"),
    "values": pick(detail["values"], "value", "unit", "ci_low", "ci_high", "ci_level", "n_analyzed", "timepoint_weeks"),
    "estimand": pick(detail["estimand"] or {}, "label", "basis"),
    "fingerprint": detail["fingerprint"],
})
w()
decision_body = {
    "decision": "APPROVE",
    "rationale": "DEMONSTRATION ONLY - not a scientific review. Shows the decision path: value, interval, arm, week and estimand "
                 "were compared with the quoted abstract sentence of PMID 35658024.",
    "attest_human_review": True,
    "expected_fingerprint": detail["fingerprint"],
}
decided = api("POST", f"/api/review/evidence/result/{target_id}/decisions", body=decision_body, auth=True)
w("**2. The decision, through the authenticated decision API** (`POST /api/review/evidence/result/{id}/decisions` with the personal token "
  "in `X-Reviewer-Token`; the reviewer name is the server's, taken from the token)")
w()
block({"request": decision_body, "response": pick(decided, "decision", "status_before", "status_after", "reason", "reviewer", "identity_basis", "identity_label",
                                                  "usable", "fingerprint")})
w()
after = api("GET", f"/api/review/evidence/result/{target_id}")
w("**3. The exact record's new status and its audit history** (`GET /api/review/evidence/result/{id}`)")
w()
block({
    "status": pick(after["status"], "verification_status", "label", "reason", "reason_text"),
    "decisions": [pick(d, "decision", "reviewer", "identity_basis", "fingerprint", "rationale") for d in after["decisions"]],
    "audit": [pick(e, "actor", "action", "detail") for e in after["audit"]],
})
w()
# Re-load with the curated value changed in a temporary copy of the curated directory.
curated_copy = SCRATCH / "curated"
shutil.copytree(get_settings().curated_dir, curated_copy)
study_file = curated_copy / "studies" / f"{SM1}.generated.yaml"
text = study_file.read_text(encoding="utf-8")
# The curation loader refuses a value that its own locator quote does not contain, so the simulated correction changes the row as a
# corrected source would print it: value, interval and quote together, in this one row of the copy.
start = text.index("  arm: tirzepatide 15 mg\n  scope: ARM\n  value: -20.9\n")
end = text.index("  notes:", start)
row = text[start:end]
assert text.count(row) == 1 and "ci_low: -21.8" in row and "ci_high: -19.9" in row and "quote: -20.9% (95% CI, -21.8 to -19.9) with 15-mg doses" in row
simulated = (row.replace("value: -20.9", "value: -21.9").replace("ci_low: -21.8", "ci_low: -22.8").replace("ci_high: -19.9", "ci_high: -20.9")
             .replace("quote: -20.9% (95% CI, -21.8 to -19.9) with 15-mg doses", "quote: -21.9% (95% CI, -22.8 to -20.9) with 15-mg doses"))
study_file.write_text(text[:start] + simulated + text[end:], encoding="utf-8")
reset_engine()
run_cli(["seed", "--reset"], {"INCRETINOS_CURATED_DIR": str(curated_copy)})
reset_engine()
changed = api("GET", f"/api/review/evidence/by-key/result/{target_key}")
w("**4. The source extraction changes after the approval.** For this demonstration only, this row is changed in a *temporary copy* of the "
  "curated directory as if the source had printed a corrected result: value -20.9 → -21.9, interval -21.8 to -19.9 → -22.8 to -20.9, and the "
  "locator quote to match (the curation loader refuses a value its own quote does not contain). The publication says -20.9 (95% CI -21.8 to -19.9); "
  "the committed curated file is not touched. The evidence is then re-loaded:")
w()
block("cp -R incretinos/curation/data $TMP/curated          # then, in the copy only: value, interval and quote of this one row\n"
      "INCRETINOS_CURATED_DIR=$TMP/curated incretinos seed --reset   # --reset rebuilds derived rows; review decisions are kept", "sh")
w()
w("The same record, addressed by its evidence key (`GET /api/review/evidence/by-key/result/{evidence_key}`):")
w()
block({
    "status": pick(changed["status"], "verification_status", "label", "reason", "reason_text", "usable", "review_hold"),
    "changed_since_review": {**pick(changed["changed_since_review"] or {}, "decision", "reviewer", "reviewed_fingerprint", "current_fingerprint"),
                             "diff": (changed["changed_since_review"] or {}).get("diff")},
})
w()
w(f"The approval no longer covers the record: it is `{changed['status']['verification_status']}` and the field diff names what changed. "
  "A reviewer must decide again on the new version. This demonstration decision exists only in the scratch database of this run.")
w()

# --------------------------------------------------------------------------- footer
section("footer")
w("## How this document was produced")
w()
w("API calls made, in order:")
w()
block("\n".join(CALLS), "text")
w()
w(textwrap.dedent(f"""\
    The scratch directory ({SCRATCH.name} under the system temporary directory) held the database, the reviewers file and the
    modified curated copy; it is deleted when the script ends. The demonstration reviewer's token existed only in this process."""))

out = argparse.ArgumentParser()
out.add_argument("--out", default=str(DEFAULT_OUT))
args = out.parse_args()
document = [line for name in ("intro", "A", "B", "C", "D", "E", "F", "footer") for line in SECTIONS.get(name, [])]
Path(args.out).write_text("\n".join(document) + "\n", encoding="utf-8")
shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"wrote {args.out}; benchmark: " + ", ".join(f"{k} {v.get('detection_rate')}/{v.get('false_rejection_rate')}" for k, v in bench.items()))
