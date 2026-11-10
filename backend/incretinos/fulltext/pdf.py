"""Tables of an FDA review PDF, read from the positioned text of its pages (pypdf).

Only text-layer PDFs are supported: every printed glyph run comes with its position, font and size, and the
widths of the embedded fonts give where each run ends. A table is found by its caption line ("FDA Table 23. ...",
bold), its header rows (bold lines that carry the column headings), its body (until the "Source:" line) and its
footnotes (the small-type block after the source line). Columns are anchored on the header headings; a body cell
is placed in the column whose heading centre is nearest, and a cell that cannot be placed is reported in the
table's notes instead of being dropped silently. Wrapped labels and wrapped cells are merged by explicit rules
(a label ending in "," or "(" continues, a line with values and no label continues the row above), and every merge
is counted on the row.

Scope (``authorship``): only a table whose source line names an FDA analysis or an FDA reviewer is FDA-authored
(US-government work). Tables copied from the applicant's submission are recognised and not extracted.

``excerpt`` writes a page excerpt for the offline fixture in which every glyph run outside the kept regions (the
extracted tables, configured FDA passages, the running header and footer) is blanked, so the committed fixture
carries no applicant-authored text.
"""

from __future__ import annotations

import io
import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .tables import BodyRow, HeaderCell, ParsedTable, clean, collect_units, parse_abbreviations

PARSE_METHOD = "pdf-positioned-text/pypdf"
PARSE_VERSION = "1"

# Standard Helvetica advance widths (1/1000 em), used only for a font that carries no width table.
_HELVETICA = dict(zip(
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
    [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, *[556] * 10, 278, 278, 584, 584, 584, 556, 1015,
     667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611,
     278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278,
     556, 500, 722, 500, 500, 500, 334, 260, 334, 584],
    strict=True,
))


@dataclass
class Chunk:
    """One glyph run as printed: position of its baseline start, its end, font size and weight."""

    x0: float
    x1: float
    y: float
    size: float
    bold: bool
    text: str


@dataclass
class Phrase:
    x0: float
    x1: float
    text: str
    bold: bool
    sup: list[str] = field(default_factory=list)  # superscript markers printed right after the phrase
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Line:
    y: float
    size: float
    bold: bool
    phrases: list[Phrase]
    lead_sup: str | None = None  # a superscript marker printed before the first phrase (footnotes)

    @property
    def text(self) -> str:
        return clean(" ".join(p.text for p in self.phrases))

    @property
    def x0(self) -> float:
        return self.phrases[0].x0 if self.phrases else 0.0


class _Widths:
    def __init__(self) -> None:
        self._cache: dict[int, tuple[int, list[float], float]] = {}

    def width(self, text: str, font: Any, size: float) -> float:
        first, widths, default = self._table(font)
        total = 0.0
        for ch in text:
            try:
                code = ch.encode("cp1252")[0]
            except (UnicodeEncodeError, IndexError):
                code = -1
            w = widths[code - first] if widths and 0 <= code - first < len(widths) else _HELVETICA.get(ch, default)
            total += float(w)
        return total * size / 1000.0

    def _table(self, font: Any) -> tuple[int, list[float], float]:
        if font is None:
            return 0, [], 556.0
        key = id(font)
        if key not in self._cache:
            try:
                widths = [float(w) for w in (font.get("/Widths") or [])]
                first = int(font.get("/FirstChar") or 0)
            except Exception:  # an unusual font dictionary: fall back to Helvetica metrics
                widths, first = [], 0
            self._cache[key] = (first, widths, 556.0)
        return self._cache[key]


def page_chunks(page: Any) -> list[Chunk]:
    """Every non-blank glyph run of a page with its geometry (unrotated pages, identity text scaling assumed)."""
    out: list[Chunk] = []
    widths = _Widths()

    def visit(text: str, cm: Sequence[float], tm: Sequence[float], font: Any, size: float) -> None:
        if not text or not text.strip():
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        scale = abs(tm[0] * cm[0]) or 1.0
        eff = float(size) * scale
        base = str(font.get("/BaseFont", "")) if font is not None else ""
        lead = len(text) - len(text.lstrip())
        x_start = x + widths.width(text[:lead], font, eff)
        out.append(Chunk(round(x_start, 2), round(x + widths.width(text.rstrip(), font, eff), 2), round(y, 2), round(eff, 2), "Bold" in base, text.strip()))

    page.extract_text(visitor_text=visit)
    return out


def lines_of(chunks: Iterable[Chunk], *, tol: float = 1.6) -> list[Line]:
    """Group runs into printed lines (top to bottom); small raised runs become superscript markers."""
    chunks = list(chunks)
    if not chunks:
        return []
    normal = [c for c in chunks if not _is_small(c, chunks)]
    small = [c for c in chunks if _is_small(c, chunks)]
    rows: list[list[Chunk]] = []
    for c in sorted(normal, key=lambda c: (-c.y, c.x0)):
        if rows and abs(rows[-1][0].y - c.y) <= tol:
            rows[-1].append(c)
        else:
            rows.append([c])
    lines: list[Line] = []
    for row in rows:
        row.sort(key=lambda c: c.x0)
        size = statistics.median(c.size for c in row)
        lines.append(Line(y=row[0].y, size=size, bold=all(c.bold for c in row), phrases=_phrases(row, size)))
    for s in small:  # attach each marker to the nearest line just below it
        target = min((ln for ln in lines if -1.0 <= s.y - ln.y <= 6.0), key=lambda ln: abs(s.y - ln.y), default=None)
        if target is None:
            continue
        before = [p for p in target.phrases if p.x1 <= s.x0 + 1.0]
        if not before:
            target.lead_sup = s.text
        else:
            before[-1].sup.append(s.text)
    return lines


def _is_small(c: Chunk, chunks: list[Chunk]) -> bool:
    # A superscript: noticeably smaller than the runs on (about) the same baseline, and a short marker.
    if len(c.text) > 3:
        return False
    peers = [o.size for o in chunks if o is not c and abs(o.y - c.y) <= 6.0 and len(o.text) > 0]
    return bool(peers) and c.size < 0.8 * max(peers)


def _phrases(row: list[Chunk], size: float) -> list[Phrase]:
    phrases: list[Phrase] = []
    for c in row:
        # Runs closer than about one and a half word spaces belong to one phrase; table cells are further apart.
        if phrases and c.x0 - phrases[-1].x1 <= 0.42 * size:
            last = phrases[-1]
            gap = c.x0 - last.x1
            last.text = last.text + (" " if gap > 0.15 * size else "") + c.text
            last.x1 = max(last.x1, c.x1)
            last.bold = last.bold and c.bold
            last.chunks.append(c)
        else:
            phrases.append(Phrase(c.x0, c.x1, c.text, c.bold, chunks=[c]))
    for p in phrases:
        p.text = clean(p.text)
    return phrases


# --------------------------------------------------------------------------- tables

_CAPTION = re.compile(r"^FDA Table (\d+)\.\s*(.*)$")
_SOURCE = re.compile(r"^Source\s*:", re.IGNORECASE)
_FDA_SOURCE = re.compile(r"FDA analysis|FDA (?:statistical |clinical |medical )?reviewer|reviewer'?s analysis|FDA'?s analysis", re.IGNORECASE)
_APPLICANT_SOURCE = re.compile(r"Applicant|NDA\s?\d+,\s?Module|CSR|sponsor", re.IGNORECASE)


@dataclass
class PageText:
    number: int  # 1-based page index in the document as retrieved
    lines: list[Line]
    plain: str = ""  # pypdf's own text extraction of the page (its word spacing is used for sentences)

    def printed_page(self) -> str | None:
        """The page number printed in the footer band (a line holding only digits)."""
        for ln in self.lines:
            if 80.0 < ln.y < FOOTER_Y and re.fullmatch(r"\d{1,4}", ln.text):
                return ln.text
        return None


def read_pages(pdf_bytes: bytes, page_numbers: Iterable[int] | None = None, *, numbering: Sequence[int] | None = None) -> tuple[list[PageText], int]:
    """Positioned lines of the given pages (1-based; all pages when None) and the file's page count.

    ``numbering`` gives, for a page excerpt, the page number each of its pages has in the document as retrieved; the
    returned pages carry those numbers, so every locator names the page of the original document.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    if numbering is not None:
        if len(numbering) != total:
            raise ValueError(f"the excerpt has {total} pages but its manifest maps {len(numbering)}")
        wanted_set = set(page_numbers) if page_numbers is not None else set(numbering)
        return [_page_text(reader.pages[i], original) for i, original in enumerate(numbering) if original in wanted_set], total
    wanted = list(range(1, total + 1)) if page_numbers is None else [p for p in page_numbers if 1 <= p <= total]
    return [_page_text(reader.pages[p - 1], p) for p in wanted], total


def _page_text(page: Any, number: int) -> PageText:
    return PageText(number, lines_of(page_chunks(page)), clean(page.extract_text() or ""))


def find_caption(pages: Sequence[PageText], label: str) -> tuple[int, int] | None:
    """(page position in ``pages``, line index) of the caption "FDA Table N." — None when it is not printed."""
    number = re.search(r"(\d+)", label)
    for pi, page in enumerate(pages):
        for li, ln in enumerate(page.lines):
            m = _CAPTION.match(ln.text)
            if m and ln.bold and number and m.group(1) == number.group(1):
                return pi, li
    return None


#: The running header (document title, trade name, "(b) (4)" labels) prints above this y; the body never does.
HEADER_Y = 715.0
#: The running footer (printed page number, version date, disclaimer, Reference ID) prints below this y.
FOOTER_Y = 112.0


def _is_running_header_or_footer(ln: Line) -> bool:
    return ln.y > HEADER_Y or ln.y < FOOTER_Y


def extract_table(pages: Sequence[PageText], label: str) -> ParsedTable | None:
    """Parse one FDA table, following it onto the next page when its source line is not on the caption page."""
    found = find_caption(pages, label)
    if found is None:
        return None
    pi, li = found
    page = pages[pi]
    caption_line = page.lines[li]
    caption = caption_line.text
    notes: list[str] = []
    i = li + 1
    # Caption continuation: bold lines that start at the caption's own margin (cells are indented by the cell padding).
    while i < len(page.lines) and page.lines[i].bold and page.lines[i].x0 < caption_line.x0 + 1.0 and len(page.lines[i].phrases) == 1:
        caption = clean(caption + " " + page.lines[i].text)
        i += 1
    table_lines: list[tuple[int, Line]] = []
    footer_lines: list[Line] = []
    source_line: str | None = None
    page_end = page.number
    cursor_page, cursor = pi, i
    header_signature: list[str] | None = None
    while cursor_page < len(pages):
        lines = pages[cursor_page].lines
        while cursor < len(lines):
            ln = lines[cursor]
            if source_line is None:
                if _SOURCE.match(ln.text):
                    source_line = ln.text
                    page_end = pages[cursor_page].number
                elif not _is_running_header_or_footer(ln):
                    table_lines.append((pages[cursor_page].number, ln))
            else:
                if ln.size >= 9.5 or _is_running_header_or_footer(ln):
                    break
                footer_lines.append(ln)
            cursor += 1
        if source_line is not None:
            break
        # The table continues on the next page (when that page was read): its repeated heading lines are skipped below.
        if cursor_page + 1 < len(pages) and pages[cursor_page + 1].number == pages[cursor_page].number + 1:
            cursor_page += 1
            cursor = 0
            notes.append(f"continues on page {pages[cursor_page].number}")
            continue
        notes.append("no source line found after the caption on the pages read; the table may continue on a page that was not read")
        break

    header_lines, body_lines = _split_header(table_lines)
    if not header_lines:
        notes.append("no header row recognised")
        return ParsedTable(label=label, caption=caption, header_rows=[], rows=[], n_columns=0, notes=notes, source_line=source_line,
                           page_start=page.number, page_end=page_end, printed_page=page.printed_page(), parse_method=PARSE_METHOD, parse_version=PARSE_VERSION)
    header_signature = [ln.text for ln in header_lines]
    # A heading repeated at the top of a continuation page is not a body row.
    body_lines = _drop_repeated_header(body_lines, header_signature)
    stub_x = min(ln.x0 for ln in header_lines + [b for _, b in body_lines]) if body_lines else header_lines[0].x0
    columns, header_rows = _columns(header_lines, stub_x)
    rows = _rows(body_lines, columns, stub_x, notes)
    table = ParsedTable(
        label=label, caption=caption, header_rows=header_rows, rows=rows, n_columns=len(columns), notes=notes, source_line=source_line,
        page_start=page.number, page_end=page_end, printed_page=page.printed_page(), parse_method=PARSE_METHOD, parse_version=PARSE_VERSION,
    )
    table.footnotes = _footnotes(footer_lines, source_line)
    for note in table.footnotes:
        if note["kind"] == "abbreviations":
            table.abbreviations.update(parse_abbreviations(note["text"]))
    table.authorship = authorship(source_line)
    table.units = collect_units(table)
    pages_text = f"PDF page {page.number}" if page_end == page.number else f"PDF pages {page.number}–{page_end}"
    table.locator = f"{label}, {pages_text}" + (f" (printed page {table.printed_page})" if table.printed_page else "")
    return table


def authorship(source_line: str | None) -> str:
    if not source_line:
        return "UNKNOWN"
    if _FDA_SOURCE.search(source_line):
        return "FDA_ANALYSIS"
    if _APPLICANT_SOURCE.search(source_line):
        return "APPLICANT"
    return "UNKNOWN"


def _is_header_line(ln: Line) -> bool:
    return ln.bold and (len(ln.phrases) >= 2 or (ln.phrases and ln.phrases[0].x0 > 150.0))


def _split_header(table_lines: list[tuple[int, Line]]) -> tuple[list[Line], list[tuple[int, Line]]]:
    header: list[Line] = []
    k = 0
    while k < len(table_lines) and _is_header_line(table_lines[k][1]):
        header.append(table_lines[k][1])
        k += 1
    return header, table_lines[k:]


def _drop_repeated_header(body: list[tuple[int, Line]], signature: list[str]) -> list[tuple[int, Line]]:
    out: list[tuple[int, Line]] = []
    for page_no, ln in body:
        if ln.bold and ln.text in signature:
            continue
        out.append((page_no, ln))
    return out


@dataclass
class _Column:
    index: int
    center: float
    x0: float
    x1: float


def _columns(header_lines: list[Line], stub_x: float) -> tuple[list[_Column], list[list[HeaderCell]]]:
    heading = [p for ln in header_lines for p in ln.phrases if p.x0 > stub_x + 40.0]
    clusters: list[list[Phrase]] = []
    for p in sorted(heading, key=lambda p: p.center):
        if clusters and abs(statistics.mean(q.center for q in clusters[-1]) - p.center) <= 25.0:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    columns = [_Column(i + 1, statistics.mean(q.center for q in c), min(q.x0 for q in c), max(q.x1 for q in c)) for i, c in enumerate(clusters)]
    header_rows: list[list[HeaderCell]] = []
    for ln in header_lines:
        row: list[HeaderCell] = []
        for p in ln.phrases:
            if p.x0 <= stub_x + 40.0:
                row.append(HeaderCell(text=p.text, col=0))
                continue
            covered = [c.index for c in columns if p.x0 - 2.0 <= c.center <= p.x1 + 2.0]
            first = min(covered) if covered else _nearest(columns, p.center).index
            row.append(HeaderCell(text=p.text, col=first, colspan=max(1, len(covered))))
        header_rows.append(row)
    return columns, header_rows


def _nearest(columns: list[_Column], x: float) -> _Column:
    return min(columns, key=lambda c: abs(c.center - x))


def _split_across_columns(p: Phrase, columns: list[_Column]) -> list[Phrase]:
    """A phrase whose runs sit nearest to different columns, separated by a real gap, is two cells printed close together."""
    if len(p.chunks) < 2 or len(columns) < 2:
        return [p]
    parts: list[list[Chunk]] = [[p.chunks[0]]]
    for prev, c in zip(p.chunks, p.chunks[1:], strict=False):
        prev_col = _nearest(columns, (prev.x0 + prev.x1) / 2).index
        col = _nearest(columns, (c.x0 + c.x1) / 2).index
        if col != prev_col and c.x0 - prev.x1 >= 0.25 * c.size:
            parts.append([c])
        else:
            parts[-1].append(c)
    if len(parts) == 1:
        return [p]
    out = []
    for part in parts:
        text = part[0].text
        for prev, c in zip(part, part[1:], strict=False):
            text += (" " if c.x0 - prev.x1 > 0.15 * c.size else "") + c.text
        out.append(Phrase(part[0].x0, part[-1].x1, clean(text), all(c.bold for c in part), chunks=part))
    out[-1].sup = list(p.sup)
    return out


def _stub_limit(columns: list[_Column], stub_x: float) -> float:
    """Phrases starting left of this x are row labels: half-way between the label margin and the first column."""
    return (stub_x + columns[0].center) / 2 if columns else stub_x + 10.0


def _rows(body_lines: list[tuple[int, Line]], columns: list[_Column], stub_x: float, notes: list[str]) -> list[BodyRow]:
    rows: list[BodyRow] = []
    group: str | None = None
    limit = _stub_limit(columns, stub_x)
    for page_no, ln in body_lines:
        stub_parts = [p for p in ln.phrases if p.x0 < limit]
        stub = clean(" ".join(p.text for p in stub_parts))
        indent = round(stub_parts[0].x0 - stub_x, 1) if stub_parts else 0.0
        refs = [s for p in stub_parts for s in p.sup]
        cells: dict[int, str] = {}
        value_phrases = [q for p in ln.phrases if p not in stub_parts for q in _split_across_columns(p, columns)]
        for p in value_phrases:
            if not columns:
                notes.append(f"page {page_no}: value {p.text!r} printed but no column heading was recognised")
                continue
            col = _nearest(columns, p.center)
            if col.index in cells:
                notes.append(f"page {page_no}: two printed values fall in column {col.index} on the line {ln.text!r}; joined")
                cells[col.index] = clean(cells[col.index] + " " + p.text)
            else:
                cells[col.index] = p.text
            if p.sup:
                notes.append(f"page {page_no}: superscript {', '.join(p.sup)} printed after the value {p.text!r} (kept as a footnote reference of the row)")
                refs += p.sup
        prev = rows[-1] if rows else None
        open_label = prev is not None and prev.kind == "data" and (prev.stub.endswith(",") or prev.stub.endswith("("))
        if stub and not cells:
            if open_label or (prev is not None and prev.kind == "data" and stub.startswith("(") and not prev.cells):
                prev.stub = clean(prev.stub + " " + stub)
                prev.footnote_refs += refs
                prev.lines += 1
            elif prev is not None and prev.kind == "group" and rows and not prev.cells:
                prev.stub = clean(prev.stub + " " + stub)
                group = prev.stub
                prev.lines += 1
            else:
                rows.append(BodyRow(stub=stub, kind="group", footnote_refs=refs, indent=indent))
                group = stub
            # A label line that was not the continuation of an open data row starts or extends a group.
            continue
        if not stub and cells:
            if prev is None:
                notes.append(f"page {page_no}: values {list(cells.values())!r} printed before any row label; not attached")
                continue
            if prev.kind == "group":
                prev.kind = "data"  # a label printed on its own line with the values below it
                group = None
                prev.group = _group_above(rows)
            for col, text in cells.items():
                prev.cells[col] = clean(prev.cells.get(col, "") + " " + text) if col in prev.cells else text
            prev.lines += 1
            continue
        if stub and cells and open_label:
            prev.stub = clean(prev.stub + " " + stub)
            for col, text in cells.items():
                prev.cells[col] = clean(prev.cells.get(col, "") + " " + text) if col in prev.cells else text
            prev.footnote_refs += refs
            prev.lines += 1
            continue
        if stub or cells:
            rows.append(BodyRow(stub=stub, cells=cells, group=group, footnote_refs=refs, indent=indent))
    for row in rows:
        if row.lines > 1:
            notes.append(f"row {row.stub!r}: {row.lines} printed lines merged")
    return rows


def _group_above(rows: list[BodyRow]) -> str | None:
    for row in reversed(rows[:-1]):
        if row.kind == "group":
            return row.stub
    return None


def _footnotes(lines: list[Line], source_line: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if source_line:
        out.append({"marker": None, "kind": "source", "text": source_line})
    for ln in lines:
        text = ln.text
        if ln.lead_sup:
            out.append({"marker": ln.lead_sup, "kind": "footnote", "text": text})
        elif re.match(r"^Abbreviations?\s*:", text, re.IGNORECASE):
            out.append({"marker": None, "kind": "abbreviations", "text": text})
        elif re.match(r"^(Cell content|Note|Data are)\b", text, re.IGNORECASE):
            out.append({"marker": None, "kind": "note", "text": text})
        elif out and out[-1]["kind"] != "source":
            out[-1]["text"] = clean(out[-1]["text"] + " " + text)
        else:
            out.append({"marker": None, "kind": "note", "text": text})
    return out


def table_region(pages: Sequence[PageText], label: str) -> list[tuple[int, float, float]]:
    """(page number, y low, y high) bands the table occupies, caption to last footnote: what an excerpt keeps."""
    found = find_caption(pages, label)
    if found is None:
        return []
    pi, li = found
    bands: list[tuple[int, float, float]] = []
    top = pages[pi].lines[li].y + 12.0
    for page in pages[pi:]:
        lines = page.lines
        start = li if page is pages[pi] else 0
        low = None
        seen_source = False
        for ln in lines[start:]:
            if _is_running_header_or_footer(ln):
                if seen_source:
                    break
                continue
            if seen_source and ln.size >= 9.5:
                break
            if _SOURCE.match(ln.text):
                seen_source = True
            low = ln.y
        if low is not None:
            bands.append((page.number, low - 4.0, top))
        if seen_source:
            break
        top = HEADER_Y
    return bands


def passage_region(page: PageText, starts_with: str, *, until: str | None = None) -> tuple[float, float] | None:
    """The band of lines from the line containing ``starts_with`` to the line containing ``until`` (or that line)."""
    start = next((ln for ln in page.lines if starts_with.lower() in ln.text.lower()), None)
    if start is None:
        return None
    end = start if until is None else next((ln for ln in page.lines if until.lower() in ln.text.lower() and ln.y <= start.y), start)
    return end.y - 4.0, start.y + 12.0


_HEADING_FDA = re.compile(r"^The FDA[’']s Assessment\b", re.IGNORECASE)
_HEADING_APPLICANT = re.compile(r"^(?:The Applicant[’']s (?:Position|Description)|Data)\s*:", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?=\s+[A-Z(\[]|\s*$)")


@dataclass
class Passage:
    page: int
    sentence: str
    first_line: int
    last_line: int
    authored_by_fda: bool | None  # None: no heading found above it on the pages read
    heading: str | None
    heading_page: int | None
    heading_line: int | None


def _norm_quotes(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def find_passage(pages: Sequence[PageText], page_number: int, starts_with: str) -> Passage | None:
    """The complete sentence that begins with ``starts_with`` on the given page, and who wrote that part of the review.

    The sentence runs to the first full stop followed by a capitalised word (or the end of the page). Authorship is
    the nearest section heading above it on the pages read: "The FDA's Assessment" (FDA) or "Data:" / "The
    Applicant's Position:" / "The Applicant's Description:" (applicant).
    """
    pi = next((i for i, p in enumerate(pages) if p.number == page_number), None)
    if pi is None:
        return None
    page = pages[pi]
    # The sentence is taken from pypdf's plain text of the page (reliable word spacing); the printed lines it spans
    # are found by comparing the text with all whitespace removed.
    hay = _norm_quotes(page.plain)
    at = hay.lower().find(_norm_quotes(clean(starts_with)).lower())
    if at < 0:
        return None
    end_match = _SENTENCE_END.search(hay, at + len(starts_with) - 1)
    end = end_match.end() if end_match else len(hay)
    sentence = page.plain[at:end].strip()
    body = [(i, ln) for i, ln in enumerate(page.lines) if not _is_running_header_or_footer(ln)]
    squeezed = ""
    spans: list[tuple[int, int, int]] = []
    for i, ln in body:
        start = len(squeezed)
        squeezed += re.sub(r"\s+", "", _norm_quotes(ln.text))
        spans.append((i, start, len(squeezed)))
    target = re.sub(r"\s+", "", _norm_quotes(sentence))
    pos = squeezed.find(target)
    if pos < 0:
        return None
    first = next(i for i, s0, s1 in spans if s0 <= pos < s1)
    last = next((i for i, s0, s1 in spans if s0 < pos + len(target) <= s1), first)
    authored: bool | None = None
    heading = heading_page = heading_line = None
    for qi in range(pi, -1, -1):
        lines = pages[qi].lines
        top = first if qi == pi else len(lines)
        for li in range(top - 1, -1, -1):
            text = _norm_quotes(lines[li].text)
            if _HEADING_FDA.match(text) or _HEADING_APPLICANT.match(text):
                authored = bool(_HEADING_FDA.match(text))
                heading, heading_page, heading_line = lines[li].text, pages[qi].number, li
                break
        if authored is not None:
            break
        if qi > 0 and pages[qi - 1].number != pages[qi].number - 1:
            break  # a page in between was not read: the heading above cannot be known
    return Passage(page_number, sentence, first, last, authored, heading, heading_page, heading_line)


def page_sentences(page: PageText) -> str:
    """The page's body text (running header and footer excluded), as one whitespace-normalised string."""
    return clean(" ".join(ln.text for ln in page.lines if not _is_running_header_or_footer(ln)))


def _mul(a: Sequence[float], b: Sequence[float]) -> list[float]:
    """Product of two PDF matrices [a b c d e f] (a applied first)."""
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def excerpt(pdf_bytes: bytes, keep: dict[int, list[tuple[float, float]]]) -> bytes:
    """A PDF of the pages in ``keep`` (1-based) in which every glyph run outside the given y-bands is blanked.

    The running header and footer bands are always kept. The page's content stream is walked with its graphics and
    text state (q/Q, cm, BT, Tm, Td, TD, TL, T*) to know where each text-showing operator prints; the strings of
    the operators outside the bands are emptied, so the kept text keeps exactly its position, font and size. Form
    XObjects (in this review: the "(b) (4)" redaction labels and the "Reference ID" stamp) are not drawn at all,
    because their content is not walked here.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, ContentStream, NameObject, TextStringObject

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for number in sorted(keep):
        page = reader.pages[number - 1]
        bands = [*keep[number], (HEADER_Y, 10_000.0), (-10_000.0, FOOTER_Y)]
        forms = {name for name, ref in ((page.get("/Resources") or {}).get("/XObject") or {}).items() if ref.get_object().get("/Subtype") == "/Form"}
        content = ContentStream(page.get_contents(), reader)
        ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        stack: list[list[float]] = []
        tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        tlm = list(tm)
        leading = 0.0
        out_ops = []
        for operands, operator in content.operations:
            op = operator.decode("latin-1") if isinstance(operator, bytes) else str(operator)
            vals = [float(v) for v in operands] if op in ("cm", "Tm", "Td", "TD", "TL") else []
            if op == "q":
                stack.append(list(ctm))
            elif op == "Q":
                ctm = stack.pop() if stack else ctm
            elif op == "cm":
                ctm = _mul(vals, ctm)
            elif op == "BT":
                tm, tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            elif op == "Tm":
                tm, tlm = list(vals), list(vals)
            elif op in ("Td", "TD"):
                if op == "TD":
                    leading = -vals[1]
                tlm = _mul([1.0, 0.0, 0.0, 1.0, vals[0], vals[1]], tlm)
                tm = list(tlm)
            elif op == "TL":
                leading = vals[0]
            elif op in ("T*", "'", '"'):
                tlm = _mul([1.0, 0.0, 0.0, 1.0, 0.0, -leading], tlm)
                tm = list(tlm)
            elif op == "Do" and operands and str(operands[0]) in forms:
                continue  # not walked, so not drawn
            if op in ("Tj", "TJ", "'", '"'):
                y = _mul(tm, ctm)[5]
                if not any(low <= y <= high for low, high in bands):
                    operands = list(operands)
                    operands[-1] = ArrayObject() if op == "TJ" else TextStringObject("")
            out_ops.append((operands, operator))
        content.operations = out_ops
        page[NameObject("/Contents")] = content
        writer.add_page(page)
    for page in writer.pages:
        page.compress_content_streams()
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
