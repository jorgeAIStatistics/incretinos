"""JATS (Europe PMC full text): licence, tables with their structure, and sentences with their location.

Tables keep the structure the XML gives them: header rows from ``<thead>`` (and header rows printed inside the
body, which start a new panel), every cell with its column index, colspan and rowspan resolved onto a grid, the
first column as the row label (stub), label-only rows as row groups, ``<table-wrap-foot>`` footnotes (with their
markers) and abbreviation definitions. A table delivered only as an image has no cells and is reported as such.

Sentences are found by a configured anchor phrase inside one paragraph (figures and tables nested in the
paragraph are left out of its text); the sentence is returned verbatim with its section path and paragraph id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET

from .tables import BodyRow, HeaderCell, ParsedTable, clean, collect_units, parse_abbreviations

PARSE_METHOD = "jats-xml/table-grid"
PARSE_VERSION = "1"
SENTENCE_METHOD = "jats-xml/sentence-pattern"

_XLINK = "{http://www.w3.org/1999/xlink}href"


def parse(xml_bytes: bytes) -> Element:
    return SafeET.fromstring(xml_bytes)


def text_of(el: Element | None, *, skip: tuple[str, ...] = ()) -> str:
    """The element's text with nested markup flattened; children named in ``skip`` are left out."""
    if el is None:
        return ""
    parts: list[str] = [el.text or ""]
    for child in el:
        if _local(child.tag) in skip:
            parts.append(child.tail or "")
            continue
        if _local(child.tag) == "break":
            parts.append(" ")
        else:
            parts.append(text_of(child, skip=skip))
        parts.append(child.tail or "")
    return "".join(parts)


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


# --------------------------------------------------------------------------- licence and identifiers


@dataclass(frozen=True)
class JatsLicence:
    url: str | None
    text: str | None
    content_type: str | None


def licence(root: Element) -> JatsLicence:
    """The ``<license>`` of the article as the publisher wrote it (URL from ali:license_ref or xlink:href)."""
    lic = next((el for el in root.iter() if _local(el.tag) == "license"), None)
    if lic is None:
        return JatsLicence(None, None, None)
    url = None
    content_type = None
    for el in lic.iter():
        if _local(el.tag) == "license_ref" and (el.text or "").strip():
            url = el.text.strip()
            content_type = el.get("content-type")
            break
    if url is None:
        url = lic.get(_XLINK)
    if url is None:
        link = next((el for el in lic.iter() if _local(el.tag) == "ext-link" and el.get(_XLINK)), None)
        url = link.get(_XLINK) if link is not None else None
    return JatsLicence(url, clean(text_of(lic))[:600] or None, content_type)


def article_ids(root: Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) == "article-id" and el.get("pub-id-type") and (el.text or "").strip():
            out.setdefault(el.get("pub-id-type"), el.text.strip())
    return out


def article_title(root: Element) -> str | None:
    el = next((e for e in root.iter() if _local(e.tag) == "article-title"), None)
    return clean(text_of(el)) or None


# --------------------------------------------------------------------------- sections and paragraphs


@dataclass
class Paragraph:
    section: str  # "Results > Changes in glycemic parameters"
    paragraph_id: str | None
    text: str


def paragraphs(root: Element) -> list[Paragraph]:
    """Every ``<p>`` of the abstract and the body with its section path (figures and tables nested in it skipped)."""
    out: list[Paragraph] = []

    def walk(el: Element, path: list[str]) -> None:
        for child in el:
            name = _local(child.tag)
            if name in ("fig", "table-wrap", "table-wrap-foot", "ref-list", "back"):
                continue
            if name == "sec":
                title = next((c for c in child if _local(c.tag) == "title"), None)
                walk(child, [*path, clean(text_of(title)) or "(untitled section)"])
            elif name == "p":
                out.append(Paragraph(" > ".join(path) or "(no section)", child.get("id"), clean(text_of(child, skip=("fig", "table-wrap")))))
            elif name not in ("title",):
                walk(child, path)

    for el in root.iter():
        if _local(el.tag) == "abstract":
            walk(el, ["Abstract"])
            break
    body = next((el for el in root.iter() if _local(el.tag) == "body"), None)
    if body is not None:
        walk(body, [])
    return out


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")


def sentence_with(paragraph: Paragraph, anchor: str) -> str | None:
    """The sentence of the paragraph that contains ``anchor`` (case and dash variants ignored), verbatim."""
    text = paragraph.text
    hay = text.lower()
    idx = hay.find(clean(anchor).lower())
    if idx < 0:
        return None
    starts = [0] + [m.end() for m in _SENTENCE_END.finditer(text)]
    start = max(s for s in starts if s <= idx)
    after = [m.start() for m in _SENTENCE_END.finditer(text) if m.start() >= idx]
    end = after[0] if after else len(text)
    return text[start:end].strip()


def find_sentence(root: Element, anchor: str, *, section: str | None = None) -> tuple[Paragraph, str] | None:
    hits = [(p, s) for p in paragraphs(root) if (section is None or section.lower() in p.section.lower()) and (s := sentence_with(p, anchor))]
    return hits[0] if len(hits) == 1 else None


def count_sentences(root: Element, anchor: str, *, section: str | None = None) -> int:
    return sum(1 for p in paragraphs(root) if (section is None or section.lower() in p.section.lower()) and sentence_with(p, anchor))


# --------------------------------------------------------------------------- tables


def table_wraps(root: Element) -> list[Element]:
    return [el for el in root.iter() if _local(el.tag) == "table-wrap"]


def _section_of(root: Element, target: Element) -> str | None:
    path: list[str] = []

    def walk(el: Element, trail: list[str]) -> bool:
        for child in el:
            if child is target:
                path.extend(trail)
                return True
            name = _local(child.tag)
            if name == "sec":
                title = next((c for c in child if _local(c.tag) == "title"), None)
                if walk(child, [*trail, clean(text_of(title))]):
                    return True
            elif walk(child, trail):
                return True
        return False

    walk(root, [])
    return " > ".join(p for p in path if p) or None


def parse_table(root: Element, wrap: Element) -> ParsedTable:
    label = clean(text_of(next((c for c in wrap if _local(c.tag) == "label"), None))) or (wrap.get("id") or "table")
    caption = clean(text_of(next((c for c in wrap if _local(c.tag) == "caption"), None)))
    notes: list[str] = []
    tables = [el for el in wrap.iter() if _local(el.tag) == "table"]
    table = ParsedTable(label=label, caption=caption, header_rows=[], rows=[], n_columns=0, notes=notes, element_id=wrap.get("id"),
                        section=_section_of(root, wrap), parse_method=PARSE_METHOD, parse_version=PARSE_VERSION, authorship="PUBLICATION")
    table.locator = f"{label} (JATS table-wrap id={wrap.get('id')})" + (f", section {table.section}" if table.section else "")
    foot = next((c for c in wrap if _local(c.tag) == "table-wrap-foot"), None)
    table.footnotes = _foot(foot)
    for note in table.footnotes:
        if re.search(r"abbreviations?\s*:", note["text"], re.IGNORECASE):
            table.abbreviations.update(parse_abbreviations(note["text"]))
    if not tables:
        graphic = any(_local(el.tag) in ("graphic", "inline-graphic") for el in wrap.iter())
        notes.append("the table is supplied as an image only: no machine-readable cells" if graphic else "no <table> element inside the table-wrap")
        return table

    panels: list[dict[str, Any]] = []
    for table_el in tables:
        # Each <table> of the wrap, and each header block printed inside a body, is one panel read against its own headings.
        head_count = sum(len(_rows_in(el)) for el in table_el if _local(el.tag) == "thead")
        grid_rows = _grid(table_el)
        n_cols = max((len(r) for r, _ in grid_rows), default=0)
        table.n_columns = max(table.n_columns, n_cols - 1)
        panel: dict[str, Any] | None = None
        for index, (cells, meta) in enumerate(grid_rows):
            origin = [c if (c and c["origin"]) else None for c in cells] + [None] * (n_cols - len(cells))
            texts = [c["text"] if c else "" for c in origin]
            is_header = index < head_count or meta["all_th"] or _looks_like_header_row(texts)
            if is_header:
                if panel is None or (panel["rows"] and table.rows):
                    panel = {"first_row": len(table.rows), "label": None, "headers": {}, "header_cells": [], "rows": 0, "_parts": {}}
                    panels.append(panel)
                spanning = [c for c in origin if c and c["text"] and c["colspan"] >= n_cols - 1 and n_cols > 2]
                if spanning and len([t for t in texts if t]) == 1:
                    panel["label"] = spanning[0]["text"]
                    panel["header_cells"].append([{"text": spanning[0]["text"], "col": texts.index(spanning[0]["text"]), "colspan": spanning[0]["colspan"], "rowspan": 1}])
                    continue
                row_cells = []
                for i, cell in enumerate(origin):
                    if cell and cell["text"]:
                        row_cells.append({"text": cell["text"], "col": i, "colspan": cell["colspan"], "rowspan": cell["rowspan"]})
                        for col in range(i, i + cell["colspan"]):
                            panel["_parts"].setdefault(col, []).append(cell["text"])
                panel["header_cells"].append(row_cells)
                continue
            if panel is None:
                panel = {"first_row": len(table.rows), "label": None, "headers": {}, "header_cells": [], "rows": 0, "_parts": {}}
                panels.append(panel)
                notes.append("a table body without a header row")
            stub = texts[0]
            values = {i: t for i, t in enumerate(texts) if i >= 1 and t}
            first = origin[0] if origin else None
            if stub and not values:
                table.rows.append(BodyRow(stub=stub, kind="group", footnote_refs=first["refs"] if first else [], panel=len(panels) - 1))
                panel["rows"] += 1
                continue
            indent = 1.0 if (first and first["raw_leading_space"]) else 0.0
            group = None
            seen_indented = False
            for r in reversed(table.rows):
                if r.panel != len(panels) - 1:
                    break
                # An indented label sits under the nearest row above that is not indented. A label that is not indented sits
                # under the last group label above it, unless the rows of that group are indented: then the group has ended.
                if indent and not r.indent:
                    group = r.stub
                    break
                if not indent and r.kind == "group":
                    group = None if seen_indented else r.stub
                    break
                seen_indented = seen_indented or bool(r.indent)
            table.rows.append(BodyRow(stub=stub, cells=values, group=group if (indent or not (first and first["bold"])) else None,
                                      footnote_refs=first["refs"] if first else [], indent=indent, panel=len(panels) - 1))
            panel["rows"] += 1
    for panel in panels:
        panel["headers"] = {str(k): clean(" ".join(v)) for k, v in sorted(panel.pop("_parts").items())}
        panel.pop("rows", None)
    if panels:
        table.header_rows = [[HeaderCell(**c) for c in row] for row in panels[0]["header_cells"]]
    if len(panels) > 1:
        table.panels = panels
        notes.append(f"{len(panels)} header blocks: the table has {len(panels)} panels, each read against its own headings"
                     + "".join(f"; panel {i + 1}: {p['label']!r}" for i, p in enumerate(panels) if p["label"]))
    elif panels and panels[0]["label"]:
        table.panels = panels
    table.units = collect_units(table)
    return table


def _rows_in(section: Element) -> list[Element]:
    return [el for el in section if _local(el.tag) == "tr"]


def _looks_like_header_row(texts: list[str]) -> bool:
    """A body row whose stub is empty and whose other cells print group sizes ("N = 105") is a header row."""
    if texts and texts[0]:
        return False
    others = [t for t in texts[1:] if t]
    return bool(others) and all(re.search(r"\bN\s?=\s?\d", t) for t in others)


def _grid(table_el: Element) -> list[tuple[list[dict[str, Any] | None], dict[str, Any]]]:
    """Rows of the table with colspan and rowspan resolved: every grid position holds the cell that covers it."""
    rows: list[Element] = []
    for section in table_el:
        name = _local(section.tag)
        if name in ("thead", "tbody", "tfoot"):
            rows.extend(_rows_in(section))
        elif name == "tr":
            rows.append(section)
    pending: dict[int, tuple[int, dict[str, Any]]] = {}  # column -> (rows still covered, cell)
    out: list[tuple[list[dict[str, Any] | None], dict[str, Any]]] = []
    for tr in rows:
        cells: list[dict[str, Any] | None] = []
        col = 0
        tds = [c for c in tr if _local(c.tag) in ("td", "th")]
        all_th = bool(tds) and all(_local(c.tag) == "th" for c in tds)
        it = iter(tds)
        while True:
            while col in pending:
                left, cell = pending[col]
                cells.append({**cell, "origin": False})
                if left <= 1:
                    del pending[col]
                else:
                    pending[col] = (left - 1, cell)
                col += 1
            td = next(it, None)
            if td is None:
                break
            colspan = int(td.get("colspan") or 1)
            rowspan = int(td.get("rowspan") or 1)
            fn_refs = [x for x in td.iter() if _local(x.tag) == "xref" and x.get("ref-type") in ("table-fn", "fn")]
            raw = _text_without(td, fn_refs) if fn_refs else text_of(td)
            refs = [clean(text_of(x)) for x in fn_refs if clean(text_of(x))]
            markers = [x for x in td.iter() if _local(x.tag) == "sup" and _MARKER.fullmatch(clean(text_of(x)))]
            if markers:
                # Footnote markers printed as superscript letters are references, not part of the label ("Deaths<sup>a</sup>").
                raw = _text_without(td, [*fn_refs, *markers])
                refs += [clean(text_of(x)) for x in markers]
            bold = any(_local(x.tag) == "bold" for x in td.iter()) and clean(text_of(td)) == clean("".join(text_of(x) for x in td.iter() if _local(x.tag) == "bold"))
            cell = {"text": clean(raw), "colspan": colspan, "rowspan": rowspan, "bold": bold, "refs": refs, "origin": True, "raw_leading_space": raw[:1].isspace()}
            for k in range(colspan):
                cells.append(cell if k == 0 else {**cell, "origin": False})
                if rowspan > 1:
                    pending[col + k] = (rowspan - 1, cell)
            col += colspan
        out.append((cells, {"all_th": all_th}))
    return out


_MARKER = re.compile(r"[a-z](?:\s?,\s?[a-z])*|[*†‡§¶#]+")


def _text_without(el: Element, drop: list[Element]) -> str:
    parts: list[str] = [el.text or ""]
    for child in el:
        if child in drop:
            parts.append(child.tail or "")
            continue
        parts.append(_text_without(child, drop) if len(child) else (child.text or ""))
        parts.append(child.tail or "")
    return "".join(parts)


def _split_marked(p: Element) -> list[tuple[str | None, str]]:
    """A footnote paragraph that starts with a superscript marker ("<sup>a</sup>Cause of death ...") -> [(marker, text)]."""
    first = next(iter(p), None)
    if first is not None and _local(first.tag) == "sup" and not (p.text or "").strip() and _MARKER.fullmatch(clean(text_of(first))):
        return [(clean(text_of(first)), clean(_text_without(p, [first])))]
    return [(None, clean(text_of(p)))]


def _foot(foot: Element | None) -> list[dict[str, Any]]:
    if foot is None:
        return []
    out: list[dict[str, Any]] = []

    def add(marker: str | None, text: str) -> None:
        if not text:
            return
        kind = "footnote" if marker else ("abbreviations" if re.match(r"abbreviations?\s*:", text, re.IGNORECASE) else "note")
        out.append({"marker": marker, "kind": kind, "text": text})

    for el in foot.iter():
        name = _local(el.tag)
        if name == "fn" and not any(_local(c.tag) == "p" for c in el):
            label = next((c for c in el if _local(c.tag) == "label"), None)
            add(clean(text_of(label)) or None, clean(text_of(el, skip=("label",))))
        elif name == "p":
            fn_label = None
            for marker, text in _split_marked(el):
                add(marker or fn_label, text)
    return out
