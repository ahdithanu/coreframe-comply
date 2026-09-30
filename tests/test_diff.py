"""Version diff: matching and change classification."""

import random
from pathlib import Path

import pytest

from coreframe.diff import diff, direction
from coreframe.schemas import Parameter, RuleSet

RULES = Path(__file__).resolve().parents[1] / "data/sample/rules"


@pytest.fixture(scope="module")
def v1():
    return RuleSet.model_validate_json((RULES / "northwind_v2025.1.reference.json").read_text())


@pytest.fixture(scope="module")
def v2():
    return RuleSet.model_validate_json((RULES / "northwind_v2025.2.reference.json").read_text())


def test_planted_v2_edits_are_found_exactly(v1, v2):
    d = diff(v1, v2)
    changed = {e.new.requirement: {c.field: c.direction for c in e.changes} for e in d.of("changed")}
    assert changed == {
        "For LTL and TL shipments, the ASN must be received at least 4 hours before the delivery appointment.":
            {"shipment.asn_sent_at": "stricter", "chargeback": "higher fee"},
        "Carton weight must be between 1 and 45 lbs.": {"cartons[].weight_lbs": "stricter"},
        "LTL shipments must use XPO, Old Dominion, or Saia.": {"shipment.carrier": "changed"},
        "For parcel shipments, the ASN must be transmitted within 60 minutes after carrier tender.":
            {"chargeback": "higher fee"},
    }
    assert {e.new.requirement for e in d.of("added")} == {
        "Inner packs must contain a uniform quantity of units.",
        "Shipments containing hazardous materials must include a Safety Data Sheet (SDS).",
        "Pallets shipped to DC 6040 must not exceed 64 inches in height.",   # not confused with the DC 6012 rule
    }
    assert [e.old.requirement for e in d.of("removed")] == ["Individual cartons must not be banded or strapped."]
    notes = {e.new.requirement: e.notes for e in d.of("unchanged") if e.notes}
    assert notes == {
        "Gross pallet weight must not exceed 2,000 lbs.": ["source sentence reworded"],
        "Cartons must be corrugated with a minimum burst strength of 200 psi (ECT 32).":
            ["moved: p. 3 3.2 Carton Construction → p. 3 3.3 Carton Construction"],
    }
    carrier = next(e for e in d.of("changed") if "Saia" in e.new.requirement)
    assert "(-Estes, +Saia)" in carrier.changes[0].new


def test_identical_rulesets_have_no_changes(v1):
    d = diff(v1, v1)
    assert len(d.of("unchanged")) == len(v1.rules) and not d.of("changed") + d.of("added") + d.of("removed")


def test_extraction_noise_is_not_a_change(v1):
    """Two extraction runs of the same guide: rules come back in a different order and with
    different wording. Nothing should be reported as added, removed or changed."""
    noisy = v1.model_copy(deep=True)
    random.Random(7).shuffle(noisy.rules)
    for r in noisy.rules:
        r.requirement = "Per the guide: " + r.requirement.rstrip(".").lower() + " at all times."
    d = diff(v1, noisy)
    assert not d.of("added") and not d.of("removed") and not d.of("changed")
    assert all(e.old.rule_id == e.new.rule_id for e in d.entries)


def test_different_retailers_rejected(v1):
    other = v1.model_copy(update={"retailer": "acme"})
    with pytest.raises(ValueError):
        diff(v1, other)


def P(op, value, unit=None, target="cartons[].weight_lbs", reference=None):
    return Parameter(name="p", target=target, operator=op, value=value, unit=unit, reference=reference)


@pytest.mark.parametrize("old, new, expected", [
    (P("lte", 50, "lbs"), P("lte", 45, "lbs"), "stricter"),
    (P("lte", 50, "lbs"), P("lte", 55, "lbs"), "looser"),
    (P("lte", 72, "in", "pallets[].height_in"), P("lte", 180, "cm", "pallets[].height_in"), "stricter"),  # 70.9 in
    (P("gte", 1, "lbs"), P("gte", 2, "lbs"), "stricter"),
    (P("between", [1, 50], "lbs"), P("between", [2, 45], "lbs"), "stricter"),
    (P("between", [1, 50], "lbs"), P("between", [0.5, 60], "lbs"), "looser"),
    (P("between", [1, 50], "lbs"), P("between", [5, 60], "lbs"), "shifted"),
    (P("in", ["UPS", "FedEx"], target="shipment.carrier"), P("in", ["UPS"], target="shipment.carrier"), "stricter"),
    (P("in", ["BOL"], target="shipment.documents"), P("in", ["BOL", "SDS"], target="shipment.documents"), "stricter"),
    (P("before", 2, "hours", "shipment.asn_sent_at", "shipment.appointment_time"),
     P("before", 1, "hours", "shipment.asn_sent_at", "shipment.appointment_time"), "looser"),
])
def test_direction(old, new, expected):
    assert direction(old, new) == expected
