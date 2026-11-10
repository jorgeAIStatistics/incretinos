"""Cited research reports (Markdown).

Reports are assembled from the same structured objects the interface shows, so a number
cannot appear in a report without its source, tier, retrieval date and verification status.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..engines.comparability import compare_findings
from ..engines.findings import (
    Finding,
    _fmt,
    analysis_dto,
    build_finding,
    format_ci,
    format_value,
    get_finding,
)
from ..engines.interpretation import interpret_analysis, interpret_finding
from ..models import Study

DISCLAIMER = (
    "> **Scope.** Research summary of public evidence for qualified researchers. It is not medical advice, "
    "not a treatment recommendation, and not a regulatory submission. Verification statuses are mechanical "
    "(machine extraction or quote anchoring); no entry has been human-verified unless a review decision says so."
)


# --------------------------------------------------------------------------- escaping
#
# Everything a report prints that is not a literal of this module comes, directly or through a generated
# sentence, from source text: study names, arm labels, endpoint titles, units, quotes, URLs. A source must not
# be able to add a heading, a link, an image, HTML, emphasis or a table column to a report, so every such value
# passes through one of the functions below. The target is CommonMark and GitHub-flavoured Markdown.
#
# The escaping is deliberately no wider than the grammar requires, because a report is also read as plain text
# and a locator quote should stay searchable in its source: "p <0.001", "[95% CI, -21.2 to -17.7]" and
# TREATMENT_REGIMEN are left exactly as they are, since none of them can start a construct.

# Line breaks, tabs and other control characters (ordinary and non-breaking spaces are kept as they are).
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]+")
# Bidirectional overrides and isolates can make digits display in a different order than they are stored.
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
# "&name;" / "&#123;" would be decoded by the renderer; a bare "&" is not.
_ENTITY = re.compile(r"&(?=#?\w+;)")
# "<" opens a tag, comment, declaration or autolink only before a letter, "/", "!" or "?". "<0.001" is text.
_TAG_OPEN = re.compile(r"<(?=[A-Za-z/!?])")
# Code spans, emphasis and strikethrough pair up anywhere on a line, also across two values.
_PAIRED = re.compile(r"([`*~])")
# An underscore between two letters or digits (TREATMENT_REGIMEN, content_hash) cannot open or close emphasis;
# every other underscore is escaped.
_EMPHASIS_UNDERSCORE = re.compile(r"(?<![^\W_])_|_(?![^\W_])")
# A link or image needs "]" directly before "(" or "[". The last character of a value is followed by text this
# function cannot see, so a closing bracket there is escaped too. (A bare "[x]" links only if the document
# defines "[x]: url", and a definition must start a line, which no value can do: see ``lead`` below.)
_LINK_CLOSE = re.compile(r"\](?=\s*[(\[]|\s*$)")
_LEADING_ORDERED = re.compile(r"^(\d{1,9})([.)])")
_SAFE_URL = re.compile(r"https?://[^\s<>]+")


def _md(text: Any, *, lead: bool = False, table: bool = False) -> str:
    """Inline text that renders as the same characters it was given.

    Line breaks and control characters become one space, so a value can never start a new block (heading,
    quote, list, table row, code fence, link definition); bidirectional overrides are dropped. Then: backslash
    doubled; entity-like ``&`` and tag-opening ``<`` written as entities; backtick, ``*``, ``~`` and
    emphasis-capable ``_`` escaped; the ``]`` of a would-be link escaped; ``://`` and ``www.`` broken up so that
    a renderer does not turn source text into a link.

    ``lead=True`` is for a value that opens a block (a list item or a paragraph): a leading ``#``, ``>``, ``-``,
    ``+``, ``=``, ``[`` or ``1.`` is neutralised as well, which also rules out link definitions and task boxes.
    ``table=True`` is for every value written inside a table row, whole cell or part of one: ``|`` is escaped so
    that it cannot end the cell (GFM drops the cells beyond the header's count, and their text with them).
    """
    out = _BIDI.sub("", _CONTROL.sub(" ", str(text))).strip()
    out = out.replace("\\", "\\\\")
    out = _TAG_OPEN.sub("&lt;", _ENTITY.sub("&amp;", out))
    out = _EMPHASIS_UNDERSCORE.sub(r"\\_", _PAIRED.sub(r"\\\1", out))
    out = _LINK_CLOSE.sub(r"\\]", out)
    out = out.replace("://", "\\://").replace("www.", "www\\.")
    if lead:
        if out[:1] in ("#", ">", "-", "+", "=", "["):
            out = "\\" + out
        out = _LEADING_ORDERED.sub(r"\1\\\2", out)
    if table:
        out = out.replace("|", "\\|")
    return out


def _code(text: Any) -> str:
    """A code span holding ``text`` literally, delimited by more backticks than the text contains."""
    body = _BIDI.sub("", _CONTROL.sub(" ", str(text))).strip()
    if not body:
        return "—"
    fence = "`" * (max((len(run) for run in re.findall(r"`+", body)), default=0) + 1)
    pad = " " if body.startswith("`") or body.endswith("`") else ""
    return f"{fence}{pad}{body}{pad}{fence}"


def _link(url: Any) -> str:
    """An http(s) address as an explicit autolink whose text is the address itself; anything else as inert text."""
    target = str(url).strip()
    return f"<{target}>" if _SAFE_URL.fullmatch(target) else _md(target, lead=True)


def _cell(text: Any) -> str:
    """A table cell: inline text in which a pipe cannot end the cell."""
    return _md(text if text not in (None, "") else "—", table=True)


def _item(text: Any) -> str:
    """A value that opens a list item or a paragraph."""
    return _md(text, lead=True)


def _source_line(index: int, source: dict[str, Any], verification: str, location: str | None, quote: str | None) -> str:
    bits = [
        f"[{index}] **{_md(source['title'])}**",
        f"{_md(source.get('publisher') or 'publisher not recorded')}",
        f"tier: {_md(source['tier_label'])}",
        f"published/updated: {_md(source.get('published_date') or 'not recorded')}",
        f"retrieved: {_md((source.get('retrieved_at') or '')[:10] or 'not recorded')}",
        f"version: {_md(source.get('version_label') or '—')}",
        f"content hash: {_code((source.get('content_hash') or '')[:16])}",
        f"verification: {_md(verification.replace('_', ' ').lower())}",
    ]
    line = " · ".join(bits)
    if source.get("url"):
        line += f"\n    {_link(source['url'])}"
    if location:
        line += f"\n    Location: {_code(location)}"
    if quote:
        line += f"\n    Locator quote: “{_md(quote)}”"
    return line


def _finding_block(tag: str, f: Finding, source_no: int) -> list[str]:
    reading = interpret_finding(f)
    lines = [
        f"### Finding {tag} — {_md(f.study_name)}: {_md(f.arm_label)}",
        "",
        f"{_item(f.statement)} [{source_no}]",
        "",
        "| Element | Value |",
        "|---|---|",
        f"| Study | {_cell(f.study_name)} ({_md(f.nct_id, table=True)}), {_cell(f.phase)}, status {_cell(f.study_status)} |",
        f"| Population | {_cell(reading['population'])} |",
        f"| Arm / dose | {_cell(f.arm_label)}" + (f" — {_fmt(f.dose_amount)} {_md(f.dose_unit or '', table=True)}" if f.dose_amount is not None else "") + (f" — formulation {_md(f.formulation_key, table=True)}" if f.formulation_key else "") + " |",
        f"| Endpoint | {_cell(f.endpoint_title)} ({_md(f.role.replace('_', ' ').lower(), table=True)}; multiplicity control: {_md(f.multiplicity_controlled.replace('_', ' ').lower(), table=True)}) |",
        f"| Estimand | {_md(f.estimand_label.replace('_', ' ').lower(), table=True)} ({_md(f.estimand_basis.replace('_', ' ').lower(), table=True)}) |",
        f"| Reported value | {_cell(format_value(f))} |",
        f"| Uncertainty | {_cell(format_ci(f))}" + (f"; {_md(f.dispersion_type or '', table=True)} {_fmt(f.dispersion_value)}" if f.dispersion_value is not None else "") + " |",
        f"| Participants analysed | {_cell(f.n_analyzed if f.n_analyzed is not None else 'not reported')} |",
        f"| Baseline / time point | {_cell(f.baseline_definition or 'not stated')} → week {_cell(_fmt(f.timepoint_weeks, 0) if f.timepoint_weeks is not None else 'not stated')} |",
        f"| Evidence tier | {_cell((f.source or {}).get('tier_label', f.tier))} |",
        f"| Verification | {_md(f.verification_status.replace('_', ' ').lower(), table=True)} |",
        "",
        "Qualifiers: " + "; ".join(_md(label["text"]) for label in f.labels) + ".",
        "",
        "**Does not establish:**",
        *[f"- {_item(item)}" for item in reading["does_not_establish"]],
    ]
    if reading["missing"]:
        lines += ["", "**Not available in this source:**", *[f"- {_item(item)}" for item in reading["missing"]]]
    if reading["derived_interval"]:
        lines += ["", f"*Inference (not reported by the source):* {_md(reading['derived_interval']['display'])}"]
    return lines + [""]


def comparison_report_markdown(session: Session, result_a: int, result_b: int) -> str:
    a, b = get_finding(session, result_a), get_finding(session, result_b)
    if a is None or b is None:
        raise LookupError("One or both findings were not found")
    comparison = compare_findings(session, a, b)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Comparison report: {_md(a.study_name)} — {_md(a.arm_label)} versus {_md(b.study_name)} — {_md(b.arm_label)}",
        "",
        f"Generated {now} by IncretinOS {__version__} from stored public evidence.",
        "",
        DISCLAIMER,
        "",
        "## Classification",
        "",
        f"**{_md(comparison.classification)}** — {_md(comparison.headline)}.",
        "",
        f"Factors: {comparison.counts.get('blocking', 0)} blocking · {comparison.counts.get('major', 0)} major differences · "
        f"{comparison.counts.get('minor', 0)} minor differences · {comparison.counts.get('unknown', 0)} not assessable · {comparison.counts.get('match', 0)} matching.",
        "",
        "### Why",
        "",
        *[f"- {_item(reason)}" for reason in comparison.reasons],
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
            lines += [f"- {_item(reading['observed'])} Source tier: {_md(analysis['source']['tier_label'] if analysis['source'] else analysis['tier'])}; verification: {_md(analysis['verification_status'].replace('_', ' ').lower())}."]
            lines += [f"  - Establishes: {_md(item)}" for item in reading["establishes"]]
            lines += [f"  - Does not establish: {_md(item)}" for item in reading["does_not_establish"]]
            lines += [f"  - Not available: {_md(item)}" for item in reading["missing"]]
    elif comparison.same_study:
        lines.append("No between-arm analysis for this pair is stored. Any difference between the two arm-level values is descriptive.")
    else:
        lines.append("None: the findings come from different trials, so no randomized between-arm analysis exists.")
    lines.append("")

    lines += ["## Descriptive difference", ""]
    if comparison.descriptive_difference:
        d = comparison.descriptive_difference
        lines.append(f"A − B = **{_fmt(d['value'])}** {_md(d['unit'] or '')} — confidence interval: not available. {_md(d['note'])}")
    else:
        lines.append("Not computed: the classification does not permit a difference between these two values.")
    lines.append("")

    lines += [
        "## Methodological comparison",
        "",
        "| Dimension | Finding A | Finding B | Assessment | Explanation |",
        "|---|---|---|---|---|",
        *[f"| {_cell(x.label)} | {_cell(x.a)} | {_cell(x.b)} | {_cell(x.assessment.replace('_', ' ').title())} | {_cell(x.explanation)} |" for x in comparison.factors],
        "",
        "## What may be said",
        "",
        *[f"- {_item(s)}" for s in comparison.permitted_statements],
        "",
        "## What may not be said",
        "",
        *[f"- {_item(s)}" for s in comparison.prohibited_statements],
        "",
    ]
    if comparison.requirements:
        lines += ["## What a rigorous comparison would require", "", *[f"- {_item(s)}" for s in comparison.requirements], ""]

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
        f"# Evidence report: {_md(study.display_name)} ({_md(study.nct_id)})",
        "",
        f"Generated {now} by IncretinOS {__version__}.",
        "",
        DISCLAIMER,
        "",
        "## Registered design",
        "",
        f"- Title: {_md(study.brief_title)}",
        f"- Phase: {_md(study.phase or 'not stated')} · status: {_md(study.overall_status)} · enrollment: {_md(study.enrollment)} ({_md((study.enrollment_type or 'type not stated').lower())})",
        f"- Allocation: {_md(study.allocation or 'not stated')} · masking: {_md(study.masking or 'not stated')} · model: {_md(study.intervention_model or 'not stated')}",
        f"- Primary completion: {_md(study.primary_completion_date or 'not stated')} ({_md((study.primary_completion_type or 'type not stated').lower())}) · registry last updated: {_md(study.last_update_posted)}",
        f"- Population: diabetes status {_md(population.diabetes_status if population else 'NOT_STATED')} — {_md((population.diabetes_basis or '') if population else '')}",
        f"- Registry record: {_link(f'https://clinicaltrials.gov/study/{study.nct_id}')}",
        "",
        "## Linked publications",
        "",
    ]
    links = sorted(study.publication_links, key=lambda link: (link.relation != "PRIMARY_RESULTS", -(link.publication.year or 0)))
    if not links:
        lines.append("None linked in the registry or PubMed.")
    for link in links[:25]:
        p = link.publication
        lines.append(f"- {'**Primary results** — ' if link.relation == 'PRIMARY_RESULTS' else ''}{_item(p.title)} *{_md(p.journal or '')}* {_md(p.year or '')}. PMID {_md(p.pmid)}" + (f"; doi:{_md(p.doi)}" if p.doi else "") + f". Link basis: {', '.join(_md(b.replace('_', ' ').lower()) for b in link.bases)} (confidence {_md(link.confidence.lower())}).")
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
            lines.append(f"- {_item(finding.statement)} [{number}]")
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
        lines.append(f"- **{_md(dto['endpoint_title'])}** — {_md(reading['observed'])} [{'; '.join(_md(label['text']) for label in dto['labels'])}]")
        for item in reading["does_not_establish"][:3]:
            lines.append(f"  - Does not establish: {_md(item)}")
    lines += ["", "## Sources", ""]
    for index, (source, verification, location, quote) in enumerate(sources.values(), start=1):
        lines.append(_source_line(index, source, verification, location, quote))
        lines.append("")
    return "\n".join(lines)
