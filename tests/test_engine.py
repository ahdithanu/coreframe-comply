"""Rule engine: hand-computed expectations, independent of the synthetic generator."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coreframe.engine import Outcome, check, convert, evaluate_rule
from coreframe.schemas import Rule, RuleSet, Shipment
from coreframe.synth import generate, mutant_label

REF = Path(__file__).resolve().parents[1] / "data/sample/rules/northwind_v2025.1.reference.json"
TZ = timezone(timedelta(hours=-7))


def rule(params, status="parameterized", category="carton", applies_to="all", chargeback=None, **kw):
    return Rule(rule_id=kw.pop("rule_id", "r1"), retailer="acme", guide_version="v1", category=category,
                requirement=kw.pop("requirement", "req"), parameters=params, applies_to=applies_to,
                chargeback=chargeback, sources=[{"page": 1, "section": "s", "snippet": "x"}],
                confidence="high", status=status, **kw)


def p(target, op, value, unit=None, reference=None):
    return {"name": "p", "target": target, "operator": op, "value": value, "unit": unit, "reference": reference}


def ship(**kw):
    base = dict(shipment_id="S1", retailer="acme", shipment_type="ltl", ship_date="2026-10-06",
                asn_sent_at="2026-10-06T06:00:00-07:00", carrier="XPO",
                appointment_time="2026-10-08T10:00:00-07:00", destination_dc="0001",
                cartons=[{"carton_id": "C1", "length_in": 16, "width_in": 12, "height_in": 10, "weight_lbs": 20,
                          "label_type": "GS1-128", "label_position": "long_side_lower_right", "sscc_present": True},
                         {"carton_id": "C2", "length_in": 16, "width_in": 12, "height_in": 10, "weight_lbs": 44,
                          "label_type": "GS1-128", "label_position": "long_side_lower_right", "sscc_present": True}],
                pallets=[{"pallet_id": "P1", "height_in": 60, "weight_lbs": 900, "footprint": "48x40",
                          "stacked": False, "label_positions": ["a", "b"]}],
                documents=["BOL", "Packing List"])
    base.update(kw)
    return Shipment(**base)


def test_per_element_failure_names_the_carton():
    r = evaluate_rule(rule([p("cartons[].weight_lbs", "lte", 35, "lbs")]), ship())
    assert r.outcome == Outcome.FAIL
    assert r.params[0].failing_elements == ["C2"] and "C2: 44" in r.params[0].actual


def test_unit_conversion_metric_rule_against_inch_fields():
    # 183 cm = 72.05 in; a 60 in pallet passes, a 73 in pallet fails
    rr = rule([p("pallets[].height_in", "lte", 183, "cm")], category="pallet")
    assert evaluate_rule(rr, ship()).outcome == Outcome.PASS
    tall = ship(pallets=[{"pallet_id": "P1", "height_in": 73, "weight_lbs": 900, "footprint": "40x48", "stacked": False}])
    assert evaluate_rule(rr, tall).outcome == Outcome.FAIL
    assert convert(1, "kg", "lbs") == pytest.approx(2.2046, rel=1e-3)


def test_unknown_unit_is_review_not_pass():
    r = evaluate_rule(rule([p("cartons[].weight_lbs", "lte", 3, "stone")]), ship())
    assert r.outcome == Outcome.NEEDS_REVIEW and "can't convert" in r.reason


def test_footprint_orientation_insensitive():
    assert evaluate_rule(rule([p("pallets[].footprint", "eq", "40x48", "in")], category="pallet"), ship()).outcome == Outcome.PASS


def test_string_and_list_normalization():
    docs = rule([p("shipment.documents", "in", ["BOL", "packing_list"])], category="documentation")
    assert evaluate_rule(docs, ship()).outcome == Outcome.PASS           # "Packing List" == "packing_list"
    r = evaluate_rule(docs, ship(documents=["BOL"]))
    assert r.outcome == Outcome.FAIL and "missing packinglist" in r.params[0].detail


def test_before_operator():
    asn = rule([p("shipment.asn_sent_at", "before", 2, "hours", "shipment.appointment_time")], category="asn_edi")
    assert evaluate_rule(asn, ship()).outcome == Outcome.PASS
    late = ship(asn_sent_at="2026-10-08T09:00:00-07:00")                 # 1 h before appointment
    r = evaluate_rule(asn, late)
    assert r.outcome == Outcome.FAIL and r.params[0].actual == "1.0 hours before"
    assert evaluate_rule(asn, ship(asn_sent_at=None)).outcome == Outcome.FAIL           # never sent
    assert evaluate_rule(asn, ship(appointment_time=None)).outcome == Outcome.NEEDS_REVIEW  # nothing to compare to
    naive = ship(asn_sent_at=datetime(2026, 10, 8, 9, 30))                # naive treated as the appointment's tz
    assert evaluate_rule(asn, naive).outcome == Outcome.FAIL


def test_exists_operator():
    appt = rule([p("shipment.appointment_time", "exists", True)], category="appointment")
    assert evaluate_rule(appt, ship()).outcome == Outcome.PASS
    assert evaluate_rule(appt, ship(appointment_time=None)).outcome == Outcome.FAIL


def test_missing_data_is_review_not_fail():
    r = rule([p("cartons[].label_type", "eq", "GS1-128")], category="labeling")
    s = ship()
    s.cartons[0].label_type = None
    assert evaluate_rule(r, s).outcome == Outcome.NEEDS_REVIEW


def test_applicability():
    parcel_only = rule([p("cartons[].weight_lbs", "lte", 35, "lbs")],
                       applies_to=[{"field": "shipment_type", "operator": "eq", "value": "parcel"}])
    assert evaluate_rule(parcel_only, ship()).outcome == Outcome.NOT_APPLICABLE
    assert evaluate_rule(parcel_only, ship(shipment_type="parcel")).outcome == Outcome.FAIL
    unknown = evaluate_rule(parcel_only, ship(shipment_type=None))
    assert unknown.outcome == Outcome.NEEDS_REVIEW and "doesn't record shipment_type" in unknown.reason


def test_needs_human_rules():
    legible = rule([], status="needs_human", category="labeling", review_note="visual check")
    assert evaluate_rule(legible, ship()).outcome == Outcome.NEEDS_REVIEW
    pallet_labels = rule([], status="needs_human", category="labeling", review_note="n",
                         requirement="Each pallet must carry two labels.")
    assert evaluate_rule(pallet_labels, ship(pallets=[])).outcome == Outcome.NOT_APPLICABLE
    policy = rule([], status="needs_human", category="chargeback_policy", review_note="n")
    assert evaluate_rule(policy, ship()).outcome == Outcome.NOT_APPLICABLE


def test_exposure_per_element_and_minimum():
    label = {"amount": 2.5, "basis": "per_carton", "text": "$2.50 per carton, $250 minimum"}
    r = evaluate_rule(rule([p("cartons[].sscc_present", "eq", True)], chargeback=label), ship(
        cartons=[{"carton_id": f"C{i}", "length_in": 10, "width_in": 10, "height_in": 10, "weight_lbs": 5,
                  "sscc_present": False} for i in range(3)]))
    assert r.exposure_usd == 250.0                                       # 3 x $2.50 < $250 minimum
    per = {"amount": 3.0, "basis": "per_carton", "text": "$3.00 per carton"}
    r = evaluate_rule(rule([p("cartons[].weight_lbs", "lte", 10, "lbs")], chargeback=per), ship())
    assert r.exposure_usd == 6.0                                         # 2 failing cartons


def test_report_sorted_by_exposure():
    rs = RuleSet(retailer="acme", guide_version="v1", guide_sha256="x", prompt_version="t", model="t",
                 extracted_at="2026-09-30T00:00:00Z", rules=[
                     rule([p("cartons[].weight_lbs", "lte", 10, "lbs")], rule_id="small",
                          chargeback={"amount": 3.0, "basis": "per_carton", "text": "$3"}),
                     rule([p("shipment.carrier", "in", ["UPS"])], rule_id="unpriced", category="routing_carrier",
                          chargeback={"amount": None, "basis": "percent_of_invoice", "text": "3% of invoice"}),
                     rule([p("shipment.documents", "in", ["commercial_invoice"])], rule_id="big",
                          category="documentation", chargeback={"amount": 500.0, "basis": "per_shipment", "text": "$500"}),
                     rule([p("cartons[].width_in", "lte", 99, "in")], rule_id="ok"),
                 ])
    rep = check(rs, ship())
    assert [x.rule.rule_id for x in rep.results] == ["big", "small", "unpriced", "ok"]
    assert rep.known_exposure_usd == 506.0


def test_generator_on_reference_rules():
    """Consistency check (not an accuracy claim): every seeded violation is caught, compliant bases pass,
    and nothing outside the mutated field fails."""
    rs = RuleSet.model_validate_json(REF.read_text())
    res = generate(rs)
    assert len(res.bases) == 9 and not res.unrepairable
    for b in res.bases:
        assert not check(rs, b).by_outcome(Outcome.FAIL)
    for m in res.mutants:
        fails = {x.rule.rule_id for x in check(rs, m.shipment).by_outcome(Outcome.FAIL)}
        lab = mutant_label(m, rs)
        assert m.rule_id in fails, lab
        assert not fails - {m.rule_id} - set(lab["collateral_rule_ids"]), lab


def test_shared_fee_line_billed_once():
    label = {"amount": 2.5, "basis": "per_carton", "text": "$2.50 per carton, $250 minimum"}
    rs = RuleSet(retailer="acme", guide_version="v1", guide_sha256="x", prompt_version="t", model="t",
                 extracted_at="2026-09-30T00:00:00Z", rules=[
                     rule([p("cartons[].sscc_present", "eq", True)], rule_id="sscc", chargeback=label),
                     rule([p("cartons[].label_position", "eq", "long_side_lower_right")], rule_id="pos", chargeback=label),
                 ])
    s = ship()
    s.cartons[0].sscc_present = False
    s.cartons[1].label_position = "top"
    rep = check(rs, s)
    assert [x.exposure_usd for x in rep.results] == [250.0, 250.0]      # each rule alone
    assert rep.known_exposure_usd == 250.0                               # one schedule line, $250 minimum once
    assert rep.fee_groups[0].units == ["C1", "C2"]
    assert rep.results[0].fee_shared_with == ["pos"]
