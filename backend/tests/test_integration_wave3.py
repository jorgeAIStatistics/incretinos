"""Guarantees that exist only because the last Session 2 branches were merged (third integration wave).

Each test holds two or more of the wave's branches together: full-text evidence (FULLTEXT rows pending review) with the
review index, the quality dimensions, the conflict engine and the indirect-comparison engine; the conflict engine with the
quality page and the indirect engine; and the continuous-integration workflow with the browser specs the branches added.
Tests that belong to one branch alone stay in that branch's own test module.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select

from incretinos.api import limits
from incretinos.engines import conflicts, indirect, quality
from incretinos.models import REVIEWABLE_MODELS, StatisticalAnalysis

from .conftest import analysis_id

REPO = Path(__file__).resolve().parents[2]


def _fulltext_keys(db) -> set[tuple[str, str]]:
    return {(etype, key) for etype, model in REVIEWABLE_MODELS.items() if hasattr(model, "fulltext_extraction_id")
            for (key,) in db.execute(select(model.evidence_key).execution_options(include_rejected=True).where(model.origin == "FULLTEXT"))}


# --------------------------------------------------------------------------- full text x review x quality x conflicts x indirect


def test_fulltext_rows_are_reviewable_and_counted_but_enter_no_analytic_surface(client, db):
    keys = _fulltext_keys(db)
    assert keys, "the seed carries FULLTEXT rows"
    # Evidence Review lists every one of them, each PENDING_HUMAN_REVIEW (a parser's reading, never machine-verified).
    listed = client.get("/api/review/evidence", params={"origin": "FULLTEXT", "limit": 500}).json()
    assert listed["total"] == len(keys)
    assert {(r["evidence_type"], r["evidence_key"]) for r in listed["rows"]} == keys
    assert {r["verification_status"] for r in listed["rows"]} == {"PENDING_HUMAN_REVIEW"}
    # Evidence Quality counts them in the review-status dimension only.
    facts = quality.collect(db)
    assert keys <= {(i.evidence_type, i.f.get("evidence_key")) for i in facts.universes[quality.ALL_EVIDENCE]}
    assert not keys & {(i.evidence_type, i.f.get("evidence_key")) for i in facts.universes[quality.EVIDENCE]}
    # The conflict engine and the indirect engine never read them.
    assert not {key for _, key in keys} & {r.evidence_key for g in conflicts.all_groups(db) for r in g.rows}
    analyses = {key for etype, key in keys if etype == "analysis"}
    assert analyses and not analyses & {r.evidence_key for r in indirect._contrast_rows(db, None)}


# --------------------------------------------------------------------------- conflicts x quality x indirect x review dossier


def test_quality_indirect_and_the_review_dossier_read_one_conflict_classification(client, db):
    sm1_15 = analysis_id(db, "NCT04184622", "15 mg Tirzepatide")
    dossier = client.get(f"/api/review/evidence/analysis/{sm1_15}").json()["source_conflict"]
    side = indirect.describe_contrast(db, db.get(StatisticalAnalysis, sm1_15))["source_conflict"]
    assert dossier["group_id"] == side["group_id"] and dossier["group_class"] == side["group_class"] == "INSUFFICIENT_INFORMATION"
    facts = quality.collect(db)
    item = next(i for i in facts.universes[quality.CONFLICTS] if i.f["group_id"] == side["group_id"])
    assert item.f["class"] == side["group_class"] and item.links[0]["href"] == f"/conflicts?group={side['group_id']}"
    # The quality page's class counts are the conflicts page's own counts.
    counts = client.get("/api/conflicts", params={"limit": 1}).json()["counts"]
    metrics = {m["id"]: m["count"] for m in quality.overview(facts)["metrics"]}
    assert (metrics["source_conflicts"], metrics["source_conflicts_insufficient"], metrics["source_conflicts_explainable"],
            metrics["source_conflicts_consistent"]) == (counts["TRUE_CONFLICT"], counts["INSUFFICIENT_INFORMATION"], counts["EXPLAINABLE_DIFFERENCE"],
                                                       counts["CONSISTENT"])


# --------------------------------------------------------------------------- routes x limits; browser specs x CI


def test_the_wave_routes_share_the_query_budget_and_take_no_unsized_body():
    assert {("GET", "/api/conflicts"), ("GET", "/api/quality"), ("GET", "/api/quality/records"), ("GET", "/api/quality/assessment"),
            ("GET", "/api/indirect/feasible")} <= limits.QUERY_BUDGET_ROUTES
    assert any(m == "POST" and p.pattern == r"/api/indirect/assess" for m, p, _ in limits.ROUTE_BODY_LIMITS)


def test_ci_runs_every_browser_spec_in_both_modes():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    specs = sorted(p.name for p in (REPO / "frontend" / "e2e").glob("*.spec.ts"))
    runs = [sorted(re.findall(r"e2e/([\w.-]+\.spec\.ts)", line)) for line in workflow.splitlines() if "npx playwright test" in line]
    assert len(runs) == 2 and "E2E_WEB: production" in workflow
    assert runs[0] == runs[1] == specs, (runs, specs)


def test_evidence_review_says_an_unreviewed_fulltext_row_is_out_of_analytic_use(client, db):
    """The review list and dossier state the quarantine the session rule applies: not usable, and why."""
    listed = client.get("/api/review/evidence", params={"origin": "FULLTEXT", "limit": 5}).json()["rows"]
    assert listed and all(r["usable"] is False and "until a reviewer approves it" in r["exclusion_reason"] for r in listed)
    detail = client.get(f"/api/review/evidence/{listed[0]['evidence_type']}/{listed[0]['id']}").json()
    assert detail["status"]["usable"] is False and "until a reviewer approves it" in detail["status"]["exclusion_reason"]
    # A registry row pending nothing stays usable: the rule is about full-text origin only.
    registry = client.get("/api/review/evidence", params={"origin": "REGISTRY", "limit": 1}).json()["rows"][0]
    assert registry["usable"] is True and registry["exclusion_reason"] is None
