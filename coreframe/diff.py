"""Version diff: two RuleSets for the same retailer -> added / removed / changed / unchanged.

Matching (deterministic, no model calls):
  * rules only match within the same category
  * score = overlap of checked fields (target + operator)
          + similarity of the requirement text with numbers masked
            ("between 1 and 50 lbs" ~ "between 1 and 45 lbs")
          + whether the applies_to conditions are identical
    rules with no checkable fields are matched on text and conditions alone
  * pairs are assigned greedily from the highest score down; below THRESHOLD the
    rules count as removed + added

A matched pair is "changed" when anything that affects compliance differs:
parameter values/units, conditions, chargeback, or status. Moving to a new
section or page, or rewording a parameterized rule without changing its
parameters, stays "unchanged" with a note, because it doesn't change what a
shipment has to do. Numeric and list changes are labeled stricter or looser.
"""

from __future__ import annotations

import difflib
import re
from typing import Literal

from pydantic import BaseModel

from coreframe.engine import convert
from coreframe.extract import _cond_key
from coreframe.render import fmt_applies, fmt_param
from coreframe.schemas import Parameter, Rule, RuleSet, RuleStatus

THRESHOLD = 0.6


class FieldChange(BaseModel):
    field: str
    old: str
    new: str
    direction: str | None = None        # stricter / looser / higher fee / lower fee / None


class RuleDiff(BaseModel):
    kind: Literal["added", "removed", "changed", "unchanged"]
    old: Rule | None = None
    new: Rule | None = None
    score: float | None = None
    changes: list[FieldChange] = []
    notes: list[str] = []

    @property
    def rule(self) -> Rule:
        return self.new or self.old  # type: ignore[return-value]

    @property
    def checkable(self) -> bool:
        return any(r is not None and r.status == RuleStatus.parameterized for r in (self.old, self.new))


class DiffReport(BaseModel):
    retailer: str
    old_version: str
    new_version: str
    entries: list[RuleDiff]

    def of(self, kind: str) -> list[RuleDiff]:
        return [e for e in self.entries if e.kind == kind]


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def _mask(text: str) -> str:
    return re.sub(r"\d+(?:[.,]\d+)*", "#", " ".join(text.lower().split()))


def text_sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _mask(a), _mask(b)).ratio()


def snippet_sim(a: Rule, b: Rule) -> float:
    """Best similarity between any cited source sentences: stable across extraction runs even
    when the model words the requirement differently."""
    return max((text_sim(x.snippet, y.snippet) for x in a.sources for y in b.sources), default=0.0)


def _same_source(a: Rule, b: Rule) -> bool:
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())  # noqa: E731
    return bool({norm(x.snippet) for x in a.sources} & {norm(y.snippet) for y in b.sources})


def _sig(r: Rule) -> set[tuple[str, str]]:
    return {(p.target, p.operator.value) for p in r.parameters if p.target}


def score(a: Rule, b: Rule) -> float:
    if a.category != b.category:
        return 0.0
    ts = text_sim(a.requirement, b.requirement)
    cond = 1.0 if _cond_key(a) == _cond_key(b) else 0.0
    sa, sb = _sig(a), _sig(b)
    if sa or sb:
        jac = len(sa & sb) / len(sa | sb)
        if jac == 0:
            return 0.3 * ts
        return 0.5 * jac + 0.3 * ts + 0.2 * cond
    return 0.8 * max(ts, snippet_sim(a, b)) + 0.2 * cond


def match(old: list[Rule], new: list[Rule]) -> tuple[list[tuple[Rule, Rule, float]], list[Rule], list[Rule]]:
    pairs = sorted(((score(a, b), i, j) for i, a in enumerate(old) for j, b in enumerate(new)),
                   key=lambda t: (-t[0], t[1], t[2]))
    used_old, used_new, matched = set(), set(), []
    for s, i, j in pairs:
        if s < THRESHOLD:
            break
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        matched.append((old[i], new[j], round(s, 3)))
    removed = [r for i, r in enumerate(old) if i not in used_old]
    added = [r for j, r in enumerate(new) if j not in used_new]
    return matched, removed, added


# --------------------------------------------------------------------------- #
# Classifying a matched pair
# --------------------------------------------------------------------------- #

def _numbers(p: Parameter, unit: str | None) -> list[float] | None:
    vals = p.value if isinstance(p.value, list) else [p.value]
    try:
        out = [convert(float(v), p.unit, unit) for v in vals]
    except (TypeError, ValueError):
        return None
    return None if any(v is None for v in out) else out


def direction(old: Parameter, new: Parameter) -> str | None:
    op = new.operator.value
    if old.operator != new.operator:
        return None
    if op in ("lte", "gte", "between", "before"):
        o, n = _numbers(old, new.unit), _numbers(new, new.unit)
        if o is None or n is None or o == n:
            return None
        if op == "lte":
            return "stricter" if n[0] < o[0] else "looser"
        if op in ("gte", "before"):
            return "stricter" if n[0] > o[0] else "looser"
        (olo, ohi), (nlo, nhi) = sorted(o), sorted(n)
        if nlo >= olo and nhi <= ohi:
            return "stricter"
        if nlo <= olo and nhi >= ohi:
            return "looser"
        return "shifted"
    if op == "in":
        o = {str(v) for v in old.value}
        n = {str(v) for v in new.value}
        required_list = new.target in ("shipment.documents", "pallets[].label_positions")
        if n < o:
            return "looser" if required_list else "stricter"
        if n > o:
            return "stricter" if required_list else "looser"
        return "changed" if n != o else None
    return None


def _list_delta(old: Parameter, new: Parameter) -> str:
    o, n = [str(v) for v in old.value], [str(v) for v in new.value]
    bits = [f"-{v}" for v in o if v not in n] + [f"+{v}" for v in n if v not in o]
    return f" ({', '.join(bits)})" if bits else ""


def _param_changes(old: Rule, new: Rule) -> list[FieldChange]:
    out = []
    by_key_old = {(p.target, p.operator.value, p.name if not p.target else ""): p for p in old.parameters}
    by_key_new = {(p.target, p.operator.value, p.name if not p.target else ""): p for p in new.parameters}
    for key in list(by_key_old) + [k for k in by_key_new if k not in by_key_old]:
        a, b = by_key_old.get(key), by_key_new.get(key)
        if a and b:
            if (a.value, (a.unit or "").lower(), a.reference) == (b.value, (b.unit or "").lower(), b.reference):
                continue
            label = b.target or b.name
            delta = _list_delta(a, b) if b.operator.value == "in" else ""
            out.append(FieldChange(field=label, old=fmt_param(a), new=fmt_param(b) + delta, direction=direction(a, b)))
        elif a:
            out.append(FieldChange(field=a.target or a.name, old=fmt_param(a), new="(check removed)"))
        else:
            out.append(FieldChange(field=b.target or b.name, old="(no check)", new=fmt_param(b)))
    return out


def _fee(r: Rule) -> str:
    return r.chargeback.text if r.chargeback else "none stated"


def classify(old: Rule, new: Rule, s: float) -> RuleDiff:
    changes = _param_changes(old, new)
    if _cond_key(old) != _cond_key(new):
        changes.append(FieldChange(field="applies to", old=fmt_applies(old), new=fmt_applies(new)))
    if _fee(old) != _fee(new):
        d = None
        if old.chargeback and new.chargeback and old.chargeback.amount is not None and new.chargeback.amount is not None:
            d = "higher fee" if new.chargeback.amount > old.chargeback.amount else "lower fee"
        changes.append(FieldChange(field="chargeback", old=_fee(old), new=_fee(new), direction=d))
    if old.status != new.status:
        changes.append(FieldChange(field="status", old=old.status.value, new=new.status.value))

    notes = []
    same_text = " ".join(old.requirement.split()) == " ".join(new.requirement.split())
    if not same_text:
        if new.status == RuleStatus.needs_human and not changes:
            # For non-checkable rules the wording IS the rule, unless the cited source sentence is
            # identical (then only the extractor's paraphrase changed).
            if _same_source(old, new):
                notes.append("extractor reworded; cited source text identical")
            else:
                changes.append(FieldChange(field="requirement", old=old.requirement, new=new.requirement))
        elif not changes:
            notes.append("reworded; checked values unchanged")
    o, n = old.sources[0], new.sources[0]
    if (o.page, o.section) != (n.page, n.section):
        notes.append(f"moved: p. {o.page} {o.section} → p. {n.page} {n.section}")
    elif " ".join(o.snippet.split()) != " ".join(n.snippet.split()) and not changes:
        notes.append("source sentence reworded")
    return RuleDiff(kind="changed" if changes else "unchanged", old=old, new=new, score=s, changes=changes, notes=notes)


def diff(old: RuleSet, new: RuleSet) -> DiffReport:
    if old.retailer.lower() != new.retailer.lower():
        raise ValueError(f"different retailers: {old.retailer} vs {new.retailer}")
    matched, removed, added = match(old.rules, new.rules)
    entries = [classify(a, b, s) for a, b, s in matched]
    entries += [RuleDiff(kind="removed", old=r) for r in removed]
    entries += [RuleDiff(kind="added", new=r) for r in added]
    order = {"changed": 0, "added": 1, "removed": 2, "unchanged": 3}
    entries.sort(key=lambda e: (order[e.kind], not e.checkable, e.rule.category.value, e.rule.rule_id))
    return DiffReport(retailer=new.retailer, old_version=old.guide_version, new_version=new.guide_version,
                      entries=entries)
