"""PDF -> page-level blocks (headings, paragraphs, tables), with page numbers kept.

Pipeline per page:
  1. find ruled tables with pymupdf's find_tables(); keep them as rows
  2. read text lines, skipping lines inside table areas and the running
     header/footer (lines that repeat across pages once digits are masked)
  3. classify lines as headings (bold or larger than body text, and numbered
     like '4.2', 'Section 4' or 'Appendix B') or paragraph text
  4. flag pages where text extraction is poor (scans or heavy tables) and render
     them to PNG so the extractor can send the image to the model as well

Assumes single-column layout. Multi-column guides would need column detection.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Literal

import pymupdf
from pydantic import BaseModel, Field

# Headings must look like "4", "4.2", "4.2.1", "Section 4", or "Appendix B" followed by a title.
HEADING_RE = re.compile(
    r"^(?:(?:section\s+)?(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\.?|(?P<app>appendix\s+[A-Z0-9]{1,2})[:.]?)\s+\S",
    re.IGNORECASE,
)
MARGIN_FRAC = 0.08          # top/bottom band scanned for running headers/footers
MIN_TEXT_CHARS = 80         # below this (with an image on the page) => likely a scan
TABLE_HEAVY_FRAC = 0.40     # table area / page area above this => send image too
RENDER_DPI = 150


class Table(BaseModel):
    page: int
    header: list[str]
    rows: list[list[str]]
    continued_from_page: int | None = None   # set when this is the tail of a table split across pages

    def to_markdown(self) -> str:
        cols = len(self.header)
        lines = [f"(table continued from page {self.continued_from_page})"] if self.continued_from_page else []
        lines += ["| " + " | ".join(self.header) + " |", "|" + "---|" * cols]
        lines += ["| " + " | ".join(r) + " |" for r in self.rows]
        return "\n".join(lines)


class Block(BaseModel):
    kind: Literal["heading", "text", "table"]
    page: int
    y0: float
    text: str = ""
    level: int | None = None        # headings only: 1 for "4" / "Appendix B", 2 for "4.2", ...
    number: str | None = None       # headings only: "4.2" or "Appendix B"
    table: Table | None = None


class Page(BaseModel):
    number: int                     # 1-based, matches what a human sees in a PDF viewer
    blocks: list[Block]
    char_count: int
    image_reasons: list[str] = Field(default_factory=list)   # e.g. ["scan"], ["table_heavy"]
    image_path: str | None = None


class ParsedGuide(BaseModel):
    path: str
    sha256: str
    retailer: str
    guide_version: str
    page_count: int
    body_font_size: float
    stripped_lines: list[str]       # running header/footer patterns removed
    pages: list[Page]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def retailer_and_version(path: Path) -> tuple[str, str]:
    """data/guides/<retailer>/<version>.pdf -> (retailer, version)."""
    return path.parent.name, path.stem


def _mask_digits(s: str) -> str:
    return re.sub(r"\d+", "#", s.strip())


def _find_running_lines(doc: pymupdf.Document) -> set[str]:
    """Digit-masked lines in the top/bottom margin that repeat on >= half the pages."""
    if doc.page_count < 3:
        return set()
    counts: Counter[str] = Counter()
    for page in doc:
        h = page.rect.height
        seen: set[str] = set()
        for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
            if y1 < h * MARGIN_FRAC or y0 > h * (1 - MARGIN_FRAC):
                for line in text.splitlines():
                    if line.strip():
                        seen.add(_mask_digits(line))
        counts.update(seen)
    return {line for line, n in counts.items() if n >= max(2, doc.page_count // 2)}


def _body_font_size(doc: pymupdf.Document) -> float:
    sizes: Counter[float] = Counter()
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                for span in line["spans"]:
                    sizes[round(span["size"], 1)] += len(span["text"].strip())
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _classify_heading(text: str, size: float, bold: bool, body: float) -> tuple[int, str] | None:
    if len(text) > 100 or text.endswith((".", ",", ";")):
        return None
    if not (bold or size >= body * 1.15):
        return None
    m = HEADING_RE.match(text)
    if not m:
        return None
    if m.group("app"):
        return 1, m.group("app").title()
    num = m.group("num")
    return num.count(".") + 1, num


def _open_edge_lines(page: pymupdf.Page) -> list[tuple[pymupdf.Point, pymupdf.Point]]:
    """Synthesize the missing top/bottom border of a table that was split across pages.

    A table continued from the previous page has vertical cell borders that start at
    the top of the content area with no horizontal rule above them (and vice versa at
    the bottom). find_tables() only sees closed cells, so we add those rules.
    """
    vert, horiz = [], []
    for d in page.get_drawings():
        r = d["rect"]
        if r.width <= 2 and r.height > 5:
            vert.append(r)
        elif r.height <= 2 and r.width > 5:
            horiz.append(r)
    extra = []
    for edge in ("y0", "y1"):
        groups: dict[int, list[pymupdf.Rect]] = {}
        for v in vert:
            groups.setdefault(round(getattr(v, edge)), []).append(v)
        for y, vs in groups.items():
            if len(vs) < 2:
                continue
            x0, x1 = min(v.x0 for v in vs), max(v.x1 for v in vs)
            if not any(abs(h.y0 - y) <= 2 and h.x0 <= x0 + 2 and h.x1 >= x1 - 2 for h in horiz):
                extra.append((pymupdf.Point(x0, y), pymupdf.Point(x1, y)))
    return extra


def _stitch_continued_tables(pages: list["Page"]) -> None:
    """If a page opens with a table matching the column count of the table that closed
    the previous page, treat it as a continuation: reuse the parent header and push the
    misdetected 'header' row back into the data rows. Rows keep their own page number."""
    for prev, cur in zip(pages, pages[1:]):
        if not prev.blocks or not cur.blocks:
            continue
        a, b = prev.blocks[-1], cur.blocks[0]
        if a.kind == b.kind == "table" and len(a.table.header) == len(b.table.header):
            if b.table.header != a.table.header:
                b.table.rows.insert(0, b.table.header)
            b.table.header = list(a.table.header)
            b.table.continued_from_page = a.page


def _clean_cell(c: str | None) -> str:
    return " ".join((c or "").split())


def _parse_page(page: pymupdf.Page, running: set[str], body: float) -> tuple[list[Block], int, float]:
    pno = page.number + 1
    page_area = page.rect.width * page.rect.height

    tables: list[Block] = []
    table_rects: list[pymupdf.Rect] = []
    table_area = 0.0
    for t in page.find_tables(add_lines=_open_edge_lines(page) or None).tables:
        rows = [[_clean_cell(c) for c in r] for r in t.extract()]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        header = [_clean_cell(n) for n in t.header.names] if not t.header.external else rows[0]
        body_rows = rows[1:] if rows and rows[0] == header else rows
        rect = pymupdf.Rect(t.bbox)
        table_rects.append(rect)
        table_area += rect.get_area()
        tables.append(Block(kind="table", page=pno, y0=rect.y0,
                            table=Table(page=pno, header=header, rows=body_rows)))

    blocks: list[Block] = []
    char_count = 0
    para: list[str] = []
    para_y0 = 0.0

    def flush() -> None:
        nonlocal para
        if para:
            blocks.append(Block(kind="text", page=pno, y0=para_y0, text=" ".join(para)))
            para = []

    for b in page.get_text("dict", sort=True)["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = " ".join("".join(s["text"] for s in line["spans"]).split())
            bbox = pymupdf.Rect(line["bbox"])
            center = (bbox.tl + bbox.br) / 2
            if any(center in r for r in table_rects):
                continue  # already captured as table rows
            if _mask_digits(text) in running:
                continue
            char_count += len(text)
            size = max(s["size"] for s in spans)
            bold = all(s["flags"] & 16 or "bold" in s["font"].lower() for s in spans)
            heading = _classify_heading(text, size, bold, body)
            if heading:
                flush()
                level, number = heading
                blocks.append(Block(kind="heading", page=pno, y0=bbox.y0, text=text, level=level, number=number))
            else:
                if not para:
                    para_y0 = bbox.y0
                para.append(text)
        flush()  # pymupdf blocks are paragraphs; don't merge across them

    char_count += sum(len(c) for t in tables for r in t.table.rows for c in r)
    merged = sorted(blocks + tables, key=lambda x: x.y0)
    return merged, char_count, table_area / page_area


def parse_guide(pdf_path: str | Path, image_dir: str | Path = "data/cache/pages") -> ParsedGuide:
    pdf_path = Path(pdf_path)
    sha = file_sha256(pdf_path)
    retailer, version = retailer_and_version(pdf_path)
    doc = pymupdf.open(pdf_path)
    running = _find_running_lines(doc)
    body = _body_font_size(doc)
    out_dir = Path(image_dir) / sha[:16]

    pages: list[Page] = []
    for page in doc:
        blocks, chars, table_frac = _parse_page(page, running, body)
        reasons = []
        if chars < MIN_TEXT_CHARS and page.get_images():
            reasons.append("scan")
        if table_frac > TABLE_HEAVY_FRAC:
            reasons.append("table_heavy")
        image_path = None
        if reasons:
            out_dir.mkdir(parents=True, exist_ok=True)
            img = out_dir / f"p{page.number + 1:03d}.png"
            if not img.exists():
                page.get_pixmap(dpi=RENDER_DPI).save(img)
            image_path = str(img)
        pages.append(Page(number=page.number + 1, blocks=blocks, char_count=chars,
                          image_reasons=reasons, image_path=image_path))

    _stitch_continued_tables(pages)
    return ParsedGuide(path=str(pdf_path), sha256=sha, retailer=retailer, guide_version=version,
                       page_count=doc.page_count, body_font_size=body,
                       stripped_lines=sorted(running), pages=pages)
