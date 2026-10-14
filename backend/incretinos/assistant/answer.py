"""Grounded research assistant.

Pipeline: guard the question → retrieve structured evidence → compose a deterministic,
cited answer from that evidence → optionally add a model-drafted summary that is kept only
if every sentence in it is SUPPORTED by the structured facts behind the cards it cites
(``assistant.verifier``; the language model is never the authority).

Answer blocks are typed so the interface can keep them visually apart:
REPORTED_EVIDENCE (values and statements as the cited source reports them), INTERPRETATION
(rule-based reading), UNAVAILABLE (what the stored sources do not contain), HYPOTHESIS, REFUSAL
and MODEL_SUMMARY (machine-drafted prose, clearly marked).

"Reported" is deliberate. A block says what a source reports; how far the record behind it has
been checked is a separate fact, carried on every evidence block as ``verification`` (labels from
``review.policy``). A block is human-verified only if every record behind it is HUMAN_VERIFIED.
Records a reviewer rejected never become evidence cards.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engines.comparability import compare_findings, find_head_to_head_trials
from ..engines.findings import Finding, analysis_dto, build_finding, source_ref
from ..engines.gaps import NATURE_LABEL, priority
from ..engines.interpretation import classify_drug, interpret_analysis
from ..engines.safety import SPONTANEOUS_REPORT_CAVEATS
from ..models import Drug, EvidenceClaim, EvidenceGap, OutcomeResult, StatisticalAnalysis, Study
from ..review import policy
from ..vocab import DRUG_ALIASES
from .claims import Vocabulary
from .facts import (
    EvidenceFact,
    claim_text,
    drug_profile_text,
    fact_from_analysis,
    fact_from_claim,
    fact_from_drug,
    fact_from_finding,
    fact_from_result,
    fact_from_study,
    fact_from_text,
    usable_mechanisms,
    verification_summary,
)
from .providers import CardSet, LLMProvider, NullProvider, call_with_deadline, sanitize_card_text
from .verifier import verify_draft

log = logging.getLogger(__name__)

#: Block kinds that are offered to a model as evidence cards and can be cited as [C#].
CARD_KINDS = ("REPORTED_EVIDENCE", "INTERPRETATION", "UNAVAILABLE")
#: Most cards offered to a model in one request. More than this is never dropped silently: the model is told,
#: the response says so, and the verifier refuses exhaustive statements drafted from a partial set.
MAX_MODEL_CARDS = 40

KEY_CATEGORIES = {"WEIGHT_PCT_CHANGE", "HBA1C_CHANGE", "MACE", "MORTALITY", "BODY_COMPOSITION", "PHYSICAL_FUNCTION", "WEIGHT_RESPONDER"}

_RECOMMENDATION = re.compile(
    r"\bshould (i|we|my (patient|mother|father|wife|husband|son|daughter)|he|she|they|a patient|patients)\b.*\b(take|start|switch|use|stop|increase|reduce|get|prescribe)|"
    r"\bwhich (drug|one|medication|treatment) (is best|should|would you recommend)|\bwhat dose should\b|\brecommend(ed)? (dose|drug|treatment) for (me|my|a patient)|"
    r"\bis it safe for me\b|\bcan i take\b|\bbest (drug|medication|treatment) for (me|my|weight loss)\b|\bprescribe\b",
    re.IGNORECASE,
)
_CAUSAL_SPONTANEOUS = re.compile(r"\b(does|do|did|can|will)\b.{0,40}\bcause[sd]?\b|\bprove[sn]?\b.{0,30}\bcaus|\bhow (common|often|frequent)\b.{0,60}\b(faers|report)|\bincidence\b.{0,60}\b(faers|spontaneous|report)", re.IGNORECASE)


@dataclass
class Block:
    kind: str  # REPORTED_EVIDENCE | INTERPRETATION | UNAVAILABLE | HYPOTHESIS | REFUSAL | MODEL_SUMMARY
    text: str
    citations: list[int] = field(default_factory=list)  # evidence_source ids
    result_ids: list[int] = field(default_factory=list)
    card_id: str | None = None  # the [C#] marker a model summary uses for this block
    evidence: list[dict[str, Any]] = field(default_factory=list)  # records behind the block: {type, id, verification_status}
    verification: dict[str, Any] | None = None  # statuses of those records; None when no reviewable record is behind the block
    tier: str | None = None
    note: str | None = None
    fact: EvidenceFact | None = field(default=None, repr=False, compare=False)  # structured fact; not serialised with the block

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "citations": list(self.citations),
            "result_ids": list(self.result_ids),
            "card_id": self.card_id,
            "evidence": list(self.evidence),
            "verification": self.verification,
            "tier": self.tier,
            "note": self.note,
        }


@dataclass
class Answer:
    question: str
    intent: str
    blocks: list[Block]
    sources: list[dict[str, Any]]
    llm: dict[str, Any]
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    markers: dict[str, dict[str, Any]] = field(default_factory=dict)  # [C#] -> block, sources and evidence it stands for

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "intent": self.intent,
            "blocks": [block.as_dict() for block in self.blocks],
            "sources": self.sources,
            "llm": self.llm,
            "retrieved": self.retrieved,
            "markers": self.markers,
        }


# --------------------------------------------------------------------------- retrieval


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9\-\.]*", text.lower()) if len(t) > 1]


def build_cards(session: Session) -> list[dict[str, Any]]:
    """Evidence cards: one self-contained, cited statement each."""
    cards: list[dict[str, Any]] = []
    for result in session.scalars(select(OutcomeResult).join(OutcomeResult.endpoint)):
        endpoint = result.endpoint
        if endpoint.category not in KEY_CATEGORIES or (result.value is None and result.value_low is None):
            continue
        if result.origin == "REGISTRY" and endpoint.role not in ("PRIMARY",):
            continue
        if not policy.is_usable(result.verification_status):
            continue  # rejected in review: kept for audit, never used as evidence
        finding = build_finding(session, result)
        cards.append({"kind": "RESULT", "ref": result.id, "text": finding.statement, "source_id": result.source_id, "study": finding.study_name, "drug": finding.drug_slug, "tier": result.tier,
                      "verification_status": policy.canonical_status(result.verification_status)})
    for analysis in session.scalars(select(StatisticalAnalysis).join(StatisticalAnalysis.endpoint)):
        if analysis.endpoint.category not in KEY_CATEGORIES or analysis.estimate is None:
            continue
        if analysis.origin == "REGISTRY" and analysis.endpoint.role != "PRIMARY":
            continue
        if not policy.is_usable(analysis.verification_status):
            continue
        dto = analysis_dto(analysis)
        reading = interpret_analysis(dto)
        text = f"{dto['study_name']} — {dto['endpoint_title']}: {reading['observed']} Qualifiers: {'; '.join(label['text'] for label in dto['labels'])}."
        cards.append({"kind": "ANALYSIS", "ref": analysis.id, "text": text, "source_id": analysis.source_id, "study": dto["study_name"], "drug": None, "tier": analysis.tier,
                      "verification_status": policy.canonical_status(analysis.verification_status)})
    for claim in session.scalars(select(EvidenceClaim)):
        if not policy.is_usable(claim.verification_status):
            continue
        cards.append({"kind": "CLAIM", "ref": claim.id, "text": claim_text(claim), "source_id": claim.source_id, "study": claim.study.display_name if claim.study else None, "drug": claim.drug.slug if claim.drug else None,
                      "tier": claim.tier, "verification_status": policy.canonical_status(claim.verification_status)})
    for gap in session.scalars(select(EvidenceGap)):
        cards.append({"kind": "GAP", "ref": gap.id, "text": f"Open question: {gap.question} Available: {gap.available_evidence} Missing: {gap.missing_evidence}", "source_id": (gap.source_ids or [None])[0], "study": None, "drug": gap.drug.slug if gap.drug else None, "tier": "GAP",
                      "verification_status": None})
    return cards


def card_fact(session: Session, card: dict[str, Any]) -> EvidenceFact | None:
    """The structured fact behind a retrieval card."""
    if card["kind"] == "RESULT":
        result = session.get(OutcomeResult, card["ref"])
        return fact_from_result(session, result) if result is not None else None
    if card["kind"] == "ANALYSIS":
        analysis = session.get(StatisticalAnalysis, card["ref"])
        return fact_from_analysis(analysis, text=card["text"]) if analysis is not None else None
    if card["kind"] == "CLAIM":
        claim = session.get(EvidenceClaim, card["ref"])
        return fact_from_claim(claim, text=card["text"]) if claim is not None else None
    return None


def bm25_search(cards: list[dict[str, Any]], query: str, k: int = 8) -> list[dict[str, Any]]:
    docs = [_tokens(card["text"]) for card in cards]
    if not docs:
        return []
    avg_len = sum(len(d) for d in docs) / len(docs)
    df: Counter = Counter()
    for doc in docs:
        df.update(set(doc))
    q_tokens = _tokens(query)
    scored = []
    for card, doc in zip(cards, docs, strict=True):
        tf = Counter(doc)
        score = 0.0
        for token in q_tokens:
            if token not in tf:
                continue
            idf = math.log(1 + (len(docs) - df[token] + 0.5) / (df[token] + 0.5))
            score += idf * tf[token] * 2.5 / (tf[token] + 1.5 * (1 - 0.75 + 0.75 * len(doc) / avg_len))
        if score > 0:
            scored.append((score, card))
    scored.sort(key=lambda pair: -pair[0])
    return [dict(card, score=round(score, 3)) for score, card in scored[:k]]


# --------------------------------------------------------------------------- helpers


def detect_drug_slugs(session: Session, text: str) -> list[str]:
    low = text.lower()
    hits: list[tuple[int, str]] = []
    if "eloratzp" in low or ("eloralintide" in low and "tirzepatide" in low and re.search(r"combination|\+|plus|eloratzp", low)):
        hits.append((low.find("elora"), "eloralintide-tirzepatide"))
    for slug, aliases in DRUG_ALIASES.items():
        if slug == "placebo":
            continue
        positions = [low.find(a) for a in aliases if a in low]
        if positions and not (slug in ("eloralintide", "tirzepatide") and hits and hits[0][1] == "eloralintide-tirzepatide" and "eloratzp" in low):
            hits.append((min(positions), slug))
    known = {d.slug for d in session.scalars(select(Drug))}
    return [slug for _, slug in sorted(hits) if slug in known]


def detect_studies(session: Session, text: str) -> list[Study]:
    low = text.lower()
    out = []
    for study in session.scalars(select(Study)):
        name = study.display_name.lower()
        if study.nct_id.lower() in low or re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9\-])", low):
            out.append((low.find(name) if name in low else low.find(study.nct_id.lower()), study))
    return [s for _, s in sorted(out, key=lambda pair: pair[0])]


def _best_weight_result(session: Session, study: Study, estimand: str | None = None, highest_dose: bool = True) -> OutcomeResult | None:
    rows = [r for r in study.results if r.endpoint.category == "WEIGHT_PCT_CHANGE" and r.value is not None and r.scope == "ARM" and r.arm is not None and not r.arm.is_placebo and not r.arm.is_pooled and " minus " not in r.arm_label and " to " not in r.arm_label.lower()
            and policy.is_usable(r.verification_status)]
    weeks = (study.design_features or {}).get("treatment_weeks")
    if weeks:
        at_primary = [r for r in rows if r.timepoint_weeks is not None and abs(r.timepoint_weeks - weeks) < 0.5]
        rows = at_primary or rows
    if estimand:
        labelled = [r for r in rows if r.estimand is not None and r.estimand.label == estimand]
        rows = labelled or []
    if not rows:
        return None
    tier_order = {"REGULATORY_LABEL": 0, "PEER_REVIEWED": 1, "REGISTRY_RESULTS": 2, "PRESS_RELEASE": 3}
    rows.sort(key=lambda r: (tier_order.get(r.tier, 9), -(r.arm.dose_amount or 0) if highest_dose else 0, r.value))
    top_tier = rows[0].tier
    same_tier = [r for r in rows if r.tier == top_tier]
    same_tier.sort(key=lambda r: (-(r.arm.dose_amount or 0), r.value))
    return same_tier[0]


class _Composer:
    def __init__(self, session: Session):
        self.session = session
        self.blocks: list[Block] = []
        self.source_ids: list[int] = []

    def cite(self, *source_ids: int | None) -> list[int]:
        ids = list(dict.fromkeys(s for s in source_ids if s))  # one marker per source, first-cited order
        for s in ids:
            if s not in self.source_ids:
                self.source_ids.append(s)
        return ids

    def add(self, kind: str, text: str, *source_ids: int | None, result_ids: list[int] | None = None, fact: EvidenceFact | None = None) -> None:
        block = Block(kind, text, self.cite(*source_ids), result_ids or [])
        if kind in CARD_KINDS:
            block.fact = fact or fact_from_text(f"text:{len(self.blocks) + 1}", text, block.citations)
            block.evidence = [dict(record) for record in block.fact.records]
            block.verification = verification_summary(block.evidence)
            block.tier = block.fact.tier
        self.blocks.append(block)

    def finding(self, f: Finding) -> None:
        if not policy.is_usable(f.verification_status):
            return  # rejected in review
        self.add("REPORTED_EVIDENCE", f.statement, (f.source or {}).get("id"), result_ids=[f.result_id], fact=fact_from_finding(f))

    def analysis(self, analysis: StatisticalAnalysis, text: str) -> None:
        self.add("REPORTED_EVIDENCE", text, analysis.source_id, fact=fact_from_analysis(analysis, text=text))

    def claim(self, claim: EvidenceClaim, text: str) -> None:
        self.add("REPORTED_EVIDENCE", text, claim.source_id, fact=fact_from_claim(claim, text=text))

    def study_record(self, study: Study, text: str) -> None:
        self.add("REPORTED_EVIDENCE", text, study.source_id, fact=fact_from_study(study, text))

    def sources(self) -> list[dict[str, Any]]:
        from ..models import EvidenceSource

        return [source_ref(self.session.get(EvidenceSource, sid)) for sid in self.source_ids]


def _drug_profile(c: _Composer, drug: Drug) -> None:
    fact = fact_from_drug(c.session, drug, text=drug_profile_text(drug))
    c.add("REPORTED_EVIDENCE", fact.text, *fact.source_ids, fact=fact)


# --------------------------------------------------------------------------- intents


def _intent_compare_drugs(c: _Composer, slugs: list[str]) -> None:
    session = c.session
    drugs = [session.scalar(select(Drug).where(Drug.slug == s)) for s in slugs[:2]]
    picked: list[Finding] = []
    for drug in drugs:
        _drug_profile(c, drug)
        studies = [s for s in session.scalars(select(Study)) if any(link.drug_id == drug.id and link.role != "COMPARATOR" for link in s.drug_links)]
        phase3 = [s for s in studies if s.phase and "PHASE3" in s.phase and s.population and s.population.diabetes_status == "NO_T2D"]
        best = None
        for study in sorted(phase3, key=lambda s: s.display_name):
            row = _best_weight_result(session, study, "TREATMENT_REGIMEN")
            if row is not None and row.arm.drug_slug == drug.slug:
                best = row
                break
        if best is not None:
            finding = build_finding(session, best)
            picked.append(finding)
            c.finding(finding)
        else:
            c.add("UNAVAILABLE", f"No Phase 3 treatment-regimen-estimand weight result in adults without type 2 diabetes is stored for {drug.name}.")
    a, b = drugs[0], drugs[1]
    trials = find_head_to_head_trials(session, a.slug, b.slug)
    reported = [t for t in trials if t["has_results"]]
    if trials and not reported:
        for t in trials:
            study = session.scalar(select(Study).where(Study.nct_id == t["nct_id"]))
            text = f"A registered trial includes both agents: {t['display_name']} ({t['nct_id']}), {t['phase']}, status {t['overall_status']}, primary completion {t['primary_completion_date']} ({(t['primary_completion_type'] or 'type not stated').lower()}). No results are posted or published."
            if study is not None:
                c.study_record(study, text)
            else:
                c.add("REPORTED_EVIDENCE", text)
        c.add("UNAVAILABLE", f"No randomized head-to-head result comparing {a.name} with {b.name} is publicly available in the stored sources.")
    elif not trials:
        c.add("UNAVAILABLE", f"No registered trial including both {a.name} and {b.name} was found among tracked studies (a statement about tracked records, not proof that none exists).")
    if len(picked) == 2:
        comparison = compare_findings(session, picked[0], picked[1])
        c.add("INTERPRETATION", f"Comparability of the two results above: {comparison.classification} — {comparison.headline}. " + " ".join(comparison.reasons[:4]))
        c.add("INTERPRETATION", "What may not be concluded: " + " ".join(comparison.prohibited_statements[:3]))
        c.add("INTERPRETATION", "What a rigorous comparison would require: " + " ".join(comparison.requirements[:3]))
    cls_a, cls_b = (classify_drug([(m.target, m.action) for m in usable_mechanisms(d)]) for d in (a, b))
    extra = sorted(set(cls_a["agonist_targets"]) ^ set(cls_b["agonist_targets"]))
    if extra:
        c.add("HYPOTHESIS", f"The agents differ in receptor targets ({', '.join(extra)}). Whether that difference translates into different clinical outcomes is a hypothesis until a randomized comparison reports.")


def _intent_exploratory(c: _Composer, studies: list[Study], slugs: list[str]) -> None:
    session = c.session
    if not studies and slugs:
        studies = [s for s in session.scalars(select(Study)) if any(link.drug.slug in slugs for link in s.drug_links)]
    found = False
    for study in studies[:6]:
        exploratory = [a for a in study.analyses if (a.endpoint.role in ("EXPLORATORY", "POST_HOC") or a.multiplicity_controlled == "NO") and policy.is_usable(a.verification_status)]
        for analysis in exploratory[:8]:
            dto = analysis_dto(analysis)
            c.analysis(analysis, f"{study.display_name} — {dto['endpoint_title']}: {interpret_analysis(dto)['observed']} [{'; '.join(label['text'] for label in dto['labels'])}]")
            found = True
        for claim in session.scalars(select(EvidenceClaim).where(EvidenceClaim.study_id == study.id)):
            if not policy.is_usable(claim.verification_status):
                continue
            if re.search(r"exploratory|multiplicity|type 1 error|type i error|secondary endpoints", claim.statement, re.IGNORECASE):
                c.claim(claim, f"{study.display_name}: {claim.statement}")
                found = True
        unstated = [e for e in study.endpoints if e.kind != "PROTOCOL" and e.multiplicity_controlled == "NOT_STATED"]
        if unstated and not exploratory:
            c.add("UNAVAILABLE", f"{study.display_name}: the stored sources do not label any reported outcome as exploratory, and they do not state the multiplicity-control status of {len(unstated)} reported endpoint(s). Absence of an 'exploratory' label is not confirmation that an endpoint was confirmatory.", study.source_id)
            found = True
    if not found:
        c.add("UNAVAILABLE", "No stored result for the named study or drug is labelled exploratory or multiplicity-uncontrolled, and no statement on the testing hierarchy is on record.")


def _intent_gaps(c: _Composer, slugs: list[str]) -> None:
    session = c.session
    gaps = [g for g in session.scalars(select(EvidenceGap)) if g.drug is not None and (not slugs or g.drug.slug in slugs)]
    gaps.sort(key=lambda g: -priority(g)["score"])
    if not gaps:
        c.add("UNAVAILABLE", "No open evidence gaps are recorded for this drug.")
    for gap in gaps[:8]:
        c.add("INTERPRETATION", f"{gap.question} — Available: {gap.available_evidence} Missing: {gap.missing_evidence} Why it is open: {NATURE_LABEL.get(gap.gap_nature, gap.gap_nature)}", *(gap.source_ids or [])[:3])
    for slug in slugs[:1]:
        drug = session.scalar(select(Drug).where(Drug.slug == slug))
        ongoing = [s for s in session.scalars(select(Study)) if any(link.drug.slug == slug for link in s.drug_links) and s.phase and "PHASE3" in s.phase and not s.has_results and (s.overall_status or "") in ("RECRUITING", "ACTIVE_NOT_RECRUITING", "NOT_YET_RECRUITING")]
        for study in ongoing[:8]:
            primary = [e.title for e in study.endpoints if e.kind == "PROTOCOL" and e.role == "PRIMARY"][:2]
            c.study_record(study, f"Ongoing Phase 3 for {drug.name}: {study.display_name} ({study.nct_id}) — {study.brief_title}. Status {study.overall_status}; planned enrollment {study.enrollment}; primary completion {study.primary_completion_date} ({(study.primary_completion_type or '').lower()}). Registered primary endpoint(s): {'; '.join(primary) or 'not listed'}.")


def _intent_rigorous(c: _Composer, studies: list[Study]) -> None:
    session = c.session
    if len(studies) < 2:
        c.add("UNAVAILABLE", "Name two trials (for example 'SURMOUNT-1 and TRIUMPH-1') to assess what a rigorous comparison would require.")
        return
    rows = []
    for study in studies[:2]:
        row = _best_weight_result(session, study, "TREATMENT_REGIMEN") or _best_weight_result(session, study, "EFFICACY") or _best_weight_result(session, study)
        if row is None:
            c.add("UNAVAILABLE", f"No percent-weight-change result is stored for {study.display_name}.", study.source_id)
            return
        rows.append(build_finding(session, row))
    for finding in rows:
        c.finding(finding)
    comparison = compare_findings(session, rows[0], rows[1])
    c.add("INTERPRETATION", f"Classification: {comparison.classification} — {comparison.headline}. " + " ".join(comparison.reasons[:5]))
    c.add("INTERPRETATION", "Requirements for a rigorous comparison: " + " ".join(comparison.requirements))


def _intent_hypothetical(c: _Composer, studies: list[Study], slugs: list[str]) -> None:
    session = c.session
    c.add("INTERPRETATION", "Results reported under the efficacy estimand depend on hypothetical treatment continuation: they estimate the effect had every participant remained on the assigned treatment (ICH E9(R1) hypothetical strategy). Results under the treatment-regimen estimand do not.")
    pool = studies or [s for s in session.scalars(select(Study)) if not slugs or any(link.drug.slug in slugs for link in s.drug_links)]
    shown = 0
    for study in pool:
        row = _best_weight_result(session, study, "EFFICACY")
        if row is None:
            continue
        c.finding(build_finding(session, row))
        shown += 1
        if shown >= 8:
            break
    if shown == 0:
        c.add("UNAVAILABLE", "No efficacy-estimand weight result is stored for the named study or drug.")


def _intent_cvot(c: _Composer, slugs: list[str]) -> None:
    session = c.session
    any_found = False
    for study in session.scalars(select(Study).order_by(Study.program, Study.display_name)):
        feats = study.design_features or {}
        if not (feats.get("outcomes_trial") or feats.get("cv_safety_assessment")):
            continue
        drug_names = [link.drug.name for link in study.drug_links if link.role != "COMPARATOR"]
        if slugs and not any(link.drug.slug in slugs for link in study.drug_links):
            continue
        any_found = True
        kind = "dedicated outcomes trial" if feats.get("outcomes_trial") else "trial with a pre-specified cardiovascular safety assessment"
        c.study_record(study, f"{', '.join(drug_names)}: {study.display_name} ({study.nct_id}) — {kind}; status {study.overall_status}; enrollment {study.enrollment} ({(study.enrollment_type or '').lower()}); primary completion {study.primary_completion_date} ({(study.primary_completion_type or '').lower()}); registry results posted: {'yes' if study.has_results else 'no'}.")
        for analysis in [a for a in study.analyses if a.endpoint.category == "MACE" and policy.is_usable(a.verification_status)][:2]:
            dto = analysis_dto(analysis)
            reading = interpret_analysis(dto)
            c.analysis(analysis, f"{study.display_name} — {dto['endpoint_title']}: {reading['observed']}")
            for item in reading["does_not_establish"][:2]:
                c.add("INTERPRETATION", f"{study.display_name}: does not establish — {item}")
    core_without = [d for d in session.scalars(select(Drug).where(Drug.tier_group == "CORE")) if (not slugs or d.slug in slugs) and not any((s.design_features or {}).get("outcomes_trial") for s in session.scalars(select(Study)) if any(link.drug_id == d.id and link.role != "COMPARATOR" for link in s.drug_links))]
    for drug in core_without:
        c.add("UNAVAILABLE", f"No prospective cardiovascular outcomes trial of {drug.name} was found among tracked registry records (not proof that none exists).")
    if not any_found and not core_without:
        c.add("UNAVAILABLE", "No cardiovascular outcomes trial is recorded for the named drug.")


def _intent_safety_reports(c: _Composer) -> None:
    c.add("REFUSAL", "Spontaneous adverse-event reports cannot establish how often an event occurs or whether a drug causes it, so this question cannot be answered from FAERS data.")
    for caveat in SPONTANEOUS_REPORT_CAVEATS[:3]:
        c.add("INTERPRETATION", caveat)
    c.add("INTERPRETATION", "What can be provided instead: within-trial adverse-event incidence by arm (events over participants at risk), and disproportionality statistics for signal screening, each labelled with its limits.")


INTENTS = [
    ("CVOT_PROGRAMS", re.compile(r"cardiovascular outcome|cvot|outcomes trial|\bmace\b", re.IGNORECASE)),
    ("EXPLORATORY_OUTCOMES", re.compile(r"exploratory|multiplicity|not controlled|post[- ]hoc", re.IGNORECASE)),
    ("HYPOTHETICAL_ESTIMAND", re.compile(r"hypothetical|treatment continuation|efficacy estimand|remained on treatment", re.IGNORECASE)),
    ("RIGOROUS_COMPARISON", re.compile(r"(required|need|take|necessary).{0,40}compar|compare.{0,40}rigorous|rigorously", re.IGNORECASE)),
    ("UNRESOLVED_QUESTIONS", re.compile(r"unresolved|open question|still unknown|evidence gap|unanswered|remain(s|ing)? (uncertain|unknown)|phase 3 (trials|program)", re.IGNORECASE)),
    ("COMPARE_DRUGS", re.compile(r"differentiat|versus|\bvs\.?\b|compare|difference between|better than", re.IGNORECASE)),
]


# --------------------------------------------------------------------------- citation fidelity


def check_citation_fidelity(draft: str, cards: list[dict[str, Any]], vocabulary: Vocabulary | None = None, truncated: bool = False) -> dict[str, Any]:
    """Decide whether a model draft may be shown: every sentence that asserts something must be SUPPORTED.

    ``cards`` are ``{"id": "C1", "text": ..., "fact": EvidenceFact}``; a card without a ``fact`` is checked as a
    statement (its wording is the only evidence). The result keeps the earlier contract (``passed``, ``problems``,
    ``sentences_checked``) and adds the per-sentence report: sentence, structured claims, verdict and reasons.
    """
    return verify_draft(draft, cards, vocabulary=vocabulary, truncated=truncated)


def _card_qualifiers(fact: EvidenceFact) -> str:
    """Tier, verification status and role of the record behind a card, in the words a summary may use."""
    parts = []
    if fact.tier:
        parts.append(f"evidence tier: {fact.tier.lower().replace('_', ' ')}")
    summary = verification_summary(fact.records)
    if summary:
        parts.append(f"verification: {summary['label'].lower()}" + ("" if summary["human_verified"] else " (not human-verified)"))
    elif fact.kind in ("STUDY_RECORD", "DERIVED_TEXT"):
        parts.append("verification: none recorded (not human-verified)")
    if fact.endpoint_role:
        parts.append(f"endpoint role: {fact.endpoint_role.lower().replace('_', ' ')}")
    if fact.multiplicity_controlled:
        parts.append(f"multiplicity controlled: {fact.multiplicity_controlled.lower().replace('_', ' ')}")
    if fact.hypothesis_type:
        parts.append(f"hypothesis: {fact.hypothesis_type.lower().replace('_', ' ')}")
    return "; ".join(parts)


def cards_for_verification(blocks: list[Block]) -> list[dict[str, Any]]:
    """Evidence cards as the verifier checks them: id, the text a model is shown, and the structured fact behind it.

    The text is the sanitised, length-limited text of the prompt, never a longer original, so a draft cannot be
    supported by wording the model was not shown.
    """
    cards = []
    for block in blocks:
        if not block.card_id or block.fact is None:
            continue
        shown = sanitize_card_text(block.text)
        cards.append({"id": block.card_id, "text": shown, "source_ids": list(block.citations), "fact": replace(block.fact, text=shown)})
    return cards


def _model_summary(session: Session, question: str, provider: LLMProvider, composer: _Composer, markers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Ask the provider for a draft and keep it only if the deterministic verifier supports every sentence."""
    meta: dict[str, Any] = {"provider": provider.name, "used": False}
    blocks = [b for b in composer.blocks if b.card_id]
    offered = blocks[:MAX_MODEL_CARDS]
    truncated = len(offered) < len(blocks)
    meta["cards"] = {"offered": len(offered), "available": len(blocks), "truncated": truncated}
    checker_cards = cards_for_verification(offered)
    model_cards = CardSet(
        ({"id": c["id"], "text": c["text"], "source_ids": c["source_ids"], "qualifiers": _card_qualifiers(c["fact"]), "facts": c["fact"].public()} for c in checker_cards),
        omitted=len(blocks) - len(offered),
    )
    draft, error = call_with_deadline(provider, question, model_cards)
    if error:
        meta["error"] = error
        return meta
    if not draft:
        meta["reason"] = "provider returned no text"
        return meta
    try:
        check = check_citation_fidelity(draft, checker_cards, vocabulary=Vocabulary.from_session(session), truncated=truncated)
    except Exception:  # fail closed: a draft the verifier cannot read is withheld, and the deterministic answer stands
        log.exception("claim verification failed; the model draft is withheld")
        meta["rejected"] = True
        meta["error"] = "VerificationError"
        return meta
    meta["citation_check"] = check
    if not check["passed"]:
        meta["rejected"] = True
        return meta
    cited_ids = list(dict.fromkeys(re.findall(r"C\d+", " ".join(re.findall(r"\[([^\]]*)\]", draft)))))
    cited_sources = [sid for cid in cited_ids for sid in markers.get(cid, {}).get("source_ids", [])]
    note = f"Drafted from {len(offered)} of {len(blocks)} evidence cards; the remaining cards were not shown to the model." if truncated else None
    composer.blocks.append(Block("MODEL_SUMMARY", draft, composer.cite(*cited_sources), note=note))
    meta["used"] = True
    meta["cited_markers"] = cited_ids
    return meta


# --------------------------------------------------------------------------- entry point


def ask(session: Session, question: str, provider: LLMProvider | None = None) -> Answer:
    provider = provider or NullProvider()
    question = question.strip()[:1000]
    composer = _Composer(session)
    slugs = detect_drug_slugs(session, question)
    studies = detect_studies(session, question)

    if _RECOMMENDATION.search(question):
        composer.add("REFUSAL", "This platform does not give treatment recommendations or advice for individual patients. It summarises what public trial evidence reports and what remains uncertain; treatment decisions belong with a qualified clinician using the approved labeling.")
        if slugs:
            _intent_gaps(composer, slugs[:1])
        return Answer(question, "REFUSED_RECOMMENDATION", composer.blocks, composer.sources(), {"provider": provider.name, "used": False, "reason": "question refused"})
    if _CAUSAL_SPONTANEOUS.search(question) or (re.search(r"faers|spontaneous|adverse event report", question, re.IGNORECASE) and re.search(r"incidence|how many patients|rate|risk|cause", question, re.IGNORECASE)):
        _intent_safety_reports(composer)
        return Answer(question, "REFUSED_SPONTANEOUS_INFERENCE", composer.blocks, composer.sources(), {"provider": provider.name, "used": False, "reason": "unsupported inference refused"})

    intent = next((name for name, pattern in INTENTS if pattern.search(question)), "RETRIEVAL")
    if intent == "COMPARE_DRUGS" and len(slugs) < 2:
        intent = "RIGOROUS_COMPARISON" if len(studies) >= 2 else "RETRIEVAL"
    if intent == "COMPARE_DRUGS":
        _intent_compare_drugs(composer, slugs)
    elif intent == "EXPLORATORY_OUTCOMES":
        _intent_exploratory(composer, studies, slugs)
    elif intent == "UNRESOLVED_QUESTIONS":
        _intent_gaps(composer, slugs)
    elif intent == "RIGOROUS_COMPARISON":
        _intent_rigorous(composer, studies)
    elif intent == "HYPOTHETICAL_ESTIMAND":
        _intent_hypothetical(composer, studies, slugs)
    elif intent == "CVOT_PROGRAMS":
        _intent_cvot(composer, slugs)

    cards = build_cards(session)
    retrieved = bm25_search(cards, question, k=8)
    if intent == "RETRIEVAL":
        if not retrieved:
            composer.add("UNAVAILABLE", "No stored evidence matches this question. The platform answers only from ingested public sources.")
        for card in retrieved:
            composer.add("REPORTED_EVIDENCE" if card["kind"] in ("RESULT", "ANALYSIS", "CLAIM") else "INTERPRETATION", card["text"], card["source_id"], result_ids=[card["ref"]] if card["kind"] == "RESULT" else [], fact=card_fact(session, card))

    # Every block a summary may cite gets its marker, with or without a model: the reader can always resolve [C#].
    markers: dict[str, dict[str, Any]] = {}
    for index, block in enumerate(composer.blocks):
        if block.kind in CARD_KINDS:
            block.card_id = f"C{len(markers) + 1}"
            markers[block.card_id] = {
                "block": index,
                "kind": block.kind,
                "source_ids": list(block.citations),
                "evidence": [dict(record) for record in block.evidence],
                "verification": block.verification["label"] if block.verification else None,
                "human_verified": bool(block.verification and block.verification["human_verified"]),
                "fact": block.fact.fact_id if block.fact is not None else None,
            }

    if provider.name != "none":
        llm_meta = _model_summary(session, question, provider, composer, markers)
    else:
        llm_meta = {"provider": provider.name, "used": False, "reason": "no language model configured; answer composed deterministically from stored evidence"}
    return Answer(question, intent, composer.blocks, composer.sources(), llm_meta, retrieved, markers)
