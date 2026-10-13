"""Generate the evidence-derived documents from the seeded database.

  docs/scientific-research.md                 — drug-by-drug evidence summary with sources
  docs/examples/acceptance-demonstrations.md  — the seven acceptance demonstrations (A–G)
  docs/examples/report-*.md                   — exported comparison and study reports

Everything is rendered from stored, cited evidence so the documents cannot drift from the
product. Usage (from backend/):  python scripts/build_docs.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from incretinos.assistant.answer import ask  # noqa: E402
from incretinos.db import session_scope  # noqa: E402
from incretinos.engines import benefit_risk  # noqa: E402
from incretinos.engines.comparability import compare_findings, harmonize_dose  # noqa: E402
from incretinos.engines.findings import _fmt, analysis_dto, build_finding, get_finding  # noqa: E402
from incretinos.engines.interpretation import classify_drug, interpret_analysis  # noqa: E402
from incretinos.engines.safety import (  # noqa: E402
    SPONTANEOUS_REPORT_CAVEATS,
    InvalidInferenceError,
    incidence_from_spontaneous_reports,
    spontaneous_overview,
)
from incretinos.enums import TIER_LABEL, EvidenceTier  # noqa: E402
from incretinos.models import (  # noqa: E402
    ChangeEvent,
    Drug,
    EvidenceGap,
    EvidenceSource,
    ExposureEquivalence,
    OutcomeResult,
    StatisticalAnalysis,
    Study,
)
from incretinos.reports.export import comparison_report_markdown, study_report_markdown  # noqa: E402

DOCS = BACKEND.parent / "docs"
NOTES = BACKEND.parent / "research" / "web-notes"
RETRIEVED = "2026-10-13"


def tier(t: str) -> str:
    return TIER_LABEL.get(EvidenceTier(t), t) if t in EvidenceTier._value2member_map_ else t


def cell(x) -> str:
    return str("—" if x in (None, "") else x).replace("|", "\\|").replace("\n", " ")


def pick(study: Study, arm: str, estimand: str | None = None, tier_: str | None = None, weeks: float | None = None, category: str = "WEIGHT_PCT_CHANGE", baseline: str | None = None):
    for r in study.results:
        if r.endpoint.category != category or r.value is None or r.scope != "ARM" or " minus " in r.arm_label or " to " in r.arm_label.lower():
            continue
        if arm.lower() not in r.arm_label.lower():
            continue
        if estimand and (r.estimand.label if r.estimand else "NOT_STATED") != estimand:
            continue
        if tier_ and r.tier != tier_:
            continue
        if weeks and r.timepoint_weeks != weeks:
            continue
        if baseline and baseline.lower() not in (r.baseline_definition or "").lower():
            continue
        return r
    raise LookupError(f"{study.display_name}: no result for {arm}/{estimand}/{tier_}")


def research_doc(session) -> str:
    out = [
        "# Scientific research summary",
        "",
        f"Evidence retrieved **{RETRIEVED}**. This document is generated from the IncretinOS evidence database (`python scripts/build_docs.py`), so every statement below is a stored, cited record. Verification statuses are mechanical: *machine-extracted* (parsed from a structured registry payload), *quote-anchored* (the locator quote was matched verbatim against the source text) or *pending human review*. Nothing here has been human-verified.",
        "",
        "Evidence tiers, in descending order of scrutiny: FDA prescribing information and FDA review documents → peer-reviewed publication → registry-posted results (sponsor-submitted, not peer reviewed) → sponsor press release (topline) and pipeline page → spontaneous reports.",
        "",
        "> Research summary of public evidence for qualified researchers. Not medical advice, not a treatment recommendation.",
        "",
    ]
    order = {"CORE": 0, "ADJACENT": 1}
    drugs = sorted([d for d in session.scalars(select(Drug)) if d.tier_group in order], key=lambda d: (order[d.tier_group], d.id))
    out += ["## 1. Regulatory status and mechanism at a glance", "", "| Molecule | Class (derived from sourced receptor actions) | Regulatory status | Status source (tier) | As of |", "|---|---|---|---|---|"]
    for d in drugs:
        cls = classify_drug([(m.target, m.action) for m in d.mechanisms])
        out.append(f"| {d.name}{f' ({d.code_name})' if d.code_name else ''} | {cls['class_label']} | {d.regulatory_status.title()} | {tier(d.regulatory_source.tier) if d.regulatory_source else '—'} | {d.regulatory_as_of} |")
    out.append("")

    for d in drugs:
        out += [f"## {d.name}{f' ({d.code_name})' if d.code_name else ''}", "", f"**Regulatory status.** {d.regulatory_summary} *Source:* {d.regulatory_source.title if d.regulatory_source else '—'} ({tier(d.regulatory_source.tier) if d.regulatory_source else ''}; {d.regulatory_source.url if d.regulatory_source else ''}).", ""]
        out += ["**Mechanism (each action separately cited).**", ""]
        for m in d.mechanisms:
            out.append(f"- {m.target} {m.action.lower()} — {tier(m.source.tier) if m.source else 'no source'}" + (f": “{m.quote}”" if m.quote else "") + f" [{m.verification_status.replace('_', ' ').lower()}]")
        if d.indications:
            out += ["", "**Indications.**", ""]
            for i in d.indications:
                out.append(f"- {i.brand or 'n/a'} — {i.status.title()}: {i.text} ({tier(i.source.tier) if i.source else '—'}; {i.verification_status.replace('_', ' ').lower()})")
        eqs = [e for e in session.scalars(select(ExposureEquivalence)) if e.dose_a.formulation.drug_id == d.id]
        if eqs:
            out += ["", "**Formulation harmonization (documented dose pairs only — there is no conversion factor).**", "", "| Investigational capsule | Marketed tablet | Basis | Source | Verification |", "|---|---|---|---|---|"]
            for e in eqs:
                out.append(f"| {e.dose_a.amount:g} mg | {e.dose_b.amount:g} mg | {e.basis.replace('_', ' ').lower()} | {e.source.key} | {e.verification_status.replace('_', ' ').lower()} |")
            out += ["", f"Locator quote: “{eqs[0].quote}” — {eqs[0].source.title}. FDA's multidisciplinary review of NDA 220934 states that review of the tablet application relied on bioequivalence of tablet and capsule dosages (curated claim, pending review because the review PDF is not machine-anchored)."]
        studies = sorted([s for s in session.scalars(select(Study)) if any(link.drug_id == d.id for link in s.drug_links)], key=lambda s: s.display_name)
        out += ["", "**Tracked trials.**", "", "| Trial | NCT | Phase | Registry status | Population | Enrolled | Registry results | Primary publication | Evidence tiers on record |", "|---|---|---|---|---|---|---|---|---|"]
        for s in studies:
            primary = next((link.publication for link in s.publication_links if link.relation == "PRIMARY_RESULTS"), None)
            tiers = sorted({tier(r.tier) for r in s.results} | {tier(a.tier) for a in s.analyses})
            out.append(f"| {s.display_name} | {s.nct_id} | {cell(s.phase)} | {cell(s.overall_status)} | {s.population.diabetes_status if s.population else '—'} | {cell(s.enrollment)} | {'posted' if s.has_results else 'not posted'} | {f'{primary.journal} {primary.year}, PMID {primary.pmid}' if primary else '—'} | {'; '.join(tiers) or 'none'} |")
        rows = []
        for s in studies:
            if not any(link.drug_id == d.id and link.role != "COMPARATOR" for link in s.drug_links):
                continue
            weeks = (s.design_features or {}).get("treatment_weeks")
            for r in s.results:
                if r.endpoint.category != "WEIGHT_PCT_CHANGE" or r.value is None or r.scope != "ARM" or " minus " in r.arm_label or " to " in r.arm_label.lower() or (r.arm and r.arm.is_pooled):
                    continue
                if weeks and r.timepoint_weeks is not None and abs(r.timepoint_weeks - weeks) > 0.5:
                    continue
                if r.origin == "REGISTRY" and r.endpoint.role != "PRIMARY":
                    continue
                f = build_finding(session, r)
                ci = f"{_fmt(r.ci_low)} to {_fmt(r.ci_high)}" if r.ci_low is not None else "not reported"
                rows.append(f"| {s.display_name} | {cell(r.arm_label)} | {_fmt(r.value)} | {f.estimand_label.replace('_', ' ').lower()} ({'stated' if 'STATED' in f.estimand_basis else 'inferred' if 'INFERRED' in f.estimand_basis else 'n/a'}) | {_fmt(r.timepoint_weeks, 0)} | {cell(r.baseline_definition)} | {ci} | {cell(r.n_analyzed)} | {tier(r.tier)} | {r.verification_status.replace('_', ' ').lower()} | {r.source.key} |")
        if rows:
            out += ["", "**Percent change in body weight as reported (arm-level; not treatment effects).** Values under different estimands are different quantities and are listed on separate rows.", "", "| Trial | Arm | % change | Estimand (basis) | Week | Baseline | CI | n | Tier | Verification | Source key |", "|---|---|---|---|---|---|---|---|---|---|---|", *rows[:70]]
        cv = [(s, a) for s in studies for a in s.analyses if a.endpoint.category in ("MACE", "MORTALITY") and a.origin == "CURATED"]
        if cv:
            out += ["", "**Cardiovascular and mortality analyses as reported.**", ""]
            for s, a in cv:
                dto = analysis_dto(a)
                out.append(f"- {s.display_name} — {dto['endpoint_title']}: {interpret_analysis(dto)['observed']} [{'; '.join(label['text'] for label in dto['labels'])}] ({a.source.key})")
        out.append("")

    conflicts = session.scalars(select(EvidenceGap).where(EvidenceGap.rule_id == "G12_SOURCE_CONFLICT")).all()
    out += ["## Discrepancies between public sources", "", "Detected automatically for the same trial, arm, endpoint, estimand and time point (rounding variants excluded).", ""]
    out += [f"- {g.question} {g.available_evidence}" for g in conflicts] or ["- None detected."]
    out += ["", "## Items that could not be confirmed from a primary source", "", "Recorded during retrieval; none of these is asserted anywhere in the product.", ""]
    for path in sorted(NOTES.glob("*.json")):
        notes = json.loads(path.read_text())
        out.append(f"**{notes.get('topic', path.stem)}**")
        out += [f"- {str(item)[:420]}" for item in notes.get("not_found", [])]
        out.append("")
    out += ["## Source register (non-registry sources)", "", "| Key | Tier | Published | Title | URL |", "|---|---|---|---|---|"]
    cited = {sid for (sid,) in session.execute(select(OutcomeResult.source_id))} | {sid for (sid,) in session.execute(select(StatisticalAnalysis.source_id))}
    for s in session.scalars(select(EvidenceSource).where(EvidenceSource.source_type.in_(["WEB", "GUIDANCE", "OPENFDA_LABEL", "PUBMED"])).order_by(EvidenceSource.tier, EvidenceSource.key)):
        if s.source_type == "PUBMED" and s.id not in cited:
            continue
        out.append(f"| {s.key} | {tier(s.tier)} | {cell(s.published_date)} | {cell(s.title)[:110]} | {cell(s.url)} |")
    out += ["", "Registry sources: one `ctgov:NCT…` record per tracked trial (ClinicalTrials.gov API v2), each stored in full with its content hash; see the Source and Audit Explorer."]
    return "\n".join(out) + "\n"


def demos(session) -> str:
    def study(nct: str) -> Study:
        return session.scalar(select(Study).where(Study.nct_id == nct))

    out = ["# Acceptance demonstrations", "", f"Generated from the offline-seeded evidence base (retrieved {RETRIEVED}) by `python scripts/build_docs.py`. Each demonstration is also covered by automated tests (`backend/tests`, `frontend/e2e`).", ""]

    # A
    reta = pick(study("NCT05929066"), "12 mg", "TREATMENT_REGIMEN", "PEER_REVIEWED", 80)
    tirz = pick(study("NCT04184622"), "15 mg", "TREATMENT_REGIMEN", "PEER_REVIEWED", 72)
    comp = compare_findings(session, get_finding(session, reta.id), get_finding(session, tirz.id))
    (DOCS / "examples" / "report-retatrutide-vs-tirzepatide.md").write_text(comparison_report_markdown(session, reta.id, tirz.id), encoding="utf-8")
    answer = ask(session, "What evidence differentiates retatrutide from tirzepatide?")
    out += ["## A. Retatrutide versus tirzepatide — no head-to-head result is invented", "", f"**Classification: {comp.classification}** — {comp.headline}.", "", *[f"- {r}" for r in comp.reasons[:5]], "", "What may not be said:", *[f"- {s}" for s in comp.prohibited_statements], "", "Assistant answer (deterministic, no language model):", ""]
    out += [f"- *{b.kind.replace('_', ' ').title()}* — {b.text}" for b in answer.blocks]
    out += ["", "Full cited report: [report-retatrutide-vs-tirzepatide.md](report-retatrutide-vs-tirzepatide.md)", ""]

    # B
    out += ["## B. Foundayo — tablet/capsule bioequivalence and efficacy versus treatment-regimen estimands", "", "| Capsule dose queried | Result |", "|---|---|"]
    for amount in (36, 12, 6, 20):
        h = harmonize_dose(session, "orforglipron-capsule", amount, "foundayo-tablet")
        out.append(f"| {amount} mg | " + (f"{h['equivalent']['amount']:g} mg tablet — {h['basis'].replace('_', ' ').lower()}, {h['verification_status'].replace('_', ' ').lower()} ({h['source_key']})" if h["status"] == "DOCUMENTED" else f"**No conversion offered.** {h['explanation']}") + " |")
    attain = study("NCT05869903")
    eff = pick(attain, "36 mg", "EFFICACY", "REGISTRY_RESULTS", 72)
    reg = pick(attain, "36 mg", "TREATMENT_REGIMEN", "PEER_REVIEWED", 72)
    comp = compare_findings(session, get_finding(session, eff.id), get_finding(session, reg.id))
    (DOCS / "examples" / "report-attain1-estimands.md").write_text(comparison_report_markdown(session, eff.id, reg.id), encoding="utf-8")
    out += ["", f"ATTAIN-1, 36 mg capsule arm, week 72: **{_fmt(eff.value)}%** under the efficacy estimand ({tier(eff.tier)}; label inferred from the registry's analysis-population text) and **{_fmt(reg.value)}%** under the treatment-regimen estimand ({tier(reg.tier)}; stated by the publication).", "", f"Comparing the two: **{comp.classification}**. " + " ".join(comp.reasons[:2]), "", "Full report: [report-attain1-estimands.md](report-attain1-estimands.md)", ""]

    # C
    profiles = benefit_risk.arm_profiles(session, "NCT06603571")
    out += ["## C. EloraTZP Phase 2b — efficacy and adverse-event discontinuation, correctly qualified", "", "| Arm | % weight change, week 48 (efficacy estimand; press release) | Treatment discontinuation due to adverse events |", "|---|---|---|"]
    for arm in profiles["arms"]:
        e = arm["efficacy"].get("weight_pct")
        t = arm["tolerability"].get("treatment_discontinuation_ae")
        discontinuation = f"{_fmt(t['percent'])}%" if t else "not reported per arm"
        out.append(f"| {arm['label']} | {_fmt(e['value']) if e else 'not reported'} | {discontinuation} |")
    el = study("NCT06603571")
    ranges = [ae for ae in el.adverse_events if ae.scope == "RANGE_ACROSS_ARMS"]
    for ae in ranges:
        out.append(f"\nRange only — {ae.arm_label}: {_fmt(ae.percent_low)}% to {_fmt(ae.percent_high)}% ({ae.measure_kind.replace('_', ' ').lower()}); not attributable to any single arm.")
    combo = pick(el, "9 mg + tirzepatide 15", "EFFICACY")
    out += ["", "Context that travels with the headline number:", "", f"> {build_finding(session, combo).statement}", ""]

    # D
    (DOCS / "examples" / "report-achieve-4.md").write_text(study_report_markdown(session, "NCT05803421"), encoding="utf-8")
    out += ["## D. ACHIEVE-4 — non-inferiority, hazard ratios, intervals and multiplicity-uncontrolled findings", ""]
    for a in [a for a in study("NCT05803421").analyses if a.origin == "CURATED" and a.param_type == "Hazard ratio"]:
        dto = analysis_dto(a)
        reading = interpret_analysis(dto)
        out += [f"**{dto['endpoint_title']}** — {reading['observed']}  ", f"Qualifiers: {'; '.join(label['text'] for label in dto['labels'])}. Source tier: {tier(a.tier)}; {a.verification_status.replace('_', ' ').lower()}.", *[f"- Establishes: {s}" for s in reading["establishes"]], *[f"- Does not establish: {s}" for s in reading["does_not_establish"][:4]], ""]
    out += ["Full study report: [report-achieve-4.md](report-achieve-4.md)", ""]

    # E
    out += ["## E. Eloralintide — Phase 3 trials and the questions they seek to answer", "", "| Trial | NCT | Population (registry) | Status | Planned N | Primary completion | Registered primary endpoint(s) |", "|---|---|---|---|---|---|---|"]
    for s in sorted([s for s in session.scalars(select(Study)) if s.display_name.startswith("ENLIGHTEN")], key=lambda s: s.display_name):
        primary = "; ".join(f"{e.title} [{e.timeframe_text}]" for e in s.endpoints if e.kind == "PROTOCOL" and e.role == "PRIMARY")
        out.append(f"| {s.display_name} | {s.nct_id} | {cell(s.brief_title)} | {s.overall_status} | {s.enrollment} | {s.primary_completion_date} ({(s.primary_completion_type or '').lower()}) | {cell(primary)} |")
    out += ["", "Open questions recorded for eloralintide:", ""]
    out += [f"- {g.question} — *{g.gap_nature.replace('_', ' ').lower()}*" for g in session.scalars(select(EvidenceGap)) if g.drug and g.drug.slug == "eloralintide"]
    out.append("")

    # F
    tz = session.scalar(select(Drug).where(Drug.slug == "tirzepatide"))
    sp = spontaneous_overview(session, tz)
    try:
        incidence_from_spontaneous_reports(sp["top_reactions"][1]["count"], sp["total_reports"])
        refusal = "(no error raised)"
    except InvalidInferenceError as exc:
        refusal = str(exc)
    nausea = next(r for r in sp["disproportionality_screen"] if r["term"] == "NAUSEA")
    out += ["## F. Safety — why spontaneous reports cannot establish incidence", "", f"FAERS (via openFDA, data through {sp['database_last_updated']}) holds **{sp['total_reports']:,} reports** that mention tirzepatide, of which {nausea['drug_reports']:,} mention nausea. Dividing one by the other is not an incidence: the platform's only code path for that calculation raises", "", f"> {refusal}", "", *[f"- {c}" for c in SPONTANEOUS_REPORT_CAVEATS], "", f"What is offered instead is a disproportionality screen — for nausea, PRR {_fmt(nausea['prr'])} (95% CI {_fmt(nausea['prr_ci'][0])} to {_fmt(nausea['prr_ci'][1])}) — labelled as a measure of reporting disproportion, not risk, alongside within-trial incidence by arm (for example SURMOUNT-1 registry adverse-event tables, events over participants at risk).", ""]

    # G
    event = session.scalar(select(ChangeEvent))
    out += ["## G. Source change — an updated registry record becomes a reviewable change summary", "", f"**{event.source.title}** — {event.summary} Significance: {event.significance}. Review status: {event.review_status}.", ""]
    if event.is_synthetic:
        out += ["> **Synthetic demonstration.** The public registry API exposes only the current record, so the “before” version here was generated from the real record by labelled edits (`make_synthetic_prior_version`). The detection, diffing and review-queue code paths are the production ones; running `incretinos check-updates --live` applies them to real changes.", ""]
    out += ["| Field | Before | After | Significance | Why it matters |", "|---|---|---|---|---|"]
    for c in event.changes:
        before = "; ".join(c["before"]) if isinstance(c["before"], list) else c["before"]
        after = "; ".join(c["after"]) if isinstance(c["after"], list) else c["after"]
        out.append(f"| {cell(c['label'])} | {cell(before)[:160]} | {cell(after)[:160]} | {c['significance']} | {cell(c['note'])} |")
    return "\n".join(out) + "\n"


def main() -> None:
    (DOCS / "examples").mkdir(parents=True, exist_ok=True)
    with session_scope() as session:
        (DOCS / "scientific-research.md").write_text(research_doc(session), encoding="utf-8")
        (DOCS / "examples" / "acceptance-demonstrations.md").write_text(demos(session), encoding="utf-8")
    for path in sorted([DOCS / "scientific-research.md", *(DOCS / "examples").glob("*.md")]):
        print(f"{path.relative_to(DOCS.parent)}  {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
