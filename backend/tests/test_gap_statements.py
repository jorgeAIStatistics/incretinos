"""Gap rules must state only what the stored evidence supports (audit XS-06, XS-07, XS-13)."""

from __future__ import annotations

from sqlalchemy import select

from incretinos.enums import GapNature
from incretinos.models import EvidenceGap, StatisticalAnalysis, Study


def _gap(db, key):
    return db.scalar(select(EvidenceGap).where(EvidenceGap.key == key))


def test_a_completed_study_with_a_linked_publication_is_not_called_unreported(db):
    for nct in ("NCT06192108", "NCT06584916"):  # ACHIEVE-2, ATTAIN-MAINTAIN
        gap = _gap(db, f"orforglipron:unreported:{nct}")
        assert gap is not None
        assert gap.gap_nature == GapNature.NOT_YET_EXTRACTED.value
        assert "PMID" in gap.available_evidence
        assert "not evidence that results are unpublished" in gap.missing_evidence


def test_withdrawal_gap_names_the_linked_publication(db):
    gap = _gap(db, "orforglipron:treatment-withdrawal")
    assert gap is not None and gap.gap_nature == GapNature.NOT_YET_EXTRACTED.value
    assert "PMID 42120723" in gap.available_evidence
    assert "without results" not in gap.available_evidence


def test_superiority_is_recorded_only_where_the_quote_states_it_and_then_clears_the_cv_gap(db):
    rewind = db.scalar(select(Study).where(Study.nct_id == "NCT01394952"))
    rows = {a.source.key: a for a in db.scalars(select(StatisticalAnalysis).where(StatisticalAnalysis.study_id == rewind.id)) if a.endpoint.category == "MACE"}
    label = rows["openfda-label:trulicity"]
    assert label.superiority_met == "YES" and "significantly reduced" in label.locator_quote
    lancet = rows["pubmed:31189511"]  # interval and p-value only: never read as the conclusion
    assert lancet.superiority_met == "NOT_STATED"
    assert _gap(db, "dulaglutide:cv-benefit-vs-safety") is None


def test_long_term_evidence_counts_only_controlled_assessments(db):
    gap = _gap(db, "retatrutide:long-term-safety")
    assert gap is not None
    assert "Longest controlled assessment among tracked studies: week 80" in gap.available_evidence
    assert "without a concurrent control arm" in gap.available_evidence and "week 104" in gap.available_evidence


def test_comparator_gap_describes_dose_comparison_trials_truthfully(db):
    gap = _gap(db, "dulaglutide:direct-comparative")
    assert gap is not None
    assert "Only placebo-controlled" not in gap.available_evidence
    assert "AWARD-11 (dose comparison)" in gap.available_evidence
