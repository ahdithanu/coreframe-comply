"""Build the synthetic Northwind Retail routing guide and its ground truth.

Northwind Retail Co. is fictional. The guide is written to exercise the
failure modes real routing guides produce:

  * rules stated only in tables (3.1 carton limits, 6.2 carriers, 9 chargebacks)
  * conditional rules (parcel-only weight cap, LTL/TL-only pallet footprint)
  * cross-references (chargebacks in Section 9 point back to 2.x-8.x;
    pallet stacking in Section 5 points into Appendix B)
  * a scanned page with no text layer (Appendix B)
  * rules that cannot be parameterized against a Shipment (burst strength,
    "legible", late-arrival refusal)
  * the same requirement stated twice (SSCC in 2.3 and 4.1)
  * a running header/footer that ingestion must strip
  * non-rule text (glossary) that extraction should NOT turn into rules

Usage:  python scripts/build_sample_guide.py
Writes: data/sample/guides/northwind/v2025.1.pdf
        data/sample/ground_truth/northwind_v2025.1.csv
"""

from __future__ import annotations

import csv
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
GUIDE_PATH = ROOT / "data/sample/guides/northwind/v2025.1.pdf"
GT_PATH = ROOT / "data/sample/ground_truth/northwind_v2025.1.csv"

LETTER = pymupdf.paper_rect("letter")
BODY = LETTER + (60, 72, -60, -72)

CSS = """
body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.35; }
h1 { font-size: 16pt; font-weight: bold; margin-top: 16pt; margin-bottom: 6pt; }
h2 { font-size: 12.5pt; font-weight: bold; margin-top: 10pt; margin-bottom: 4pt; }
p  { margin-bottom: 6pt; }
table { border-collapse: collapse; margin-bottom: 8pt; }
th, td { border: 1px solid black; padding: 3pt 6pt; font-size: 10pt; }
th { font-weight: bold; }
.cover { font-size: 22pt; font-weight: bold; margin-top: 180pt; }
.sub { font-size: 13pt; }
.note { font-size: 9pt; font-style: italic; }
"""

COVER_HTML = """
<p class="cover">Northwind Retail Co.<br/>Vendor Routing &amp; Compliance Guide</p>
<p class="sub">Version 2025.1 &#8212; Effective March 1, 2025</p>
<p class="note">SYNTHETIC SAMPLE. Northwind Retail Co. is a fictional retailer. This document was
written to test Coreframe Comply and does not describe any real retailer's requirements.</p>
"""

BODY_HTML = """
<h1>1 General Requirements</h1>
<h2>1.1 Scope</h2>
<p>This guide applies to all merchandise shipped to Northwind distribution centers (DCs).
Vendors are responsible for the compliance of any third-party logistics provider acting on
their behalf.</p>
<h2>1.2 Compliance Program</h2>
<p>Non-compliance results in chargebacks as described in Section 9. Chargebacks are deducted
from vendor invoices. Chargeback disputes must be submitted through the Vendor Portal within
30 days of the chargeback notice.</p>
<p>Merchandise must be packed to withstand normal handling in transit.</p>

<h1>2 Advance Ship Notice (ASN)</h1>
<h2>2.1 ASN Requirement</h2>
<p>Vendors must transmit an EDI 856 Advance Ship Notice for every shipment.</p>
<h2>2.2 ASN Timing</h2>
<p>For LTL and TL shipments, the ASN must be received by Northwind no later than 2 hours
before the scheduled delivery appointment.</p>
<p>For parcel shipments, the ASN must be transmitted within 60 minutes after the parcel is
tendered to the carrier.</p>
<h2>2.3 ASN Content</h2>
<p>Each carton listed in the ASN must be identified by a unique SSCC-18 that matches the
SSCC printed on the carton label.</p>

<h1>3 Carton Requirements</h1>
<h2>3.1 Dimensions and Weight</h2>
<p>Cartons must fall within the limits below.</p>
<table>
<tr><th>Attribute</th><th>Minimum</th><th>Maximum</th></tr>
<tr><td>Length</td><td>6 in</td><td>24 in</td></tr>
<tr><td>Width</td><td>4 in</td><td>18 in</td></tr>
<tr><td>Height</td><td>2 in</td><td>18 in</td></tr>
<tr><td>Weight</td><td>1 lb</td><td>50 lbs</td></tr>
</table>
<p>Cartons shipped via a parcel carrier must not exceed 35 lbs. This limit supersedes the
table above for parcel shipments.</p>
<h2>3.2 Carton Construction</h2>
<p>Cartons must be corrugated with a minimum burst strength of 200 psi (ECT 32).
Do not use banding or strapping on individual cartons.</p>

<h1>4 Labeling</h1>
<h2>4.1 Carton Labels</h2>
<p>Every carton must carry a 4 in x 6 in GS1-128 shipping label containing a scannable
SSCC-18 barcode.</p>
<p>The label must be placed on the longest side of the carton, in the lower right corner,
no closer than 1.25 inches to any edge.</p>
<p>Labels must be legible and must not be placed over carton seams or covered with tape.</p>
<h2>4.2 Pallet Labels</h2>
<p>Each pallet must carry two pallet labels, formatted as specified in Section 4.1, affixed
to two adjacent sides of the pallet.</p>

<h1>5 Pallet Requirements</h1>
<p>All LTL and TL shipments must be palletized on 40 x 48 inch GMA pallets.</p>
<p>Maximum pallet height is 72 inches (183 cm), including the pallet.</p>
<p>Maximum gross pallet weight is 2,000 lbs.</p>
<p>Double-stacked pallets are accepted at all DCs except those listed in Appendix B.</p>

<h1>6 Routing and Carrier Selection</h1>
<h2>6.1 Routing Requests</h2>
<p>Shipments of 150 lbs or more must be routed through the Northwind Routing Portal at least
3 business days before the ship date.</p>
<h2>6.2 Approved Carriers</h2>
<table>
<tr><th>Mode</th><th>Approved Carriers</th></tr>
<tr><td>Parcel</td><td>UPS, FedEx Ground</td></tr>
<tr><td>LTL</td><td>XPO, Old Dominion, Estes</td></tr>
<tr><td>TL</td><td>Assigned by the Northwind Routing Portal</td></tr>
</table>

<h1>7 Delivery Appointments</h1>
<p>All LTL and TL deliveries require a scheduled appointment. Appointments must be requested
at least 48 hours before the requested delivery time.</p>
<p>Carriers arriving more than 30 minutes after the appointment time will be refused.</p>

<h1>8 Documentation</h1>
<p>Each shipment must include a Bill of Lading (BOL) that references the Northwind PO number,
and a packing list.</p>
<p>A commercial invoice is required for shipments originating outside the United States.</p>

<h1>9 Chargeback Schedule</h1>
<table>
<tr><th>Violation</th><th>Section</th><th>Chargeback</th></tr>
<tr><td>Missing ASN</td><td>2.1</td><td>$500 per shipment</td></tr>
<tr><td>Late ASN</td><td>2.2</td><td>$250 per shipment</td></tr>
<tr><td>ASN / label SSCC mismatch</td><td>2.3</td><td>$5.00 per carton</td></tr>
<tr><td>Carton outside dimension or weight limits</td><td>3.1</td><td>$3.00 per carton</td></tr>
<tr><td>Missing or non-compliant carton label</td><td>4.1</td><td>$2.50 per carton, $250 minimum</td></tr>
<tr><td>Pallet size, height, or weight violation</td><td>5</td><td>$75 per pallet</td></tr>
<tr><td>Unapproved carrier</td><td>6.2</td><td>3% of invoice value</td></tr>
<tr><td>Missed or unscheduled appointment</td><td>7</td><td>$150 per occurrence</td></tr>
<tr><td>Missing BOL or packing list</td><td>8</td><td>$100 per shipment</td></tr>
</table>
<p>Repeat violations of the same type within a 90-day period are charged at 150% of the
listed amount.</p>

<h1>Appendix A Glossary</h1>
<p><b>ASN</b>: Advance Ship Notice, transmitted as EDI transaction set 856.</p>
<p><b>GMA pallet</b>: Grocery Manufacturers Association standard 40 x 48 inch pallet.</p>
<p><b>SSCC-18</b>: Serial Shipping Container Code, an 18-digit identifier encoded in a
GS1-128 barcode.</p>
"""

# Appendix B is rendered to an image with no text layer, like a scanned insert.
SCAN_HTML = """
<h1>Appendix B DC-Specific Requirements</h1>
<p><b>DC 6012 (Reno, NV)</b>: Double-stacked pallets are not accepted. Maximum pallet height
is 60 inches.</p>
<p><b>DC 6031 (Columbus, OH)</b>: Double-stacked pallets are not accepted. Deliveries are
accepted Monday through Friday only.</p>
"""

HEADER = "Northwind Retail Co. | Vendor Routing & Compliance Guide v2025.1"

# Ground truth: one row per (rule, parameter). `anchor` is a verbatim phrase used to
# locate the page; rules in the scanned appendix use anchor=None and page="scan".
# parameter_name / value / unit are blank for rules with no checkable parameter.
GROUND_TRUTH = [
    # rule_id, category, requirement, param_name, value, unit, anchor, section, chargeback, notes
    ("NW-001", "chargeback_policy", "Chargeback disputes must be submitted via the Vendor Portal within 30 days of the notice.", "dispute window", "30", "days", "30 days of the chargeback", "1.2 Compliance Program", "", "not a shipment attribute"),
    ("NW-002", "other", "Merchandise must be packed to withstand normal handling in transit.", "", "", "", "withstand normal handling", "1.2 Compliance Program", "", "needs_human: vague"),
    ("NW-003", "asn_edi", "An EDI 856 ASN must be transmitted for every shipment.", "asn sent", "true", "", "EDI 856 Advance Ship Notice for every", "2.1 ASN Requirement", "$500 per shipment", "chargeback is in Section 9 (cross-reference)"),
    ("NW-004", "asn_edi", "For LTL/TL shipments the ASN must be received at least 2 hours before the delivery appointment.", "asn lead time before appointment", "2", "hours", "no later than 2 hours", "2.2 ASN Timing", "$250 per shipment", "conditional: LTL/TL"),
    ("NW-005", "asn_edi", "For parcel shipments the ASN must be transmitted within 60 minutes after tender to the carrier.", "asn max delay after tender", "60", "minutes", "within 60 minutes after", "2.2 ASN Timing", "$250 per shipment", "conditional: parcel; 'after' relation not expressible -> needs_human"),
    ("NW-006", "asn_edi", "Each carton in the ASN must have a unique SSCC-18 matching its label.", "sscc present", "true", "", "unique SSCC-18", "2.3 ASN Content", "$5.00 per carton", "overlaps NW-014"),
    ("NW-007", "carton", "Carton length must be between 6 and 24 inches.", "carton length", "6-24", "in", "Length", "3.1 Dimensions and Weight", "$3.00 per carton", "table"),
    ("NW-008", "carton", "Carton width must be between 4 and 18 inches.", "carton width", "4-18", "in", "Width", "3.1 Dimensions and Weight", "$3.00 per carton", "table"),
    ("NW-009", "carton", "Carton height must be between 2 and 18 inches.", "carton height", "2-18", "in", "Height", "3.1 Dimensions and Weight", "$3.00 per carton", "table"),
    ("NW-010", "carton", "Carton weight must be between 1 and 50 lbs.", "carton weight", "1-50", "lbs", "50 lbs", "3.1 Dimensions and Weight", "$3.00 per carton", "table"),
    ("NW-011", "carton", "Parcel cartons must not exceed 35 lbs.", "carton max weight (parcel)", "35", "lbs", "must not exceed 35 lbs", "3.1 Dimensions and Weight", "$3.00 per carton", "conditional: parcel; supersedes NW-010"),
    ("NW-012", "carton", "Cartons must be corrugated with minimum 200 psi burst strength (ECT 32).", "burst strength", "200", "psi", "burst strength of 200 psi", "3.2 Carton Construction", "", "not a shipment attribute"),
    ("NW-013", "carton", "Individual cartons must not be banded or strapped.", "", "", "", "banding or strapping", "3.2 Carton Construction", "", "not a shipment attribute"),
    ("NW-014", "labeling", "Every carton must carry a 4x6 GS1-128 label with a scannable SSCC-18.", "label type", "GS1-128", "", "GS1-128 shipping label", "4.1 Carton Labels", "$2.50 per carton, $250 minimum", ""),
    ("NW-014", "labeling", "Every carton must carry a 4x6 GS1-128 label with a scannable SSCC-18.", "sscc present", "true", "", "GS1-128 shipping label", "4.1 Carton Labels", "$2.50 per carton, $250 minimum", ""),
    ("NW-015", "labeling", "Carton label must be on the longest side, lower right, at least 1.25 in from any edge.", "label position", "long_side_lower_right", "", "longest side of the carton", "4.1 Carton Labels", "$2.50 per carton, $250 minimum", "edge distance not checkable"),
    ("NW-016", "labeling", "Labels must be legible and not over seams or covered with tape.", "", "", "", "must be legible", "4.1 Carton Labels", "$2.50 per carton, $250 minimum", "needs_human"),
    ("NW-017", "labeling", "Each pallet must carry two labels on adjacent sides, formatted per Section 4.1.", "pallet label count", "2", "", "two pallet labels", "4.2 Pallet Labels", "", "cross-reference to 4.1"),
    ("NW-018", "pallet", "LTL and TL shipments must use 40x48 GMA pallets.", "pallet footprint", "40x48", "in", "40 x 48 inch GMA pallets", "5 Pallet Requirements", "$75 per pallet", "conditional: LTL/TL"),
    ("NW-019", "pallet", "Pallet height must not exceed 72 inches including the pallet.", "pallet max height", "72", "in", "72 inches", "5 Pallet Requirements", "$75 per pallet", "metric equivalent also stated"),
    ("NW-020", "pallet", "Gross pallet weight must not exceed 2,000 lbs.", "pallet max weight", "2000", "lbs", "2,000 lbs", "5 Pallet Requirements", "$75 per pallet", ""),
    ("NW-021", "pallet", "Pallets must not be double-stacked when shipping to DC 6012 or DC 6031.", "stacked", "false", "", "Double-stacked pallets are accepted", "5 Pallet Requirements", "$75 per pallet", "cross-reference into scanned Appendix B; conditional on DC"),
    ("NW-022", "pallet", "Pallets to DC 6012 must not exceed 60 inches in height.", "pallet max height (DC 6012)", "60", "in", None, "Appendix B DC-Specific Requirements", "$75 per pallet", "scanned page; conditional on DC"),
    ("NW-023", "appointment", "DC 6031 accepts deliveries Monday through Friday only.", "delivery weekdays", "Mon-Fri", "", None, "Appendix B DC-Specific Requirements", "", "scanned page; conditional on DC"),
    ("NW-024", "routing_carrier", "Shipments of 150 lbs or more must be routed via the Routing Portal at least 3 business days before ship date.", "routing lead time", "3", "business days", "150 lbs or more", "6.1 Routing Requests", "", "no routing-request field in Shipment"),
    ("NW-025", "routing_carrier", "Parcel shipments must use UPS or FedEx Ground.", "carrier", "UPS|FedEx Ground", "", "UPS, FedEx Ground", "6.2 Approved Carriers", "3% of invoice value", "table; conditional: parcel"),
    ("NW-026", "routing_carrier", "LTL shipments must use XPO, Old Dominion, or Estes.", "carrier", "XPO|Old Dominion|Estes", "", "XPO, Old Dominion, Estes", "6.2 Approved Carriers", "3% of invoice value", "table; conditional: LTL"),
    ("NW-027", "routing_carrier", "TL carriers are assigned by the Northwind Routing Portal.", "", "", "", "Assigned by the Northwind", "6.2 Approved Carriers", "3% of invoice value", "needs_human"),
    ("NW-028", "appointment", "LTL/TL deliveries require an appointment requested at least 48 hours before delivery.", "appointment lead time", "48", "hours", "at least 48 hours", "7 Delivery Appointments", "$150 per occurrence", "conditional: LTL/TL; request time not in Shipment"),
    ("NW-029", "appointment", "Carriers more than 30 minutes late to the appointment will be refused.", "max late arrival", "30", "minutes", "more than 30 minutes", "7 Delivery Appointments", "", "arrival time not in Shipment"),
    ("NW-030", "documentation", "Each shipment must include a BOL referencing the Northwind PO number.", "required document", "BOL", "", "Bill of Lading", "8 Documentation", "$100 per shipment", ""),
    ("NW-031", "documentation", "Each shipment must include a packing list.", "required document", "packing_list", "", "and a packing list", "8 Documentation", "$100 per shipment", ""),
    ("NW-032", "documentation", "Shipments originating outside the US require a commercial invoice.", "required document", "commercial_invoice", "", "commercial invoice is required", "8 Documentation", "", "conditional on origin (not in Shipment)"),
    ("NW-033", "chargeback_policy", "Repeat violations of the same type within 90 days are charged at 150%.", "repeat multiplier", "150", "%", "150% of the", "9 Chargeback Schedule", "", ""),
]


def _render_story(html: str, out: Path) -> None:
    writer = pymupdf.DocumentWriter(str(out))
    story = pymupdf.Story(html=html, user_css=CSS)
    more = 1
    while more:
        dev = writer.begin_page(LETTER)
        more, _ = story.place(BODY)
        story.draw(dev)
        writer.end_page()
    writer.close()


def build(tmp_dir: Path) -> pymupdf.Document:
    cover_pdf, main_pdf, scan_pdf = (tmp_dir / f"{n}.pdf" for n in ("cover", "main", "scan"))
    _render_story(COVER_HTML, cover_pdf)
    _render_story(BODY_HTML, main_pdf)
    _render_story(SCAN_HTML, scan_pdf)

    doc = pymupdf.open(cover_pdf)
    doc.insert_pdf(pymupdf.open(main_pdf))

    # Rasterize Appendix B and insert it as an image-only page (no text layer).
    scan_src = pymupdf.open(scan_pdf)
    pix = scan_src[0].get_pixmap(dpi=110, colorspace=pymupdf.csGRAY)
    scan_page = doc.new_page(width=LETTER.width, height=LETTER.height)
    scan_page.insert_image(scan_page.rect, pixmap=pix)

    total = doc.page_count
    for i, page in enumerate(doc):
        if i == 0:
            continue  # cover has no running header
        page.insert_text((60, 45), HEADER, fontsize=8, fontname="helv")
        page.insert_text((60, LETTER.height - 36), f"Page {i + 1} of {total}", fontsize=8, fontname="helv")
    return doc


def locate_pages(doc: pymupdf.Document) -> dict[str, int]:
    """Find the page of each ground-truth anchor; fail loudly if an anchor is missing."""
    scan_page = doc.page_count  # appendix B is the last page
    pages: dict[str, int] = {}
    for row in GROUND_TRUTH:
        rule_id, anchor = row[0], row[6]
        if anchor is None:
            pages[rule_id] = scan_page
            continue
        needle = anchor.split("\n")[0]
        hits = [p.number + 1 for p in doc if p.search_for(needle)]
        # 'Length'/'Width'/... appear in the 3.1 table; take the first hit in section order.
        if not hits:
            raise SystemExit(f"anchor not found for {rule_id}: {anchor!r}")
        pages.setdefault(rule_id, hits[0])
    return pages


def main() -> None:
    import tempfile

    GUIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        doc = build(Path(tmp))
        doc.set_metadata({"title": "Northwind Retail Co. Vendor Routing & Compliance Guide v2025.1 (synthetic)",
                          "creationDate": "D:20250301000000", "modDate": "D:20250301000000"})
        doc.save(GUIDE_PATH, garbage=4, deflate=True, no_new_id=True)
        pages = locate_pages(doc)

    with GT_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rule_id", "category", "requirement", "parameter_name", "parameter_value",
                    "unit", "page", "section", "chargeback", "notes"])
        for rid, cat, req, pname, pval, unit, _anchor, section, cb, notes in GROUND_TRUTH:
            w.writerow([rid, cat, req, pname, pval, unit, pages[rid], section, cb, notes])

    n_rules = len({r[0] for r in GROUND_TRUTH})
    print(f"wrote {GUIDE_PATH.relative_to(ROOT)} ({doc.page_count} pages)")
    print(f"wrote {GT_PATH.relative_to(ROOT)} ({n_rules} rules, {len(GROUND_TRUTH)} rows)")


if __name__ == "__main__":
    main()
