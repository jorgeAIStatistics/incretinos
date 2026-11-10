"""The parsed-table structure shared by the JATS and PDF parsers, and the reading of single cells.

A ``ParsedTable`` keeps a table the way the document printed it: label, caption, header rows (each cell with its
column index and span), body rows (a stub label, the cell texts by column, a row-group label, footnote markers),
footnotes, abbreviation definitions and the units the parser saw, plus ``notes`` for anything about the
structure it could not settle. Nothing in it is normalised beyond whitespace and dash variants: a cell's text is
what the document says.

Cell readers (``read_*``) recognise one printed pattern each ("−5.3 (−6.1, −4.5)", "227 (23.9)", "103.9 (22.0)",
"<0.001") and return None for anything else; they never guess a number that is not printed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

DASHES = str.maketrans({"−": "-", "–": "-", "—": "-", "‐": "-", "‑": "-", " ": " ", " ": " ", " ": " ", " ": " ", " ": " "})


def clean(text: str | None) -> str:
    """Whitespace collapsed and dash variants unified; nothing else changes."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", text.translate(DASHES)).strip()


@dataclass
class HeaderCell:
    text: str
    col: int  # first column index (0 = stub column)
    colspan: int = 1
    rowspan: int = 1


@dataclass
class BodyRow:
    stub: str
    cells: dict[int, str] = field(default_factory=dict)  # column index (>= 1) -> text as printed
    group: str | None = None  # the row-group label this row sits under, if any
    kind: str = "data"  # data | group (a label row without values)
    footnote_refs: list[str] = field(default_factory=list)
    lines: int = 1  # how many printed lines were merged into this row
    panel: int = 0  # the header block (panel) the row is read against; 0 unless headings are repeated inside the body
    indent: float = 0.0  # how far the label is indented from the label margin (points or nesting level); a sub-row of the row above when > 0


@dataclass
class ParsedTable:
    label: str
    caption: str
    header_rows: list[list[HeaderCell]]
    rows: list[BodyRow]
    n_columns: int  # data columns, the stub column excluded
    footnotes: list[dict[str, Any]] = field(default_factory=list)  # {"marker": str | None, "text": str, "kind": "footnote" | "abbreviations" | "note" | "source"}
    abbreviations: dict[str, str] = field(default_factory=dict)
    units: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source_line: str | None = None
    authorship: str = "UNKNOWN"
    locator: str = ""
    section: str | None = None
    element_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    printed_page: str | None = None
    parse_method: str = ""
    parse_version: str = ""
    # Further header blocks printed inside the body: [{"first_row", "label", "headers": {col: text}}]; panel 0 is the
    # table's own header (``header_rows``).
    panels: list[dict[str, Any]] = field(default_factory=list)

    # -- views
    def column_headers(self, panel: int = 0) -> dict[int, str]:
        """Column index -> the header text of that column (all header rows joined top to bottom) for one panel."""
        if self.panels and panel < len(self.panels):
            return {int(k): v for k, v in self.panels[panel]["headers"].items()}
        out: dict[int, list[str]] = {}
        for row in self.header_rows:
            for cell in row:
                for col in range(cell.col, cell.col + cell.colspan):
                    if cell.text:
                        out.setdefault(col, []).append(cell.text)
        return {col: clean(" ".join(parts)) for col, parts in sorted(out.items())}

    def panel_label(self, panel: int = 0) -> str | None:
        return self.panels[panel].get("label") if self.panels and panel < len(self.panels) else None

    def stub_header(self) -> str | None:
        return self.column_headers().get(0)

    def header_json(self) -> list[list[dict[str, Any]]]:
        return [[asdict(c) for c in row] for row in self.header_rows]

    def rows_json(self) -> list[dict[str, Any]]:
        return [{**asdict(r), "cells": {str(k): v for k, v in sorted(r.cells.items())}} for r in self.rows]

    def content_hash(self) -> str:
        payload = {"label": self.label, "caption": self.caption, "header": self.header_json(), "rows": self.rows_json(), "footnotes": self.footnotes,
                   "panels": self.panels}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def find_rows(self, pattern: str, *, group: str | None = None) -> list[tuple[int, BodyRow]]:
        rx = re.compile(pattern, re.IGNORECASE)
        return [(i, r) for i, r in enumerate(self.rows) if r.kind == "data" and rx.search(r.stub) and (group is None or (r.group and re.search(group, r.group, re.IGNORECASE)))]

    def footnote(self, marker: str) -> str | None:
        return next((f["text"] for f in self.footnotes if f.get("marker") == marker), None)


# --------------------------------------------------------------------------- abbreviations and units

_ABBREV_ITEM = re.compile(r"([A-Za-z][A-Za-z0-9+/\-]*(?: [A-Za-z][A-Za-z0-9+/\-]*)?)\s?=\s?([^,;]+)")


def parse_abbreviations(text: str) -> dict[str, str]:
    """"Abbreviations: CI=confidence interval, OFG=orforglipron" -> {"CI": "confidence interval", "OFG": "orforglipron"}.

    Also reads the "BMI = body mass index; eGFR = ..." form. Only explicit NAME=definition pairs are taken.
    """
    body = re.sub(r"^\s*abbreviations?\s*:\s*", "", clean(text), flags=re.IGNORECASE)
    out: dict[str, str] = {}
    for name, definition in _ABBREV_ITEM.findall(body):
        name, definition = name.strip(), definition.strip().rstrip(".")
        if name and definition and name not in out:
            out[name] = definition
    return out


_UNIT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\((%)\)", "%"),
    (r"\b(kg/m\s?2|kg/m²)\b", "kg/m2"),
    (r"\((kg)\)|,\s?(kg)\b", "kg"),
    (r"\((cm)\)|,\s?(cm)\b", "cm"),
    (r"\((mm\s?Hg)\)|,\s?(mmHg)\b", "mmHg"),
    (r"\((mg/dL)\)|,\s?(mg/dL)\b", "mg/dL"),
    (r"\((pmol/L)\)|,\s?(pmol/L)\b", "pmol/L"),
    (r"\bpercent(?:age)? change\b", "%"),
    (r",\s?%(?=\s|,|$)", "%"),
)


def units_in(text: str | None) -> list[str]:
    """Units printed in a label or caption, in order, without duplicates."""
    found: list[str] = []
    for pattern, unit in _UNIT_PATTERNS:
        if text and re.search(pattern, text, re.IGNORECASE) and unit not in found:
            found.append(unit)
    return found


def collect_units(table: ParsedTable) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for unit in units_in(table.caption):
        out.append({"where": "caption", "unit": unit, "text": table.caption})
    for col, header in table.column_headers().items():
        for unit in units_in(header):
            out.append({"where": "header", "column": col, "unit": unit, "text": header})
    for i, row in enumerate(table.rows):
        for unit in units_in(row.stub):
            out.append({"where": "row_label", "row": i, "unit": unit, "text": row.stub})
        if row.group:
            for unit in units_in(row.group):
                entry = {"where": "group_label", "row": i, "unit": unit, "text": row.group}
                if entry not in out:
                    out.append(entry)
    for note in table.footnotes:
        for unit in units_in(note.get("text")):
            out.append({"where": "footnote", "marker": note.get("marker"), "unit": unit, "text": note.get("text")})
    return out


# --------------------------------------------------------------------------- single-cell readers

_NUM = r"[-+]?\d+(?:\.\d+)?"


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float
    text: str
    irregular: str | None = None  # how the printed cell departs from the pattern, when it was still read


_INTERVAL = re.compile(rf"({_NUM})\s?%?\s?[\(\[]\s?({_NUM})\s?%?\s?(?:,|to|;)\s?({_NUM})\s?%?\s?[\)\]]")


def read_estimate_ci(text: str | None) -> Interval | None:
    """"-5.3 (-6.1, -4.5)" or "-0.12% [-0.18, -0.06]" -> estimate and limits. Anything else -> None.

    Two printing slips are read and reported (``irregular``), never silently: a space between a minus sign and its
    digits ("- 6.1") and a doubled closing bracket ("17.6))"). Nothing else is accepted.
    """
    t = clean(text)
    m = _INTERVAL.fullmatch(t)
    if m:
        return Interval(float(m.group(1)), float(m.group(2)), float(m.group(3)), t)
    slips: list[str] = []
    fixed = t
    if re.search(r"-\s+\d", fixed):
        fixed = re.sub(r"-\s+(\d)", r"-\1", fixed)
        slips.append("a space is printed between a minus sign and its digits")
    if re.search(r"[\)\]]{2}$", fixed):
        fixed = fixed[:-1]
        slips.append("the closing bracket is printed twice")
    m = _INTERVAL.fullmatch(fixed)
    if slips and m:
        return Interval(float(m.group(1)), float(m.group(2)), float(m.group(3)), t, "; ".join(slips))
    return None


def read_mean_dispersion(text: str | None) -> tuple[float, float] | None:
    """"-2.1 (0.3)" -> (-2.1, 0.3); "47.6 ± 12.9" -> (47.6, 12.9)."""
    t = clean(text)
    m = re.fullmatch(rf"({_NUM})\s?(?:\(\s?({_NUM})\s?\)|±\s?({_NUM}))", t)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2) or m.group(3))


def read_count_percent(text: str | None) -> tuple[int, float | None] | None:
    """"227 (23.9)" -> (227, 23.9); "0" -> (0, None). A bare count carries no percentage: none is invented."""
    t = clean(text)
    m = re.fullmatch(r"(\d+)\s?(?:\(\s?(\d+(?:\.\d+)?)\s?%?\s?\))?", t)
    if not m:
        return None
    return int(m.group(1)), (float(m.group(2)) if m.group(2) is not None else None)


def read_p_value(text: str | None) -> str | None:
    """"<0.001" / "0.003" / "p = 0.003" -> the printed comparison, normalised to "<0.001" / "0.003". Else None."""
    t = clean(text).replace(" ", "")
    m = re.fullmatch(r"(?:p)?([<>≤≥=]?)(0?\.\d+|1(?:\.0+)?)", t, re.IGNORECASE)
    if not m:
        return None
    return f"{m.group(1) if m.group(1) != '=' else ''}{m.group(2)}"


def read_header_n(header: str | None) -> int | None:
    """"Placebo N=949" / "Orforglipron N = 124" -> 949 / 124 (the N printed in the column header)."""
    m = re.search(r"\bN\s?=\s?(\d[\d,]*)\b", clean(header))
    return int(m.group(1).replace(",", "")) if m else None


def ci_level_in(text: str | None) -> float | None:
    m = re.search(r"(\d{2}(?:\.\d+)?)\s?%\s?(?:CI|confidence)", clean(text), re.IGNORECASE)
    return float(m.group(1)) if m else None
