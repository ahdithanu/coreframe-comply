"""Deterministic rule engine: RuleSet x Shipment -> per-rule PASS / FAIL / NEEDS_REVIEW / NOT_APPLICABLE.

No model calls. Every outcome is explainable from the rule's typed parameters
and the shipment's fields:

  * applies_to is evaluated first. A condition on a field the shipment doesn't
    record (e.g. shipment_type missing) makes applicability unknown, and a
    parameterized rule then becomes NEEDS_REVIEW instead of a guess.
  * needs_human rules that plausibly apply come back as NEEDS_REVIEW, with the
    extractor's review note.
  * List targets (cartons[], pallets[]) are checked per element; a FAIL names
    the failing carton/pallet IDs.
  * Units are converted to the shipment field's unit (in/cm/mm/ft, lbs/kg/oz).
    An unknown unit is NEEDS_REVIEW, never a silent pass.
  * Missing data is NEEDS_REVIEW, except where absence is itself the
    violation: `exists` and the target side of `before` (e.g. ASN never sent).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel

from coreframe.render import fmt_param, fmt_value
from coreframe.schemas import TARGETS, Category, Parameter, Rule, RuleSet, RuleStatus, Shipment


class Outcome(str, Enum):
    FAIL = "FAIL"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    PASS = "PASS"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ParamResult(BaseModel):
    parameter: Parameter
    outcome: Outcome
    expected: str
    actual: str | None = None
    failing_elements: list[str] = []
    detail: str | None = None


class RuleResult(BaseModel):
    rule: Rule
    outcome: Outcome
    params: list[ParamResult] = []
    reason: str | None = None
    exposure_usd: float | None = None     # this rule's chargeback on its own, if the shipment goes out as-is
    fee_shared_with: list[str] = []       # other failing rules billed under the same schedule line


class FeeGroup(BaseModel):
    """One chargeback schedule line. Several rules can map to it (length/width/height/weight
    all bill as 'carton outside limits'); the retailer bills the line once per unit."""
    text: str
    rule_ids: list[str]
    units: list[str]
    exposure_usd: float | None


class CheckReport(BaseModel):
    shipment_id: str
    retailer: str
    guide_version: str
    results: list[RuleResult]
    fee_groups: list[FeeGroup] = []

    def by_outcome(self, outcome: Outcome) -> list[RuleResult]:
        return [r for r in self.results if r.outcome == outcome]

    @property
    def known_exposure_usd(self) -> float:
        """Total estimated chargeback, billing each schedule line once."""
        return round(sum(g.exposure_usd or 0 for g in self.fee_groups), 2)


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #

UNIT_FACTORS = {  # to the canonical unit of each dimension
    "in": ("length", 1.0), "inch": ("length", 1.0), "inches": ("length", 1.0), '"': ("length", 1.0),
    "cm": ("length", 1 / 2.54), "mm": ("length", 1 / 25.4), "ft": ("length", 12.0), "feet": ("length", 12.0),
    "lbs": ("mass", 1.0), "lb": ("mass", 1.0), "pounds": ("mass", 1.0),
    "kg": ("mass", 2.20462262), "g": ("mass", 0.00220462262), "oz": ("mass", 1 / 16),
}
DURATIONS = {"minute": 1, "minutes": 1, "min": 1, "mins": 1, "hour": 60, "hours": 60, "hr": 60, "hrs": 60,
             "day": 1440, "days": 1440}


def norm(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(v).lower())


def convert(value: float, from_unit: str | None, to_unit: str | None) -> float | None:
    """Convert value into to_unit. None if the units are unknown or incompatible."""
    if not from_unit or not to_unit or from_unit.lower() == to_unit.lower():
        return float(value)
    a, b = UNIT_FACTORS.get(from_unit.lower()), UNIT_FACTORS.get(to_unit.lower())
    if not a or not b or a[0] != b[0]:
        return None
    return float(value) * a[1] / b[1]


def _footprint(s: str) -> tuple[float, float] | None:
    parts = re.split(r"\s*[x×]\s*", str(s).lower().replace("in", "").strip())
    try:
        return tuple(sorted(float(p) for p in parts))  # type: ignore[return-value]
    except ValueError:
        return None


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "yes", "y", "1"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "no", "n", "0"):
        return False
    return None


def _as_datetime(v: Any, like: datetime | None = None) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, date):
        dt = datetime.combine(v, time.min)
    else:
        return None
    if like is not None and (dt.tzinfo is None) != (like.tzinfo is None):
        # Mixed naive/aware: assume the naive one is in the other's timezone.
        dt = dt.replace(tzinfo=like.tzinfo) if dt.tzinfo is None else dt.replace(tzinfo=None)
    return dt


def _present(v: Any) -> bool:
    return v is not None and v != "" and v != []


# --------------------------------------------------------------------------- #
# Applicability
# --------------------------------------------------------------------------- #

def applicability(rule: Rule, shipment: Shipment) -> tuple[bool | None, str | None]:
    """True / False / None (unknown), plus a reason when not simply True."""
    if rule.applies_to == "all" or not rule.applies_to:
        return True, None
    unknown = []
    for c in rule.applies_to:
        actual = getattr(shipment, c.field, None)
        wanted = c.value if isinstance(c.value, list) else [c.value]
        if actual is None:
            unknown.append(c.field)
            continue
        hit = norm(actual) in {norm(w) for w in wanted}
        if (c.operator in ("eq", "in") and not hit) or (c.operator == "not_in" and hit):
            return False, f"{c.field} is {actual!r}; rule applies to {c.operator} {fmt_value(c.value)}"
    if unknown:
        return None, f"can't tell if this applies: shipment doesn't record {', '.join(unknown)}"
    return True, None


def _needs_elements(rule: Rule) -> set[str]:
    """Which element lists the rule is about, so pallet rules don't apply to parcel-only shipments."""
    scopes = {p.target.split(".")[0] for p in rule.parameters if p.target and "[]" in p.target}
    if rule.category == Category.pallet or re.search(r"\bpallets?\b", rule.requirement, re.I):
        scopes.add("pallets[]")
    if rule.category == Category.carton:
        scopes.add("cartons[]")
    return scopes


# --------------------------------------------------------------------------- #
# Parameter evaluation
# --------------------------------------------------------------------------- #

def _elements(target: str, shipment: Shipment) -> list[tuple[str, Any]]:
    scope, field = target.split(".", 1)
    if scope == "shipment":
        return [("shipment", getattr(shipment, field))]
    items = shipment.cartons if scope == "cartons[]" else shipment.pallets
    id_attr = "carton_id" if scope == "cartons[]" else "pallet_id"
    return [(getattr(i, id_attr), getattr(i, field)) for i in items]


def _compare(p: Parameter, actual: Any, field_type: str, field_unit: str | None) -> tuple[bool | None, str | None]:
    """(passes?, detail). passes=None means undecidable -> NEEDS_REVIEW."""
    op = p.operator.value
    if op == "exists":
        return _present(actual) == bool(p.value), None
    if actual is None or (field_type == "string" and actual == ""):
        return None, "not recorded on the shipment"

    if op in ("lte", "gte", "between"):
        bounds = p.value if op == "between" else [p.value]
        conv = [convert(b, p.unit, field_unit) for b in bounds]
        if any(c is None for c in conv):
            return None, f"can't convert {p.unit!r} to {field_unit!r}"
        a = float(actual)
        eps = 1e-9
        if op == "lte":
            return a <= conv[0] + eps, None
        if op == "gte":
            return a >= conv[0] - eps, None
        lo, hi = sorted(conv)
        return lo - eps <= a <= hi + eps, None

    if op == "eq":
        if field_type == "bool":
            want = _as_bool(p.value)
            return (None, f"expected a boolean, rule says {p.value!r}") if want is None else (bool(actual) == want, None)
        if field_type == "number":
            want = convert(p.value, p.unit, field_unit)
            return (None, f"can't convert {p.unit!r} to {field_unit!r}") if want is None else (abs(float(actual) - want) < 1e-6, None)
        if p.target and p.target.endswith("footprint"):
            a, w = _footprint(actual), _footprint(p.value)
            return (None, "unparseable footprint") if a is None or w is None else (a == w, None)
        return norm(actual) == norm(p.value), None

    if op == "in":
        wanted = {norm(v) for v in p.value}
        if field_type.startswith("list"):
            missing = wanted - {norm(a) for a in actual}
            return not missing, (f"missing {', '.join(sorted(missing))}" if missing else None)
        return norm(actual) in wanted, None

    return None, f"operator {op!r} not handled here"


def _eval_before(p: Parameter, shipment: Shipment) -> ParamResult:
    expected = fmt_param(p)
    minutes_per = DURATIONS.get((p.unit or "").lower())
    if minutes_per is None:
        return ParamResult(parameter=p, outcome=Outcome.NEEDS_REVIEW, expected=expected,
                           detail=f"unsupported duration unit {p.unit!r}")
    ref_raw = getattr(shipment, p.reference.split(".", 1)[1], None)
    tgt_raw = getattr(shipment, p.target.split(".", 1)[1], None)
    ref = _as_datetime(ref_raw)
    if ref is None:
        return ParamResult(parameter=p, outcome=Outcome.NEEDS_REVIEW, expected=expected,
                           detail=f"{p.reference} not recorded, can't evaluate")
    tgt = _as_datetime(tgt_raw, like=ref)
    if tgt is None:
        return ParamResult(parameter=p, outcome=Outcome.FAIL, expected=expected, actual="never",
                           failing_elements=["shipment"], detail=f"{p.target} missing")
    lead = ref - tgt
    required = timedelta(minutes=float(p.value) * minutes_per)
    ok = lead >= required
    hours = lead.total_seconds() / 3600
    return ParamResult(parameter=p, outcome=Outcome.PASS if ok else Outcome.FAIL, expected=expected,
                       actual=f"{hours:.1f} hours before", failing_elements=[] if ok else ["shipment"])


def evaluate_param(p: Parameter, shipment: Shipment) -> ParamResult:
    if p.operator.value == "before":
        return _eval_before(p, shipment)
    field_type, field_unit, _ = TARGETS[p.target]
    expected = fmt_param(p)
    elements = _elements(p.target, shipment)
    if not elements:
        return ParamResult(parameter=p, outcome=Outcome.NOT_APPLICABLE, expected=expected,
                           detail=f"shipment has no {p.target.split('[')[0]}")
    failing, undecided, actuals, details = [], [], [], set()
    for elem_id, actual in elements:
        ok, detail = _compare(p, actual, field_type, field_unit)
        if detail:
            details.add(detail)
        if ok is False:
            failing.append(elem_id)
            actuals.append(f"{elem_id}: {fmt_value(actual) if actual is not None else 'none'}")
        elif ok is None:
            undecided.append(elem_id)
    if failing:
        outcome = Outcome.FAIL
    elif undecided:
        outcome = Outcome.NEEDS_REVIEW
        actuals = [f"{e}: undecidable" for e in undecided]
    else:
        outcome = Outcome.PASS
        actuals = [fmt_value(elements[0][1])] if len(elements) == 1 else [f"all {len(elements)} ok"]
    unit = f" {field_unit}" if field_unit and failing and field_type == "number" else ""
    return ParamResult(parameter=p, outcome=outcome, expected=expected,
                       actual="; ".join(actuals) + unit, failing_elements=failing,
                       detail="; ".join(sorted(details)) or None)


# --------------------------------------------------------------------------- #
# Rules, exposure, report
# --------------------------------------------------------------------------- #

_MIN_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*minimum", re.I)


def _fee(cb, units: set[str]) -> float | None:
    if cb is None or cb.amount is None:
        return None
    if cb.basis in ("per_carton", "per_pallet", "per_unit"):
        total = cb.amount * max(1, len(units - {"shipment"}))
    else:
        total = cb.amount
    m = _MIN_RE.search(cb.text)
    if m:
        total = max(total, float(m.group(1).replace(",", "")))
    return round(total, 2)


def exposure(rule: Rule, params: list[ParamResult]) -> float | None:
    return _fee(rule.chargeback, {e for pr in params for e in pr.failing_elements})


def _fee_key(rule: Rule) -> tuple | None:
    cb = rule.chargeback
    return None if cb is None else (cb.amount, cb.basis, " ".join(cb.text.lower().split()))


def fee_groups(results: list[RuleResult]) -> list[FeeGroup]:
    """Group failing rules by identical chargeback line. Assumes identical fee text means the
    same schedule line, which holds when fees come from one schedule table (as extraction does)."""
    groups: dict[tuple, list[RuleResult]] = {}
    for r in results:
        if r.outcome == Outcome.FAIL and (key := _fee_key(r.rule)) is not None:
            groups.setdefault(key, []).append(r)
    out = []
    for members in groups.values():
        units = {e for m in members for pr in m.params for e in pr.failing_elements}
        ids = [m.rule.rule_id for m in members]
        for m in members:
            m.fee_shared_with = [i for i in ids if i != m.rule.rule_id]
        out.append(FeeGroup(text=members[0].rule.chargeback.text, rule_ids=ids,
                            units=sorted(units), exposure_usd=_fee(members[0].rule.chargeback, units)))
    return sorted(out, key=lambda g: -(g.exposure_usd if g.exposure_usd is not None else -1))


def evaluate_rule(rule: Rule, shipment: Shipment) -> RuleResult:
    if rule.category == Category.chargeback_policy and rule.status == RuleStatus.needs_human:
        return RuleResult(rule=rule, outcome=Outcome.NOT_APPLICABLE, reason="billing policy, not a shipment check")
    applies, why = applicability(rule, shipment)
    if applies is False:
        return RuleResult(rule=rule, outcome=Outcome.NOT_APPLICABLE, reason=why)
    missing = [s for s in _needs_elements(rule) if not (shipment.cartons if s == "cartons[]" else shipment.pallets)]
    if missing:
        return RuleResult(rule=rule, outcome=Outcome.NOT_APPLICABLE,
                          reason=f"shipment has no {', '.join(m.rstrip('[]') for m in missing)}")
    if rule.status == RuleStatus.needs_human:
        return RuleResult(rule=rule, outcome=Outcome.NEEDS_REVIEW,
                          reason=rule.review_note or "rule could not be parameterized")
    if applies is None:
        return RuleResult(rule=rule, outcome=Outcome.NEEDS_REVIEW, reason=why)

    params = [evaluate_param(p, shipment) for p in rule.parameters]
    outcomes = {pr.outcome for pr in params}
    if Outcome.FAIL in outcomes:
        outcome = Outcome.FAIL
    elif Outcome.NEEDS_REVIEW in outcomes:
        outcome = Outcome.NEEDS_REVIEW
    elif outcomes == {Outcome.NOT_APPLICABLE}:
        outcome = Outcome.NOT_APPLICABLE
    else:
        outcome = Outcome.PASS
    reason = "; ".join(pr.detail for pr in params if pr.detail and pr.outcome != Outcome.PASS) or None
    return RuleResult(rule=rule, outcome=outcome, params=params, reason=reason,
                      exposure_usd=exposure(rule, params) if outcome == Outcome.FAIL else None)


_ORDER = {Outcome.FAIL: 0, Outcome.NEEDS_REVIEW: 1, Outcome.PASS: 2, Outcome.NOT_APPLICABLE: 3}


def check(ruleset: RuleSet, shipment: Shipment) -> CheckReport:
    results = [evaluate_rule(r, shipment) for r in ruleset.rules]
    # FAILs by exposure (unpriced after priced), then review, pass, n/a; stable within groups.
    results.sort(key=lambda r: (_ORDER[r.outcome], -(r.exposure_usd if r.exposure_usd is not None else -1)))
    return CheckReport(shipment_id=shipment.shipment_id, retailer=ruleset.retailer,
                       guide_version=ruleset.guide_version, results=results, fee_groups=fee_groups(results))
