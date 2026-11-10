"""Evidence Quality: every metric against an independent count, every drill-down against the records it names.

The independent counts are SQLAlchemy Core statements executed on the session's connection: they bypass the ORM
session rule in ``incretinos.db`` (so they state the rejected/held exclusion themselves) and share no code with
``engines/quality.py``. Values are never written into a test: each is counted from the seeded database.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import and_, exists, func, or_, select

from incretinos.engines import quality
from incretinos.models import (
    REVIEWABLE_MODELS,
    AdverseEvent,
    ChangeEvent,
    Drug,
    Endpoint,
    Estimand,
    EvidenceGap,
    EvidenceSource,
    EvidenceVersion,
    LabelRecord,
    OutcomeResult,
    ReviewDecision,
    ReviewQueueItem,
    SourceSnapshot,
    StatisticalAnalysis,
    StudyArm,
)
from incretinos.review.lifecycle import record_decision

STATUSES = ("MACHINE_EXTRACTED", "QUOTE_ANCHORED", "PENDING_HUMAN_REVIEW", "HUMAN_VERIFIED", "REJECTED", "SOURCE_CHANGED_RECHECK_REQUIRED")
REVIEWER = "Dr. Quality Tester"
RATIONALE = "Compared the stored value, arm and endpoint with the cited source table."


def _t(model):
    return model.__table__


def _usable(table):
    """The analytic-use rule, written out independently: not REJECTED and not on review hold; a full-text row (origin
    FULLTEXT, read from a document by a parser) only once a reviewer has approved it (HUMAN_VERIFIED)."""
    rule = and_(table.c.verification_status != "REJECTED", table.c.review_hold.is_(False))
    if "fulltext_extraction_id" in table.c:
        rule = and_(rule, or_(table.c.origin != "FULLTEXT", table.c.verification_status == "HUMAN_VERIFIED"))
    return rule


def _count(db, stmt) -> int:
    return db.connection().execute(stmt).scalar_one()


def _sum_tables(db, where=None, *, analytic: bool) -> int:
    total = 0
    for model in REVIEWABLE_MODELS.values():
        t = _t(model)
        conditions = [*([_usable(t)] if analytic else []), *([where(t)] if where is not None else [])]
        total += _count(db, select(func.count()).select_from(t).where(*conditions))
    return total


def independent_counts(db, as_of: datetime) -> dict[str, tuple[int, int]]:
    """(count, denominator) for every metric that takes no value, by Core statements only."""
    r, a, ae = _t(OutcomeResult), _t(StatisticalAnalysis), _t(AdverseEvent)
    est, ep, arm = _t(Estimand), _t(Endpoint), _t(StudyArm)
    c = lambda stmt: _count(db, stmt)  # noqa: E731
    results = c(select(func.count()).select_from(r).where(_usable(r)))
    analyses = c(select(func.count()).select_from(a).where(_usable(a)))
    aes = c(select(func.count()).select_from(ae).where(_usable(ae)))
    everything = _sum_tables(db, analytic=False)
    analytic = _sum_tables(db, analytic=True)
    out: dict[str, tuple[int, int]] = {"review_rows": (everything, everything)}
    for status in STATUSES:
        out[f"status_{status.lower()}"] = (_sum_tables(db, lambda t, s=status: t.c.verification_status == s, analytic=False), everything)
    out["status_other"] = (_sum_tables(db, lambda t: t.c.verification_status.not_in(STATUSES), analytic=False), everything)
    out["review_hold"] = (_sum_tables(db, lambda t: t.c.review_hold.is_(True), analytic=False), everything)

    def est_count(table, condition):
        return c(select(func.count()).select_from(table.outerjoin(est, table.c.estimand_id == est.c.id)).where(_usable(table), condition))

    not_stated = or_(est.c.id.is_(None), est.c.label == "NOT_STATED")
    named = and_(est.c.id.is_not(None), est.c.label != "NOT_STATED")
    blank = lambda col: or_(col.is_(None), func.trim(col) == "")  # noqa: E731
    out.update({
        "result_no_ci": (c(select(func.count()).select_from(r).where(_usable(r), or_(r.c.ci_low.is_(None), r.c.ci_high.is_(None)))), results),
        "result_no_n": (c(select(func.count()).select_from(r).where(_usable(r), r.c.n_analyzed.is_(None))), results),
        "result_no_unit": (c(select(func.count()).select_from(r).where(_usable(r), blank(r.c.unit))), results),
        "result_estimand_not_stated": (est_count(r, not_stated), results),
        "result_estimand_inferred": (est_count(r, and_(named, est.c.basis.in_(["INFERRED_FROM_ANALYSIS_DESCRIPTION", "INFERRED_BY_CURATOR"]))), results),
        "result_estimand_stated": (est_count(r, and_(named, est.c.basis == "STATED_BY_SOURCE")), results),
        "analysis_no_method": (c(select(func.count()).select_from(a).where(_usable(a), blank(a.c.method))), analyses),
        "analysis_multiplicity_not_stated": (c(select(func.count()).select_from(a).where(_usable(a), a.c.multiplicity_controlled == "NOT_STATED")), analyses),
        "analysis_no_method_or_multiplicity": (c(select(func.count()).select_from(a).where(_usable(a), or_(blank(a.c.method), a.c.multiplicity_controlled == "NOT_STATED"))), analyses),
        "analysis_estimand_not_stated": (est_count(a, not_stated), analyses),
        "ae_no_denominator": (c(select(func.count()).select_from(ae).where(_usable(ae), ae.c.n_at_risk.is_(None))), aes),
        "result_with_interval": (c(select(func.count()).select_from(r).where(_usable(r), r.c.ci_low.is_not(None), r.c.ci_high.is_not(None))), results),
        "analysis_with_interval": (c(select(func.count()).select_from(a).where(_usable(a), a.c.ci_low.is_not(None), a.c.ci_high.is_not(None))), analyses),
        "authority_rows": (analytic, analytic),
    })
    for level in ("HIGH", "MEDIUM", "LOW"):
        out[f"risk_{level.lower()}"] = (_sum_tables(db, lambda t, lv=level: t.c.risk_level == lv, analytic=True), analytic)
    out["risk_none"] = (_sum_tables(db, lambda t: or_(t.c.risk_level.is_(None), t.c.risk_level == "NONE"), analytic=True), analytic)

    # Press-release-only: a press-release result for a trial and endpoint category no other tier reports.
    r2, ep2 = r.alias("other"), ep.alias("other_endpoint")
    other_tier = exists(select(r2.c.id).select_from(r2.join(ep2, r2.c.endpoint_id == ep2.c.id))
                        .where(r2.c.study_id == r.c.study_id, ep2.c.category == ep.c.category, r2.c.tier != "PRESS_RELEASE", _usable(r2)))
    out["press_release_only"] = (c(select(func.count()).select_from(r.join(ep, r.c.endpoint_id == ep.c.id))
                                   .where(_usable(r), r.c.tier == "PRESS_RELEASE", ~other_tier)), results)

    # Molecules: trial adverse-event rows with denominators on the molecule's own arms; label safety sections.
    drugs = list(db.connection().execute(select(_t(Drug).c.id, _t(Drug).c.slug)))
    with_ae = {slug for (slug,) in db.connection().execute(
        select(arm.c.drug_slug).select_from(ae.join(arm, ae.c.arm_id == arm.c.id))
        .where(_usable(ae), ae.c.measure_kind == "PARTICIPANTS_WITH_EVENT", ae.c.n_at_risk.is_not(None)).distinct())}
    with_label = set()
    for drug_id, sections in db.connection().execute(select(_t(LabelRecord).c.drug_id, _t(LabelRecord).c.sections)):
        sections = json.loads(sections) if isinstance(sections, str) else sections
        if drug_id and any(str(sections.get(k) or "").strip() for k in ("boxed_warning", "warnings_and_cautions", "adverse_reactions")):
            with_label.add(drug_id)
    out["drug_no_trial_ae_denominators"] = (sum(1 for _, slug in drugs if slug not in with_ae), len(drugs))
    out["drug_no_label_safety"] = (sum(1 for i, _ in drugs if i not in with_label), len(drugs))
    out["drug_incomplete_safety"] = (sum(1 for i, slug in drugs if slug not in with_ae or i not in with_label), len(drugs))

    sources = c(select(func.count()).select_from(_t(EvidenceSource)))
    out["authority_sources"] = (sources, sources)
    snap, ce = _t(SourceSnapshot), _t(ChangeEvent)
    hashes = dict(db.connection().execute(select(snap.c.source_id, func.count(func.distinct(snap.c.content_hash))).group_by(snap.c.source_id)).all())
    changed_by_event = {sid for (sid,) in db.connection().execute(select(ce.c.source_id).distinct())}
    source_rows = list(db.connection().execute(select(_t(EvidenceSource).c.id, _t(EvidenceSource).c.source_type, _t(EvidenceSource).c.retrieved_at)))
    out["sources_changed"] = (sum(1 for sid, _, _ in source_rows if hashes.get(sid, 0) > 1 or sid in changed_by_event), sources)
    due = 0
    for _, stype, retrieved in source_rows:
        if retrieved is None:
            due += 1
            continue
        retrieved = retrieved if retrieved.tzinfo else retrieved.replace(tzinfo=UTC)
        if (as_of - retrieved).total_seconds() / 86400 > quality.DEFAULT_REFRESH_DAYS.get(stype, quality.FALLBACK_REFRESH_DAYS):
            due += 1
    out["sources_refresh_due"] = (due, sources)

    changes = c(select(func.count()).select_from(ce))
    out["change_events_open"] = (c(select(func.count()).select_from(ce).where(ce.c.review_status == "OPEN")), changes)
    q = _t(ReviewQueueItem)
    queue = c(select(func.count()).select_from(q))
    out["queue_open"] = (c(select(func.count()).select_from(q).where(q.c.status == "OPEN")), queue)
    out["queue_in_review"] = (c(select(func.count()).select_from(q).where(q.c.status == "IN_REVIEW")), queue)
    conflicts = c(select(func.count()).select_from(_t(EvidenceGap)).where(_t(EvidenceGap).c.gap_nature == "SOURCE_CONFLICT"))
    out["source_conflicts"] = (conflicts, conflicts)
    return out


AS_OF = datetime(2026, 11, 9, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def facts(seeded):
    from incretinos.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield quality.collect(session, as_of=AS_OF)
    finally:
        session.close()


def _overview_metrics(facts) -> dict[str, dict]:
    return {m["id"]: m for m in quality.overview(facts)["metrics"]}


# --------------------------------------------------------------------------- every metric equals an independent count


def test_every_metric_equals_an_independent_count_on_the_seed(db, facts):
    expected = independent_counts(db, AS_OF)
    metrics = _overview_metrics(facts)
    decision_free = {"open_requests", "orphaned_decisions"}
    assert set(metrics) == set(expected) | decision_free, sorted(set(metrics) ^ (set(expected) | decision_free))
    for metric_id, (count, denominator) in expected.items():
        assert (metrics[metric_id]["count"], metrics[metric_id]["denominator"]) == (count, denominator), metric_id
    # Open requests and orphaned decisions are derived from the decision history by the review module; they are checked
    # against its own list and summary in test_every_metric_links_to_exactly_the_records_it_counted. With no decision
    # recorded (a fresh seed; earlier test modules may record some) neither can exist.
    if _count(db, select(func.count()).select_from(_t(ReviewDecision))) == 0:
        assert metrics["open_requests"]["count"] == 0 and metrics["orphaned_decisions"]["count"] == 0
    # The seed is not a trivial case for the metrics that matter most.
    for metric_id in ("result_no_ci", "result_no_n", "result_estimand_inferred", "analysis_no_method", "ae_no_denominator", "press_release_only",
                      "risk_high", "drug_incomplete_safety", "source_conflicts", "result_with_interval", "status_pending_human_review"):
        assert metrics[metric_id]["count"] > 0, metric_id


def test_review_status_adds_up_and_matches_the_review_index(db, facts):
    metrics = _overview_metrics(facts)
    statuses = sum(metrics[f"status_{s.lower()}"]["count"] for s in STATUSES) + metrics["status_other"]["count"]
    assert statuses == metrics["review_rows"]["count"]
    current = _count(db, select(func.count()).select_from(_t(EvidenceVersion)).where(_t(EvidenceVersion).c.is_current.is_(True)))
    assert current == metrics["review_rows"]["count"]
    by_type = metrics["review_rows"]["by_type"]
    for etype, model in REVIEWABLE_MODELS.items():
        assert by_type[etype] == [_count(db, select(func.count()).select_from(_t(model)))] * 2, etype


def test_flag_codes_are_counted_once_per_record(db, facts):
    seen: Counter[str] = Counter()
    for model in REVIEWABLE_MODELS.values():
        t = _t(model)
        for (flags,) in db.connection().execute(select(t.c.check_flags).where(_usable(t))):
            flags = json.loads(flags) if isinstance(flags, str) else flags
            seen.update({f["code"] for f in flags or []})
    distribution = {d["value"]: d["count"] for d in quality.overview(facts)["distributions"]["flag_codes"]}
    assert {k: v for k, v in distribution.items() if v} == dict(seen) and sum(seen.values()) > 0


def test_interval_width_quartiles_match_numpy(facts):
    widths = quality.overview(facts)["interval_widths"]
    assert widths["groups"], "no comparable interval group in the seed"
    for group in widths["groups"]:
        listed = quality.records(facts, "interval_width_group", value=group["key"], limit=500)
        assert listed["total"] == group["n"] >= quality.MIN_WIDTH_GROUP
        values = [row for row in facts.universes[quality.EVIDENCE] if quality.width_group_key(row) == group["key"]]
        w = np.array([float(i.f["ci_high"]) - float(i.f["ci_low"]) for i in values if float(i.f["ci_high"]) >= float(i.f["ci_low"])])
        assert len(w) == group["n"]
        assert group["min"] == pytest.approx(w.min()) and group["max"] == pytest.approx(w.max())
        assert [group["p25"], group["median"], group["p75"]] == pytest.approx(list(np.percentile(w, [25, 50, 75])))
    assert "Descriptive" in widths["note"]


def test_breakdown_cells_equal_independent_group_counts(db, facts):
    r, ev = _t(OutcomeResult), _t(EvidenceVersion)
    joined = r.join(ev, and_(ev.c.evidence_key == r.c.evidence_key, ev.c.evidence_type == "result", ev.c.is_current.is_(True)))
    by_drug = {g["value"]: g["counts"] for g in quality.breakdown(facts, "drug")["groups"]}
    assert by_drug, "no molecule groups"
    for slug, counts in by_drug.items():
        no_ci = _count(db, select(func.count()).select_from(joined).where(_usable(r), ev.c.drug_slugs.like(f"%|{slug}|%"), or_(r.c.ci_low.is_(None), r.c.ci_high.is_(None))))
        all_results = _count(db, select(func.count()).select_from(joined).where(_usable(r), ev.c.drug_slugs.like(f"%|{slug}|%")))
        assert counts["result_no_ci"] == [no_ci, all_results], slug
    by_tier = {g["value"]: g["counts"] for g in quality.breakdown(facts, "tier")["groups"]}
    for tier, counts in by_tier.items():
        expected = _sum_tables(db, lambda t, tr=tier: t.c.evidence_key.in_(select(ev.c.evidence_key).where(ev.c.is_current.is_(True), ev.c.tier == tr)), analytic=False)
        assert counts["review_rows"] == [expected, expected], tier
    breakdown = quality.breakdown(facts, "study")
    assert "drug_incomplete_safety" in breakdown["not_applicable"] and "orphaned_decisions" in breakdown["not_applicable"]


# --------------------------------------------------------------------------- drill-downs return exactly the counted records


def _page_total(client, link) -> int:
    api = link["api"]
    params = {**api["params"], "limit": 1} if api["total_field"] == "total" else api["params"]
    response = client.get(api["path"], params=params)
    assert response.status_code == 200, (api, response.text[:200])
    return response.json()[api["total_field"]]


def test_every_metric_links_to_exactly_the_records_it_counted(client, facts):
    overview = quality.overview(facts)
    for metric in overview["metrics"]:
        listed = quality.records(facts, metric["id"], limit=500)
        assert listed["total"] == metric["count"] == listed["metric"]["count"], metric["id"]
        assert metric["links"]["records"]["api"]["params"] == {"metric": metric["id"]}
        if metric["links"]["page"]:
            assert _page_total(client, metric["links"]["page"]) == metric["count"], (metric["id"], metric["links"]["page"])
    pages = {m["id"] for m in overview["metrics"] if m["links"]["page"]}
    assert {"status_pending_human_review", "status_rejected", "open_requests", "source_conflicts", "queue_open", "orphaned_decisions"} <= pages
    # Parametrised metrics: every listed value drills to its own count.
    for d in overview["distributions"]["flag_codes"] + overview["distributions"]["retrieval_age"]:
        assert quality.records(facts, d["links"]["records"]["api"]["params"]["metric"], value=d["value"], limit=1)["total"] == d["count"]
    for name in ("records_by_tier", "records_by_source_type", "sources_by_tier", "sources_by_source_type"):
        rows = overview["distributions"][name]
        assert sum(d["count"] for d in rows) == (overview_metric := {m["id"]: m for m in overview["metrics"]}["authority_rows" if name.startswith("records") else "authority_sources"])["count"]
        for d in rows:
            params = d["links"]["records"]["api"]["params"]
            filters = {k: v for k, v in params.items() if k != "metric"}
            assert quality.records(facts, params["metric"], filters=filters, limit=1)["total"] == d["count"], (name, d["value"], overview_metric["id"])


def test_the_records_route_returns_the_records_of_the_count(client, facts):
    metrics = _overview_metrics(facts)
    body = client.get("/api/quality/records", params={"metric": "result_no_ci", "drug": "tirzepatide", "limit": 500}).json()
    cell = {g["value"]: g["counts"] for g in quality.breakdown(facts, "drug")["groups"]}["tirzepatide"]["result_no_ci"]
    assert body["total"] == cell[0] == len(body["rows"]) and body["denominator"] == cell[1]
    assert all(r["evidence_type"] == "result" and "tirzepatide" in r["drugs"] for r in body["rows"])
    assert all(any(link["href"].startswith("/review?type=result&key=") for link in r["links"]) for r in body["rows"])
    # Paging: stable, complete, true total.
    total = metrics["press_release_only"]["count"]
    seen = []
    for offset in range(0, total, 7):
        page = client.get("/api/quality/records", params={"metric": "press_release_only", "limit": 7, "offset": offset}).json()
        assert page["total"] == total and page["offset"] == offset
        seen += [r["key"] for r in page["rows"]]
    assert len(seen) == len(set(seen)) == total
    assert client.get("/api/quality/records", params={"metric": "press_release_only", "limit": 3, "offset": total}).json()["rows"] == []


def test_review_status_drill_down_matches_the_review_list_within_each_group(client, facts):
    for by in ("drug", "tier", "source_type"):
        for group in quality.breakdown(facts, by)["groups"]:
            for metric_id in ("review_rows", "status_pending_human_review", "status_quote_anchored"):
                link = quality.links(quality.METRICS[metric_id], None, {by: group["value"]})["page"]
                assert link is not None
                assert _page_total(client, link) == group["counts"][metric_id][0], (by, group["value"], metric_id)


def test_breakdown_cells_match_their_records_for_every_group(facts):
    for by in quality.BREAKDOWNS:
        result = quality.breakdown(facts, by)
        assert result["groups"], by
        for group in result["groups"]:
            for metric_id in ("result_no_ci", "ae_no_denominator", "risk_high", "authority_rows"):
                if metric_id not in result["metrics"]:
                    continue
                listed = quality.records(facts, metric_id, filters={by: group["value"]}, limit=1)
                assert [listed["total"], listed["denominator"]] == group["counts"][metric_id], (by, group["value"], metric_id)


def test_source_conflict_drill_down_matches_the_gap_tracker_per_molecule(client, facts):
    groups = {g["value"]: g["counts"]["source_conflicts"] for g in quality.breakdown(facts, "drug")["groups"]}
    assert any(count for count, _ in groups.values())
    for slug, (count, _) in groups.items():
        if count:
            link = quality.links(quality.METRICS["source_conflicts"], None, {"drug": slug})
            assert link["href"].startswith("/gaps?nature=SOURCE_CONFLICT") and _page_total(client, link["page"]) == count, slug
    # A filter the gap tracker cannot apply never produces a page link that would show a different set.
    assert quality.links(quality.METRICS["source_conflicts"], None, {"study": "NCT04184622"})["page"] is None


# --------------------------------------------------------------------------- rejected rows: counted in review status, not in completeness


def test_a_rejected_row_counts_in_review_status_and_nowhere_else(db):
    before = quality.collect(db, as_of=AS_OF)
    target = db.scalar(select(OutcomeResult).where(OutcomeResult.ci_low.is_(None), OutcomeResult.verification_status == "MACHINE_EXTRACTED",
                                                   OutcomeResult.risk_level == "NONE").order_by(OutcomeResult.id).limit(1))
    assert target is not None
    drug = next(iter(next(i for i in before.universes[quality.EVIDENCE] if i.evidence_type == "result" and i.f["id"] == target.id).groups["drug"]))
    record_decision(db, evidence_type="result", row_id=target.id, decision="REJECT", reviewer=REVIEWER, rationale=RATIONALE, attested=True,
                    expected_fingerprint=target.fingerprint)
    db.flush()
    after = quality.collect(db, as_of=AS_OF)
    m0, m1 = _overview_metrics(before), _overview_metrics(after)
    # Review status: counted, the same total, moved from machine-extracted to rejected.
    assert m1["review_rows"]["count"] == m0["review_rows"]["count"]
    assert m1["status_rejected"]["count"] == m0["status_rejected"]["count"] + 1
    assert m1["status_machine_extracted"]["count"] == m0["status_machine_extracted"]["count"] - 1
    rejected = quality.records(after, "status_rejected", limit=500)["rows"]
    assert [r["id"] for r in rejected if r["evidence_type"] == "result"] == [target.id]
    # Completeness, precision, authority, reliability: gone from count and denominator alike.
    assert (m1["result_no_ci"]["count"], m1["result_no_ci"]["denominator"]) == (m0["result_no_ci"]["count"] - 1, m0["result_no_ci"]["denominator"] - 1)
    assert m1["result_with_interval"]["denominator"] == m0["result_with_interval"]["denominator"] - 1
    assert m1["authority_rows"]["count"] == m0["authority_rows"]["count"] - 1
    assert m1["risk_none"]["count"] == m0["risk_none"]["count"] - 1
    assert target.id not in {r["id"] for r in quality.records(after, "result_no_ci", limit=5000)["rows"]}
    # The same holds within the molecule's breakdown cell.
    cell0 = {g["value"]: g["counts"] for g in quality.breakdown(before, "drug")["groups"]}[drug]
    cell1 = {g["value"]: g["counts"] for g in quality.breakdown(after, "drug")["groups"]}[drug]
    assert cell1["status_rejected"] == [cell0["status_rejected"][0] + 1, cell0["status_rejected"][1]]
    assert cell1["result_no_ci"] == [cell0["result_no_ci"][0] - 1, cell0["result_no_ci"][1] - 1]
    # And the independent count agrees with both.
    expected = independent_counts(db, AS_OF)
    assert (m1["status_rejected"]["count"], m1["result_no_ci"]["count"]) == (expected["status_rejected"][0], expected["result_no_ci"][0])
    # Nothing on this page became human-verified by code.
    assert m1["status_human_verified"]["count"] == m0["status_human_verified"]["count"]


# --------------------------------------------------------------------------- freshness rule


def test_the_refresh_rule_is_stated_configurable_and_applied(db):
    newest = max(i.f["retrieved_at"] for i in quality.collect(db, as_of=AS_OF).universes[quality.SOURCES] if i.f["retrieved_at"])
    later = newest + timedelta(days=45)
    facts = quality.collect(db, as_of=later)
    due = {i.f["source_key"] for i in facts.universes[quality.SOURCES] if i.f["refresh_due"]}
    expected = {i.f["source_key"] for i in facts.universes[quality.SOURCES]
                if i.f["retrieved_at"] is None or (later - i.f["retrieved_at"]).days >= 0 and (later - i.f["retrieved_at"]).total_seconds() / 86400 > quality.DEFAULT_REFRESH_DAYS.get(i.f["source_type"], 180)}
    assert due == expected and any(k.startswith("ctgov:") for k in due)
    assert _overview_metrics(facts)["sources_refresh_due"]["count"] == len(due) == independent_counts(db, later)["sources_refresh_due"][0]
    relaxed = quality.collect(db, as_of=later, refresh_days=quality.parse_refresh_rule("CTGOV=400, WEB=400, GUIDANCE=4000"))
    assert not any(i.f["refresh_due"] and i.f["source_type"] == "CTGOV" for i in relaxed.universes[quality.SOURCES])
    rule = quality.overview(relaxed)["refresh_rule"]
    assert {d["source_type"]: d["days"] for d in rule["days"]}["CTGOV"] == 400 and rule["setting"] == "INCRETINOS_QUALITY_REFRESH_DAYS"
    for bad in ("CTGOV", "CTGOV=0", "ctgov=5", "CTGOV=five"):
        with pytest.raises(ValueError):
            quality.parse_refresh_rule(bad)


def test_the_refresh_setting_is_validated_at_start_up():
    from incretinos.config import Settings

    assert Settings(_env_file=None, quality_refresh_days="CTGOV=14,PUBMED=730").quality_refresh_days == "CTGOV=14,PUBMED=730"
    with pytest.raises(ValueError):
        Settings(_env_file=None, quality_refresh_days="CTGOV=fourteen")


# --------------------------------------------------------------------------- assessment and API contract


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_assessment_shows_each_dimension_separately_with_rules_and_counts_and_no_score(client, facts):
    for params, dim, value in (({"drug": "tirzepatide"}, "drug", "tirzepatide"), ({"study": "NCT04184622"}, "study", "NCT04184622")):
        body = client.get("/api/quality/assessment", params=params).json()
        assert [s["id"] for s in body["sections"]] == list(quality.ASSESSMENT_SECTIONS)
        assert not {k for k in _keys(body) if "score" in k.lower() or "grade" in k.lower() or "overall" in k.lower()}
        assert any("does not mean the drug works" in n for n in body["notes"])
        for section in body["sections"]:
            assert section["caveat"] and section["question"]
            for metric in section["metrics"]:
                assert metric["rule"] and metric["denominator"] >= metric["count"] >= 0
                assert quality.records(facts, metric["id"], filters={dim: value}, limit=1)["total"] == metric["count"], (dim, metric["id"])
    assert client.get("/api/quality/assessment").status_code == 422
    assert client.get("/api/quality/assessment", params={"drug": "tirzepatide", "study": "NCT04184622"}).status_code == 422
    assert client.get("/api/quality/assessment", params={"drug": "no-such-molecule"}).status_code == 404


def test_overview_states_what_completeness_does_not_mean(client):
    body = client.get("/api/quality", params={"by": "endpoint_category"}).json()
    assert any("does not mean the drug works" in n for n in body["notes"])
    assert "does not mean the drug works" in {d["id"]: d for d in body["dimensions"]}["completeness"]["caveat"]
    assert body["breakdown"]["by"] == "endpoint_category" and body["breakdown"]["groups"]
    assert {m["id"]: m for m in body["metrics"]}["status_rejected"]["includes_excluded_rows"] is True
    assert {m["id"]: m for m in body["metrics"]}["result_no_ci"]["includes_excluded_rows"] is False
    assert client.get("/api/quality", params={"by": "colour"}).status_code == 422


@pytest.mark.parametrize("params", [
    {"metric": "bogus"}, {"metric": "flag_code"}, {"metric": "result_no_ci", "value": "x"}, {"metric": "source_conflicts", "tier": "PEER_REVIEWED"},
    {"metric": "drug_incomplete_safety", "study": "NCT04184622"}, {"metric": "result_no_ci", "study": "NCT1"}, {"metric": "result_no_ci", "limit": 0},
    {"metric": "result_no_ci", "offset": -1}, {"metric": "result_no_ci", "drug": "x" * 61}, {},
])
def test_records_refuses_a_query_that_does_not_apply(client, params):
    assert client.get("/api/quality/records", params=params).status_code == 422


def test_quality_reads_are_budgeted_and_never_write(client, db):
    from incretinos.api import limits

    assert {("GET", "/api/quality"), ("GET", "/api/quality/records"), ("GET", "/api/quality/assessment")} <= limits.QUERY_BUDGET_ROUTES
    before = _sum_tables(db, analytic=False), _count(db, select(func.count()).select_from(_t(ReviewDecision)))
    assert client.get("/api/quality").status_code == 200
    assert (_sum_tables(db, analytic=False), _count(db, select(func.count()).select_from(_t(ReviewDecision)))) == before


def test_records_list_what_each_row_is_and_where_to_open_it(facts):
    seen_kinds = set()
    for metric_id in ("drug_incomplete_safety", "sources_changed", "change_events_open", "queue_open", "queue_in_review", "source_conflicts", "risk_high"):
        listed = quality.records(facts, metric_id, limit=5)
        for row in listed["rows"]:
            seen_kinds.add(row["kind"])
            assert row["links"] and all(link["href"].startswith("/") for link in row["links"]), (metric_id, row)
            assert row["note"], (metric_id, row)
    assert {"drug", "source", "conflict", "evidence"} <= seen_kinds
    # Every countable thing, in every universe, says where it can be opened.
    for universe, items in facts.universes.items():
        assert all(item.links and all(link["href"].startswith("/") for link in item.links) for item in items), universe
    # A synthetic demonstration change is labelled as one wherever it is listed.
    changes = facts.universes[quality.CHANGES]
    assert changes, "the seed records a source change"
    note = quality.METRICS["change_events_open"].note
    assert all("synthetic" in note(item, None) for item in changes if item.f["is_synthetic"])
