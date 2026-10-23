"""Ingestion + chunking against the committed synthetic guide."""

from pathlib import Path

import pytest

from coreframe.chunking import chunk_guide
from coreframe.ingest import parse_guide

SAMPLE = Path(__file__).resolve().parents[1] / "data/sample/guides/northwind/v2025.1.pdf"


@pytest.fixture(scope="module")
def guide(tmp_path_factory):
    return parse_guide(SAMPLE, image_dir=tmp_path_factory.mktemp("pages"))


@pytest.fixture(scope="module")
def chunks(guide):
    return {c.section: c for c in chunk_guide(guide)}


def test_identity(guide):
    assert (guide.retailer, guide.guide_version, guide.page_count) == ("northwind", "v2025.1", 6)


def test_running_header_and_footer_stripped(guide):
    assert "Page # of #" in guide.stripped_lines
    all_text = " ".join(b.text for p in guide.pages for b in p.blocks)
    assert "Page 2 of 6" not in all_text and "Compliance Guide v2025.1" not in all_text


def test_headings_detected_with_levels(guide):
    heads = {b.number: b.level for p in guide.pages for b in p.blocks if b.kind == "heading" and b.number}
    assert heads["3"] == 1 and heads["3.1"] == 2 and heads["Appendix A"] == 1
    assert len(heads) == 21  # 9 top-level + 11 subsections + Appendix A
    unnumbered = [b.text for p in guide.pages for b in p.blocks if b.kind == "heading" and not b.number]
    assert unnumbered == ["Vendor Routing & Compliance Guide", "Version 2025.1 — Effective March 1, 2025"]  # cover


@pytest.mark.parametrize("text, size, bold, body, expected", [
    ("EDI REQUIREMENTS", 12, True, 10, (1, None)),               # Academy: bold caps, larger than body
    ("GS1 LABEL PLACEMENT", 10, True, 10, (2, None)),             # Academy: bold caps at body size
    ("Section 6 – Carton Packing, Packaging, Labeling", 12, True, 12, (1, "6")),  # Foot Locker, after line merge
    ("Requirements for Shoe Boxes", 12, True, 12, (2, None)),
    ("FOOTWEAR", 12, True, 12, (2, None)),
    ("Bin:", 12, True, 12, None),                                  # inline label
    ("Date", 10, True, 10, None),                                  # lone title-case word: a table/form label
    ("GS1-128 LABEL REQUIREMENTS…..………………31", 10, True, 10, None), # table-of-contents line
    ("Hats and caps can easily be damaged when the appropriate packing procedures are not", 12, True, 12, None),
    ("Packing and Packaging Guidelines", 12, False, 12, None),     # not bold, not larger
    ("JUNCTION CITY, KS 66441", 12, True, 12, None),               # address inside a label example
    ("Note: “Color” only necessary for apparel", 12, True, 12, None),
])
def test_heading_classifier_on_real_guide_styles(text, size, bold, body, expected):
    from coreframe.ingest import _classify_heading
    assert _classify_heading(text, size, bold, body) == expected


def test_wrapped_heading_lines_are_joined():
    import pymupdf
    from coreframe.ingest import _merge_wrapped_headings
    r = pymupdf.Rect(0, 0, 10, 10)
    lines = [{"text": "Section 6 – Carton Packing,", "bbox": r, "size": 12, "bold": True},
             {"text": "Packaging, Labeling, Shipping & Loading Requirements", "bbox": r, "size": 12, "bold": True},
             {"text": "Packing and Packaging Guidelines", "bbox": r, "size": 12, "bold": True}]
    out = _merge_wrapped_headings(lines)
    assert [l["text"] for l in out] == ["Section 6 – Carton Packing, Packaging, Labeling, Shipping & Loading Requirements",
                                        "Packing and Packaging Guidelines"]


def test_every_chunk_has_pages(chunks):
    assert all(c.pages for c in chunks.values())


def test_section_path(chunks):
    assert chunks["4.2 Pallet Labels"].section_path == ["4 Labeling", "4.2 Pallet Labels"]


def test_split_table_is_stitched_and_keeps_page(chunks):
    c = chunks["3.1 Dimensions and Weight"]
    assert c.pages == [2, 3]
    head, tail = c.tables
    assert head.page == 2 and [r[0] for r in head.rows] == ["Length", "Width", "Height"]
    assert tail.page == 3 and tail.continued_from_page == 2
    assert tail.header == head.header and tail.rows == [["Weight", "1 lb", "50 lbs"]]
    assert "[page 3]" in c.text


def test_chargeback_table_structured(chunks):
    t = chunks["9 Chargeback Schedule"].tables[0]
    assert t.header == ["Violation", "Section", "Chargeback"]
    assert ["Unapproved carrier", "6.2", "3% of invoice value"] in t.rows


def test_table_text_not_duplicated_as_paragraph(guide):
    paras = [b.text for p in guide.pages for b in p.blocks if b.kind == "text"]
    assert not any("UPS, FedEx Ground" in t for t in paras)


def test_scanned_page_flagged_rendered_and_chunked(guide, chunks):
    p6 = guide.pages[5]
    assert p6.image_reasons == ["scan"] and Path(p6.image_path).exists()
    scan = chunks["[page 6: image only]"]
    assert scan.pages == [6] and scan.images[0].page == 6


def test_ligatures_are_folded(guide):
    # the PDF encodes "fi" in "identified" as U+FB01; the model and the citation check must see "fi"
    text = " ".join(b.text for p in guide.pages for b in p.blocks)
    assert "ﬁ" not in text and "identified by a unique SSCC-18" in text
