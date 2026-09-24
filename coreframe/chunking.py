"""Section-based chunking.

A new chunk starts at every heading. Each chunk carries:
  * its section title and the full heading path (so "4.2 Pallet Labels" knows
    it sits under "4 Labeling")
  * every page it touches, with inline [page N] markers in the text so the
    model can cite the right page when a section spans a page break
  * the page images for any poor-text pages it touches

Pages with no text layer at all (scans) become their own chunk, since we
can't tell which section they belong to. The model reads the heading from
the image.

Chunks over MAX_CHARS are split at block boundaries and marked "(part k)".
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from coreframe.ingest import Block, ParsedGuide, Table

MAX_CHARS = 12_000


class PageImage(BaseModel):
    page: int
    path: str
    reasons: list[str]


class Chunk(BaseModel):
    chunk_id: str
    section: str                              # "4.2 Pallet Labels"
    section_path: list[str]                   # ["4 Labeling", "4.2 Pallet Labels"]
    pages: list[int]
    text: str                                 # model-facing text with [page N] markers
    tables: list[Table] = Field(default_factory=list)
    images: list[PageImage] = Field(default_factory=list)


class _Draft:
    def __init__(self, section_path: list[str]):
        self.section_path = section_path
        self.blocks: list[Block] = []

    @property
    def has_body(self) -> bool:
        return any(b.kind != "heading" for b in self.blocks)


def _render(blocks: list[Block]) -> str:
    out: list[str] = []
    current_page = None
    for b in blocks:
        if b.page != current_page:
            current_page = b.page
            out.append(f"[page {b.page}]")
        if b.kind == "heading":
            out.append("#" * min((b.level or 1) + 1, 6) + " " + b.text)
        elif b.kind == "table":
            out.append(b.table.to_markdown())
        else:
            out.append(b.text)
    return "\n\n".join(out)


def _split(blocks: list[Block]) -> list[list[Block]]:
    parts, cur, size = [], [], 0
    for b in blocks:
        n = len(b.text) + (len(b.table.to_markdown()) if b.table else 0)
        if cur and size + n > MAX_CHARS:
            parts.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += n
    if cur:
        parts.append(cur)
    return parts


def chunk_guide(guide: ParsedGuide) -> list[Chunk]:
    images = {p.number: PageImage(page=p.number, path=p.image_path, reasons=p.image_reasons)
              for p in guide.pages if p.image_path}

    drafts: list[_Draft] = []
    stack: list[tuple[int, str]] = []          # (level, heading text)
    current = _Draft(["Front matter"])
    drafts.append(current)

    for page in guide.pages:
        if not page.blocks and page.number in images:
            # Image-only page: section unknown until the model reads it, so it gets its own chunk.
            prev = stack[-1][1] if stack else "none"
            scan = _Draft([f"[page {page.number}: image only]"])
            scan.blocks.append(Block(kind="text", page=page.number, y0=0,
                                     text=f"(No text layer. Read this page from the attached image. Preceding section: {prev}.)"))
            drafts.append(scan)
            continue
        for b in page.blocks:
            if b.kind == "heading":
                while stack and stack[-1][0] >= b.level:
                    stack.pop()
                stack.append((b.level, b.text))
                current = _Draft([h for _, h in stack])
                drafts.append(current)
            current.blocks.append(b)

    chunks: list[Chunk] = []
    for d in drafts:
        if not d.has_body:
            continue  # e.g. "4 Labeling" immediately followed by "4.1 ..."
        parts = _split(d.blocks)
        for k, blocks in enumerate(parts, 1):
            section = d.section_path[-1] + (f" (part {k})" if len(parts) > 1 else "")
            pages = sorted({b.page for b in blocks})
            chunks.append(Chunk(
                chunk_id=f"c{len(chunks) + 1:03d}",
                section=section,
                section_path=d.section_path,
                pages=pages,
                text=_render(blocks),
                tables=[b.table for b in blocks if b.table],
                images=[images[p] for p in pages if p in images],
            ))
    return chunks
