"""Full-text evidence: JATS and PDF table parsing, licence gating, extraction to reviewable rows, limitations.

Everything runs offline from the committed fixtures (``fixtures/europepmc``, ``fixtures/fdadocs``,
``fixtures/fulltext/manifest.json``) or from small documents written by the tests themselves.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select

from incretinos.config import get_settings
from incretinos.db import INCLUDE_REJECTED, rejected_visible
from incretinos.enums import Origin, VerificationStatus
from incretinos.fulltext import jats, mapping, pdf
from incretinos.fulltext.licence import classify
from incretinos.fulltext.loader import FullTextStep, read_manifest
from incretinos.fulltext.tables import read_estimate_ci
from incretinos.ingestion.transport import FixtureTransport
from incretinos.models import (
    REVIEWABLE_MODELS,
    AdverseEvent,
    EvidenceClaim,
    ExtractedTable,
    FullTextDocument,
    FullTextExtraction,
    OutcomeResult,
    ReviewQueueItem,
    SourceSnapshot,
    StatisticalAnalysis,
    Study,
)
from incretinos.review import checks
from incretinos.review.lifecycle import reconcile

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FULLTEXT = Origin.FULLTEXT.value
PENDING = VerificationStatus.PENDING_HUMAN_REVIEW.value
FDA_KEY = "fda:nda220934:multidiscipline-review"
FULLTEXT_TYPES = ("result", "analysis", "adverse_event", "claim")

# --------------------------------------------------------------------------- a small JATS document

SMALL_JATS = """<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:ali="http://www.niso.org/schemas/ali/1.0/">
<front><article-meta><article-id pub-id-type="pmcid">PMC0000001</article-id><article-id pub-id-type="pmid">1</article-id>
<title-group><article-title>A small test article</article-title></title-group>
<permissions><license><ali:license_ref>{licence_url}</ali:license_ref><license-p>Licence text.</license-p></license></permissions>
</article-meta></front>
<body><sec><title>Results</title>
<p id="Par1">Body weight fell. The difference was significant (estimated treatment difference [95% confidence interval] −2.5% [−3.0, −2.0]; p &lt; 0.001).</p>
<table-wrap id="Tab1"><label>Table 1</label><caption><p>Outcomes at Week 72</p></caption>
<table><thead>
<tr><th rowspan="2">Parameter</th><th colspan="2">Treatment group</th></tr>
<tr><th>Drug A 10 mg N = 100</th><th>Placebo N = 50</th></tr>
</thead><tbody>
<tr><td colspan="3">Body weight</td></tr>
<tr><td> Change, %<sup>a</sup></td><td>−5.0 (0.4)</td><td>−1.0 (0.5)</td></tr>
<tr><td> Difference vs placebo (95% CI)</td><td>−4.0 (−5.1, −2.9)</td><td/></tr>
<tr><td>SAEs<xref ref-type="table-fn" rid="fn2">b</xref></td><td>3 (3.0)</td><td>1 (2.0)</td></tr>
</tbody></table>
<table><thead><tr><th colspan="3">Cohort 2</th></tr></thead><tbody>
<tr><td/><td>Drug A 10 mg N = 40</td><td>Placebo N = 20</td></tr>
<tr><td>SAEs</td><td>0</td><td>1 (5.0)</td></tr>
</tbody></table>
<table-wrap-foot><fn id="fn1"><p><sup>a</sup>Least squares mean (SE) from an MMRM.</p><p><sup>b</sup>Serious adverse events.</p>
<p>Abbreviations: CI = confidence interval; SE = standard error</p></fn></table-wrap-foot>
</table-wrap>
</sec></body></article>
"""


def _small_jats(licence_url: str) -> bytes:
    return SMALL_JATS.format(licence_url=licence_url).encode("utf-8")


def test_jats_table_keeps_spans_panels_stubs_and_footnotes():
    root = jats.parse(_small_jats("https://creativecommons.org/licenses/by/4.0/"))
    table = jats.parse_table(root, jats.table_wraps(root)[0])
    assert table.label == "Table 1" and table.caption == "Outcomes at Week 72"
    assert table.n_columns == 2
    # The spanning heading sits over both data columns; the rowspan stub heading covers column 0.
    first = table.header_rows[0]
    assert {"text": "Treatment group", "col": 1, "colspan": 2} == {k: v for k, v in vars(first[1]).items() if k in ("text", "col", "colspan")}
    assert first[0].text == "Parameter" and first[0].rowspan == 2
    assert table.column_headers(0)[1] == "Treatment group Drug A 10 mg N = 100"
    # A label row spanning the whole table is a row group; indented rows sit under it; markers are references, not text.
    kinds = [(r.kind, r.stub, r.group, r.footnote_refs) for r in table.rows]
    assert ("group", "Body weight", None, []) in kinds
    assert ("data", "Change, %", "Body weight", ["a"]) in kinds
    assert ("data", "SAEs", None, ["b"]) in kinds
    # The second <table> is a second panel read against its own headings.
    assert len(table.panels) == 2 and table.panels[1]["label"] == "Cohort 2"
    cohort2 = [r for r in table.rows if r.panel == 1]
    assert cohort2[0].stub == "SAEs" and cohort2[0].cells == {1: "0", 2: "1 (5.0)"}
    assert table.column_headers(1) == {1: "Drug A 10 mg N = 40", 2: "Placebo N = 20"}
    # Footnotes keep their markers; abbreviations are read from the foot.
    assert {"marker": "a", "kind": "footnote", "text": "Least squares mean (SE) from an MMRM."} in table.footnotes
    assert table.footnote("b") == "Serious adverse events."
    assert table.abbreviations == {"CI": "confidence interval", "SE": "standard error"}
    assert any(u["unit"] == "%" and u["where"] == "row_label" for u in table.units)
    # An empty cell stays empty: nothing is filled in.
    diff = next(r for r in table.rows if r.stub.startswith("Difference"))
    assert diff.cells == {1: "-4.0 (-5.1, -2.9)"}


def test_jats_sentence_is_found_verbatim_with_its_place():
    root = jats.parse(_small_jats("https://creativecommons.org/licenses/by/4.0/"))
    paragraph, sentence = jats.find_sentence(root, "The difference was significant", section="Results")
    assert paragraph.paragraph_id == "Par1" and paragraph.section == "Results"
    assert sentence.startswith("The difference was significant") and sentence.endswith(").")


@pytest.mark.parametrize(
    ("licence", "url", "storable", "name"),
    [
        ("cc by", None, True, "CC BY"),
        (None, "https://creativecommons.org/licenses/by/4.0/", True, "CC BY"),
        ("cc0", None, True, "CC0"),
        ("cc by-nc-nd", None, False, "CC BY-NC-ND"),
        ("CC BY-NC 4.0", None, False, "CC BY-NC"),
        (None, "https://creativecommons.org/licenses/by-nc-nd/4.0/", False, "CC BY-NC-ND"),
        ("cc by-sa", None, False, "CC BY-SA"),
        (None, None, False, None),
        ("all rights reserved", None, False, None),
    ],
)
def test_licence_classification(licence, url, storable, name):
    decision = classify(licence, url)
    assert decision.storable is storable and decision.normalised == name and decision.reason


def test_us_government_work_is_storable_only_when_declared():
    assert classify(None, us_government_work=True).storable
    assert not classify(None).storable


def test_interval_reader_reports_printing_slips_instead_of_hiding_them():
    assert read_estimate_ci("-5.3 (-6.1, -4.5)").irregular is None
    slip = read_estimate_ci("14.1 (10.7, 17.6))")
    assert (slip.estimate, slip.low, slip.high) == (14.1, 10.7, 17.6) and "twice" in slip.irregular
    assert read_estimate_ci("-9.4 (-12.6, - 6.1)").irregular
    assert read_estimate_ci("Not applicable") is None
    assert read_estimate_ci("-5.3") is None


# --------------------------------------------------------------------------- arm mapping

ARMS = [
    mapping.ArmCandidate(1, "Placebo", "placebo", None, None, None, True, 949),
    mapping.ArmCandidate(2, "6 mg Orforglipron", "orforglipron", 6.0, "mg", "orforglipron-capsule", False, 723),
    mapping.ArmCandidate(3, "12 mg Orforglipron", "orforglipron", 12.0, "mg", "orforglipron-capsule", False, 725),
]


def test_header_maps_to_a_unique_arm_through_the_tables_own_abbreviations():
    match = mapping.match_arm("OFG 6 mg N=723", ARMS, abbreviations={"OFG": "orforglipron", "N": "number randomized"})
    assert match.status == mapping.UNIQUE and match.arm_id == 2 and match.n_agrees is True
    assert match.expansions == {"OFG": "orforglipron"}
    assert mapping.match_arm("Placebo N=949", ARMS).arm_id == 1


def test_header_maps_through_a_documented_exposure_equivalence_and_says_so():
    match = mapping.match_arm("orforglipron 5.5 mg", ARMS, equivalent_doses={("orforglipron-capsule", 6.0): {5.5}})
    assert match.status == mapping.VIA_EQUIVALENCE and match.arm_id == 2 and "equivalence" in match.basis


def test_ambiguous_or_unmatched_header_is_kept_without_an_arm():
    ambiguous = mapping.match_arm("Orforglipron N = 124", ARMS)
    assert ambiguous.status == mapping.AMBIGUOUS and ambiguous.arm_id is None and len(ambiguous.candidates) == 2
    unknown = mapping.match_arm("OFG 24 mg", ARMS, abbreviations={"OFG": "orforglipron"})
    assert unknown.status == mapping.NONE and unknown.arm_id is None
    assert mapping.match_arm("Total N = 204", ARMS).status == mapping.NONE


# --------------------------------------------------------------------------- the PDF fixture


@pytest.fixture(scope="module")
def fda_pages():
    manifest = read_manifest(FIXTURES)["documents"][FDA_KEY]
    data = (FIXTURES / manifest["fixture"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["fixture_sha256"]
    pages, _ = pdf.read_pages(data, numbering=manifest["excerpt_pages"])
    return pages


def test_pdf_fixture_records_the_original_document():
    manifest = read_manifest(FIXTURES)["documents"][FDA_KEY]
    assert manifest["page_count"] == 564 and manifest["bytes"] > 18_000_000 and len(manifest["sha256"]) == 64
    assert manifest["url"].startswith("https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/220934")


def test_pdf_table_extraction_on_the_recorded_fixture(fda_pages):
    table = pdf.extract_table(fda_pages, "FDA Table 23")
    assert table.authorship == "FDA_ANALYSIS" and table.source_line == "Source: FDA analysis; adsl.xpt, advs.xpt"
    assert (table.page_start, table.page_end, table.printed_page) == (135, 135, "112")
    assert table.caption == "FDA Table 23. Primary Analysis: Percentage Change in Body Weight at Week 72, ATTAIN-1"
    assert table.column_headers() == {0: "Parameter", 1: "Placebo N=949", 2: "OFG 6 mg N=723", 3: "OFG 12 mg N=725", 4: "OFG 36 mg N=730"}
    diff = table.find_rows(r"^Difference from placebo")[0][1]
    # The placebo column of the difference row is empty and stays empty.
    assert diff.cells == {2: "-5.3 (-6.1, -4.5)", 3: "-6.2 (-7.1, -5.3)", 4: "-9.0 (-10.0, -8.1)"}
    estimate = table.find_rows(r"^Estimate")[0][1]
    assert estimate.footnote_refs == ["1"] and estimate.cells[1] == "-2.1 (0.3)"
    assert table.footnote("1").startswith("Analysis of covariance model for endpoint included treatment")
    assert table.abbreviations["OFG"] == "orforglipron" and table.abbreviations["N"] == "number of subjects randomized"
    assert not table.notes


def test_pdf_table_continued_on_the_next_page_keeps_its_row_groups(fda_pages):
    table = pdf.extract_table(fda_pages, "FDA Table 24")
    assert (table.page_start, table.page_end) == (139, 140)
    groups = [r.stub for r in table.rows if r.kind == "group"]
    assert groups == ["≥5% body weight loss", "≥10% body weight loss", "≥15% body weight loss", "≥20% body weight loss",
                      "Waist circumference (cm): Change from baseline at Week 72"]
    diffs = table.find_rows(r"^Difference from placebo", group=r"^≥15%")
    assert diffs[0][1].cells[3] == "14.1 (10.7, 17.6))"  # printed so; read as an irregular interval, never corrected
    assert any("continues on page 140" in n for n in table.notes)


def test_pdf_passages_are_complete_fda_sentences(fda_pages):
    passage = pdf.find_passage(fda_pages, 123, "FDA agrees with the Applicant’s multiplicity control strategy")
    assert passage.authored_by_fda is True and passage.heading_page == 121
    assert passage.sentence.endswith("as prespecified in the SAP.")


def test_pdf_excerpt_fixture_holds_no_applicant_section_text():
    from pypdf import PdfReader

    manifest = read_manifest(FIXTURES)["documents"][FDA_KEY]
    reader = PdfReader(FIXTURES / manifest["fixture"])
    text = " ".join(p.extract_text() for p in reader.pages)
    for applicant_only in ("The Applicant’s Position:", "The Applicant’s Description:", "Table 8.4. Applicant"):
        assert applicant_only not in text


# --------------------------------------------------------------------------- the seeded database


def _fulltext_rows(db):
    with rejected_visible(db):
        return [(etype, row) for etype in FULLTEXT_TYPES for row in db.scalars(select(REVIEWABLE_MODELS[etype]).where(REVIEWABLE_MODELS[etype].origin == FULLTEXT))]


def test_every_fulltext_row_is_pending_review_with_complete_provenance(db):
    rows = _fulltext_rows(db)
    counts = {etype: sum(1 for t, _ in rows if t == etype) for etype in FULLTEXT_TYPES}
    assert counts == {"result": 8, "analysis": 41, "adverse_event": 4, "claim": 12}
    for etype, row in rows:
        assert row.verification_status == PENDING and row.base_verification_status == PENDING, (etype, row.id)
        assert row.evidence_key and row.fingerprint and row.source_snapshot_id
        extraction = db.get(FullTextExtraction, row.fulltext_extraction_id)
        assert extraction is not None and extraction.evidence_type == etype
        assert extraction.cells and extraction.parse_method and extraction.parse_version and extraction.quality and extraction.confidence
        document = extraction.document
        assert document.fetch_status == "FETCHED" and document.content_hash and document.licence_basis
        if extraction.table is not None:
            assert extraction.table.document_id == document.id and extraction.table.locator
        if etype != "claim":
            assert row.source_location and row.locator_quote and row.tier in ("REGULATORY_REVIEW", "PEER_REVIEWED")


def test_fda_treatment_regimen_differences_are_new_rows_with_their_cells(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT05869903"))
    with rejected_visible(db):
        rows = db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.study_id == study.id, StatisticalAnalysis.origin == FULLTEXT,
                                                            StatisticalAnalysis.param_type == "Difference from placebo, (95% CI)")).all()
    primary = {r.treatment_label: r for r in rows if r.endpoint.category == "WEIGHT_PCT_CHANGE"}
    six = primary["OFG 6 mg N=723"]
    assert (six.estimate, six.ci_low, six.ci_high, six.ci_level, six.unit, six.p_value_text) == (-5.3, -6.1, -4.5, 95.0, "%", "<0.001")
    assert six.treatment_arm.label == "6 mg Orforglipron" and six.comparator_arm.label == "Placebo"
    assert six.estimand.label == "TREATMENT_REGIMEN" and six.estimand.basis == "INFERRED_FROM_ANALYSIS_DESCRIPTION"
    assert six.estimand.quote.startswith("FDA agrees with use of treatment-regimen estimand")
    assert six.endpoint.role == "PRIMARY" and six.timepoint_weeks == 72
    assert six.method.startswith("Analysis of covariance model")
    assert {(r.estimate, r.ci_low, r.ci_high) for r in primary.values()} == {(-5.3, -6.1, -4.5), (-6.2, -7.1, -5.3), (-9.0, -10.0, -8.1)}
    extraction = six.fulltext_extraction
    assert extraction.table.label == "FDA Table 23" and extraction.row_label == "Difference from placebo, (95% CI)"
    assert extraction.column_header == "OFG 6 mg N=723" and extraction.comparator_header == "Placebo N=949"
    used = [c for c in extraction.cells if c["role"] == "estimate_and_interval"][0]
    assert used["text"] == "-5.3 (-6.1, -4.5)" and extraction.table.body_rows[used["row_index"]]["cells"][str(used["col_index"])] == used["text"]
    quality = extraction.quality
    assert quality["arm_match"]["status"] == "UNIQUE" and quality["arm_match"]["n_agrees"] is True
    assert quality["estimand"]["in_table"] is False and quality["unit"]["value"] == "%"
    codes = {f["code"] for f in six.check_flags}
    assert "FULLTEXT_ESTIMAND_NOT_IN_TABLE" in codes
    # The registry holds the efficacy-estimand contrasts; these treatment-regimen ones are new.
    with rejected_visible(db):
        registry = db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.study_id == study.id, StatisticalAnalysis.origin != FULLTEXT,
                                                                StatisticalAnalysis.estimate == -5.3)).all()
    assert not [r for r in registry if r.ci_low == -6.1 and r.ci_high == -4.5]
    attain2 = db.scalar(select(Study).where(Study.nct_id == "NCT05872620"))
    with rejected_visible(db):
        a2 = {r.treatment_label: (r.estimate, r.ci_low, r.ci_high) for r in attain2.analyses if r.origin == FULLTEXT and r.endpoint.category == "WEIGHT_PCT_CHANGE"}
    assert a2 == {"OFG 6 mg N=329": (-2.6, -3.5, -1.6), "OFG 12 mg N=332": (-4.5, -5.4, -3.6), "OFG 36 mg N=322": (-7.1, -8.1, -6.1)}


def test_irregular_cell_is_read_and_flagged(db):
    with rejected_visible(db):
        row = db.scalar(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == FULLTEXT, StatisticalAnalysis.estimate_text == "14.1 (10.7, 17.6))"))
    assert (row.estimate, row.ci_low, row.ci_high) == (14.1, 10.7, 17.6)
    assert "FULLTEXT_IRREGULAR_CELL" in {f["code"] for f in row.check_flags}
    assert row.fulltext_extraction.confidence == "LOW"


def test_surmount5_prediabetes_results_are_post_hoc_subgroup_evidence(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT05822830"))
    with rejected_visible(db):
        rows = [a for a in study.analyses if a.origin == FULLTEXT]
    hba1c = next(a for a in rows if a.endpoint.category == "HBA1C_CHANGE")
    assert (hba1c.estimate, hba1c.ci_low, hba1c.ci_high, hba1c.ci_level, hba1c.unit, hba1c.p_value_text) == (-0.12, -0.18, -0.06, 95.0, "%", "<0.001")
    assert hba1c.endpoint.role == "POST_HOC" and hba1c.class_title.startswith("Subgroup: participants with study-defined prediabetes")
    assert hba1c.estimand.label == "EFFICACY" and hba1c.estimand.basis == "STATED_BY_SOURCE"
    assert hba1c.treatment_arm.drug_slug == "tirzepatide" and hba1c.comparator_arm.drug_slug == "semaglutide"
    assert "not the trial's primary result" in hba1c.notes
    codes = {f["code"] for f in hba1c.check_flags}
    assert {"FULLTEXT_SUBGROUP_POST_HOC", "FULLTEXT_ARM_INDIRECT", "FULLTEXT_VALUE_FROM_TEXT"} <= codes
    quality = hba1c.fulltext_extraction.quality
    assert quality["denominators"] == {"treatment": 215, "comparator": 210, "source": "column headings of Table 1"}
    assert quality["arm_match"]["status"] == "SUBSET_OF_ARM"
    # A value whose unit the document does not print is stored without one and flagged.
    whtr = next(a for a in rows if "waist-to-height" in a.endpoint.title)
    assert whtr.unit is None and "unit" in whtr.missing_fields and "FULLTEXT_UNIT_NOT_FOUND" in {f["code"] for f in whtr.check_flags}
    assert whtr.ci_level is None  # the sentence prints no confidence level: none is assumed


def test_cohort_columns_are_kept_as_subsets_of_the_registered_arm(db):
    with rejected_visible(db):
        events = db.scalars(select(AdverseEvent).where(AdverseEvent.origin == FULLTEXT)).all()
    got = sorted((e.arm_label, e.n_affected, e.n_at_risk, e.percent) for e in events)
    assert got == [("Orforglipron N = 105 — Cohort 2 (semaglutide)", 1, 105, 1.0), ("Orforglipron N = 124 — Cohort 1 (tirzepatide)", 3, 124, 2.4),
                   ("Placebo N = 66 — Cohort 2 (semaglutide)", 1, 66, 1.5), ("Placebo N = 80 — Cohort 1 (tirzepatide)", 0, 80, None)]
    assert all(e.is_serious and e.fulltext_extraction.quality["arm_match"]["status"] == "SUBSET_OF_ARM" for e in events)
    assert all("DUPLICATE_KEY_DIFFERENT_VALUES" not in {f["code"] for f in e.check_flags} for e in events)


def test_documents_licences_and_recorded_limitations(db):
    docs = {d.key: d for d in db.scalars(select(FullTextDocument))}
    fda = docs[FDA_KEY]
    assert not fda.full_text_stored and fda.licence is None and "17 U.S.C." in fda.licence_basis
    assert db.get(SourceSnapshot, db.scalar(select(SourceSnapshot.id).where(SourceSnapshot.source_id == fda.source_id))).content is None
    pmc = docs["pmc:PMC13451520"]
    assert pmc.licence == "cc by" and pmc.full_text_stored and pmc.open_access_flag == "Y"
    snap = db.scalar(select(SourceSnapshot).where(SourceSnapshot.source_id == pmc.source_id))
    assert snap.content and "Reversion to normoglycemia" in snap.content
    ndnc = docs["pmc:PMC13453602"]
    assert ndnc.licence == "cc by-nc-nd" and not ndnc.full_text_storable and not ndnc.full_text_stored
    assert {"LICENCE_RESTRICTS_STORAGE", "NO_EXTRACTION_CONFIGURED"} <= {lim["code"] for lim in ndnc.limitations}
    maintain = docs["pmc:PMC13375559"]
    assert sum(1 for lim in maintain.limitations if lim["code"] == "TABLE_WITHOUT_CELLS") == 3
    assert any(lim["code"] == "NOT_EXTRACTED" and "pool ATTAIN-1 and ATTAIN-2" in lim["text"] for lim in fda.limitations)
    assert db.scalar(select(func.count()).select_from(ReviewQueueItem).where(ReviewQueueItem.item_type == "FULLTEXT_LIMITATION")) >= 7


def test_unreviewed_fulltext_rows_stay_out_of_analytic_queries(db):
    analytic = db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == FULLTEXT)).all()
    assert analytic == []
    assert db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == FULLTEXT).execution_options(**{INCLUDE_REJECTED: True})).all()


def test_review_detail_shows_document_table_cells_method_and_quality(client, db):
    with rejected_visible(db):
        row = db.scalar(select(StatisticalAnalysis).where(StatisticalAnalysis.origin == FULLTEXT, StatisticalAnalysis.estimate == -5.3))
    detail = client.get(f"/api/review/evidence/by-key/analysis/{row.evidence_key}").json()
    assert detail["origin"] == FULLTEXT and detail["anchoring"]["basis"] == "FULLTEXT_EXTRACTION"
    assert detail["status"]["verification_status"] == PENDING
    ft = detail["fulltext"]
    assert ft["document"]["key"] == FDA_KEY and ft["document"]["full_text_stored"] is False and ft["document"]["licence_basis"]
    assert ft["table"]["label"] == "FDA Table 23" and ft["table"]["page_start"] == 135 and ft["table"]["printed_page"] == "112"
    assert ft["table"]["footnotes"] and ft["table"]["header_rows"] and ft["table"]["body_rows"]
    r, c = ft["used_cells"][0]
    assert ft["table"]["body_rows"][r]["cells"][str(c)] == "-5.3 (-6.1, -4.5)"
    assert ft["extraction"]["parse_method"] == pdf.PARSE_METHOD and ft["extraction"]["quality"]["arm_match"]["status"] == "UNIQUE"
    listed = client.get("/api/review/evidence", params={"origin": FULLTEXT, "limit": 200}).json()
    assert listed["total"] == 65 and all(r["verification_status"] == PENDING for r in listed["rows"])


# --------------------------------------------------------------------------- the step on its own


def _step(db, tmp_path: Path, documents: list[dict], files: dict[str, bytes], manifest: dict | None = None) -> FullTextStep:
    curated = tmp_path / "curated"
    fixtures = tmp_path / "fixtures"
    curated.mkdir()
    (curated / "fulltext.yaml").write_text(yaml.safe_dump({"documents": documents}, allow_unicode=True), encoding="utf-8")
    for key, content in files.items():
        target = fixtures / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if manifest is not None:
        (fixtures / "fulltext").mkdir(parents=True, exist_ok=True)
        (fixtures / "fulltext" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return FullTextStep(db, FixtureTransport(fixtures), curated_dir=curated, fixture_dir=fixtures)


def _search(pmcid: str, licence: str) -> bytes:
    return json.dumps({"resultList": {"result": [{"pmcid": pmcid, "pmid": "1", "doi": "10.0/x", "title": "A small test article", "isOpenAccess": "Y",
                                                  "license": licence, "inEPMC": "Y"}]}}).encode()


def test_nc_nd_full_text_is_never_stored_but_its_cells_are(db, tmp_path):
    pmcid = "PMC0000001"
    doc = {"key": f"pmc:{pmcid}", "kind": "JATS", "pmcid": pmcid, "tier": "PEER_REVIEWED", "study": "NCT05822830",
           "tables": [{"label": "Table 1", "expect": "Outcomes at Week 72"}]}
    step = _step(db, tmp_path, [doc], {f"europepmc/search/{pmcid}.json": _search(pmcid, "cc by-nc-nd"),
                                         f"europepmc/fulltext/{pmcid}.xml": _small_jats("https://creativecommons.org/licenses/by-nc-nd/4.0/")})
    step.run()
    document = db.scalar(select(FullTextDocument).where(FullTextDocument.key == f"pmc:{pmcid}"))
    assert document.licence == "cc by-nc-nd" and not document.full_text_storable and not document.full_text_stored
    assert document.content_hash  # the hash of what was read is kept
    snapshot = db.scalar(select(SourceSnapshot).where(SourceSnapshot.source_id == document.source_id))
    assert snapshot.content is None and snapshot.source.stores_full_text is False
    table = db.scalar(select(ExtractedTable).where(ExtractedTable.document_id == document.id))
    assert table.body_rows and table.label == "Table 1"  # the extracted cells are kept
    assert "LICENCE_RESTRICTS_STORAGE" in {lim["code"] for lim in document.limitations}


def test_cc_by_full_text_is_stored(db, tmp_path):
    pmcid = "PMC0000001"
    doc = {"key": f"pmc:{pmcid}", "kind": "JATS", "pmcid": pmcid, "tier": "PEER_REVIEWED", "study": "NCT05822830", "tables": [{"label": "Table 1"}]}
    step = _step(db, tmp_path, [doc], {f"europepmc/search/{pmcid}.json": _search(pmcid, "cc by"),
                                         f"europepmc/fulltext/{pmcid}.xml": _small_jats("https://creativecommons.org/licenses/by/4.0/")})
    step.run()
    document = db.scalar(select(FullTextDocument).where(FullTextDocument.key == f"pmc:{pmcid}"))
    assert document.full_text_stored
    assert db.scalar(select(SourceSnapshot.content).where(SourceSnapshot.source_id == document.source_id)).startswith("<?xml")


def test_licence_disagreement_applies_the_stricter_reading(db, tmp_path):
    pmcid = "PMC0000001"
    doc = {"key": f"pmc:{pmcid}", "kind": "JATS", "pmcid": pmcid, "tier": "PEER_REVIEWED", "study": "NCT05822830", "tables": [{"label": "Table 1"}]}
    step = _step(db, tmp_path, [doc], {f"europepmc/search/{pmcid}.json": _search(pmcid, "cc by"),
                                         f"europepmc/fulltext/{pmcid}.xml": _small_jats("https://creativecommons.org/licenses/by-nc/4.0/")})
    step.run()
    document = db.scalar(select(FullTextDocument).where(FullTextDocument.key == f"pmc:{pmcid}"))
    assert not document.full_text_stored and "LICENCE_DISAGREEMENT" in {lim["code"] for lim in document.limitations}


def test_recording_never_writes_a_full_text_that_may_not_be_stored(db, tmp_path):
    pmcid = "PMC0000001"
    recorded: dict[str, bytes] = {}

    class Live:
        mode = "LIVE"

        def get(self, service, path, params=None, *, fixture_key=None):
            content = _search(pmcid, "cc by-nc-nd") if path == "/search" else _small_jats("https://creativecommons.org/licenses/by-nc-nd/4.0/")
            if fixture_key:
                recorded[fixture_key] = content
            return content

        def record(self, key, content):
            recorded[key] = content

    curated = tmp_path / "curated"
    curated.mkdir()
    doc = {"key": f"pmc:{pmcid}", "kind": "JATS", "pmcid": pmcid, "tier": "PEER_REVIEWED", "study": "NCT05822830", "tables": [{"label": "Table 1"}]}
    (curated / "fulltext.yaml").write_text(yaml.safe_dump({"documents": [doc]}), encoding="utf-8")
    FullTextStep(db, Live(), curated_dir=curated, fixture_dir=tmp_path / "fx", record=True).run()
    assert f"europepmc/search/{pmcid}.json" in recorded
    assert f"europepmc/fulltext/{pmcid}.xml" not in recorded
    manifest = json.loads(recorded["fulltext/manifest.json"])
    assert manifest["resources"][f"europepmc/fulltext/{pmcid}.xml"]["recorded"] is False


def test_unparseable_documents_record_a_limitation_and_produce_no_rows(db, tmp_path):
    pmcid = "PMC0000002"
    docs = [
        {"key": f"pmc:{pmcid}", "kind": "JATS", "pmcid": pmcid, "tier": "PEER_REVIEWED", "study": "NCT05822830", "tables": [{"label": "Table 1"}]},
        {"key": "fda:broken", "kind": "PDF", "title": "A broken PDF", "service": "fdadocs", "path": "/broken.pdf", "fixture": "fdadocs/broken.pdf",
         "tier": "REGULATORY_REVIEW", "pages": [1], "tables": [{"label": "FDA Table 1", "study": "NCT05869903"}]},
    ]
    garbage = b"%PDF-1.4 this is not a PDF body"
    manifest = {"documents": {"fda:broken": {"fixture_sha256": hashlib.sha256(garbage).hexdigest(), "excerpt_pages": [1], "sha256": "0" * 64,
                                             "bytes": len(garbage), "page_count": 1, "retrieved_at": "2026-11-09T00:00:00+00:00"}}, "resources": {}}
    step = _step(db, tmp_path, docs, {f"europepmc/search/{pmcid}.json": _search(pmcid, "cc by"),
                                       f"europepmc/fulltext/{pmcid}.xml": b"<article><body><p>unterminated", "fdadocs/broken.pdf": garbage}, manifest)
    stats = step.run()
    for key in (f"pmc:{pmcid}", "fda:broken"):
        document = db.scalar(select(FullTextDocument).where(FullTextDocument.key == key))
        assert document.parse_status == "FAILED" and "UNPARSEABLE" in {lim["code"] for lim in document.limitations}, key
        assert not db.scalars(select(FullTextExtraction).where(FullTextExtraction.document_id == document.id)).all()
        assert db.scalar(select(ReviewQueueItem).where(ReviewQueueItem.item_type == "FULLTEXT_LIMITATION", ReviewQueueItem.payload["document"].as_string() == key)) is not None
        assert stats["documents"][key] == {**stats["documents"][key], "result": 0, "analysis": 0, "adverse_event": 0, "claim": 0}
    with rejected_visible(db):
        for model in (OutcomeResult, StatisticalAnalysis, AdverseEvent, EvidenceClaim):
            assert not db.scalars(select(model).where(model.origin == FULLTEXT)).all()


def test_a_missing_fixture_is_a_recorded_limitation(db, tmp_path):
    doc = {"key": "fda:absent", "kind": "PDF", "title": "Absent", "service": "fdadocs", "path": "/absent.pdf", "fixture": "fdadocs/absent.pdf",
           "tier": "REGULATORY_REVIEW", "pages": [1], "tables": [{"label": "FDA Table 1", "study": "NCT05869903"}]}
    _step(db, tmp_path, [doc], {}).run()
    document = db.scalar(select(FullTextDocument).where(FullTextDocument.key == "fda:absent"))
    assert document.fetch_status == "FAILED" and "NOT_FETCHED" in {lim["code"] for lim in document.limitations}


def test_extraction_is_idempotent_and_keys_are_stable_across_rebuilds(db):
    settings = get_settings()

    def snapshot() -> dict[tuple[str, str], str]:
        db.expire_all()
        return {(etype, row.evidence_key): row.fingerprint for etype, row in _fulltext_rows(db)}

    before = snapshot()
    assert len(before) == 65
    for _ in range(2):
        FullTextStep(db, FixtureTransport(settings.fixture_dir), curated_dir=settings.curated_dir, fixture_dir=settings.fixture_dir).run()
        outcome = reconcile(db).summary()
        # The rebuilt rows get their keys written again, but no key, version or content is new.
        assert (outcome["changed"], outcome["new_versions"], outcome["first_seen_keys"], outcome["disappeared_keys"]) == (0, 0, 0, 0)
        assert snapshot() == before
    assert reconcile(db).summary()["rows_updated"] == 0


def test_checks_flag_an_ambiguous_column(db):
    with rejected_visible(db):
        row = db.scalar(select(AdverseEvent).where(AdverseEvent.origin == FULLTEXT, AdverseEvent.n_affected == 3))
    quality = dict(row.fulltext_extraction.quality)
    quality["arm_match"] = {**quality["arm_match"], "status": "AMBIGUOUS", "candidates": ["Orforglipron A", "Orforglipron B"]}
    row.fulltext_extraction.quality = quality
    db.flush()
    codes = {f.code for f in checks.evaluate(db, "adverse_event", row)}
    assert "FULLTEXT_ARM_AMBIGUOUS" in codes
    row.fulltext_extraction_id = None
    db.flush()
    assert "FULLTEXT_LINK_MISSING" in {f.code for f in checks.evaluate(db, "adverse_event", row)}
