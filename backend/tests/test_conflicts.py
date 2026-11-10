"""Source conflict engine: grouping, classification and its regression examples on the seeded database.

The examples are the ones the audit named: ATTAIN-1 36 mg (registry and press release under the efficacy estimand,
NEJM and FDA label under the treatment-regimen estimand, the label naming the bioequivalent 17.2 mg tablet dosage),
SURMOUNT-1 (registry efficacy against NEJM treatment-regimen) and SURMOUNT-4 (a publication that does not name its
estimand against a press release that does). Rows are located by what they are, never by id.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import permutations

import pytest
from sqlalchemy import select

from incretinos.engines import conflicts as C
from incretinos.engines.findings import endpoint_definition, resolve_arm
from incretinos.engines.gaps import detect_gaps
from incretinos.enums import EvidenceTier
from incretinos.models import EvidenceGap, EvidenceSource, OutcomeResult, Study, StudyArm
from incretinos.review.lifecycle import record_decision
from incretinos.vocab import norm_arm_label

ATTAIN_1, SURMOUNT_1, SURMOUNT_4 = "NCT05869903", "NCT04184622", "NCT04660643"
REVIEWER = "Dr. Test Reviewer"
REJECT_WHY = "The stored value is not the one printed for this group in the cited label table."


def _study(db, nct: str) -> Study:
    study = db.scalar(select(Study).where(Study.nct_id == nct))
    assert study is not None, f"{nct} is not seeded"
    return study


def _group(db, nct: str, *, arm: str, category: str = "WEIGHT_PCT_CHANGE", weeks: float | None = 72, evidence_type: str = "result") -> C.ConflictGroup:
    found = [g for g in C.study_groups(db, _study(db, nct)) if g.evidence_type == evidence_type and g.category == category and g.rows[0].arm_group == arm
             and g.rows[0].timepoint_weeks == weeks and not (g.rows[0].definition and g.rows[0].definition.threshold)]
    assert len(found) == 1, [(g.group_id, g.rows[0].arm_group, g.rows[0].timepoint_weeks) for g in found]
    return found[0]


def _row(group: C.ConflictGroup, *, tier: str, value: str | None = None) -> C.Row:
    rows = [r for r in group.rows if r.tier == tier and (value is None or r.printed == value)]
    assert len(rows) == 1, [(r.tier, r.printed) for r in group.rows]
    return rows[0]


def _pair(group: C.ConflictGroup, a: C.Row, b: C.Row) -> dict:
    return next(c for c in group.comparisons if {c["a"], c["b"]} == {a.ref, b.ref})


def _dim(comparison: dict, name: str) -> dict:
    return next(d for d in comparison["dimensions"] if d["dimension"] == name)


# --------------------------------------------------------------------------- reporting precision


def test_precision_comes_from_the_reported_text_not_the_float():
    assert C.reported_number(-5.5, "-5.50") == (Decimal("-5.50"), 2, "REPORTED_TEXT")  # a printed trailing zero is kept
    assert C.reported_number(-12.4, "−12.4") == (Decimal("-12.4"), 1, "REPORTED_TEXT")
    # No text of its own: the one way the number is printed in the locator quote ("21.0%", Lancet's "62·1%").
    assert C.reported_number(21.0, None, "diarrhea (21.0%, 22.8% and 23.1%) vs. 9.6% with placebo") == (Decimal("21.0"), 1, "LOCATOR_QUOTE")
    assert C.reported_number(62.1, None, "851 (62·1%) of 1371 orforglipron participants")[1:] == (1, "LOCATOR_QUOTE")
    # Neither: the stored number, and the basis says so.
    assert C.reported_number(14.0, None, None)[2] == "STORED_NUMBER"


def test_values_agree_only_when_the_finer_one_rounds_to_the_coarser_one():
    d = Decimal
    assert C.agree_at_precision(d("-12.35"), 2, d("-12.4"), 1)  # a tie rounds either way
    assert C.agree_at_precision(d("-9.27"), 2, d("-9.3"), 1)
    assert not C.agree_at_precision(d("-12.34"), 2, d("-12.4"), 1)
    # The same number of decimals: equal or not the same number. -11.2 and -11.1 are not rounding variants of each other.
    assert not C.agree_at_precision(d("-11.2"), 1, d("-11.1"), 1)
    assert C.agree_at_precision(d("-5.50"), 2, d("-5.5"), 1) and not C.agree_at_precision(d("-5.50"), 2, d("-5.6"), 1)
    assert C.agree_at_precision(d("6"), 0, d("5.9"), 1) and not C.agree_at_precision(d("6"), 0, d("5.4"), 1)


# --------------------------------------------------------------------------- ATTAIN-1 36 mg: estimand and formulation


def test_attain_1_efficacy_and_treatment_regimen_values_are_an_explainable_difference_never_a_conflict(db):
    group = _group(db, ATTAIN_1, arm="36 mg Orforglipron")
    registry, release = _row(group, tier="REGISTRY_RESULTS"), _row(group, tier="PRESS_RELEASE")
    nejm, label, review = _row(group, tier="PEER_REVIEWED"), _row(group, tier="REGULATORY_LABEL"), _row(group, tier="REGULATORY_REVIEW")
    assert (registry.printed, release.printed, nejm.printed, label.printed) == ("−12.35", "−12.4", "−11.2", "−11.1")
    assert (registry.estimand_label, release.estimand_label, nejm.estimand_label, label.estimand_label) == ("EFFICACY", "EFFICACY", "TREATMENT_REGIMEN", "TREATMENT_REGIMEN")
    for efficacy in (registry, release):
        for regimen in (nejm, label, review):
            pair = _pair(group, efficacy, regimen)
            assert pair["class"] == C.EXPLAINABLE, pair["explanation"]
            estimand = _dim(pair, "estimand")
            assert estimand["status"] == C.DIFFERENT and estimand["explains"]
            assert "not a contradiction" in pair["explanation"] and "conflict" not in pair["explanation"].lower()
            assert not any(a.startswith("Check both values") for a in pair["next_actions"])
    # The registry's -12.35 and the release's -12.4 are one value at the precision printed.
    assert _pair(group, registry, release)["class"] == C.CONSISTENT
    assert _pair(group, nejm, review)["class"] == C.CONSISTENT


def test_attain_1_nejm_and_label_treatment_regimen_values_are_a_true_conflict_at_reporting_precision(db):
    """-11.2 (NEJM) and -11.1 (label) are both treatment-regimen, intention-to-treat, n = 730, week 72, the same randomized group.

    The label names the group by its bioequivalent tablet dosage (17.2 mg), but the participants took the 36 mg capsule:
    the formulation named cannot change the number, so it is recorded and not used as an explanation. Printed to one
    decimal each, the values are not the same number, so this is a true conflict at reporting precision. Its reasons
    include one-digit rounding differences and the label's stated imputation; no tier is consulted.
    """
    group = _group(db, ATTAIN_1, arm="36 mg Orforglipron")
    nejm, label = _row(group, tier="PEER_REVIEWED"), _row(group, tier="REGULATORY_LABEL")
    pair = _pair(group, nejm, label)
    assert pair["class"] == C.TRUE_CONFLICT
    assert [(_dim(pair, d)["status"]) for d in ("estimand", "baseline", "analysis_set", "time_point")] == [C.SAME] * 4
    assert nejm.analysis_set == label.analysis_set == "ITT" and nejm.n == label.n == 730
    formulation = _dim(pair, "formulation")
    assert formulation["status"] == C.NOTED and not formulation["explains"]
    assert "17.2 mg" in formulation["b" if pair["b"] == label.ref else "a"] and "documented exposure-equivalent pair" in formulation["note"]
    assert "one unit in the last printed digit" in " ".join(pair["possible_reasons"])
    assert any("label's analysis population" in a for a in pair["next_actions"])
    text = " ".join([pair["explanation"], *pair["possible_reasons"], *pair["next_actions"]]).lower()
    assert "prefer" not in text and "more reliable" not in text and "regulator-reviewed value" not in text
    assert group.cls == C.TRUE_CONFLICT


def test_capsule_and_tablet_rows_meet_only_through_a_documented_equivalence(db):
    """The label's tablet dosages sit in the capsule groups they are documented to be equivalent to, and nowhere else."""
    study = _study(db, ATTAIN_1)
    groups = [g for g in C.study_groups(db, study) if g.evidence_type == "result" and g.category == "WEIGHT_PCT_CHANGE"]
    where = {r.dose_reported: g.rows[0].arm_group for g in groups for r in g.rows if r.formulation == "foundayo-tablet"}
    assert where == {"5.5 mg": "6 mg Orforglipron", "9 mg": "12 mg Orforglipron", "17.2 mg": "36 mg Orforglipron"}

    # A tablet arm stored as an arm of its own joins the 36 mg capsule group only because 17.2 mg is a documented pair.
    label = db.scalar(select(EvidenceSource).where(EvidenceSource.key == "openfda-label:foundayo"))
    template = db.scalar(select(OutcomeResult).where(OutcomeResult.study_id == study.id, OutcomeResult.source_id == label.id, OutcomeResult.value == -11.1))
    assert template is not None

    def tablet_row(dose: float, value: float) -> OutcomeResult:
        arm = StudyArm(study_id=study.id, origin="CURATED", label=f"Foundayo {dose:g} mg tablet", norm_label=norm_arm_label(f"Foundayo {dose:g} mg tablet"),
                       arm_type="CURATED", drug_slug="orforglipron", dose_amount=dose, dose_unit="mg", formulation_key="foundayo-tablet")
        db.add(arm)
        db.flush()
        row = OutcomeResult(study_id=study.id, endpoint_id=template.endpoint_id, arm_id=arm.id, estimand_id=template.estimand_id, origin="CURATED",
                            arm_label=arm.label, scope="ARM", value=value, value_text=f"{value}", unit="%", timepoint_weeks=72, source_id=label.id,
                            tier=label.tier, verification_status="PENDING_HUMAN_REVIEW", n_analyzed=730)
        db.add(row)
        db.flush()
        return row

    documented, undocumented = tablet_row(17.2, -11.1), tablet_row(20, -11.1)
    assert resolve_arm(db, documented.arm).basis.endswith("documented exposure-equivalent pair of two formulations.")
    assert not resolve_arm(db, undocumented.arm).resolved
    groups = C.study_groups(db, study)
    home = next(g for g in groups if any(r.id == documented.id for r in g.rows))
    assert home.rows[0].arm_group == "36 mg Orforglipron" and len({r.source_id for r in home.rows}) >= 4
    assert not any(r.id == undocumented.id for g in groups for r in g.rows), "a tablet dose without a documented pair stays out of every group"


# --------------------------------------------------------------------------- SURMOUNT-1 and SURMOUNT-4


def test_surmount_1_registry_efficacy_against_nejm_treatment_regimen_is_explainable(db):
    for arm in ("5 mg Tirzepatide", "10 mg Tirzepatide", "15 mg Tirzepatide", "Placebo"):
        group = _group(db, SURMOUNT_1, arm=arm)
        assert group.cls == C.EXPLAINABLE, (arm, group.explanation())
        assert all(_dim(c, "estimand")["explains"] for c in group.comparisons)
    assert not [g for g in detect_gaps(db) if g.key.startswith("tirzepatide:conflict:NCT04184622:WEIGHT_PCT_CHANGE")]


def test_surmount_4_publication_against_press_release_is_insufficient_information_estimand_not_named(db):
    for arm, pub, release in (("Tirzepatide MTD", "−5.5", "−6.7"), ("Placebo", "14.0", "14.8")):
        group = _group(db, SURMOUNT_4, arm=arm, weeks=88)
        publication, press = _row(group, tier="PEER_REVIEWED", value=pub), _row(group, tier="PRESS_RELEASE", value=release)
        assert publication.estimand_label == "NOT_STATED" and publication.baseline_class == press.baseline_class == "RANDOMIZATION_AFTER_LEAD_IN"
        pair = _pair(group, publication, press)
        assert pair["class"] == C.INSUFFICIENT, pair["explanation"]
        assert _dim(pair, "estimand")["status"] == C.UNKNOWN
        assert any(m["evidence_key"] == publication.evidence_key and m["field"] == "estimand" for m in pair["missing_details"])
        assert any(a.startswith("Confirm the estimand of the publication value") for a in pair["next_actions"])
        # Against the change from study entry the baseline is a stated difference, whatever the estimand.
        from_entry = _row(group, tier="PRESS_RELEASE", value={"Tirzepatide MTD": "−26.0", "Placebo": "−9.5"}[arm])
        entry_pair = _pair(group, publication, from_entry)
        assert entry_pair["class"] == C.EXPLAINABLE and _dim(entry_pair, "baseline")["explains"]
        assert group.cls == C.INSUFFICIENT
    keys = {g.key for g in detect_gaps(db)}
    assert any(k.startswith("tirzepatide:conflict:NCT04660643:") and k.endswith(":unreconciled") for k in keys)


# --------------------------------------------------------------------------- the rules themselves


def _synthetic(value_text: str, *, tier: str, source_id: int, estimand: str = "TREATMENT_REGIMEN", n: int | None = 300, analysis_set: str | None = "ITT") -> C.Row:
    definition = endpoint_definition("WEIGHT_PCT_CHANGE", "Percent change in body weight", None, None, "%")
    number, places, basis = C.reported_number(float(value_text), value_text)
    return C.Row(evidence_type="result", id=source_id, evidence_key=f"k{source_id}", study_id=1, nct_id="NCT00000001", origin="CURATED", source_id=source_id,
                 source={"title": f"Source {source_id}", "key": f"s{source_id}"}, tier=tier, verification_status="QUOTE_ANCHORED", arm_label="drug 10 mg",
                 arm_group="10 mg Drug", arm_group_basis="Arm group as registered.", arm_resolved=True, arm_natural="arm:10 mg drug#", legacy_arm_key="drug|10",
                 formulation=None, formulation_basis="not stated", dose_reported=None, endpoint_title="Percent change in body weight", category="WEIGHT_PCT_CHANGE",
                 class_title=None, definition=definition, unit="%", value=float(value_text), number=number, places=places, precision_basis=basis, n=n,
                 estimand_label=estimand, estimand_basis="STATED_BY_SOURCE", analysis_set=analysis_set, analysis_set_field="analysis_population" if analysis_set else None,
                 analysis_set_text="intention-to-treat" if analysis_set else None, timepoint_weeks=68, baseline_class="STUDY_BASELINE", baseline_definition="baseline")


def test_an_in_memory_true_conflict_and_its_neighbours():
    a, b = _synthetic("-14.9", tier="PEER_REVIEWED", source_id=1), _synthetic("-15.6", tier="PRESS_RELEASE", source_id=2)
    pair = C.compare(None, a, b, {})  # type: ignore[arg-type]
    assert pair["class"] == C.TRUE_CONFLICT and pair["values"]["difference"] == "0.7"
    # Same digits: consistent. A finer value that rounds to the coarser one: consistent.
    assert C.compare(None, a, _synthetic("-14.9", tier="PRESS_RELEASE", source_id=3), {})["class"] == C.CONSISTENT  # type: ignore[arg-type]
    assert C.compare(None, a, _synthetic("-14.94", tier="PRESS_RELEASE", source_id=3), {})["class"] == C.CONSISTENT  # type: ignore[arg-type]
    # A named difference explains it; an estimand that is not named leaves it unreconciled.
    assert C.compare(None, a, _synthetic("-15.6", tier="PRESS_RELEASE", source_id=4, estimand="EFFICACY"), {})["class"] == C.EXPLAINABLE  # type: ignore[arg-type]
    assert C.compare(None, a, _synthetic("-15.6", tier="PRESS_RELEASE", source_id=5, estimand="NOT_STATED"), {})["class"] == C.INSUFFICIENT  # type: ignore[arg-type]
    # Different numbers analysed without a named analysis set: not the same quantity as far as the records show.
    assert C.compare(None, a, _synthetic("-15.6", tier="PRESS_RELEASE", source_id=6, n=280, analysis_set=None), {})["class"] == C.INSUFFICIENT  # type: ignore[arg-type]
    # Named, different analysis sets explain it.
    other = _synthetic("-15.6", tier="PRESS_RELEASE", source_id=7, analysis_set="PP")
    explained = C.compare(None, a, other, {})  # type: ignore[arg-type]
    assert explained["class"] == C.EXPLAINABLE and _dim(explained, "analysis_set")["explains"]


def test_evidence_tier_never_decides_a_classification():
    """Every assignment of tiers to the same two values gives the same class and the same field-by-field verdicts."""
    tiers = [t.value for t in EvidenceTier]
    for estimand_b in ("TREATMENT_REGIMEN", "EFFICACY", "NOT_STATED"):
        outcomes = set()
        for tier_a, tier_b in permutations(tiers, 2):
            a = _synthetic("-14.9", tier=tier_a, source_id=1)
            b = _synthetic("-15.6", tier=tier_b, source_id=2, estimand=estimand_b)
            pair = C.compare(None, a, b, {})  # type: ignore[arg-type]
            outcomes.add((pair["class"], tuple((d["dimension"], d["status"], d["explains"]) for d in pair["dimensions"])))
        assert len(outcomes) == 1, (estimand_b, outcomes)
    # And on the seeded database: relabelling every row's tier changes no class.
    from incretinos.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        study = _study(session, ATTAIN_1)
        before = {g.group_id: [c["class"] for c in g.comparisons] for g in C.study_groups(session, study)}
        original = C._source_fields

        def reversed_tier(row):
            fields = original(row)
            fields["tier"] = tiers[-1 - tiers.index(fields["tier"])] if fields["tier"] in tiers else fields["tier"]
            return fields

        C._source_fields = reversed_tier
        try:
            after = {g.group_id: [c["class"] for c in g.comparisons] for g in C.study_groups(session, study)}
        finally:
            C._source_fields = original
        assert {k: sorted(v) for k, v in after.items()} == {k: sorted(v) for k, v in before.items()}
    finally:
        session.close()


def test_group_identity_is_derived_from_the_natural_key_not_row_ids(db):
    group = _group(db, ATTAIN_1, arm="36 mg Orforglipron")
    assert group.group_id == C.group_id_for(ATTAIN_1, group.natural_key) and group.group_id.startswith(ATTAIN_1 + "-")
    assert not any(str(r.id) in group.natural_key.split("|") for r in group.rows)
    assert "arm:36mg orforglipron#" in group.natural_key  # the registered group's normalised label and its randomization stratum
    assert C.find_group(db, group.group_id).natural_key == group.natural_key


def test_adverse_event_counts_and_percentages_are_compared_as_reported(db):
    nausea = [g for g in C.study_groups(db, _study(db, ATTAIN_1)) if g.evidence_type == "adverse_event" and (g.rows[0].term or "").lower() == "nausea"
              and g.rows[0].arm_group == "36 mg Orforglipron"]
    assert len(nausea) == 1
    pair = nausea[0].comparisons[0]
    assert pair["class"] == C.CONSISTENT and "calculated here from the reported counts" in pair["values"]["detail"]
    assert "245 of 728" in pair["values"]["detail"] and "33.7%" in pair["values"]["detail"]


def test_between_arm_analyses_are_grouped_by_pair_and_orientation(db):
    group = _group(db, SURMOUNT_1, arm="15 mg Tirzepatide versus Placebo", evidence_type="analysis")
    registry, label = _row(group, tier="REGISTRY_RESULTS"), _row(group, tier="REGULATORY_LABEL")
    pair = _pair(group, registry, label)
    assert pair["class"] == C.INSUFFICIENT and _dim(pair, "estimand")["status"] == C.UNKNOWN
    assert _dim(pair, "effect_measure")["status"] == C.SAME


# --------------------------------------------------------------------------- gaps, rejection and the API


def test_gap_tracker_follows_the_classification_and_keeps_earlier_keys(db):
    drafts = {g.key: g for g in detect_gaps(db) if g.rule_id == "G12_SOURCE_CONFLICT"}
    for dose in (6, 12, 36):  # the keys the earlier rule used for the ATTAIN-1 treatment-regimen conflicts
        assert f"orforglipron:conflict:{ATTAIN_1}:WEIGHT_PCT_CHANGE:orforglipron|{dose}:TREATMENT_REGIMEN:72.0" in drafts
    gap = drafts[f"orforglipron:conflict:{ATTAIN_1}:WEIGHT_PCT_CHANGE:orforglipron|36:TREATMENT_REGIMEN:72.0"]
    assert "-11.2 (peer reviewed, source #" in gap.available_evidence and "-11.1 (regulatory label, source #" in gap.available_evidence
    assert "-12.35" not in gap.available_evidence, "an explained (efficacy) value is not part of the conflict"
    assert all("prefer" not in g.analytic_approach.lower() for g in drafts.values())
    explained = {g.group_id for g in C.all_groups(db) if g.cls in (C.EXPLAINABLE, C.CONSISTENT)}
    assert not any(gid.split("-", 1)[1] in key for gid in explained for key in drafts)


def test_a_rejected_row_leaves_every_conflict_and_its_gap(db):
    group = _group(db, ATTAIN_1, arm="36 mg Orforglipron")
    label = _row(group, tier="REGULATORY_LABEL")
    key = f"orforglipron:conflict:{ATTAIN_1}:WEIGHT_PCT_CHANGE:orforglipron|36:TREATMENT_REGIMEN:72.0"
    row = db.get(OutcomeResult, label.id)
    record_decision(db, evidence_type="result", row_id=row.id, decision="REJECT", reviewer=REVIEWER, rationale=REJECT_WHY, attested=True,
                    expected_fingerprint=row.fingerprint)
    after = C.find_group(db, group.group_id)
    assert after is not None and label.ref not in {r.ref for r in after.rows}
    assert after.cls != C.TRUE_CONFLICT  # the remaining treatment-regimen values (-11.2, -11.2) agree
    assert db.scalar(select(EvidenceGap).where(EvidenceGap.key == key)) is None
    assert C.conflict_for_row(db, "result", row)["group_id"] is None


def test_conflicts_api_lists_with_true_totals_and_filters(client):
    everything = client.get("/api/conflicts", params={"limit": 500}).json()
    assert everything["total"] == len(everything["rows"]) == sum(everything["counts"].values()) == everything["all_groups"]
    assert set(everything["counts"]) == set(C.CLASSES) and all(everything["counts"][c] > 0 for c in C.CLASSES)
    true = client.get("/api/conflicts", params={"class": "TRUE_CONFLICT", "limit": 2}).json()
    assert true["total"] == everything["counts"]["TRUE_CONFLICT"] and len(true["rows"]) == 2 and true["counts"] == everything["counts"]
    narrowed = client.get("/api/conflicts", params={"drug": "orforglipron", "study": ATTAIN_1, "endpoint_category": "WEIGHT_PCT_CHANGE", "evidence_type": "result"}).json()
    assert narrowed["total"] == sum(narrowed["counts"].values()) == 4 and {r["study"]["nct_id"] for r in narrowed["rows"]} == {ATTAIN_1}
    assert client.get("/api/conflicts", params={"drug": "not-a-drug"}).status_code == 422
    assert client.get("/api/conflicts", params={"endpoint_category": "NOT_A_CATEGORY"}).status_code == 422
    assert client.get("/api/conflicts", params={"class": "WRONG"}).status_code == 422


def test_conflict_detail_and_review_dossier(client, db):
    group = _group(db, ATTAIN_1, arm="36 mg Orforglipron")
    detail = client.get(f"/api/conflicts/{group.group_id}").json()
    assert detail["class"] == C.TRUE_CONFLICT and len(detail["rows"]) == 5
    assert {r["value_printed"] for r in detail["rows"]} == {"−12.35", "−12.4", "−11.2", "−11.1"}
    assert all(r["review_href"].startswith("/review?type=result&key=") for r in detail["rows"])
    assert "Context only" in detail["source_hierarchy"]["note"] and detail["source_hierarchy"]["tiers"][0]["tier"] == "REGULATORY_LABEL"
    assert detail["next_actions"] and detail["possible_reasons"] and detail["missing_details"]
    assert client.get(f"/api/conflicts/{ATTAIN_1}-0000000000000000").status_code == 404
    assert client.get("/api/conflicts/not-an-id").status_code == 422
    label = _row(group, tier="REGULATORY_LABEL")
    dossier = client.get(f"/api/review/evidence/by-key/result/{label.evidence_key}").json()
    conflict = dossier["source_conflict"]
    assert conflict["group_id"] == group.group_id and conflict["class"] == C.TRUE_CONFLICT and conflict["href"] == f"/conflicts?group={group.group_id}"
    assert {c["class"] for c in conflict["comparisons"]} == {C.TRUE_CONFLICT, C.EXPLAINABLE}  # -11.1 agrees with no other source


@pytest.mark.parametrize("nct", [ATTAIN_1, SURMOUNT_4])
def test_every_group_explains_itself(db, nct):
    for group in C.study_groups(db, _study(db, nct)):
        detail = group.detail()
        assert detail["explanation"] and all(c["explanation"] for c in detail["comparisons"])
        if group.cls in (C.TRUE_CONFLICT, C.INSUFFICIENT):
            assert detail["next_actions"], group.group_id
        if group.cls == C.INSUFFICIENT:
            assert detail["missing_details"], group.group_id
