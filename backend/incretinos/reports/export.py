"""Cited research reports (Markdown).

Reports are assembled from the same structured objects the interface shows, so a number
cannot appear in a report without its source, tier, retrieval date and verification status.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..engines.comparability import compare_findings
from ..engines.findings import Finding, _fmt, analysis_dto, build_finding, format_ci, format_value, get_finding
from ..engines.interpretation import interpret_analysis, interpret_finding
from ..models import Study

DISCLAIMER = (
    "> **Scope.** Research summary of public evidence for qualified researchers. It is not medical advice, "
    "not a treatment recommendation, and not a regulatory submission. Verification statuses are mechanical "
    "(machine extraction or quote anchoring); no entry has been human-verified unless a review decision says so."
)


def _cell(text: Any) -> str:
    return str(text if text not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def _source_line(index: int, source: dict[str, Any], verification: str, location: str | None, quote: str | None) -> str:
    bits = [
        f"[{index}] **{source['title']}**",
        f"{source.get('publisher') or 'publisher not recorded'}",
        f"tier: {source['tier_label']}",
        f"published/updated: {source.get('published_date') or 'not recorded'}",
        f"retrieved: {(source.get('retrieved_at') or '')[:10] or 'not recorded'}",
        f"version: {source.get('version_label') or '—'}",
        f"content hash: `{(source.get('content_hash') or '')[:16] or '—'}`",
        f"verification: {verification.replace('_', ' ').lower()}",
    ]
    line = " · ".join(bits)
    if source.get("url"):
        line += f"\n    {source['url']}"
    if location:
        line += f"\n    Location: `{location}`"
    if quote:
        line += f"\n    Locator quote: “{quote}”"
    return line


def _finding_block(tag: str, f: Finding, source_no: int) -> list[str]:
    reading = interpret_finding(f)
    lines = [
        f"### Finding {tag} — {f.study_name}: {f.arm_label}",
        "",
        f"{f.statement} [{source_no}]",
        "",
        "| Element | Value |",
        "|---|---|",
        f"| Study | {_cell(f.study_name)} ({f.nct_id}), {_cell(f.phase)}, status {_cell(f.study_status)} |",
        f"| Population | {_cell(reading['population'])} |",
        f"| Arm / dose | {_cell(f.arm_label)}" + (f" — {_fmt(f.dose_amount)} {f.dose_unit}" if f.dose_amount is not None else "") + (f" — formulation {f.formulation_key}" if f.formulation_key else "") + " |",
        f"| Endpoint | {_cell(f.endpoint_title)} ({f.role.replace('_', ' ').lower()}; multiplicity control: {f.multiplicity_controlled.replace('_', ' ').lower()}) |",
        f"| Estimand | {f.estimand_label.replace('_', ' ').lower()} ({f.estimand_basis.replace('_', ' ').lower()}) |",
        f"| Reported value | {_cell(format_value(f))} |",
        f"| Uncertainty | {_cell(format_ci(f))}" + (f"; {f.dispersion_type} {_fmt(f.dispersion_value)}" if f.dispersion_value is not None else "") + " |",
        f"| Participants analysed | {_cell(f.n_analyzed if f.n_analyzed is not None else 'not reported')} |",
        f"| Baseline / time point | {_cell(f.baseline_definition or 'not stated')} → week {_cell(_fmt(f.timepoint_weeks, 0) if f.timepoint_weeks is not None else 'not stated')} |",
        f"| Evidence tier | {_cell((f.source or {}).get('tier_label', f.tier))} |",
        f"| Verification | {f.verification_status.replace('_', ' ').lower()} |",
        "",
        "Qualifiers: " + "; ".join(label["text"] for label in f.labels) + ".",
        "",
        "**Does not establish:**",
        *[f"- {item}" for item in reading["does_not_establish"]],
    ]
    if reading["missing"]:
        lines += ["", "**Not available in this source:**", *[f"- {item}" for item in reading["missing"]]]
    if reading["derived_interval"]:
        lines += ["", f"*Inference (not reported by the source):* {reading['derived_interval']['display']}"]
    return lines + [""]


def comparison_report_markdown(session: Session, result_a: int, result_b: int) -> str:
    a, b = get_finding(session, result_a), get_finding(session, result_b)
    if a is None or b is None:
        raise LookupError("One or both findings were not found")
    comparison = compare_findings(session, a, b)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Comparison report: {a.study_name} — {a.arm_label} versus {b.study_name} — {b.arm_label}",
        "",
        f"Generated {now} by IncretinOS {__version__} from stored public evidence.",
        "",
        DISCLAIMER,
        "",
        "## Classification",
        "",
        f"**{comparison.classification}** — {comparison.headline}.",
        "",
        f"Factors: {comparison.counts.get('blocking', 0)} blocking · {comparison.counts.get('major', 0)} major differences · "
        f"{comparison.counts.get('minor', 0)} minor differences · {comparison.counts.get('unknown', 0)} not assessable · {comparison.counts.get('match', 0)} matching.",
        "",
        "### Why",
        "",
        *[f"- {reason}" for reason in comparison.reasons],
        "",
        "## The two findings",
        "",
        *_finding_block("A", a, 1),
        *_finding_block("B", b, 2),
    ]

    lines += ["## Reported between-arm analysis", ""]
    if comparison.reported_analyses:
        for analysis in comparison.reported_analyses:
            reading = interpret_analysis(analysis)
            lines += [f"- {reading['observed']} Source tier: {analysis['source']['tier_label'] if analysis['source'] else analysis['tier']}; verification: {analysis['verification_status'].replace('_', ' ').lower()}."]
            lines += [f"  - Establishes: {item}" for item in reading["establishes"]]
            lines += [f"  - Does not establish: {item}" for item in reading["does_not_establish"]]
            lines += [f"  - Not available: {item}" for item in reading["missing"]]
    elif comparison.same_study:
        lines.append("No between-arm analysis for this pair is stored. Any difference between the two arm-level values is descriptive.")
    else:
        lines.append("None: the findings come from different trials, so no randomized between-arm analysis exists.")
    lines.append("")

    lines += ["## Descriptive difference", ""]
    if comparison.descriptive_difference:
        d = comparison.descriptive_difference
        lines.append(f"A − B = **{_fmt(d['value'])}** {d['unit'] or ''} — confidence interval: not available. {d['note']}")
    else:
        lines.append("Not computed: the classification does not permit a difference between these two values.")
    lines.append("")

    lines += [
        "## Methodological comparison",
        "",
        "| Dimension | Finding A | Finding B | Assessment | Explanation |",
        "|---|---|---|---|---|",
        *[f"| {_cell(x.label)} | {_cell(x.a)} | {_cell(x.b)} | {x.assessment.replace('_', ' ').title()} | {_cell(x.explanation)} |" for x in comparison.factors],
        "",
        "## What may be said",
        "",
        *[f"- {s}" for s in comparison.permitted_statements],
        "",
        "## What may not be said",
        "",
        *[f"- {s}" for s in comparison.prohibited_statements],
        "",
    ]
    if comparison.requirements:
        lines += ["## What a rigorous comparison would require", "", *[f"- {s}" for s in comparison.requirements], ""]

    lines += ["## Sources", ""]
    for index, f in ((1, a), (2, b)):
        if f.source:
            lines.append(_source_line(index, f.source, f.verification_status, f.source_location, f.locator_quote))
            lines.append("")
    extra = 3
    for analysis in comparison.reported_analyses:
        if analysis["source"]:
            lines.append(_source_line(extra, analysis["source"], analysis["verification_status"], analysis["source_location"], analysis["locator_quote"]))
            lines.append("")
            extra += 1
    lines += [
        "## Epistemic key",
        "",
        "- *Documented* — reported by the cited source and reproduced as reported.",
        "- *Inference* — calculated or classified by this platform from documented values (labelled wherever it appears).",
        "- *Not reported* — absent from the stored source; never estimated or zero-filled.",
        "",
    ]
    return "\n".join(lines)


def study_report_markdown(session: Session, nct_id: str) -> str:
    study = session.scalar(select(Study).where(Study.nct_id == nct_id.upper()))
    if study is None:
        raise LookupError(f"Study {nct_id} is not on record")
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    population = study.population
    lines = [
        f"# Evidence report: {study.display_name} ({study.nct_id})",
        "",
        f"Generated {now} by IncretinOS {__version__}.",
        "",
        DISCLAIMER,
        "",
        "## Registered design",
        "",
        f"- Title: {study.brief_title}",
        f"- Phase: {study.phase or 'not stated'} · status: {study.overall_status} · enrollment: {study.enrollment} ({(study.enrollment_type or 'type not stated').lower()})",
        f"- Allocation: {study.allocation or 'not stated'} · masking: {study.masking or 'not stated'} · model: {study.intervention_model or 'not stated'}",
        f"- Primary completion: {study.primary_completion_date or 'not stated'} ({(study.primary_completion_type or 'type not stated').lower()}) · registry last updated: {study.last_update_posted}",
        f"- Population: diabetes status {population.diabetes_status if population else 'NOT_STATED'} — {population.diabetes_basis if population else ''}",
        f"- Registry record: https://clinicaltrials.gov/study/{study.nct_id}",
        "",
        "## Linked publications",
        "",
    ]
    links = sorted(study.publication_links, key=lambda link: (link.relation != "PRIMARY_RESULTS", -(link.publication.year or 0)))
    if not links:
        lines.append("None linked in the registry or PubMed.")
    for link in links[:25]:
        p = link.publication
        lines.append(f"- {'**Primary results** — ' if link.relation == 'PRIMARY_RESULTS' else ''}{p.title} *{p.journal or ''}* {p.year or ''}. PMID {p.pmid}" + (f"; doi:{p.doi}" if p.doi else "") + f". Link basis: {', '.join(b.replace('_', ' ').lower() for b in link.bases)} (confidence {link.confidence.lower()}).")
    lines += ["", "## Primary and key results as reported", ""]
    shown = 0
    sources: dict[int, tuple[dict, str, str | None, str | None]] = {}
    for result in study.results:
        if result.endpoint.role not in ("PRIMARY", "KEY_SECONDARY") or result.value is None and result.value_low is None:
            continue
        finding = build_finding(session, result)
        if finding.source:
            sources.setdefault(finding.source["id"], (finding.source, finding.verification_status, finding.source_location, finding.locator_quote))
            number = list(sources).index(finding.source["id"]) + 1
            lines.append(f"- {finding.statement} [{number}]")
            shown += 1
        if shown >= 40:
            lines.append("- … further results are available in the application.")
            break
    if shown == 0:
        lines.append("No primary or key-secondary results are stored for this study.")
    lines += ["", "## Between-arm analyses", ""]
    analyses = [a for a in study.analyses if a.endpoint.role in ("PRIMARY", "KEY_SECONDARY", "EXPLORATORY")][:30]
    if not analyses:
        lines.append("None stored.")
    for analysis in analyses:
        dto = analysis_dto(analysis)
        reading = interpret_analysis(dto)
        lines.append(f"- **{dto['endpoint_title']}** — {reading['observed']} [{'; '.join(label['text'] for label in dto['labels'])}]")
        for item in reading["does_not_establish"][:3]:
            lines.append(f"  - Does not establish: {item}")
    lines += ["", "## Sources", ""]
    for index, (source, verification, location, quote) in enumerate(sources.values(), start=1):
        lines.append(_source_line(index, source, verification, location, quote))
        lines.append("")
    return "\n".join(lines)
