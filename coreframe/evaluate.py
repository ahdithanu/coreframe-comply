"""Evaluation: extracted rules vs hand-labeled ground truth, plus a checker eval and a release gate.

Extraction eval
  1. Propose matches between ground-truth rules and extracted rules (LLM-assisted, or a
     deterministic heuristic when offline) and write them to a CSV.
  2. A human confirms or corrects that CSV. Only rows with confirmed = y count. A
     confirmation is tied to the extracted rule's text, so if a later extraction changes
     the rule, the row goes stale and has to be confirmed again.
  3. Metrics on confirmed matches: rule precision/recall, parameter accuracy (value AND
     unit), citation accuracy (page), each broken down by category.
  4. Every miss goes to a failure-analysis CSV with a `failure_tag` column for the human
     to fill in. Tags survive reruns.

Checker eval
  Shipments and violation labels are generated from a hand-written REFERENCE rule set
  when one exists, and checked with the EXTRACTED rules. A rule the extractor missed or
  got wrong then shows up as a missed violation, so catch rate measures the whole
  pipeline. Without a reference set, labels come from the extracted rules themselves,
  which only tests self-consistency; the report says which mode was used.

Gate
  eval_config.yaml holds thresholds. gate() returns failures; eval.py exits nonzero on any.
"""

from __future__ import annotations

import csv
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from coreframe.diff import text_sim
from coreframe.engine import Outcome, check
from coreframe.schemas import Parameter, Rule, RuleSet
from coreframe.synth import generate, mutant_label

FAILURE_TAGS = ["table parsing", "cross-reference between sections", "conditional rule",
                "ambiguous wording", "model reasoning error", "other"]
MATCH_COLUMNS = ["gt_rule_id", "gt_requirement", "extracted_rule_id", "extracted_requirement",
                 "proposed_by", "confidence", "reason", "confirmed", "notes"]
FAILURE_COLUMNS = ["guide", "type", "gt_rule_id", "extracted_rule_id", "category", "detail", "page", "section",
                   "chargeback", "suggested_tag", "failure_tag", "notes"]
HEURISTIC_THRESHOLD = 0.55

# --------------------------------------------------------------------------- #
# Ground truth
# --------------------------------------------------------------------------- #


@dataclass
class GTRule:
    rule_id: str
    category: str
    requirement: str
    page: int
    section: str
    chargeback: str
    notes: str
    params: list[tuple[str, str, str]] = field(default_factory=list)   # (name, value, unit)


def load_ground_truth(path: Path) -> list[GTRule]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = {"rule_id", "category", "requirement", "parameter_name", "parameter_value", "unit", "page",
               "section", "chargeback", "notes"} - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    rules: dict[str, GTRule] = {}
    for row in df.to_dict("records"):
        r = rules.setdefault(row["rule_id"], GTRule(
            rule_id=row["rule_id"], category=row["category"].strip(), requirement=row["requirement"].strip(),
            page=int(float(row["page"])), section=row["section"].strip(), chargeback=row["chargeback"].strip(),
            notes=row["notes"].strip()))
        if row["parameter_value"].strip():
            r.params.append((row["parameter_name"].strip(), row["parameter_value"].strip(), row["unit"].strip()))
    return list(rules.values())

# --------------------------------------------------------------------------- #
# Value / unit comparison
# --------------------------------------------------------------------------- #


UNIT_ALIASES = {"lb": "lbs", "pound": "lbs", "pounds": "lbs", "inch": "in", "inches": "in", '"': "in",
                "hour": "hours", "hr": "hours", "hrs": "hours", "minute": "minutes", "min": "minutes",
                "mins": "minutes", "day": "days", "%": "percent", "pct": "percent"}


def _n(s: Any) -> str:
    return re.sub(r"[^a-z0-9.]+", "", str(s).lower())


def _num(s: Any) -> float | None:
    if isinstance(s, bool):
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def canon_unit(u: str | None) -> str:
    u = (u or "").strip().lower()
    return UNIT_ALIASES.get(u, u)


def canon_gt(value: str) -> tuple[str, Any]:
    v = value.strip()
    m = re.fullmatch(r"(-?[\d.,]+)\s*-\s*([\d.,]+)", v)
    if m and _num(m.group(1)) is not None and _num(m.group(2)) is not None:
        return "range", (_num(m.group(1)), _num(m.group(2)))
    if "|" in v:
        return "set", frozenset(_n(x) for x in v.split("|"))
    if v.lower() in ("true", "false"):
        return "bool", v.lower() == "true"
    if _num(v) is not None:
        return "num", _num(v)
    return "str", _n(v)


def canon_param(p: Parameter) -> tuple[str, Any]:
    v = p.value
    if p.operator.value == "between":
        lo, hi = sorted(float(x) for x in v)
        return "range", (lo, hi)
    if isinstance(v, list):
        return "set", frozenset(_n(x) for x in v)
    if isinstance(v, bool):
        return "bool", v
    if _num(v) is not None:
        return "num", _num(v)
    return "str", _n(v)


def value_matches(gt_value: str, p: Parameter) -> bool:
    gk, gv = canon_gt(gt_value)
    pk, pv = canon_param(p)
    if gk == pk:
        return gv == pv
    if gk == "str" and pk == "set":          # GT "BOL" vs extracted documents in [BOL, packing_list]
        return gv in pv
    if gk == "set" and pk == "str":
        return gv == frozenset([pv])
    if gk == "num" and pk == "str":
        return _num(pv) == gv
    return False


def param_correct(gt: tuple[str, str, str], rules: list[Rule]) -> bool:
    """A ground-truth parameter is correct if some parameter of a matched rule has the same
    value and, when the ground truth states a unit, the same unit."""
    _, value, unit = gt
    for r in rules:
        for p in r.parameters:
            if value_matches(value, p) and (not unit or canon_unit(unit) == canon_unit(p.unit)):
                return True
    return False

# --------------------------------------------------------------------------- #
# Match proposals
# --------------------------------------------------------------------------- #


def _heuristic_score(gt: GTRule, r: Rule) -> float:
    ts = max(text_sim(gt.requirement, r.requirement),
             difflib.SequenceMatcher(None, gt.requirement.lower(), r.requirement.lower()).ratio())
    cat = 1.0 if gt.category == r.category.value else 0.0
    page = 1.0 if any(s.page == gt.page for s in r.sources) else 0.0
    if gt.params:
        hit = sum(any(value_matches(v, p) for p in r.parameters) for _, v, _ in gt.params) / len(gt.params)
        return 0.45 * ts + 0.25 * hit + 0.15 * cat + 0.15 * page
    return 0.65 * ts + 0.2 * cat + 0.15 * page


def propose_heuristic(gt: list[GTRule], rules: list[Rule]) -> list[dict]:
    """Deterministic proposals: each GT rule's best extracted rule, plus each still-unclaimed
    extracted rule's best GT rule (catches a rule the extractor split in two)."""
    rows, claimed = [], set()
    for g in gt:
        scored = sorted(((_heuristic_score(g, r), r) for r in rules), key=lambda t: -t[0])
        if scored and scored[0][0] >= HEURISTIC_THRESHOLD:
            s, r = scored[0]
            claimed.add(r.rule_id)
            rows.append(_row(g, r, "heuristic", f"{s:.2f}", "best text/parameter/page overlap"))
    for r in rules:
        if r.rule_id in claimed:
            continue
        scored = sorted(((_heuristic_score(g, r), g) for g in gt), key=lambda t: -t[0])
        if scored and scored[0][0] >= HEURISTIC_THRESHOLD + 0.1:
            s, g = scored[0]
            rows.append(_row(g, r, "heuristic", f"{s:.2f}", "second extracted rule for this GT rule (possible split)"))
    return rows


def _row(g: GTRule, r: Rule, by: str, confidence: str, reason: str) -> dict:
    return {"gt_rule_id": g.rule_id, "gt_requirement": g.requirement, "extracted_rule_id": r.rule_id,
            "extracted_requirement": r.requirement, "proposed_by": by, "confidence": confidence,
            "reason": reason, "confirmed": "", "notes": ""}


MATCH_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["matches"],
    "properties": {"matches": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["gt_rule_id", "extracted_rule_ids", "confidence", "reason"],
        "properties": {
            "gt_rule_id": {"type": "string"},
            "extracted_rule_ids": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["low", "med", "high"]},
            "reason": {"type": "string"},
        }}}},
}


def propose_llm(gt: list[GTRule], rs: RuleSet, llm, prompt_name: str = "match_v1") -> list[dict]:
    """Ask the model which extracted rule(s) state each ground-truth rule. Proposals only:
    nothing counts until a human confirms the CSV."""
    from coreframe.extract import PROMPTS_DIR
    from coreframe.llm import sha

    system = [{"type": "text", "text": (PROMPTS_DIR / f"{prompt_name}.md").read_text()}]
    payload = {
        "ground_truth": [{"gt_rule_id": g.rule_id, "category": g.category, "requirement": g.requirement,
                          "parameters": [{"name": n, "value": v, "unit": u} for n, v, u in g.params],
                          "page": g.page, "section": g.section} for g in gt],
        "extracted": [{"rule_id": r.rule_id, "category": r.category.value, "requirement": r.requirement,
                       "parameters": [{"target": p.target, "operator": p.operator.value, "value": p.value,
                                       "unit": p.unit} for p in r.parameters],
                       "pages": sorted({s.page for s in r.sources}), "section": r.sources[0].section}
                      for r in rs.rules],
    }
    body = json.dumps(payload, sort_keys=True)
    text = llm.json_call(namespace=f"{rs.guide_sha256[:16]}/{prompt_name}", key=f"match_{sha(body)[:12]}",
                         system=system, messages=[{"role": "user", "content": body}], schema=MATCH_SCHEMA,
                         log_extra={"step": "match"})
    by_id = {r.rule_id: r for r in rs.rules}
    gt_by_id = {g.rule_id: g for g in gt}
    rows = []
    for m in json.loads(text)["matches"]:
        g = gt_by_id.get(m["gt_rule_id"])
        for rid in m["extracted_rule_ids"]:
            if g and rid in by_id:
                rows.append(_row(g, by_id[rid], "llm", m["confidence"], m["reason"]))
    return rows


def merge_matches(path: Path, proposals: list[dict], rs: RuleSet) -> pd.DataFrame:
    """Write proposals without losing human decisions. Existing rows are kept; a row whose
    extracted rule text changed since it was confirmed is un-confirmed and marked STALE."""
    current = {r.rule_id: r.requirement for r in rs.rules}
    rows: dict[tuple[str, str], dict] = {}
    if path.exists():
        for row in pd.read_csv(path, dtype=str, keep_default_na=False).to_dict("records"):
            rid = row["extracted_rule_id"]
            if rid and current.get(rid) != row["extracted_requirement"]:
                if row["confirmed"].strip():
                    row["notes"] = ("STALE: extracted rule changed since this was confirmed. " + row["notes"]).strip()
                    row["confirmed"] = ""
                else:
                    continue  # unconfirmed proposal about a rule that no longer exists: drop
            rows[(row["gt_rule_id"], rid)] = {c: row.get(c, "") for c in MATCH_COLUMNS}
    for p in proposals:
        rows.setdefault((p["gt_rule_id"], p["extracted_rule_id"]), p)
    df = pd.DataFrame(sorted(rows.values(), key=lambda r: (r["gt_rule_id"], r["extracted_rule_id"])),
                      columns=MATCH_COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)
    return df

# --------------------------------------------------------------------------- #
# Extraction metrics
# --------------------------------------------------------------------------- #


def _suggest_tag(notes: str) -> str:
    n = notes.lower()
    if "table" in n:
        return "table parsing"
    if "cross-reference" in n:
        return "cross-reference between sections"
    if "conditional" in n:
        return "conditional rule"
    if "vague" in n or "needs_human" in n:
        return "ambiguous wording"
    return ""


@dataclass
class Counts:
    gt_rules: int = 0
    gt_matched: int = 0
    extracted_rules: int = 0
    extracted_matched: int = 0
    params: int = 0
    params_correct: int = 0
    citations: int = 0
    citations_correct: int = 0

    def add(self, o: "Counts") -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(o, k))

    @staticmethod
    def _ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    def metrics(self) -> dict[str, float | None]:
        return {"recall": self._ratio(self.gt_matched, self.gt_rules),
                "precision": self._ratio(self.extracted_matched, self.extracted_rules),
                "parameter_accuracy": self._ratio(self.params_correct, self.params),
                "citation_accuracy": self._ratio(self.citations_correct, self.citations)}


@dataclass
class ExtractionEval:
    guide: str
    total: Counts
    by_category: dict[str, Counts]
    failures: list[dict]
    unconfirmed: int
    stale: int


def evaluate_extraction(guide: str, gt: list[GTRule], rs: RuleSet, matches: pd.DataFrame) -> ExtractionEval:
    by_id = {r.rule_id: r for r in rs.rules}
    confirmed = matches[matches["confirmed"].str.strip().str.lower().isin(["y", "yes", "true", "1"])]
    pending = matches[matches["confirmed"].str.strip() == ""]
    gt_to_rules: dict[str, list[Rule]] = {}
    matched_extracted: set[str] = set()
    for row in confirmed.to_dict("records"):
        r = by_id.get(row["extracted_rule_id"])
        if r is not None:
            gt_to_rules.setdefault(row["gt_rule_id"], []).append(r)
            matched_extracted.add(r.rule_id)

    total, cats, failures = Counts(), {}, []

    def cat(name: str) -> Counts:
        return cats.setdefault(name, Counts())

    def fail(kind: str, g: GTRule | None, r: Rule | None, detail: str) -> None:
        src = r.sources[0] if r is not None else None
        failures.append({
            "guide": guide, "type": kind, "gt_rule_id": g.rule_id if g else "",
            "extracted_rule_id": r.rule_id if r is not None else "",
            "category": g.category if g else r.category.value, "detail": detail,
            "page": g.page if g else src.page, "section": g.section if g else src.section,
            "chargeback": g.chargeback if g else (r.chargeback.text if r.chargeback else ""),
            "suggested_tag": _suggest_tag(g.notes) if g else "", "failure_tag": "", "notes": ""})

    for g in gt:
        for c in (total, cat(g.category)):
            c.gt_rules += 1
        rules = gt_to_rules.get(g.rule_id, [])
        if not rules:
            fail("missed_rule", g, None, f"not extracted: {g.requirement}")
            continue
        page_ok = any(s.page == g.page for r in rules for s in r.sources)
        for c in (total, cat(g.category)):
            c.gt_matched += 1
            c.citations += 1
            c.citations_correct += page_ok
        if not page_ok:
            got = sorted({s.page for r in rules for s in r.sources})
            fail("wrong_citation", g, rules[0], f"expected page {g.page}, cited {got}")
        for gp in g.params:
            ok = param_correct(gp, rules)
            for c in (total, cat(g.category)):
                c.params += 1
                c.params_correct += ok
            if not ok:
                got = "; ".join(f"{p.target or p.name} {p.operator.value} {p.value} {p.unit or ''}".strip()
                                for r in rules for p in r.parameters) or "no parameters"
                fail("wrong_parameter", g, rules[0], f"expected {gp[0]} = {gp[1]} {gp[2]}".strip() + f"; extracted: {got}")

    for r in rs.rules:
        ok = r.rule_id in matched_extracted
        for c in (total, cat(r.category.value)):
            c.extracted_rules += 1
            c.extracted_matched += ok
        if not ok:
            fail("spurious_rule", None, r, f"extracted but not in ground truth: {r.requirement}")

    stale = int(matches["notes"].str.startswith("STALE").sum())
    return ExtractionEval(guide, total, cats, failures, unconfirmed=len(pending), stale=stale)

# --------------------------------------------------------------------------- #
# Checker eval
# --------------------------------------------------------------------------- #


@dataclass
class CheckerEval:
    guide: str
    mode: str                       # "end_to_end" (labels from reference rules) or "self_consistency"
    seeded: int = 0
    caught: int = 0
    left_for_review: int = 0        # seeded violation came back NEEDS_REVIEW instead of FAIL
    shipments: int = 0
    shipments_with_false_positive: int = 0
    outcomes: int = 0               # rule outcomes excluding NOT_APPLICABLE
    outcomes_review: int = 0
    failures: list[dict] = field(default_factory=list)

    def add(self, o: "CheckerEval") -> None:
        for k in ("seeded", "caught", "left_for_review", "shipments", "shipments_with_false_positive",
                  "outcomes", "outcomes_review"):
            setattr(self, k, getattr(self, k) + getattr(o, k))

    def metrics(self) -> dict[str, float | None]:
        r = Counts._ratio
        return {"catch_rate": r(self.caught, self.seeded),
                "false_positive_rate": r(self.shipments_with_false_positive, self.shipments),
                "needs_review_rate": r(self.outcomes_review, self.outcomes)}


def _on_field(pr, target: str) -> bool:
    return pr.parameter.target == target or pr.parameter.reference == target


def evaluate_checker(guide: str, extracted: RuleSet, reference: RuleSet | None) -> CheckerEval:
    labels_from = reference or extracted
    ev = CheckerEval(guide, "end_to_end" if reference is not None else "self_consistency")
    res = generate(labels_from)
    ref_by_id = {r.rule_id: r for r in labels_from.rules}

    def tally(report) -> None:
        for x in report.results:
            if x.outcome != Outcome.NOT_APPLICABLE:
                ev.outcomes += 1
                ev.outcomes_review += x.outcome == Outcome.NEEDS_REVIEW

    for b in res.bases:
        rep = check(extracted, b)
        tally(rep)
        ev.shipments += 1
        fails = rep.by_outcome(Outcome.FAIL)
        if fails:
            ev.shipments_with_false_positive += 1
            ev.failures.append(_checker_failure(guide, "false_positive", fails[0].rule,
                                                f"compliant shipment {b.shipment_id} failed: {fails[0].rule.requirement}"))
    for m in res.mutants:
        rep = check(extracted, m.shipment)
        tally(rep)
        ev.shipments += 1
        ev.seeded += 1
        on_field = [pr for x in rep.results for pr in x.params if _on_field(pr, m.target)]
        caught = any(pr.outcome == Outcome.FAIL and m.element in pr.failing_elements for pr in on_field)
        ev.caught += caught
        lab = mutant_label(m, labels_from)
        if not caught:
            review = any(pr.outcome == Outcome.NEEDS_REVIEW for pr in on_field)
            ev.left_for_review += review
            rule = ref_by_id[m.rule_id]
            ev.failures.append(_checker_failure(
                guide, "missed_violation", rule,
                f"{m.shipment.shipment_id}: {m.mutation} not flagged" + (" (came back as needs review)" if review else "")))
        unexpected = [x for x in rep.by_outcome(Outcome.FAIL)
                      if not any(_on_field(pr, m.target) for pr in x.params if pr.outcome == Outcome.FAIL)]
        if unexpected:
            ev.shipments_with_false_positive += 1
            ev.failures.append(_checker_failure(
                guide, "false_positive", unexpected[0].rule,
                f"{m.shipment.shipment_id}: unrelated to the seeded change ({lab['target']}): {unexpected[0].rule.requirement}"))
    return ev


def _checker_failure(guide: str, kind: str, rule: Rule, detail: str) -> dict:
    s = rule.sources[0]
    return {"guide": guide, "type": kind, "gt_rule_id": "", "extracted_rule_id": rule.rule_id,
            "category": rule.category.value, "detail": detail, "page": s.page, "section": s.section,
            "chargeback": rule.chargeback.text if rule.chargeback else "", "suggested_tag": "",
            "failure_tag": "", "notes": ""}

# --------------------------------------------------------------------------- #
# Failure-analysis CSV, gate
# --------------------------------------------------------------------------- #


def _failure_key(row: dict) -> tuple:
    # checker misses are keyed by rule + type (one row per rule, with a count), not per shipment
    return (row["guide"], row["type"], row["gt_rule_id"], row["extracted_rule_id"], row["detail"])


def collapse_checker_failures(rows: list[dict]) -> list[dict]:
    """Many seeded shipments can miss on the same rule; keep one row per (type, rule) with a count."""
    out: dict[tuple, dict] = {}
    for r in rows:
        if r["type"] not in ("missed_violation", "false_positive"):
            out[_failure_key(r)] = r
            continue
        key = (r["guide"], r["type"], r["extracted_rule_id"])
        if key in out:
            out[key]["_n"] += 1
        else:
            out[key] = {**r, "_n": 1}
    final = []
    for r in out.values():
        n = r.pop("_n", None)
        if n and n > 1:
            r["detail"] = f"{r['detail']} (+{n - 1} more shipments)"
        final.append(r)
    return final


def write_failures(path: Path, rows: list[dict]) -> pd.DataFrame:
    """Write the failure-analysis CSV, carrying over failure_tag / notes the human already entered."""
    tagged: dict[tuple, tuple[str, str]] = {}
    if path.exists():
        for old in pd.read_csv(path, dtype=str, keep_default_na=False).to_dict("records"):
            if old.get("failure_tag") or old.get("notes"):
                tagged[(old["guide"], old["type"], old["gt_rule_id"], old["extracted_rule_id"])] = (
                    old.get("failure_tag", ""), old.get("notes", ""))
    for r in rows:
        tag, notes = tagged.get((r["guide"], r["type"], r["gt_rule_id"], r["extracted_rule_id"]), ("", ""))
        r["failure_tag"], r["notes"] = tag, notes
    df = pd.DataFrame(rows, columns=FAILURE_COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def gate(metrics: dict[str, float | None], thresholds: dict[str, float]) -> list[str]:
    """Threshold keys are metric names (minimum) or 'max_<metric>' (maximum). A metric that
    could not be computed fails the gate: no data is not a pass."""
    problems = []
    for key, limit in thresholds.items():
        name, is_max = (key[4:], True) if key.startswith("max_") else (key, False)
        value = metrics.get(name)
        if value is None:
            problems.append(f"{name}: not measured (threshold {limit})")
        elif is_max and value > limit:
            problems.append(f"{name}: {value:.3f} above maximum {limit}")
        elif not is_max and value < limit:
            problems.append(f"{name}: {value:.3f} below threshold {limit}")
    return problems


_MONEY = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_SEVERITY = {"missed_rule": 4, "missed_violation": 4, "wrong_parameter": 3, "false_positive": 2,
             "wrong_citation": 1, "spurious_rule": 1}


def worst_misses(failures: list[dict], n: int = 5) -> list[dict]:
    """Rank misses by how much they could cost: missing a rule or a violation outranks a wrong
    value, which outranks a wrong page; ties break on the stated chargeback amount."""
    def money(row: dict) -> float:
        m = _MONEY.search(row.get("chargeback") or "")
        return float(m.group(1).replace(",", "")) if m else 0.0
    return sorted(failures, key=lambda r: (-_SEVERITY.get(r["type"], 0), -money(r)))[:n]
