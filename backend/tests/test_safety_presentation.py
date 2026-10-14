"""Fields the web app needs to present trial adverse events honestly.

The safety chart once decided which arm was "placebo" by matching the word in the arm label, so
semaglutide arms named "Placebo + 1.0 mg Semaglutide" were drawn as placebo. The arm role now comes
from stored arm data, and every percentage says whether the source reported it or it was calculated
here from the reported counts.
"""

from __future__ import annotations

from types import SimpleNamespace

from incretinos.engines.safety import ARM_ROLE_LABEL, ae_coverage, arm_role

BELIEVE = "NCT05616013"
SURMOUNT_1 = "NCT04184622"
ELORATZP = "NCT06603571"


def _arm(**kw):
    base = {"arm_type": None, "drug_slug": None, "is_placebo": False, "dose_amount": None, "dose_unit": None}
    return SimpleNamespace(**{**base, **kw})


def test_arm_role_is_read_from_data_not_from_the_label():
    roles = {"bimagrumab": "INVESTIGATIONAL", "semaglutide": "COMPARATOR"}
    # A semaglutide-only arm in a trial where semaglutide is the comparator drug: an active comparator, whatever its label says.
    assert arm_role(_arm(arm_type="EXPERIMENTAL", drug_slug="semaglutide"), roles)["arm_role"] == "ACTIVE_COMPARATOR"
    # A combination that includes the investigational drug is a study-drug arm.
    assert arm_role(_arm(arm_type="EXPERIMENTAL", drug_slug="bimagrumab+semaglutide"), roles)["arm_role"] == "STUDY_DRUG"
    assert arm_role(_arm(arm_type="PLACEBO_COMPARATOR", drug_slug="placebo", is_placebo=True), roles)["arm_role"] == "PLACEBO"
    # The sponsor's registered comparator type is an explicit statement of the arm's role.
    assert arm_role(_arm(arm_type="ACTIVE_COMPARATOR", drug_slug="tirzepatide"), {"tirzepatide": "INVESTIGATIONAL"})["arm_role"] == "ACTIVE_COMPARATOR"
    # Registered as placebo but stored with an active drug (a switch or add-on arm): never shown as pure placebo.
    switched = arm_role(_arm(arm_type="PLACEBO_COMPARATOR", drug_slug="bimagrumab"), roles)
    assert switched["arm_role"] == "UNCLASSIFIED"
    assert "not treated as placebo" in switched["arm_role_basis"]
    # The registry adapter stores a registered placebo comparator as a placebo arm even when it switches to an active
    # drug later; such an arm is still not shown as pure placebo, while a matching placebo ("Bimagrumab Placebo") is.
    stored_as_placebo = arm_role(_arm(arm_type="PLACEBO_COMPARATOR", drug_slug="placebo", is_placebo=True, label="Placebo/30 mg/kg Bimagrumab"), roles)
    assert stored_as_placebo["arm_role"] == "UNCLASSIFIED" and "not treated as placebo" in stored_as_placebo["arm_role_basis"]
    assert arm_role(_arm(arm_type="PLACEBO_COMPARATOR", drug_slug="placebo", is_placebo=True, label="Bimagrumab Placebo + Tirzepatide Placebo"), roles)["arm_role"] == "PLACEBO"
    assert arm_role(_arm(arm_type="PLACEBO_COMPARATOR", drug_slug="placebo", is_placebo=True, label="Placebo"), roles)["arm_role"] == "PLACEBO"
    # Nothing known about the arm: unclassified, not guessed.
    assert arm_role(None, roles)["arm_role"] == "UNCLASSIFIED"
    assert arm_role(_arm(drug_slug="dapagliflozin"), roles)["arm_role"] == "UNCLASSIFIED"
    assert arm_role(_arm(), roles)["arm_role"] == "UNCLASSIFIED"
    for role in ("PLACEBO", "ACTIVE_COMPARATOR", "STUDY_DRUG", "UNCLASSIFIED"):
        assert ARM_ROLE_LABEL[role]


def test_semaglutide_arms_named_placebo_plus_are_active_comparators(client):
    body = client.get("/api/safety/trial-ae", params={"nct": BELIEVE, "measure_kind": "PARTICIPANTS_WITH_EVENT", "term": "Nausea"}).json()
    rows = [r for r in body["rows"] if r["term"] == "Nausea" and r["scope"] == "ARM"]
    by_label = {r["arm_label"]: r for r in rows}
    for label in ("Placebo + 1.0 mg Semaglutide", "Placebo + 2.4 mg Semaglutide"):
        row = by_label[label]
        assert row["arm_role"] == "ACTIVE_COMPARATOR", label
        assert row["arm_role_label"] == "Active comparator"
        assert row["arm_is_placebo"] is False
        assert row["arm_drug_slug"] == "semaglutide"
    # No arm of this adverse-event table is a pure placebo arm, so none may be presented as one.
    assert [r["arm_label"] for r in rows if r["arm_role"] == "PLACEBO"] == []
    assert by_label["Placebo/30 mg/kg Bimagrumab"]["arm_role"] == "UNCLASSIFIED"
    assert by_label["30 mg/kg Bimagrumab + 2.4 mg Semaglutide"]["arm_role"] == "STUDY_DRUG"
    assert body["arm_role_labels"]["ACTIVE_COMPARATOR"] == "Active comparator"


def test_every_row_has_a_role_and_placebo_follows_the_stored_flag(client):
    body = client.get("/api/safety/trial-ae", params={"nct": SURMOUNT_1, "measure_kind": "PARTICIPANTS_WITH_EVENT", "term": "Nausea"}).json()
    assert body["rows"]
    for row in body["rows"]:
        assert row["arm_role"] in ARM_ROLE_LABEL
        assert row["arm_role_basis"]
        assert (row["arm_role"] == "PLACEBO") == bool(row["arm_is_placebo"])
    roles = {r["arm_role"] for r in body["rows"]}
    assert {"PLACEBO", "STUDY_DRUG"} <= roles


def test_percent_basis_separates_reported_from_calculated(client):
    registry = client.get("/api/safety/trial-ae", params={"nct": BELIEVE, "measure_kind": "PARTICIPANTS_WITH_EVENT", "term": "Nausea"}).json()["rows"]
    assert registry
    for row in registry:
        # Registry rows carry counts only: the percentage is a calculation made here and must say so.
        assert row["percent_reported"] is None
        assert row["percent_basis"] == "CALCULATED_FROM_COUNTS"
        assert row["percent_calculated"] == round(100 * row["n_affected"] / row["n_at_risk"], 2)
    curated = client.get("/api/safety/trial-ae", params={"nct": ELORATZP, "measure_kind": "TREATMENT_DISCONTINUATION_DUE_TO_AE"}).json()["rows"]
    arm_level = [r for r in curated if r["scope"] == "ARM"]
    ranges = [r for r in curated if r["scope"] != "ARM"]
    assert arm_level and ranges
    assert all(r["percent_basis"] == "REPORTED" and r["percent_calculated"] is None for r in arm_level)
    # A range across arms has neither a reported arm-level percentage nor counts: no basis, and no arm it could belong to.
    assert all(r["percent_basis"] is None and r["arm_role"] == "UNCLASSIFIED" for r in ranges)


def test_coverage_names_the_trials_that_hold_incidence_data(client, db):
    body = client.get("/api/safety/trial-ae", params={"drug": "tirzepatide", "limit": 1}).json()
    assert len(body["rows"]) == 1  # the row limit does not limit the coverage summary
    coverage = {c["nct_id"]: c for c in body["coverage"]}
    # EloraTZP has one discontinuation disclosure and no adverse-event incidence table.
    assert "PARTICIPANTS_WITH_EVENT" not in coverage[ELORATZP]["by_measure_kind"]
    assert coverage[ELORATZP]["by_measure_kind"]["TREATMENT_DISCONTINUATION_DUE_TO_AE"]["rows"] >= 1
    surmount = coverage[SURMOUNT_1]["by_measure_kind"]["PARTICIPANTS_WITH_EVENT"]
    assert surmount["arm_level_rows"] > 0 and surmount["arm_level_terms"] > 0
    assert surmount["label"]
    # The summary agrees with a direct count of stored rows.
    from sqlalchemy import func, select

    from incretinos.models import AdverseEvent, Study

    stored = db.scalar(select(func.count(AdverseEvent.id)).join(Study, AdverseEvent.study_id == Study.id).where(Study.nct_id == SURMOUNT_1))
    assert coverage[SURMOUNT_1]["rows"] == stored
    assert ae_coverage(db, nct_id=SURMOUNT_1)[0]["rows"] == stored
