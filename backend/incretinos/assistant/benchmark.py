"""Adversarial benchmark for the claim verifier.

A benchmark set is a YAML file of cases. Each case names stored evidence by its scientific description
(ids are not stable across seeds), gives one sentence citing that evidence as [C1], [C2] ... and says
whether a faithful verifier must accept it (``SUPPORTED``) or refuse it (``REJECT``). The runner builds
the same evidence facts the assistant builds, runs the same verifier, and reports

- the detection rate for unsupported sentences, overall and per category;
- the false-rejection rate on valid sentences;
- citation accuracy: valid cases whose claims are bound to a fact from a cited source;
- attribution accuracy: valid cases bound to the expected fact (source, arm and endpoint);
- benchmark errors (a selector that matches nothing, or an analysis selector that matches several rows)
  - these are never counted as detections;
- the list of misses.

The verifier knows nothing about benchmark sentences; nothing here feeds back into it.

Sets and their role (``SET_ROLES``). Only a set nobody developing the verifier has seen measures generalisation:

- ``dev``: written while the verifier was built;
- ``dev-review``: the sentences an independent reviewer used to attack the verifier (Session 2 review, F01-F22), added
  after the attack; development data;
- ``heldout`` (file ``heldout.yaml``): heldout-v1. Measured once, blind, on the merged Session 2 code (detection 98.3%,
  113/115; false rejection 20.8%, 10/48). Its misses were studied afterwards, so it is development data from then on;
- ``heldout-v2``: a blind held-out set written without sight of the verifier. Run when the file is present.

A case's sentence may hold up to three sentences; it counts as accepted only if every factual sentence is SUPPORTED.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import BACKEND_ROOT
from ..models import Drug, EvidenceClaim, OutcomeResult, StatisticalAnalysis, Study
from .claims import Vocabulary, split_sentences
from .facts import EvidenceFact, fact_from_analysis, fact_from_claim, fact_from_drug, fact_from_result
from .verifier import NOT_FACTUAL, SUPPORTED, verify_draft

# backend/benchmarks/claims in a checkout. An installed package (the container image) cannot find it from its own
# path, so INCRETINOS_BENCHMARK_DIR names it explicitly there, like the fixture, curation and migration directories.
BENCHMARK_DIR = Path(os.environ.get("INCRETINOS_BENCHMARK_DIR") or BACKEND_ROOT / "benchmarks" / "claims")
SETS = ("dev", "dev-review", "heldout", "heldout-v2")
SET_ROLES = {
    "dev": "development",
    "dev-review": "development (independent reviewer's adversarial sentences, added after the attack)",
    "heldout": "heldout-v1 (development after first measurement)",
    "heldout-v2": "blind held-out",
}
SET_ALIASES = {"heldout-v1": "heldout"}
CATEGORIES = (
    "VALID", "DRUG_SWAPPED", "DIRECTION_REVERSED", "COMPARATOR_SWAPPED", "ARM_SWAPPED", "UNIT_CHANGED", "CI_MISSTATED", "NI_AS_SUPERIORITY", "SIGNIFICANCE_INVENTED",
    "TRIAL_MISATTRIBUTED", "ESTIMAND_MIXED", "EXPLORATORY_AS_CONFIRMATORY", "TIER_MISSTATED", "MISSING_AS_ZERO", "UNVERIFIED_AS_HUMAN_VERIFIED", "TIMEPOINT_CHANGED",
    "POPULATION_CHANGED", "MAGNITUDE_CHANGED", "UNCITED_CLAIM", "INJECTED_INSTRUCTION",
    # added with the Session 2 review
    "CAUSAL_LANGUAGE", "RECOMMENDATION", "RESPONDER_VS_MEAN", "RELATIVE_VS_ABSOLUTE", "SUBGROUP_SHIFT", "TEXT_FACT_ALTERED", "COREFERENCE_SHIFT", "CROSS_TRIAL_RANKING",
)
MAX_SENTENCES = 3


class BenchmarkError(Exception):
    """A case that cannot be run as written. Reported; never scored."""


def _study(session: Session, nct: Any) -> Study:
    study = session.scalar(select(Study).where(Study.nct_id == str(nct).upper())) if nct else None
    if study is None:
        raise BenchmarkError(f"study {nct!r} is not stored")
    return study


def _estimand_label(row: OutcomeResult | StatisticalAnalysis) -> str:
    return row.estimand.label if row.estimand is not None else "NOT_STATED"


def select_result(session: Session, spec: dict[str, Any]) -> OutcomeResult:
    """The first stored arm-level result matching the description (same rule as ``tests/conftest.result_id``)."""
    study = _study(session, spec.get("nct"))
    arm = str(spec.get("arm_contains", "")).lower()
    category = spec.get("category", "WEIGHT_PCT_CHANGE")
    for row in sorted(study.results, key=lambda r: r.id):
        if row.endpoint.category != category or row.value is None or row.scope != "ARM" or " minus " in row.arm_label or " to " in row.arm_label.lower():
            continue
        if arm not in row.arm_label.lower():
            continue
        if spec.get("estimand") and _estimand_label(row) != spec["estimand"]:
            continue
        if spec.get("tier") and row.tier != spec["tier"]:
            continue
        if spec.get("weeks") and row.timepoint_weeks != float(spec["weeks"]):
            continue
        return row
    raise BenchmarkError(f"no result matches {spec}")


def select_analysis(session: Session, spec: dict[str, Any]) -> StatisticalAnalysis:
    """The one stored between-arm analysis matching the description; more than one match is an error."""
    study = _study(session, spec.get("nct"))
    treatment = str(spec.get("treatment_contains", "")).lower()
    comparator = str(spec.get("comparator_contains", "")).lower()
    rows = []
    for row in sorted(study.analyses, key=lambda a: a.id):
        if spec.get("category") and row.endpoint.category != spec["category"]:
            continue
        if treatment not in (row.treatment_label or "").lower() or comparator not in (row.comparator_label or "").lower():
            continue
        if spec.get("estimand") and _estimand_label(row) != spec["estimand"]:
            continue
        if spec.get("tier") and row.tier != spec["tier"]:
            continue
        if spec.get("weeks") and row.timepoint_weeks != float(spec["weeks"]):
            continue
        if spec.get("hypothesis_type") and row.hypothesis_type != spec["hypothesis_type"]:
            continue
        rows.append(row)
    if len(rows) != 1:
        raise BenchmarkError(f"{len(rows)} analyses match {spec} (exactly one is required)")
    return rows[0]


def resolve_evidence(session: Session, spec: dict[str, Any]) -> EvidenceFact:
    """The evidence fact a selector describes."""
    if not isinstance(spec, dict):
        raise BenchmarkError(f"evidence entry is not a mapping: {spec!r}")
    kind = spec.get("kind")
    if kind == "result":
        fact = fact_from_result(session, select_result(session, spec))
    elif kind == "analysis":
        fact = fact_from_analysis(select_analysis(session, spec))
    elif kind == "claim":
        claim = session.scalar(select(EvidenceClaim).where(EvidenceClaim.key == spec.get("key")))
        if claim is None:
            raise BenchmarkError(f"no claim with key {spec.get('key')!r}")
        fact = fact_from_claim(claim)
    elif kind == "drug_status":
        # ``drug:`` names the drug's slug; ``slug:`` is accepted as the same field (heldout-v2 writes it that way).
        if spec.get("drug") is not None and spec.get("slug") is not None and spec["drug"] != spec["slug"]:
            raise BenchmarkError(f"a drug_status selector names two different drugs: {spec}")
        slug = spec.get("drug") if spec.get("drug") is not None else spec.get("slug")
        drug = session.scalar(select(Drug).where(Drug.slug == slug))
        if drug is None:
            raise BenchmarkError(f"no drug with slug {slug!r}")
        fact = fact_from_drug(session, drug)
    elif kind == "answer_card":
        fact = _answer_card(session, spec)
    else:
        raise BenchmarkError(f"unknown evidence kind {kind!r}")
    if not fact.usable:
        raise BenchmarkError(f"the selected record was rejected in review and is never offered as evidence: {spec}")
    return fact


_ANSWER_CARDS: dict[tuple[int, str], list[dict[str, Any]]] = {}


def _answer_card(session: Session, spec: dict[str, Any]) -> EvidenceFact:
    """A card exactly as the assistant composes it for a question (rule-based readings exist only there).

    ``question`` is asked without a model; ``text_contains`` must pick exactly one of its evidence cards.
    """
    from .answer import ask, cards_for_verification

    question, needle = spec.get("question"), spec.get("text_contains")
    if not question or not needle:
        raise BenchmarkError(f"an answer_card selector needs 'question' and 'text_contains': {spec}")
    key = (id(session), question)
    if key not in _ANSWER_CARDS:
        _ANSWER_CARDS[key] = cards_for_verification(ask(session, question).blocks)
    matches = [card for card in _ANSWER_CARDS[key] if needle in card["text"]]
    if len(matches) != 1:
        raise BenchmarkError(f"{len(matches)} answer cards match {spec} (exactly one is required)")
    return matches[0]["fact"]


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or data.get("version") != 1:
        raise BenchmarkError(f"{path.name}: unsupported benchmark version {data.get('version') if isinstance(data, dict) else None!r} (expected 1)")
    cases = data.get("cases") or []
    if not isinstance(cases, list):
        raise BenchmarkError(f"{path.name}: 'cases' is not a list")
    return cases


def run_case(session: Session, case: dict[str, Any], vocabulary: Vocabulary) -> dict[str, Any]:
    """Run one case. ``outcome`` is ACCEPTED, REJECTED or ERROR."""
    result: dict[str, Any] = {
        "id": case.get("id"),
        "category": case.get("category"),
        "expected": case.get("expected"),
        "sentence": case.get("sentence"),
        "outcome": "ERROR",
        "correct": None,
        "error": None,
        "reasons": [],
        "bound": [],
    }
    try:
        if case.get("category") not in CATEGORIES:
            raise BenchmarkError(f"unknown category {case.get('category')!r}")
        if case.get("expected") not in ("SUPPORTED", "REJECT"):
            raise BenchmarkError(f"expected must be SUPPORTED or REJECT, not {case.get('expected')!r}")
        if not isinstance(case.get("sentence"), str) or not case["sentence"].strip():
            raise BenchmarkError("the case has no sentence")
        if len(split_sentences(case["sentence"])) > MAX_SENTENCES:
            raise BenchmarkError(f"a case holds at most {MAX_SENTENCES} sentences")
        facts = [resolve_evidence(session, spec) for spec in case.get("evidence") or []]
        expected_fact = case.get("expected_fact")
        if expected_fact is not None and not (isinstance(expected_fact, int) and 1 <= expected_fact <= len(facts)):
            raise BenchmarkError(f"expected_fact {expected_fact!r} does not name an evidence entry")
    except BenchmarkError as exc:
        result["error"] = str(exc)
        return result

    cards = [{"id": f"C{index}", "text": fact.text, "source_ids": fact.source_ids, "fact": fact} for index, fact in enumerate(facts, start=1)]
    check = verify_draft(case["sentence"], cards, vocabulary=vocabulary)
    accepted = check["passed"]
    result["outcome"] = "ACCEPTED" if accepted else "REJECTED"
    result["correct"] = accepted == (case["expected"] == "SUPPORTED")
    result["reasons"] = check["problems"]
    result["verdicts"] = [report["verdict"] for report in check["sentences"]]
    bound = [b for report in check["sentences"] if report["verdict"] == SUPPORTED for b in report["bound"]]
    result["bound"] = bound
    if case["expected"] == "SUPPORTED":
        by_card = {card["id"]: card["fact"] for card in cards}
        factual = [report for report in check["sentences"] if report["verdict"] != NOT_FACTUAL]
        cited_sources = {sid for report in factual for cid in report["cited"] if cid in by_card for sid in by_card[cid].source_ids}
        bound_facts = [by_card[b["card"]] for b in bound if b["card"] in by_card]
        result["citation_ok"] = bool(accepted and bound_facts and all(set(f.source_ids) & cited_sources or not f.source_ids for f in bound_facts))
        if expected_fact is not None:
            result["attribution_ok"] = bool(accepted and any(b["card"] == f"C{expected_fact}" for b in bound))
    return result


def summarize(name: str, results: list[dict[str, Any]], path: Path | None = None) -> dict[str, Any]:
    errors = [r for r in results if r["outcome"] == "ERROR"]
    scored = [r for r in results if r["outcome"] != "ERROR"]
    unsupported = [r for r in scored if r["expected"] == "REJECT"]
    valid = [r for r in scored if r["expected"] == "SUPPORTED"]
    detected = [r for r in unsupported if r["outcome"] == "REJECTED"]
    rejected_valid = [r for r in valid if r["outcome"] == "REJECTED"]
    with_expected = [r for r in valid if "attribution_ok" in r]

    def rate(part: int, whole: int) -> float | None:
        return round(part / whole, 4) if whole else None

    per_category: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        rows = [r for r in scored if r["category"] == category]
        if not rows:
            continue
        if category == "VALID":
            accepted = sum(1 for r in rows if r["outcome"] == "ACCEPTED")
            per_category[category] = {"cases": len(rows), "accepted": accepted, "false_rejection_rate": rate(len(rows) - accepted, len(rows))}
        else:
            caught = sum(1 for r in rows if r["outcome"] == "REJECTED")
            per_category[category] = {"cases": len(rows), "detected": caught, "detection_rate": rate(caught, len(rows))}
    misses = [
        {"id": r["id"], "category": r["category"], "expected": r["expected"], "outcome": r["outcome"], "sentence": r["sentence"], "reasons": r["reasons"][:4]}
        for r in scored
        if not r["correct"]
    ]
    return {
        "set": name,
        "path": str(path) if path else None,
        "present": True,
        "cases": len(results),
        "scored": len(scored),
        "unsupported_cases": len(unsupported),
        "detected": len(detected),
        "detection_rate": rate(len(detected), len(unsupported)),
        "valid_cases": len(valid),
        "false_rejections": len(rejected_valid),
        "false_rejection_rate": rate(len(rejected_valid), len(valid)),
        "citation_accuracy": rate(sum(1 for r in valid if r.get("citation_ok")), len(valid)),
        "attribution_cases": len(with_expected),
        "attribution_accuracy": rate(sum(1 for r in with_expected if r["attribution_ok"]), len(with_expected)),
        "per_category": per_category,
        "errors": [{"id": r["id"], "category": r["category"], "error": r["error"]} for r in errors],
        "misses": misses,
    }


def run_set(session: Session, name: str, directory: Path | None = None) -> dict[str, Any]:
    """Run one benchmark set. A set whose file is absent is reported as not present, not as a failure."""
    name = SET_ALIASES.get(name, name)
    path = (directory or BENCHMARK_DIR) / f"{name}.yaml"
    if not path.exists():
        return {"set": name, "role": SET_ROLES.get(name), "path": str(path), "present": False, "cases": 0, "note": f"{path.name} is not present; nothing was run."}
    try:
        cases = load_cases(path)
    except (BenchmarkError, yaml.YAMLError) as exc:
        return {"set": name, "path": str(path), "present": True, "cases": 0, "errors": [{"id": None, "category": None, "error": f"could not read the set: {exc}"}], "misses": []}
    vocabulary = Vocabulary.from_session(session)
    results = [run_case(session, case if isinstance(case, dict) else {"id": None}, vocabulary) for case in cases]
    summary = summarize(name, results, path)
    summary["role"] = SET_ROLES.get(name)
    summary["results"] = results
    return summary


def run(session: Session, which: str = "dev", directory: Path | None = None) -> list[dict[str, Any]]:
    names = SETS if which == "all" else (SET_ALIASES.get(which, which),)
    return [run_set(session, name, directory) for name in names]


def format_report(report: dict[str, Any]) -> str:
    """Plain-text rendering of one set's report."""
    role = f" - {report['role']}" if report.get("role") else ""
    if not report.get("present"):
        return f"[{report['set']}{role}] {report.get('note', 'not present')}"
    lines = [f"[{report['set']}{role}] {report.get('cases', 0)} cases ({report.get('scored', 0)} scored, {len(report.get('errors', []))} benchmark errors)"]
    if report.get("scored"):
        def pct(value: float | None) -> str:
            return "n/a" if value is None else f"{100 * value:.1f}%"

        lines.append(f"  detection rate (unsupported claims): {pct(report['detection_rate'])} ({report['detected']}/{report['unsupported_cases']})")
        lines.append(f"  false-rejection rate (valid claims):  {pct(report['false_rejection_rate'])} ({report['false_rejections']}/{report['valid_cases']})")
        lines.append(f"  citation accuracy (valid claims):     {pct(report['citation_accuracy'])}")
        lines.append(f"  attribution accuracy (valid claims):  {pct(report['attribution_accuracy'])} (of {report['attribution_cases']} cases naming an expected fact)")
        lines.append("  per category:")
        for category, row in report["per_category"].items():
            if category == "VALID":
                lines.append(f"    {category:<30} accepted {row['accepted']}/{row['cases']}")
            else:
                lines.append(f"    {category:<30} detected {row['detected']}/{row['cases']}  ({pct(row['detection_rate'])})")
    for error in report.get("errors", []):
        lines.append(f"  BENCHMARK ERROR {error['id']}: {error['error']}")
    for miss in report.get("misses", []):
        lines.append(f"  MISS {miss['id']} [{miss['category']}] expected {miss['expected']}, got {miss['outcome']}: {miss['sentence']}")
        for reason in miss["reasons"]:
            lines.append(f"       - {reason}")
    return "\n".join(lines)
