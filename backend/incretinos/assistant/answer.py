"""Grounded research assistant.

Pipeline: guard the question → retrieve structured evidence → compose a deterministic,
cited answer from that evidence → optionally add a model-drafted summary that is kept only
if every number in it is traceable to a cited evidence card.

Answer blocks are typed so the interface can keep them visually apart:
VERIFIED_EVIDENCE (reported values with citations), INTERPRETATION (rule-based reading),
UNAVAILABLE (what the stored sources do not contain), HYPOTHESIS, REFUSAL and
MODEL_SUMMARY (machine-drafted prose, clearly marked).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engines.comparability import compare_findings, find_head_to_head_trials
from ..engines.findings import Finding, analysis_dto, build_finding, source_ref
from ..engines.gaps import NATURE_LABEL, priority
from ..engines.interpretation import classify_drug, guard_statement, interpret_analysis
from ..engines.safety import SPONTANEOUS_REPORT_CAVEATS
from ..models import Drug, EvidenceClaim, EvidenceGap, OutcomeResult, StatisticalAnalysis, Study
from ..vocab import DRUG_ALIASES
from .providers import LLMProvider, NullProvider

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
    kind: str  # VERIFIED_EVIDENCE | INTERPRETATION | UNAVAILABLE | HYPOTHESIS | REFUSAL | MODEL_SUMMARY
    text: str
    citations: list[int] = field(default_factory=list)  # evidence_source ids
    result_ids: list[int] = field(default_factory=list)


@dataclass
class Answer:
    question: str
    intent: str
    blocks: list[Block]
    sources: list[dict[str, Any]]
    llm: dict[str, Any]
    retrieved: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
        finding = build_finding(session, result)
        cards.append({"kind": "RESULT", "ref": result.id, "text": finding.statement, "source_id": result.source_id, "study": finding.study_name, "drug": finding.drug_slug, "tier": result.tier})
    for analysis in session.scalars(select(StatisticalAnalysis).join(StatisticalAnalysis.endpoint)):
        if analysis.endpoint.category not in KEY_CATEGORIES or analysis.estimate is None:
            continue
        if analysis.origin == "REGISTRY" and analysis.endpoint.role != "PRIMARY":
            continue
        dto = analysis_dto(analysis)
        reading = interpret_analysis(dto)
        text = f"{dto['study_name']} — {dto['endpoint_title']}: {reading['observed']} Qualifiers: {'; '.join(label['text'] for label in dto['labels'])}."
        cards.append({"kind": "ANALYSIS", "ref": analysis.id, "text": text, "source_id": analysis.source_id, "study": dto["study_name"], "drug": None, "tier": analysis.tier})
    for claim in session.scalars(select(EvidenceClaim)):
        subject = claim.drug.name if claim.drug else (claim.study.display_name if claim.study else "General")
        cards.append({"kind": "CLAIM", "ref": claim.id, "text": f"{subject}: {claim.statement}", "source_id": claim.source_id, "study": claim.study.display_name if claim.study else None, "drug": claim.drug.slug if claim.drug else None, "tier": claim.tier})
    for gap in session.scalars(select(EvidenceGap)):
        cards.append({"kind": "GAP", "ref": gap.id, "text": f"Open question: {gap.question} Available: {gap.available_evidence} Missing: {gap.missing_evidence}", "source_id": (gap.source_ids or [None])[0], "study": None, "drug": gap.drug.slug if gap.drug else None, "tier": "GAP"})
    return cards


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
    rows = [r for r in study.results if r.endpoint.category == "WEIGHT_PCT_CHANGE" and r.value is not None and r.scope == "ARM" and r.arm is not None and not r.arm.is_placebo and not r.arm.is_pooled and " minus " not in r.arm_label and " to " not in r.arm_label.lower()]
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
        ids = [s for s in source_ids if s]
        for s in ids:
            if s not in self.source_ids:
                self.source_ids.append(s)
        return ids

    def add(self, kind: str, text: str, *source_ids: int | None, result_ids: list[int] | None = None) -> None:
        self.blocks.append(Block(kind, text, self.cite(*source_ids), result_ids or []))

    def finding(self, f: Finding) -> None:
        self.add("VERIFIED_EVIDENCE", f.statement, (f.source or {}).get("id"), result_ids=[f.result_id])

    def sources(self) -> list[dict[str, Any]]:
        from ..models import EvidenceSource

        return [source_ref(self.session.get(EvidenceSource, sid)) for sid in self.source_ids]


def _drug_profile(c: _Composer, drug: Drug) -> None:
    cls = classify_drug([(m.target, m.action) for m in drug.mechanisms])
    mech_sources = [m.source_id for m in drug.mechanisms]
    c.add("VERIFIED_EVIDENCE", f"{drug.name} — pharmacologic class: {cls['class_label']} (derived from sourced receptor actions). Regulatory status: {drug.regulatory_status.lower()} as of {drug.regulatory_as_of}. {drug.regulatory_summary}", drug.regulatory_source_id, *mech_sources)


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
            c.add("VERIFIED_EVIDENCE", f"A registered trial includes both agents: {t['display_name']} ({t['nct_id']}), {t['phase']}, status {t['overall_status']}, primary completion {t['primary_completion_date']} ({(t['primary_completion_type'] or 'type not stated').lower()}). No results are posted or published.", study.source_id if study else None)
        c.add("UNAVAILABLE", f"No randomized head-to-head result comparing {a.name} with {b.name} is publicly available in the stored sources.")
    elif not trials:
        c.add("UNAVAILABLE", f"No registered trial including both {a.name} and {b.name} was found among tracked studies (a statement about tracked records, not proof that none exists).")
    if len(picked) == 2:
        comparison = compare_findings(session, picked[0], picked[1])
        c.add("INTERPRETATION", f"Comparability of the two results above: {comparison.classification} — {comparison.headline}. " + " ".join(comparison.reasons[:4]))
        c.add("INTERPRETATION", "What may not be concluded: " + " ".join(comparison.prohibited_statements[:3]))
        c.add("INTERPRETATION", "What a rigorous comparison would require: " + " ".join(comparison.requirements[:3]))
    cls_a, cls_b = (classify_drug([(m.target, m.action) for m in d.mechanisms]) for d in (a, b))
    extra = sorted(set(cls_a["agonist_targets"]) ^ set(cls_b["agonist_targets"]))
    if extra:
        c.add("HYPOTHESIS", f"The agents differ in receptor targets ({', '.join(extra)}). Whether that difference translates into different clinical outcomes is a hypothesis until a randomized comparison reports.")


def _intent_exploratory(c: _Composer, studies: list[Study], slugs: list[str]) -> None:
    session = c.session
    if not studies and slugs:
        studies = [s for s in session.scalars(select(Study)) if any(link.drug.slug in slugs for link in s.drug_links)]
    found = False
    for study in studies[:6]:
        exploratory = [a for a in study.analyses if a.endpoint.role in ("EXPLORATORY", "POST_HOC") or a.multiplicity_controlled == "NO"]
        for analysis in exploratory[:8]:
            dto = analysis_dto(analysis)
            c.add("VERIFIED_EVIDENCE", f"{study.display_name} — {dto['endpoint_title']}: {interpret_analysis(dto)['observed']} [{'; '.join(label['text'] for label in dto['labels'])}]", analysis.source_id)
            found = True
        for claim in session.scalars(select(EvidenceClaim).where(EvidenceClaim.study_id == study.id)):
            if re.search(r"exploratory|multiplicity|type 1 error|type i error|secondary endpoints", claim.statement, re.IGNORECASE):
                c.add("VERIFIED_EVIDENCE", f"{study.display_name}: {claim.statement}", claim.source_id)
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
            c.add("VERIFIED_EVIDENCE", f"Ongoing Phase 3 for {drug.name}: {study.display_name} ({study.nct_id}) — {study.brief_title}. Status {study.overall_status}; planned enrollment {study.enrollment}; primary completion {study.primary_completion_date} ({(study.primary_completion_type or '').lower()}). Registered primary endpoint(s): {'; '.join(primary) or 'not listed'}.", study.source_id)


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
        c.add("VERIFIED_EVIDENCE", f"{', '.join(drug_names)}: {study.display_name} ({study.nct_id}) — {kind}; status {study.overall_status}; enrollment {study.enrollment} ({(study.enrollment_type or '').lower()}); primary completion {study.primary_completion_date} ({(study.primary_completion_type or '').lower()}); registry results posted: {'yes' if study.has_results else 'no'}.", study.source_id)
        for analysis in [a for a in study.analyses if a.endpoint.category == "MACE"][:2]:
            dto = analysis_dto(analysis)
            reading = interpret_analysis(dto)
            c.add("VERIFIED_EVIDENCE", f"{study.display_name} — {dto['endpoint_title']}: {reading['observed']}", analysis.source_id)
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


def numbers_in(text: str) -> set[str]:
    cleaned = re.sub(r"\[C\d+\]", " ", text.replace("−", "-").replace("−", "-"))
    return {n.lstrip("-+").rstrip(".") for n in re.findall(r"[-+]?\d+(?:\.\d+)?", cleaned)}


def check_citation_fidelity(draft: str, cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Reject a model draft that states an uncited fact or a number absent from its cited cards."""
    by_id = {card["id"]: card for card in cards}
    problems: list[str] = []
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\[])", draft) if s.strip()]
    for sentence in sentences:
        cited = re.findall(r"\[(C\d+)\]", sentence)
        unknown = [cid for cid in cited if cid not in by_id]
        if unknown:
            problems.append(f"Cites unknown card(s) {unknown}: “{sentence[:120]}”")
            continue
        figures = numbers_in(sentence)
        if figures and not cited:
            problems.append(f"States a number without a citation: “{sentence[:120]}”")
            continue
        if cited:
            allowed = set().union(*(numbers_in(by_id[cid]["text"]) for cid in cited))
            missing = sorted(n for n in figures if n not in allowed)
            if missing:
                problems.append(f"Number(s) {missing} not found in the cited card(s): “{sentence[:120]}”")
    for violation in guard_statement(draft, cross_trial=True, lean_mass=True, data_family="SPONTANEOUS" if re.search(r"faers|spontaneous", draft, re.IGNORECASE) else None):
        if violation["code"] in ("CROSS_TRIAL_SUPERIORITY", "LEAN_AS_MUSCLE", "RECOMMENDATION", "SPONTANEOUS_AS_INCIDENCE", "SPONTANEOUS_AS_CAUSAL"):
            problems.append(f"{violation['message']} “{violation['sentence'][:120]}”")
    return {"passed": not problems, "problems": problems, "sentences_checked": len(sentences)}


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
            composer.add("VERIFIED_EVIDENCE" if card["kind"] in ("RESULT", "ANALYSIS", "CLAIM") else "INTERPRETATION", card["text"], card["source_id"], result_ids=[card["ref"]] if card["kind"] == "RESULT" else [])

    llm_meta: dict[str, Any] = {"provider": provider.name, "used": False}
    if provider.name != "none":
        llm_cards = [{"id": f"C{i + 1}", "text": block.text, "source_ids": block.citations} for i, block in enumerate(b for b in composer.blocks if b.kind in ("VERIFIED_EVIDENCE", "INTERPRETATION", "UNAVAILABLE"))][:14]
        try:
            draft = provider.summarize(question, llm_cards)
        except Exception as exc:  # a provider failure never breaks the deterministic answer
            draft, llm_meta["error"] = None, f"{type(exc).__name__}: {exc}"[:200]
        if draft:
            check = check_citation_fidelity(draft, llm_cards)
            llm_meta["citation_check"] = check
            if check["passed"]:
                cited = sorted({sid for cid in re.findall(r"\[(C\d+)\]", draft) for card in llm_cards if card["id"] == cid for sid in card["source_ids"]})
                composer.blocks.append(Block("MODEL_SUMMARY", draft, composer.cite(*cited)))
                llm_meta["used"] = True
            else:
                llm_meta["rejected"] = True
        elif "error" not in llm_meta:
            llm_meta["reason"] = "provider returned no text"
    else:
        llm_meta["reason"] = "no language model configured; answer composed deterministically from stored evidence"
    return Answer(question, intent, composer.blocks, composer.sources(), llm_meta, retrieved)
