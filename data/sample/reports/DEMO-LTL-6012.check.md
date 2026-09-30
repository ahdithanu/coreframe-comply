# Pre-ship check: DEMO-LTL-6012

Northwind routing guide v2025.1 · ltl · carrier FedEx Freight · DC 6012 · ship date 2026-10-06

**10 violations**, estimated chargeback exposure **$686.00** plus 1 unpriced · 9 need review · 8 passed · 7 not applicable

## Violations (highest exposure first)

| Rule | Expected | Actual | Chargeback | Exposure | Source |
|---|---|---|---|---|---|
| **For LTL and TL shipments, the ASN must be received at least 2 hours before the delivery appointment.**<br>`northwind_v2025.1_asn_edi_002` | `shipment.asn_sent_at at least 2 hours before shipment.appointment_time` | 0.8 hours before | $250 per shipment | $250.00 | p. 2, 2.2 ASN Timing: "the ASN must be received by Northwind no later than 2 hours before the scheduled delivery appointment" |
| **Every carton must carry a 4 x 6 in GS1-128 label with a scannable SSCC-18 barcode.**<br>`northwind_v2025.1_labeling_001` | `cartons[].label_type = GS1-128` | C004: undecidable (not recorded on the shipment) | $2.50 per carton, $250 minimum | $250.00 (shared fee) | p. 3, 4.1 Carton Labels: "Every carton must carry a 4 in x 6 in GS1-128 shipping label" |
| **Every carton must carry a 4 x 6 in GS1-128 label with a scannable SSCC-18 barcode.**<br>`northwind_v2025.1_labeling_001` | `cartons[].sscc_present = true` | C003: false | $2.50 per carton, $250 minimum | $250.00 (shared fee) | p. 3, 4.1 Carton Labels: "Every carton must carry a 4 in x 6 in GS1-128 shipping label" |
| **The carton label must be on the longest side, in the lower right corner.**<br>`northwind_v2025.1_labeling_002` | `cartons[].label_position = long_side_lower_right` | C004: short_side_center | $2.50 per carton, $250 minimum | $250.00 (shared fee) | p. 3, 4.1 Carton Labels: "placed on the longest side of the carton, in the lower right corner" |
| **Each shipment must include a packing list.**<br>`northwind_v2025.1_documentation_002` | `shipment.documents in packing_list` | shipment: BOL (missing packinglist) | $100 per shipment | $100.00 | p. 4, 8 Documentation: "and a packing list." |
| **Pallets shipped to DC 6012 or DC 6031 must not be double-stacked.**<br>`northwind_v2025.1_pallet_004` | `pallets[].stacked = false` | P01: true | $75 per pallet | $75.00 (shared fee) | p. 3, 5 Pallet Requirements: "Double-stacked pallets are accepted at all DCs except those listed in Appendix B." |
| **Pallets shipped to DC 6012 must not exceed 60 inches in height.**<br>`northwind_v2025.1_pallet_005` | `pallets[].height_in ≤ 60 in` | P01: 66 in | $75 per pallet | $75.00 (shared fee) | p. 6, Appendix B DC-Specific Requirements: "Maximum pallet height is 60 inches." |
| **Each carton in the ASN must carry an SSCC-18 matching its label.**<br>`northwind_v2025.1_asn_edi_004` | `cartons[].sscc_present = true` | C003: false | $5.00 per carton | $5.00 | p. 2, 2.3 ASN Content: "Each carton listed in the ASN must be identified by a unique SSCC-18" |
| **Carton length must be between 6 and 24 inches.**<br>`northwind_v2025.1_carton_001` | `cartons[].length_in between 6 and 24 in` | C003: 26 in | $3.00 per carton | $3.00 (shared fee) | p. 2, 3.1 Dimensions and Weight: "Length 6 in 24 in" |
| **Carton weight must be between 1 and 50 lbs.**<br>`northwind_v2025.1_carton_004` | `cartons[].weight_lbs between 1 and 50 lbs` | C002: 52 lbs | $3.00 per carton | $3.00 (shared fee) | p. 3, 3.1 Dimensions and Weight: "Weight 1 lb 50 lbs" |
| **LTL shipments must use XPO, Old Dominion, or Estes.**<br>`northwind_v2025.1_routing_carrier_003` | `shipment.carrier in XPO, Old Dominion, Estes` | shipment: FedEx Freight | 3% of invoice value | unknown | p. 4, 6.2 Approved Carriers: "LTL XPO, Old Dominion, Estes" |

### Exposure by chargeback line

Each schedule line is billed once, however many rules map to it.

| Chargeback line | Rules | Units | Exposure |
|---|---|---|---|
| $250 per shipment | 1 | shipment | $250.00 |
| $2.50 per carton, $250 minimum | 2 | C003, C004 | $250.00 |
| $100 per shipment | 1 | shipment | $100.00 |
| $75 per pallet | 2 | P01 | $75.00 |
| $3.00 per carton | 2 | C002, C003 | $6.00 |
| $5.00 per carton | 1 | C003 | $5.00 |
| 3% of invoice value | 1 | shipment | unknown |

## Needs human review

- **Merchandise must be packed to withstand normal handling in transit.** `northwind_v2025.1_other_001`: Vague: no measurable criterion. (p. 2, 1.2 Compliance Program)
- **Cartons must be corrugated with a minimum burst strength of 200 psi (ECT 32).** `northwind_v2025.1_carton_006`: No shipment field records carton construction. (p. 3, 3.2 Carton Construction)
- **Individual cartons must not be banded or strapped.** `northwind_v2025.1_carton_007`: No shipment field records banding. (p. 3, 3.2 Carton Construction)
- **Labels must be legible and not placed over seams or covered with tape.** `northwind_v2025.1_labeling_003`: Visual inspection only. (p. 3, 4.1 Carton Labels)
- **Each pallet must carry two labels, formatted per Section 4.1, on two adjacent sides.** `northwind_v2025.1_labeling_004`: Adjacency of label sides can't be expressed with the current operators. (p. 3, 4.2 Pallet Labels)
- **Shipments of 150 lbs or more must be routed via the Routing Portal at least 3 business days before ship date.** `northwind_v2025.1_routing_carrier_001`: Routing-request date isn't recorded on the shipment. (p. 3, 6.1 Routing Requests)
- **Appointments must be requested at least 48 hours before the requested delivery time.** `northwind_v2025.1_appointment_003`: Appointment request time isn't recorded. (p. 4, 7 Delivery Appointments)
- **Carriers arriving more than 30 minutes after the appointment time will be refused.** `northwind_v2025.1_appointment_004`: Arrival time isn't recorded. (p. 4, 7 Delivery Appointments)
- **Shipments originating outside the United States require a commercial invoice.** `northwind_v2025.1_documentation_003`: Origin country isn't recorded on the shipment. (p. 4, 8 Documentation)

## Passed

- An EDI 856 ASN must be transmitted for every shipment. `northwind_v2025.1_asn_edi_001`
- Carton width must be between 4 and 18 inches. `northwind_v2025.1_carton_002`
- Carton height must be between 2 and 18 inches. `northwind_v2025.1_carton_003`
- LTL and TL shipments must use 40 x 48 in GMA pallets. `northwind_v2025.1_pallet_001`
- Pallet height must not exceed 72 inches including the pallet. `northwind_v2025.1_pallet_002`
- Gross pallet weight must not exceed 2,000 lbs. `northwind_v2025.1_pallet_003`
- LTL and TL deliveries require a scheduled appointment. `northwind_v2025.1_appointment_002`
- Each shipment must include a Bill of Lading referencing the Northwind PO number. `northwind_v2025.1_documentation_001`
