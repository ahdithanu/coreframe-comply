# Northwind routing guide: v2025.1 → v2025.2

**4 changed · 3 added · 1 removed** · 29 unchanged.
5 of these affect automatic checks; the rest need a human to update SOPs.

## Changed

| Rule | What changed | v2025.1 | v2025.2 | Impact |
|---|---|---|---|---|
| **For LTL and TL shipments, the ASN must be received at least 4 hours before the delivery appointment.**<br>`northwind_v2025.1_asn_edi_002` → `northwind_v2025.2_asn_edi_002`<br>p. 2, 2.2 ASN Timing → p. 2, 2.2 ASN Timing | shipment.asn_sent_at | shipment.asn_sent_at at least 2 hours before shipment.appointment_time | shipment.asn_sent_at at least 4 hours before shipment.appointment_time | stricter |
|  | chargeback | $250 per shipment | $300 per shipment | higher fee |
| **Carton weight must be between 1 and 45 lbs.**<br>`northwind_v2025.1_carton_004` → `northwind_v2025.2_carton_004`<br>p. 3, 3.1 Dimensions and Weight → p. 3, 3.1 Dimensions and Weight | cartons[].weight_lbs | cartons[].weight_lbs between 1 and 50 lbs | cartons[].weight_lbs between 1 and 45 lbs | stricter |
| **LTL shipments must use XPO, Old Dominion, or Saia.**<br>`northwind_v2025.1_routing_carrier_003` → `northwind_v2025.2_routing_carrier_003`<br>p. 4, 6.2 Approved Carriers → p. 4, 6.2 Approved Carriers | shipment.carrier | shipment.carrier in XPO, Old Dominion, Estes | shipment.carrier in XPO, Old Dominion, Saia (-Estes, +Saia) | changed |
| **For parcel shipments, the ASN must be transmitted within 60 minutes after carrier tender.**<br>`northwind_v2025.1_asn_edi_003` → `northwind_v2025.2_asn_edi_003`<br>p. 2, 2.2 ASN Timing → p. 2, 2.2 ASN Timing | chargeback | $250 per shipment | $300 per shipment | higher fee |

## Added in v2025.2

- **Shipments containing hazardous materials must include a Safety Data Sheet (SDS).** `northwind_v2025.2_documentation_004` · p. 4, 8 Documentation
- **Pallets shipped to DC 6040 must not exceed 64 inches in height.** `northwind_v2025.2_pallet_006` · p. 6, Appendix B DC-Specific Requirements · chargeback $75 per pallet
- **Inner packs must contain a uniform quantity of units.** _(needs human review)_ `northwind_v2025.2_carton_007` · p. 3, 3.2 Inner Packs

## Removed since v2025.1

- ~~Individual cartons must not be banded or strapped.~~ `northwind_v2025.1_carton_007` · was p. 3, 3.2 Carton Construction

## Unchanged, but moved or reworded

- Gross pallet weight must not exceed 2,000 lbs.: source sentence reworded
- Cartons must be corrugated with a minimum burst strength of 200 psi (ECT 32).: moved: p. 3 3.2 Carton Construction → p. 3 3.3 Carton Construction
