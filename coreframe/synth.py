"""Synthetic shipments from a RuleSet: compliant bases + seeded violations with labels.

1. Scenarios: one base shipment per (shipment_type x destination DC) mentioned in
   the rules' applies_to conditions, plus a DC no rule mentions. That way
   conditional rules (parcel-only, DC-specific) are exercised on both sides.
2. Repair: start from a neutral template and fix every parameterized FAIL
   until the base passes all checkable rules. The engine is the oracle here;
   a base that can't be repaired is reported, not silently kept.
3. Seed: for every checkable parameter that applies to a base, make one mutant
   that breaks it on a single carton/pallet/field (numeric `between` gets two:
   above and below). The label records which rule the mutation targets.

Labels are what Phase 5 scores the checker against. A mutation can also break
other rules on the same field (a 56 lb parcel carton breaks both the 1-50 lb
and the 35 lb parcel rule). Those count as "collateral" in the labels, not as
false positives.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from coreframe.engine import DURATIONS, Outcome, applicability, check, convert
from coreframe.schemas import TARGETS, Carton, Pallet, Parameter, RuleSet, RuleStatus, Shipment

DEFAULT_DC = "0001"   # a DC no rule mentions
SHIP_DATE = date(2026, 10, 6)  # a Tuesday
TZ = timezone(timedelta(hours=-7))
BAD_STRING = "noncompliant"
BAD_CARRIER = "Unapproved Freight Co"


@dataclass
class Mutant:
    shipment: Shipment
    base_id: str
    rule_id: str
    parameter: str
    target: str
    element: str
    mutation: str


@dataclass
class SynthResult:
    bases: list[Shipment] = field(default_factory=list)
    mutants: list[Mutant] = field(default_factory=list)
    unrepairable: list[tuple[str, list[str]]] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)   # (rule_id, parameter, why)


def _scenarios(rs: RuleSet) -> tuple[list[str], list[str]]:
    types, dcs = set(), set()
    for r in rs.rules:
        if r.applies_to == "all":
            continue
        for c in r.applies_to:
            vals = c.value if isinstance(c.value, list) else [c.value]
            if c.field == "shipment_type":
                types.update(vals)
            elif c.field == "destination_dc":
                dcs.update(vals)
    return sorted(types) or ["ltl"], [DEFAULT_DC, *sorted(dcs)]


def _template(retailer: str, stype: str, dc: str) -> Shipment:
    parcel = stype == "parcel"
    cartons = [Carton(carton_id=f"C{i}", length_in=16, width_in=12, height_in=10, weight_lbs=20,
                      label_type="standard", label_position="unspecified", sscc_present=True)
               for i in range(1, 4 if parcel else 7)]
    pallets = [] if parcel else [
        Pallet(pallet_id=f"P{i}", height_in=48, weight_lbs=700, footprint="40x48", stacked=False,
               label_positions=["side_a", "side_b"]) for i in (1, 2)]
    return Shipment(
        shipment_id=f"{retailer[:3].upper()}-{stype}-{dc}", retailer=retailer, shipment_type=stype,
        ship_date=SHIP_DATE, asn_sent_at=None, carrier="Generic Carrier", appointment_time=None,
        destination_dc=dc, cartons=cartons, pallets=pallets, documents=[],
    )


def _default_datetime(field_name: str) -> datetime:
    if field_name == "appointment_time":
        return datetime.combine(SHIP_DATE + timedelta(days=2), time(10, 0), TZ)
    return datetime.combine(SHIP_DATE, time(6, 0), TZ)


def _set(ship: Shipment, target: str, element: str, value: Any) -> None:
    scope, name = target.split(".", 1)
    if scope == "shipment":
        setattr(ship, name, value)
        return
    items = ship.cartons if scope == "cartons[]" else ship.pallets
    id_attr = "carton_id" if scope == "cartons[]" else "pallet_id"
    for it in items:
        if getattr(it, id_attr) == element:
            setattr(it, name, value)


def _get(ship: Shipment, target: str, element: str) -> Any:
    scope, name = target.split(".", 1)
    if scope == "shipment":
        return getattr(ship, name)
    items = ship.cartons if scope == "cartons[]" else ship.pallets
    id_attr = "carton_id" if scope == "cartons[]" else "pallet_id"
    return next(getattr(it, name) for it in items if getattr(it, id_attr) == element)


def _num(p: Parameter, v: float) -> float | None:
    return convert(v, p.unit, TARGETS[p.target][1])


def _satisfying(p: Parameter, current: Any, ship: Shipment) -> Any:
    op, typ = p.operator.value, TARGETS[p.target][0]
    if op == "exists":
        if not p.value:
            return [] if typ.startswith("list") else None
        return _default_datetime(p.target.split(".")[1]) if typ == "datetime" else "present"
    if op == "eq":
        return _num(p, p.value) if typ == "number" else p.value
    if op == "lte":
        return round(_num(p, p.value) * 0.9, 1)
    if op == "gte":
        return round(_num(p, p.value) * 1.1, 1)
    if op == "between":
        lo, hi = sorted(_num(p, v) for v in p.value)
        return round((lo + hi) / 2, 1)
    if op == "in":
        if typ.startswith("list"):
            return sorted(set(current or []) | {str(v) for v in p.value})
        return p.value[0]
    if op == "before":
        ref = getattr(ship, p.reference.split(".")[1]) or _default_datetime(p.reference.split(".")[1])
        return ref - timedelta(minutes=float(p.value) * DURATIONS[p.unit.lower()] + 60)
    raise ValueError(op)


def repair(ship: Shipment, rs: RuleSet, max_rounds: int = 6) -> list[str]:
    """Mutate ship in place until no parameterized rule FAILs. Returns rule_ids still failing."""
    for _ in range(max_rounds):
        report = check(rs, ship)
        fails = report.by_outcome(Outcome.FAIL)
        if not fails:
            return []
        for rr in fails:
            for pr in rr.params:
                if pr.outcome != Outcome.FAIL:
                    continue
                p = pr.parameter
                if p.operator.value == "before" and getattr(ship, p.reference.split(".")[1]) is None:
                    _set(ship, p.reference, "shipment", _default_datetime(p.reference.split(".")[1]))
                for el in pr.failing_elements:
                    _set(ship, p.target, el, _satisfying(p, _get(ship, p.target, el), ship))
    return [rr.rule.rule_id for rr in check(rs, ship).by_outcome(Outcome.FAIL)]


def _violations(p: Parameter, current: Any, ship: Shipment) -> list[tuple[Any, str]]:
    """Values that break p, with a short description."""
    op, typ = p.operator.value, TARGETS[p.target][0]
    if op == "exists":
        if p.value:
            return [([] if typ.startswith("list") else None, "removed")]
        return [(_default_datetime(p.target.split(".")[1]) if typ == "datetime" else "present", "added")]
    if op == "lte":
        b = _num(p, p.value)
        return [(round(b + max(1.0, b * 0.1), 1), "above max")]
    if op == "gte":
        return [(round(_num(p, p.value) * 0.8, 1), "below min")]
    if op == "between":
        lo, hi = sorted(_num(p, v) for v in p.value)
        out = [(round(hi + max(1.0, hi * 0.1), 1), "above max")]
        if lo > 0:
            out.append((round(lo * 0.5, 2), "below min"))
        return out
    if op == "eq":
        if typ == "bool":
            return [(not bool(p.value), "flipped")]
        if typ == "number":
            return [(round(_num(p, p.value) * 1.2 + 1, 1), "changed")]
        if p.target.endswith("footprint"):
            return [("48x45", "changed")]
        return [(BAD_STRING, "changed")]
    if op == "in":
        if typ.startswith("list"):
            drop = {str(v) for v in p.value}
            return [([d for d in (current or []) if d not in drop], f"dropped {', '.join(sorted(drop))}")]
        return [(BAD_CARRIER if p.target.endswith("carrier") else BAD_STRING, "not in allowed list")]
    if op == "before":
        ref = getattr(ship, p.reference.split(".")[1])
        if ref is None:
            return []
        return [(ref - timedelta(minutes=float(p.value) * DURATIONS[p.unit.lower()] / 2), "too late")]
    return []


def _fmt(v: Any) -> str:
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes")
    return repr(v)


def generate(rs: RuleSet) -> SynthResult:
    res = SynthResult()
    types, dcs = _scenarios(rs)
    for stype in types:
        for dc in dcs:
            base = _template(rs.retailer, stype, dc)
            still_failing = repair(base, rs)
            if still_failing:
                res.unrepairable.append((base.shipment_id, still_failing))
                continue
            res.bases.append(base)

            base_report = {rr.rule.rule_id: rr for rr in check(rs, base).results}
            n = 0
            for rule in rs.rules:
                if rule.status != RuleStatus.parameterized:
                    continue
                if base_report[rule.rule_id].outcome != Outcome.PASS:
                    continue  # not applicable to this base (or undecidable): nothing to seed
                for p in rule.parameters:
                    scope = p.target.split(".")[0]
                    element = "shipment" if scope == "shipment" else (
                        base.cartons[0].carton_id if scope == "cartons[]" else base.pallets[0].pallet_id)
                    current = _get(base, p.target, element)
                    options = _violations(p, current, base)
                    if not options:
                        res.skipped.append((rule.rule_id, p.name, "no violating value available"))
                    for value, how in options:
                        n += 1
                        m = copy.deepcopy(base)
                        m.shipment_id = f"{base.shipment_id}-m{n:02d}"
                        _set(m, p.target, element, value)
                        m = Shipment.model_validate(m.model_dump())  # re-validate (e.g. dims > 0)
                        res.mutants.append(Mutant(
                            shipment=m, base_id=base.shipment_id, rule_id=rule.rule_id, parameter=p.name,
                            target=p.target, element=element,
                            mutation=f"{p.target.split('.', 1)[1]} {_fmt(current)} -> {_fmt(value)} ({how})"))
    return res


def mutant_label(m: Mutant, rs: RuleSet) -> dict:
    """Label for one mutant. `collateral` = other applicable rules checking the same field,
    which the mutation may legitimately also break."""
    same_field = []
    for r in rs.rules:
        if r.rule_id == m.rule_id or r.status != RuleStatus.parameterized:
            continue
        if applicability(r, m.shipment)[0] is not True:
            continue
        if any(p.target == m.target or p.reference == m.target for p in r.parameters):
            same_field.append(r.rule_id)
    return {"shipment_id": m.shipment.shipment_id, "base_id": m.base_id, "seeded_rule_id": m.rule_id,
            "parameter": m.parameter, "target": m.target, "element": m.element, "mutation": m.mutation,
            "collateral_rule_ids": same_field}
