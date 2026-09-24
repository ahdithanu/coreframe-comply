"""Command-line entry point: python -m coreframe <command> ..."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from coreframe.chunking import chunk_guide
from coreframe.ingest import parse_guide


def cmd_ingest(args: argparse.Namespace) -> int:
    guide = parse_guide(args.guide, image_dir=args.cache_dir / "pages")
    chunks = chunk_guide(guide)
    out = args.out or Path("data/chunks") / f"{guide.retailer}_{guide.guide_version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"guide": guide.model_dump(exclude={"pages"}),
                               "chunks": [c.model_dump() for c in chunks]}, indent=2))

    print(f"{guide.retailer} {guide.guide_version}  sha256={guide.sha256[:12]}  "
          f"pages={guide.page_count}  body_font={guide.body_font_size}pt")
    print(f"stripped running lines: {guide.stripped_lines}")
    for p in guide.pages:
        kinds = [b.kind for b in p.blocks]
        flag = f"  IMAGE ({', '.join(p.image_reasons)})" if p.image_reasons else ""
        print(f"  p{p.number}: {kinds.count('heading')} headings, {kinds.count('text')} paragraphs, "
              f"{kinds.count('table')} tables, {p.char_count} chars{flag}")
    print(f"\n{len(chunks)} chunks -> {out}")
    for c in chunks:
        extras = []
        if c.tables:
            extras.append(f"{len(c.tables)} table")
        if c.images:
            extras.append("image p" + ",".join(str(i.page) for i in c.images))
        print(f"  {c.chunk_id}  pp.{'-'.join(map(str, [c.pages[0], c.pages[-1]])) if len(c.pages) > 1 else c.pages[0]:<5} "
              f"{' > '.join(c.section_path):<60} {len(c.text):>5} chars  {'; '.join(extras)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coreframe")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="parse a guide PDF into section chunks (debug view of phase 1)")
    p.add_argument("--guide", type=Path, required=True)
    p.add_argument("--out", type=Path)
    p.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    p.set_defaults(func=cmd_ingest)

    args = parser.parse_args(argv)
    return args.func(args)
