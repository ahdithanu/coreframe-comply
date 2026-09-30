"""Hand-written reference RuleSet for the synthetic Northwind guide.

This is what a perfect extraction would produce. It lets the checker and the
shipment generator be tested and evaluated independently of extraction quality:
checker bugs and extraction misses are different failure sources and should be
measured separately.

Every snippet is verified against the parsed page text at build time.

Usage:  python scripts/build_reference_rules.py
Writes: data/sample/rules/northwind_v2025.1.reference.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coreframe.extract import _norm, assign_ids, page_texts  # noqa: E402
from coreframe.ingest import parse_guide  # noqa: E402
from coreframe.schemas import Rule, RuleSet  # noqa: E402

GUIDE = ROOT / "data/sample/guides/northwind/v2025.1.pdf"
OUT = ROOT / "data/sample/rules/northwind_v2025.1.reference.json"

LTL_TL = [{"field": "shipment_type", "operator": "in", "value": ["ltl", "tl"]}]
PARCEL = [{"field": "shipment_type", "operator": "eq", "value": "parcel"}]


def cb(amount, basis, text):
    return {"amount": amount, "basis": basis, "text": text}


def src(page, section, snippet):
    return [{"page": page, "section": section, "snippet": snippet}]


def p(name, target, op, value, unit=None, reference=None):
    return {"name": name, "target": target, "operator": op, "value": value, "unit": unit, "reference": reference}


def human(category, requirement, sources, note, **kw):
    return dict(category=category, requirement=requirement, parameters=kw.pop("parameters", []),
                sources=sources, confidence=kw.pop("confidence", "high"), status="needs_human",
                review_note=note, **kw)


def checked(category, requirement, parameters, sources, **kw):
    return dict(category=category, requirement=requirement, parameters=parameters, sources=sources,
                confidence=kw.pop("confidence", "high"), status="parameterized", **kw)


S12, S21, S22, S23 = "1.2 Compliance Program", "2.1 ASN Requirement", "2.2 ASN Timing", "2.3 ASN Content"
S31, S32, S41, S42 = "3.1 Dimensions and Weight", "3.2 Carton Construction", "4.1 Carton Labels", "4.2 Pallet Labels"
S5, S61, S62, S7, S8, S9 = ("5 Pallet Requirements", "6.1 Routing Requests", "6.2 Approved Carriers",
                            "7 Delivery Appointments", "8 Documentation", "9 Chargeback Schedule")
SB = "Appendix B DC-Specific Requirements"
CB_CARTON = cb(3.0, "per_carton", "$3.00 per carton")
CB_LABEL = cb(2.5, "per_carton", "$2.50 per carton, $250 minimum")
CB_PALLET = cb(75.0, "per_pallet", "$75 per pallet")
CB_CARRIER = cb(None, "percent_of_invoice", "3% of invoice value")
CB_DOCS = cb(100.0, "per_shipment", "$100 per shipment")

RULES = [
    # NW-001 .. NW-002
    human("chargeback_policy", "Chargeback disputes must be submitted through the Vendor Portal within 30 days of the notice.",
          src(2, S12, "Chargeback disputes must be submitted through the Vendor Portal within 30 days"),
          "Billing process, not a shipment attribute.",
          parameters=[p("dispute window", None, "lte", 30, "days")]),
    human("other", "Merchandise must be packed to withstand normal handling in transit.",
          src(2, S12, "Merchandise must be packed to withstand normal handling in transit."), "Vague: no measurable criterion."),
    # NW-003 .. NW-006
    checked("asn_edi", "An EDI 856 ASN must be transmitted for every shipment.",
            [p("ASN sent", "shipment.asn_sent_at", "exists", True)],
            src(2, S21, "Vendors must transmit an EDI 856 Advance Ship Notice for every shipment."),
            chargeback=cb(500.0, "per_shipment", "$500 per shipment")),
    checked("asn_edi", "For LTL and TL shipments, the ASN must be received at least 2 hours before the delivery appointment.",
            [p("ASN lead time", "shipment.asn_sent_at", "before", 2, "hours", "shipment.appointment_time")],
            src(2, S22, "the ASN must be received by Northwind no later than 2 hours before the scheduled delivery appointment"),
            applies_to=LTL_TL, chargeback=cb(250.0, "per_shipment", "$250 per shipment")),
    human("asn_edi", "For parcel shipments, the ASN must be transmitted within 60 minutes after carrier tender.",
          src(2, S22, "the ASN must be transmitted within 60 minutes after the parcel is tendered to the carrier"),
          "An 'after tender' window can't be expressed, and tender time isn't recorded.",
          applies_to=PARCEL, chargeback=cb(250.0, "per_shipment", "$250 per shipment")),
    checked("asn_edi", "Each carton in the ASN must carry an SSCC-18 matching its label.",
            [p("SSCC present", "cartons[].sscc_present", "eq", True)],
            src(2, S23, "Each carton listed in the ASN must be identified by a unique SSCC-18"),
            chargeback=cb(5.0, "per_carton", "$5.00 per carton"), confidence="med",
            review_note="Only SSCC presence is checkable; ASN-to-label matching needs the EDI 856."),
    # NW-007 .. NW-011
    checked("carton", "Carton length must be between 6 and 24 inches.",
            [p("carton length", "cartons[].length_in", "between", [6, 24], "in")],
            src(2, S31, "Length 6 in 24 in"), chargeback=CB_CARTON),
    checked("carton", "Carton width must be between 4 and 18 inches.",
            [p("carton width", "cartons[].width_in", "between", [4, 18], "in")],
            src(2, S31, "Width 4 in 18 in"), chargeback=CB_CARTON),
    checked("carton", "Carton height must be between 2 and 18 inches.",
            [p("carton height", "cartons[].height_in", "between", [2, 18], "in")],
            src(2, S31, "Height 2 in 18 in"), chargeback=CB_CARTON),
    checked("carton", "Carton weight must be between 1 and 50 lbs.",
            [p("carton weight", "cartons[].weight_lbs", "between", [1, 50], "lbs")],
            src(3, S31, "Weight 1 lb 50 lbs"), chargeback=CB_CARTON),
    checked("carton", "For parcel shipments, cartons must not exceed 35 lbs.",
            [p("parcel carton max weight", "cartons[].weight_lbs", "lte", 35, "lbs")],
            src(3, S31, "Cartons shipped via a parcel carrier must not exceed 35 lbs."),
            applies_to=PARCEL, chargeback=CB_CARTON),
    # NW-012 .. NW-013
    human("carton", "Cartons must be corrugated with a minimum burst strength of 200 psi (ECT 32).",
          src(3, S32, "Cartons must be corrugated with a minimum burst strength of 200 psi (ECT 32)."),
          "No shipment field records carton construction.", parameters=[p("burst strength", None, "gte", 200, "psi")]),
    human("carton", "Individual cartons must not be banded or strapped.",
          src(3, S32, "Do not use banding or strapping on individual cartons."), "No shipment field records banding."),
    # NW-014 .. NW-017
    checked("labeling", "Every carton must carry a 4 x 6 in GS1-128 label with a scannable SSCC-18 barcode.",
            [p("label type", "cartons[].label_type", "eq", "GS1-128"), p("SSCC present", "cartons[].sscc_present", "eq", True)],
            src(3, S41, "Every carton must carry a 4 in x 6 in GS1-128 shipping label"), chargeback=CB_LABEL),
    checked("labeling", "The carton label must be on the longest side, in the lower right corner.",
            [p("label position", "cartons[].label_position", "eq", "long_side_lower_right")],
            src(3, S41, "placed on the longest side of the carton, in the lower right corner"), chargeback=CB_LABEL,
            review_note="The 1.25 in edge clearance isn't recorded and needs a visual check."),
    human("labeling", "Labels must be legible and not placed over seams or covered with tape.",
          src(3, S41, "Labels must be legible and must not be placed over carton seams or covered with tape."),
          "Visual inspection only.", chargeback=CB_LABEL),
    human("labeling", "Each pallet must carry two labels, formatted per Section 4.1, on two adjacent sides.",
          src(3, S42, "Each pallet must carry two pallet labels, formatted as specified in Section 4.1"),
          "Adjacency of label sides can't be expressed with the current operators."),
    # NW-018 .. NW-022
    checked("pallet", "LTL and TL shipments must use 40 x 48 in GMA pallets.",
            [p("pallet footprint", "pallets[].footprint", "eq", "40x48", "in")],
            src(3, S5, "All LTL and TL shipments must be palletized on 40 x 48 inch GMA pallets."),
            applies_to=LTL_TL, chargeback=CB_PALLET),
    checked("pallet", "Pallet height must not exceed 72 inches including the pallet.",
            [p("pallet max height", "pallets[].height_in", "lte", 72, "in")],
            src(3, S5, "Maximum pallet height is 72 inches (183 cm), including the pallet."), chargeback=CB_PALLET),
    checked("pallet", "Gross pallet weight must not exceed 2,000 lbs.",
            [p("pallet max weight", "pallets[].weight_lbs", "lte", 2000, "lbs")],
            src(3, S5, "Maximum gross pallet weight is 2,000 lbs."), chargeback=CB_PALLET),
    checked("pallet", "Pallets shipped to DC 6012 or DC 6031 must not be double-stacked.",
            [p("stacked", "pallets[].stacked", "eq", False)],
            src(3, S5, "Double-stacked pallets are accepted at all DCs except those listed in Appendix B.")
            + src(6, SB, "Double-stacked pallets are not accepted."),
            applies_to=[{"field": "destination_dc", "operator": "in", "value": ["6012", "6031"]}], chargeback=CB_PALLET),
    checked("pallet", "Pallets shipped to DC 6012 must not exceed 60 inches in height.",
            [p("pallet max height (DC 6012)", "pallets[].height_in", "lte", 60, "in")],
            src(6, SB, "Maximum pallet height is 60 inches."),
            applies_to=[{"field": "destination_dc", "operator": "eq", "value": "6012"}], chargeback=CB_PALLET),
    # NW-023
    human("appointment", "DC 6031 accepts deliveries Monday through Friday only.",
          src(6, SB, "Deliveries are accepted Monday through Friday only."),
          "Day-of-week constraints can't be expressed with the current operators.",
          applies_to=[{"field": "destination_dc", "operator": "eq", "value": "6031"}]),
    # NW-024 .. NW-027
    human("routing_carrier", "Shipments of 150 lbs or more must be routed via the Routing Portal at least 3 business days before ship date.",
          src(3, S61, "Shipments of 150 lbs or more must be routed through the Northwind Routing Portal"),
          "Routing-request date isn't recorded on the shipment."),
    checked("routing_carrier", "Parcel shipments must use UPS or FedEx Ground.",
            [p("carrier", "shipment.carrier", "in", ["UPS", "FedEx Ground"])],
            src(4, S62, "Parcel UPS, FedEx Ground"), applies_to=PARCEL, chargeback=CB_CARRIER),
    checked("routing_carrier", "LTL shipments must use XPO, Old Dominion, or Estes.",
            [p("carrier", "shipment.carrier", "in", ["XPO", "Old Dominion", "Estes"])],
            src(4, S62, "LTL XPO, Old Dominion, Estes"),
            applies_to=[{"field": "shipment_type", "operator": "eq", "value": "ltl"}], chargeback=CB_CARRIER),
    human("routing_carrier", "TL carriers are assigned by the Northwind Routing Portal.",
          src(4, S62, "TL Assigned by the Northwind Routing Portal"), "Assigned carrier isn't in the shipment record.",
          applies_to=[{"field": "shipment_type", "operator": "eq", "value": "tl"}], chargeback=CB_CARRIER),
    # NW-028 .. NW-029
    checked("appointment", "LTL and TL deliveries require a scheduled appointment.",
            [p("appointment scheduled", "shipment.appointment_time", "exists", True)],
            src(4, S7, "All LTL and TL deliveries require a scheduled appointment."),
            applies_to=LTL_TL, chargeback=cb(150.0, "other", "$150 per occurrence")),
    human("appointment", "Appointments must be requested at least 48 hours before the requested delivery time.",
          src(4, S7, "Appointments must be requested at least 48 hours before the requested delivery time."),
          "Appointment request time isn't recorded.", applies_to=LTL_TL,
          parameters=[p("appointment request lead time", None, "gte", 48, "hours")]),
    human("appointment", "Carriers arriving more than 30 minutes after the appointment time will be refused.",
          src(4, S7, "Carriers arriving more than 30 minutes after the appointment time will be refused."),
          "Arrival time isn't recorded.", applies_to=LTL_TL),
    # NW-030 .. NW-033
    checked("documentation", "Each shipment must include a Bill of Lading referencing the Northwind PO number.",
            [p("BOL included", "shipment.documents", "in", ["BOL"])],
            src(4, S8, "Each shipment must include a Bill of Lading (BOL) that references the Northwind PO number"),
            chargeback=CB_DOCS, review_note="The PO reference on the BOL isn't checkable."),
    checked("documentation", "Each shipment must include a packing list.",
            [p("packing list included", "shipment.documents", "in", ["packing_list"])],
            src(4, S8, "and a packing list."), chargeback=CB_DOCS),
    human("documentation", "Shipments originating outside the United States require a commercial invoice.",
          src(4, S8, "A commercial invoice is required for shipments originating outside the United States."),
          "Origin country isn't recorded on the shipment."),
    human("chargeback_policy", "Repeat violations of the same type within 90 days are charged at 150% of the listed amount.",
          src(4, S9, "Repeat violations of the same type within a 90-day period are charged at 150% of the"),
          "Needs chargeback history across shipments."),
]


def main() -> None:
    guide = parse_guide(GUIDE, image_dir=ROOT / "data/sample/cache/pages")
    texts = page_texts(guide)
    rules = [Rule(rule_id=f"tmp{i}", retailer="northwind", guide_version="v2025.1", **r) for i, r in enumerate(RULES)]
    for r in rules:
        for s in r.sources:
            page = texts[s.page]
            s.verified = (_norm(s.snippet) in page) if page else None
            if s.verified is False:
                raise SystemExit(f"snippet not on page {s.page}: {s.snippet!r}")
    assign_ids(rules, "northwind", "v2025.1")
    rs = RuleSet(retailer="northwind", guide_version="v2025.1", guide_sha256=guide.sha256,
                 prompt_version="reference", model="human",
                 extracted_at=datetime(2026, 9, 30, tzinfo=timezone.utc), rules=rules)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rs.model_dump(mode="json"), indent=2) + "\n")
    n_param = sum(r.status.value == "parameterized" for r in rules)
    print(f"wrote {OUT.relative_to(ROOT)}: {len(rules)} rules ({n_param} parameterized, {len(rules) - n_param} needs_human)")


if __name__ == "__main__":
    main()
