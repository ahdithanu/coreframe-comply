import pytest
from pydantic import ValidationError

from coreframe.schemas import Parameter, Rule, Shipment

BASE = dict(
    rule_id="nw_carton_001", retailer="northwind", guide_version="v2025.1", category="carton",
    requirement="Cartons must not exceed 50 lbs.",
    parameters=[{"name": "max carton weight", "target": "cartons[].weight_lbs",
                 "operator": "lte", "value": 50, "unit": "lbs"}],
    chargeback={"amount": 3.0, "basis": "per_carton", "text": "$3.00 per carton"},
    sources=[{"page": 2, "section": "3.1 Dimensions and Weight", "snippet": "Weight 1 lb 50 lbs"}],
    confidence="high", status="parameterized",
)


def rule(**overrides):
    return Rule(**{**BASE, **overrides})


def test_valid_rule():
    assert rule().parameters[0].target == "cartons[].weight_lbs"


def test_needs_human_rule_may_have_no_parameters():
    r = rule(status="needs_human", parameters=[], requirement="Labels must be legible.")
    assert r.parameters == []


@pytest.mark.parametrize("params, msg", [
    ([], "at least one parameter"),
    ([{"name": "x", "operator": "eq", "value": "y"}], "unmapped parameters"),
])
def test_parameterized_requires_mapped_parameters(params, msg):
    with pytest.raises(ValidationError, match=msg):
        rule(parameters=params)


@pytest.mark.parametrize("param, msg", [
    ({"operator": "between", "value": 5}, "between"),
    ({"operator": "in", "value": []}, "non-empty list"),
    ({"operator": "lte", "value": "fifty"}, "numeric"),
    ({"operator": "eq", "value": [1, 2]}, "scalar"),
    ({"operator": "before", "value": 2, "unit": "hours"}, "reference"),
    ({"operator": "eq", "value": 1, "reference": "shipment.ship_date"}, "only valid with operator 'before'"),
    ({"operator": "eq", "value": 1, "target": "cartons[].color"}, "unknown target"),
])
def test_parameter_shape_validation(param, msg):
    with pytest.raises(ValidationError, match=msg):
        Parameter(**{"name": "p", "target": "cartons[].weight_lbs", **param})


def test_before_operator():
    p = Parameter(name="ASN lead time", target="shipment.asn_sent_at", operator="before",
                  value=2, unit="hours", reference="shipment.appointment_time")
    assert p.reference == "shipment.appointment_time"


def test_snippet_under_25_words():
    with pytest.raises(ValidationError, match="under 25 words"):
        rule(sources=[{"page": 1, "section": "s", "snippet": " ".join(["w"] * 25)}])


def test_sources_required():
    with pytest.raises(ValidationError):
        rule(sources=[])


def test_applies_to_conditions():
    r = rule(applies_to=[{"field": "shipment_type", "operator": "eq", "value": "parcel"}])
    assert r.applies_to[0].value == "parcel"
    with pytest.raises(ValidationError):
        rule(applies_to=[{"field": "color", "operator": "eq", "value": "red"}])


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        rule(page=3)


def test_shipment():
    s = Shipment(
        shipment_id="S1", retailer="northwind", shipment_type="ltl", ship_date="2026-09-20",
        asn_sent_at="2026-09-20T08:00:00-07:00", carrier="XPO", destination_dc="6012",
        cartons=[{"carton_id": "C1", "length_in": 20, "width_in": 14, "height_in": 12,
                  "weight_lbs": 32, "label_type": "GS1-128", "sscc_present": True}],
        pallets=[{"pallet_id": "P1", "height_in": 60, "weight_lbs": 1200, "footprint": "40x48",
                  "stacked": False, "label_positions": ["side_a", "side_b"]}],
        documents=["BOL", "packing_list"],
    )
    assert s.asn_sent_at.tzinfo is not None


def test_shipment_rejects_bad_footprint_and_nonpositive_dims():
    with pytest.raises(ValidationError, match="40x48"):
        Shipment(shipment_id="S", retailer="r", ship_date="2026-01-01", carrier="c", destination_dc="d",
                 pallets=[{"pallet_id": "P", "height_in": 1, "weight_lbs": 1, "footprint": "big", "stacked": False}])
    with pytest.raises(ValidationError):
        Shipment(shipment_id="S", retailer="r", ship_date="2026-01-01", carrier="c", destination_dc="d",
                 cartons=[{"carton_id": "C", "length_in": 0, "width_in": 1, "height_in": 1,
                           "weight_lbs": 1, "sscc_present": True}])
