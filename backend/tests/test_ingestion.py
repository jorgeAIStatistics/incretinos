"""Ingestion: transport resilience, registry parsing, snapshots, change detection, reconciliation."""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from sqlalchemy import func, select

from incretinos.db import Base, make_engine
from incretinos.engines.estimands import classify_estimand_text
from incretinos.enums import DiabetesStatus, EstimandBasis, EstimandLabel
from incretinos.ingestion.ctgov import CTGovAdapter, canonical_study_hash, infer_diabetes_status, parse_study, split_eligibility, validate_nct
from incretinos.ingestion.ctgov_diff import diff_studies, overall_significance
from incretinos.ingestion.pipeline import Pipeline, make_synthetic_prior_version
from incretinos.ingestion.pubmed import parse_efetch_xml
from incretinos.ingestion.snapshots import quote_in_text, record_snapshot, sha256_text
from incretinos.ingestion.transport import FixtureMissing, FixtureTransport, LiveTransport, RateLimiter, ResourceNotFound, TransportError, backoff_delay
from incretinos.models import ChangeEvent, EvidenceSource, IngestionRun, OutcomeResult, ReviewQueueItem, SourceSnapshot, Study, StudyPublication
from incretinos.vocab import categorize_endpoint, parse_timeframe, to_float

from .conftest import FIXTURES


def _raw(nct: str) -> dict:
    return json.loads((FIXTURES / "ctgov" / "studies" / f"{nct}.json").read_text())


# --------------------------------------------------------------------------- transport


def _live(handler, **kwargs) -> tuple[LiveTransport, list[float]]:
    sleeps: list[float] = []
    transport = LiveTransport(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append, rate_limits={}, **kwargs)
    return transport, sleeps


def test_retries_with_backoff_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    transport, sleeps = _live(handler, max_retries=5)
    assert json.loads(transport.get("ctgov", "/studies/NCT00000001")) == {"ok": True}
    assert calls["n"] == 3 and len(sleeps) == 2 and sleeps[1] >= sleeps[0] * 0.5


def test_rate_limit_response_honours_retry_after():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "7"}) if calls["n"] == 1 else httpx.Response(200, content=b"{}")

    transport, sleeps = _live(handler)
    transport.get("openfda", "/drug/label.json")
    assert sleeps == [7.0]


def test_gives_up_after_max_retries_and_reports_failure():
    transport, sleeps = _live(lambda request: httpx.Response(500), max_retries=2)
    with pytest.raises(TransportError) as err:
        transport.get("eutils", "/efetch.fcgi")
    assert "after 3 attempts" in str(err.value) and len(sleeps) == 2


def test_network_error_is_retried_and_surfaces():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    transport, _ = _live(handler, max_retries=1)
    with pytest.raises(TransportError):
        transport.get("ctgov", "/studies/NCT00000001")


def test_404_is_definitive_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    transport, sleeps = _live(handler)
    with pytest.raises(ResourceNotFound):
        transport.get("openfda", "/drug/label.json")
    assert calls["n"] == 1 and sleeps == []


def test_non_retryable_client_error_fails_immediately():
    transport, sleeps = _live(lambda request: httpx.Response(400))
    with pytest.raises(TransportError):
        transport.get("ctgov", "/studies")
    assert sleeps == []


def test_rate_limiter_spaces_requests():
    now = {"t": 0.0}
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now["t"] += seconds

    limiter = RateLimiter(2.0, clock=lambda: now["t"], sleep=sleep)
    for _ in range(3):
        limiter.acquire()
    assert waits == [0.5, 0.5]


def test_backoff_grows_and_is_capped():
    delays = [backoff_delay(i, jitter=lambda: 1.0) for i in range(10)]
    assert delays[0] == 0.5 and delays[1] == 1.0 and delays[2] == 2.0 and max(delays) == 30.0


def test_fixture_transport_never_guesses(tmp_path):
    transport = FixtureTransport(tmp_path)
    with pytest.raises(FixtureMissing):
        transport.get("ctgov", "/studies/NCT00000001", fixture_key="ctgov/studies/NCT00000001.json")
    (tmp_path / "x.json.404").write_text("recorded 404")
    with pytest.raises(ResourceNotFound):
        transport.get("openfda", "/drug/label.json", fixture_key="x.json")


# --------------------------------------------------------------------------- parsing helpers


def test_nct_validation_rejects_malformed_identifiers():
    assert validate_nct(" nct04184622 ") == "NCT04184622"
    for bad in ("NCT123", "04184622", "NCT04184622; DROP TABLE study", ""):
        with pytest.raises(ValueError):
            validate_nct(bad)


def test_missing_numbers_stay_missing():
    assert to_float("NA") is None and to_float("") is None and to_float(None) is None
    assert to_float("-22.5") == -22.5 and to_float("1,234") == 1234.0


def test_timeframe_parsing_keeps_baseline_definition():
    assert parse_timeframe("Baseline, Week 72") == (72.0, "Baseline")
    weeks, baseline = parse_timeframe("Randomization (Week 36), Week 88")
    assert weeks == 88.0 and baseline == "Randomization (Week 36)"
    assert parse_timeframe("Baseline, 36 Months")[0] == pytest.approx(156.0)
    assert parse_timeframe(None) == (None, None)


def test_endpoint_categorisation():
    assert categorize_endpoint("Percent Change From Baseline in Body Weight")[0].value == "WEIGHT_PCT_CHANGE"
    assert categorize_endpoint("Percentage of Participants Who Achieve ≥15% Body Weight Reduction") == (categorize_endpoint("x ≥15% body weight reduction")[0], 15.0)
    assert categorize_endpoint("Percentage of Participants Who Maintain ≥80% of the Body Weight Lost")[0].value == "WEIGHT_RESPONDER"
    assert categorize_endpoint("Percent change in lean mass by DXA")[0].value == "BODY_COMPOSITION"
    assert categorize_endpoint("Time to First Occurrence of MACE-4")[0].value == "MACE"


def test_eligibility_split():
    inclusion, exclusion = split_eligibility("Inclusion Criteria:\n\n* BMI ≥30\n* Adults\n\nExclusion Criteria:\n\n* Diabetes mellitus\n* History of pancreatitis")
    assert inclusion == ["BMI ≥30", "Adults"] and exclusion == ["Diabetes mellitus", "History of pancreatitis"]


@pytest.mark.parametrize(
    "text,label,basis",
    [
        ("All participants ... excluding data after prematurely stopping study drug.", EstimandLabel.EFFICACY, EstimandBasis.INFERRED_FROM_ANALYSIS_DESCRIPTION),
        ("Data points up to the earliest date of discontinuation of study drug were included.", EstimandLabel.EFFICACY, EstimandBasis.INFERRED_FROM_ANALYSIS_DESCRIPTION),
        ("All randomized participants regardless of treatment discontinuation.", EstimandLabel.TREATMENT_REGIMEN, EstimandBasis.INFERRED_FROM_ANALYSIS_DESCRIPTION),
        ("Results are for the treatment-regimen estimand.", EstimandLabel.TREATMENT_REGIMEN, EstimandBasis.STATED_BY_SOURCE),
        ("All participants who received at least one dose and had evaluable data.", EstimandLabel.NOT_STATED, EstimandBasis.NOT_DETERMINABLE),
        ("", EstimandLabel.NOT_STATED, EstimandBasis.NOT_DETERMINABLE),
    ],
)
def test_estimand_classification(text, label, basis):
    result = classify_estimand_text(text)
    assert (result.label, result.basis) == (label, basis)


def test_estimand_text_matching_both_strategies_is_not_classified():
    text = "Excluding data after discontinuation of study drug ... regardless of adherence"
    assert classify_estimand_text(text).label == EstimandLabel.NOT_STATED


# --------------------------------------------------------------------------- registry record


def test_parse_surmount1_record():
    parsed = parse_study(_raw("NCT04184622"))
    assert parsed.acronym == "SURMOUNT-1" and parsed.phase == "PHASE3" and parsed.enrollment == 2539 and parsed.has_results
    assert [a["label"] for a in parsed.arms][:2] == ["Placebo", "5 mg Tirzepatide"]
    assert "Diabetes mellitus" in parsed.exclusion
    assert any(r.get("pmid") == "35658024" for r in parsed.references)
    assert infer_diabetes_status(parsed)[0] == DiabetesStatus.NO_T2D


def test_population_inference_from_registry_text():
    assert infer_diabetes_status(parse_study(_raw("NCT05869903")))[0] == DiabetesStatus.NO_T2D  # ATTAIN-1 excludes T1D and T2D
    assert infer_diabetes_status(parse_study(_raw("NCT05872620")))[0] == DiabetesStatus.T2D  # ATTAIN-2
    assert infer_diabetes_status(parse_study(_raw("NCT06383390")))[0] == DiabetesStatus.MIXED  # TRIUMPH-Outcomes
    assert infer_diabetes_status(parse_study(_raw("NCT05882045")))[0] == DiabetesStatus.NOT_STATED  # TRIUMPH-3: only T1D excluded


def test_hash_ignores_registry_data_version_stamp():
    raw = _raw("NCT04184622")
    other = copy.deepcopy(raw)
    other["derivedSection"]["miscInfoModule"] = {"versionHolder": "2099-01-01"}
    assert canonical_study_hash(raw) == canonical_study_hash(other)
    other["protocolSection"]["statusModule"]["overallStatus"] = "TERMINATED"
    assert canonical_study_hash(raw) != canonical_study_hash(other)


def test_registry_results_extracted_exactly_as_posted(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT04184622"))
    primary = [r for r in study.results if r.origin == "REGISTRY" and r.endpoint.role == "PRIMARY" and r.endpoint.category == "WEIGHT_PCT_CHANGE"]
    values = {r.arm_label: (r.value, r.dispersion_value, r.n_analyzed) for r in primary}
    assert values == {"Placebo": (-2.4, 0.40, 635), "5 mg Tirzepatide": (-16.0, 0.39, 623), "10 mg Tirzepatide": (-21.4, 0.39, 629), "15 mg Tirzepatide": (-22.5, 0.39, 625)}
    row = primary[0]
    assert row.ci_low is None and row.ci_high is None and "confidence_interval" in row.missing_fields  # SE posted, CI not: stays missing
    assert row.verification_status == "MACHINE_EXTRACTED" and row.tier == "REGISTRY_RESULTS"
    assert row.source_location.startswith("resultsSection.outcomeMeasuresModule.outcomeMeasures[0]")
    assert row.estimand.label == "EFFICACY" and row.estimand.basis == "INFERRED_FROM_ANALYSIS_DESCRIPTION"
    analysis = next(a for a in study.analyses if a.origin == "REGISTRY" and a.endpoint_id == row.endpoint_id and a.treatment_label == "5 mg Tirzepatide")
    assert (analysis.estimate, analysis.ci_low, analysis.ci_high, analysis.ci_level, analysis.p_value_text) == (-13.5, -14.6, -12.5, 95.0, "<0.001")
    assert analysis.comparator_label == "Placebo" and analysis.hypothesis_type == "SUPERIORITY"
    # Every stored between-arm analysis for the primary outcome equals the registry payload, value for value.
    posted = _raw("NCT04184622")["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]["analyses"]
    stored = sorted((a.estimate, a.ci_low, a.ci_high) for a in study.analyses if a.origin == "REGISTRY" and a.endpoint_id == row.endpoint_id)
    assert stored == sorted((float(x["paramValue"]), float(x["ciLowerLimit"]), float(x["ciUpperLimit"])) for x in posted)


def test_registry_reports_no_hazard_ratio_for_surpass_cvot_and_nothing_is_invented(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT04255433"))
    registry_mace = [r for r in study.results if r.origin == "REGISTRY" and r.endpoint.category == "MACE" and r.endpoint.role == "PRIMARY"]
    assert {r.value for r in registry_mace} == {801.0, 862.0}
    assert not [a for a in study.analyses if a.origin == "REGISTRY" and a.endpoint.category == "MACE"]


def test_study_withdrawal_is_stored_separately_from_treatment_discontinuation(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT04184622"))
    kinds = {ae.measure_kind for ae in study.adverse_events}
    assert {"STUDY_WITHDRAWAL_DUE_TO_AE", "TREATMENT_DISCONTINUATION_DUE_TO_AE", "PARTICIPANTS_WITH_EVENT", "DEATHS"} <= kinds
    registry_withdrawal = next(ae for ae in study.adverse_events if ae.measure_kind == "STUDY_WITHDRAWAL_DUE_TO_AE" and ae.arm_label == "15 mg Tirzepatide")
    assert (registry_withdrawal.n_affected, registry_withdrawal.n_at_risk) == (6, 630)


# --------------------------------------------------------------------------- snapshots & changes


@pytest.fixture()
def scratch_session(tmp_path):
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'scratch.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def test_reingesting_unchanged_record_adds_no_snapshot(scratch_session):
    adapter = CTGovAdapter(FixtureTransport(FIXTURES))
    first = adapter.ingest_study(scratch_session, "NCT05822830")
    n_results = scratch_session.scalar(select(func.count()).select_from(OutcomeResult))
    second = adapter.ingest_study(scratch_session, "NCT05822830")
    assert first.outcome.is_new_source and not second.outcome.changed
    assert scratch_session.scalar(select(func.count()).select_from(SourceSnapshot)) == 1
    assert scratch_session.scalar(select(func.count()).select_from(Study)) == 1
    assert scratch_session.scalar(select(func.count()).select_from(OutcomeResult)) == n_results  # no duplicates


def test_updated_record_produces_reviewable_change_summary(scratch_session):
    adapter = CTGovAdapter(FixtureTransport(FIXTURES))
    real = _raw("NCT07321886")
    prior = make_synthetic_prior_version(real)
    adapter.ingest_study(scratch_session, "NCT07321886", raw_override=json.dumps(prior).encode(), is_synthetic=True)
    result = adapter.ingest_study(scratch_session, "NCT07321886")
    assert result.outcome.changed and result.previous_parsed is not None
    changes = diff_studies(result.previous_parsed, result.parsed)
    fields = {c.field for c in changes}
    assert {"overall_status", "enrollment", "secondary_outcomes.added", "exclusion.added"} <= fields
    assert overall_significance(changes) == "HIGH"
    status = next(c for c in changes if c.field == "overall_status")
    assert (status.before, status.after) == ("NOT_YET_RECRUITING", real["protocolSection"]["statusModule"]["overallStatus"])
    assert scratch_session.scalar(select(func.count()).select_from(SourceSnapshot)) == 2


def test_seeded_demo_change_is_flagged_synthetic_and_queued(db):
    event = db.scalar(select(ChangeEvent))
    assert event is not None and event.is_synthetic and event.significance == "HIGH" and event.review_status == "OPEN"
    assert db.scalar(select(ReviewQueueItem).where(ReviewQueueItem.item_type == "SOURCE_CHANGE")) is not None
    study = db.scalar(select(Study).where(Study.nct_id == "NCT07321886"))
    assert study.overall_status == "RECRUITING"  # current data reflect the real record, not the synthetic prior


def test_snapshot_dedup_and_restricted_data_refused(scratch_session):
    kwargs = dict(key="web:test", source_type="WEB", tier="PRESS_RELEASE", title="t", content=None)
    a = record_snapshot(scratch_session, content_hash=sha256_text("one"), **kwargs)
    b = record_snapshot(scratch_session, content_hash=sha256_text("one"), **kwargs)
    c = record_snapshot(scratch_session, content_hash=sha256_text("two"), **kwargs)
    assert a.is_new_source and not b.changed and c.changed and c.snapshot.version_no == 2
    with pytest.raises(PermissionError):
        record_snapshot(scratch_session, content_hash="x", classification="RESTRICTED", **{**kwargs, "key": "restricted:1"})


def test_quote_matching_is_whitespace_and_typography_tolerant():
    text = "weight change was −20.9% (95% CI, −21.9 to −19.9) with 15 mg"
    assert quote_in_text("-20.9% (95% CI, -21.9 to -19.9) with 15 mg", text)
    assert quote_in_text("Elora 9 mg + TZP 15 mg | -23.3 %", "Elora 9 mg + TZP 15 mg -23.3 % -54.1 lbs")
    assert not quote_in_text("-21.9%", "weight change was -20.9%")


# --------------------------------------------------------------------------- reconciliation


PUBMED_XML = b"""<?xml version="1.0"?><PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>99999999</PMID>
<Article><Journal><Title>Test Journal</Title><JournalIssue><PubDate><Year>2025</Year><Month>Sep</Month></PubDate></JournalIssue></Journal>
<ArticleTitle>A trial of something</ArticleTitle><Abstract><AbstractText Label="RESULTS">Weight change was -12.4% (NCT05869903).</AbstractText></Abstract>
<AuthorList><Author><LastName>Doe</LastName><Initials>J</Initials></Author></AuthorList>
<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
<DataBankList><DataBank><DataBankName>ClinicalTrials.gov</DataBankName><AccessionNumberList><AccessionNumber>NCT05869903</AccessionNumber></AccessionNumberList></DataBank></DataBankList>
</Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1000/TEST.1</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"""


def test_pubmed_record_parsing_extracts_identifiers():
    article = parse_efetch_xml(PUBMED_XML)[0]
    assert (article.pmid, article.doi, article.year, article.journal) == ("99999999", "10.1000/test.1", 2025, "Test Journal")
    assert article.databank_ncts == ["NCT05869903"] and article.abstract_ncts == ["NCT05869903"]
    assert article.abstract_sha256 and "abstract" not in article.to_fixture()  # abstract text is never written to fixtures


def test_xml_entity_expansion_is_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><PubmedArticleSet>&b;</PubmedArticleSet>'
    with pytest.raises(Exception):  # noqa: B017 - defusedxml raises its own exception types
        parse_efetch_xml(bomb)


def test_primary_publication_linked_by_independent_identifiers(db):
    study = db.scalar(select(Study).where(Study.nct_id == "NCT04184622"))
    link = next(link for link in study.publication_links if link.publication.pmid == "35658024")
    assert link.relation == "PRIMARY_RESULTS" and link.confidence == "HIGH"
    assert {"REGISTRY_REFERENCE", "PUBMED_DATABANK"} <= set(link.bases)
    assert link.publication.doi and link.publication.journal.startswith("The New England journal")
    assert link.publication.abstract_sha256 and len(link.publication.abstract_sha256) == 64
    assert db.scalar(select(func.count()).select_from(StudyPublication).where(StudyPublication.study_id == study.id, StudyPublication.publication_id == link.publication_id)) == 1


def test_committed_pubmed_fixtures_contain_no_abstract_text():
    for path in list((FIXTURES / "pubmed" / "articles").glob("*.json"))[:40]:
        data = json.loads(path.read_text())
        assert "abstract" not in data and "abstract_sha256" in data


# --------------------------------------------------------------------------- failure handling


class _FailingTransport(FixtureTransport):
    """Serves fixtures except for one study, which fails as an exhausted live request would."""

    def get(self, service, path, params=None, *, fixture_key=None):
        if fixture_key and "NCT05822830" in fixture_key:
            raise TransportError("ctgov /studies/NCT05822830 failed after 6 attempts (HTTP 503)", service="ctgov")
        return super().get(service, path, params, fixture_key=fixture_key)


def test_api_failure_is_recorded_and_does_not_invent_data(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from incretinos.config import get_settings

    engine = make_engine(f"sqlite:///{tmp_path / 'fail.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    pipeline = Pipeline(session, get_settings(), transport=_FailingTransport(FIXTURES))
    report = pipeline.run(only=["NCT05822830", "NCT04184622"], skip={"faers", "discover"})
    assert any(f["target"] == "ctgov:NCT05822830" for f in report.failures)
    assert session.scalar(select(Study).where(Study.nct_id == "NCT05822830")) is None  # nothing fabricated for the failed study
    assert session.scalar(select(Study).where(Study.nct_id == "NCT04184622")) is not None  # the rest of the run completed
    assert session.scalar(select(ReviewQueueItem).where(ReviewQueueItem.item_type == "INGEST_FAILURE")) is not None
    assert session.scalar(select(IngestionRun).where(IngestionRun.adapter == "ctgov")) is not None
    session.close()


def test_seed_report_has_no_failures(seeded):
    assert seeded["failures"] == [] and len(seeded["studies"]) >= 45 and seeded["mode"] == "OFFLINE"


def test_every_source_has_hash_and_retrieval_time(db):
    for source in db.scalars(select(EvidenceSource)):
        assert source.content_hash and source.retrieved_at and source.snapshots, source.key
