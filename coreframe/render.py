"""Jinja2 rendering for human-readable outputs (markdown + self-contained HTML)."""

from __future__ import annotations

import json
from pathlib import Path

import jinja2

from coreframe.schemas import Category, Parameter, Rule, RuleSet, RuleStatus

TEMPLATES = Path(__file__).parent / "templates"

CATEGORY_LABELS = {
    Category.labeling: "Labeling",
    Category.asn_edi: "ASN / EDI",
    Category.carton: "Cartons",
    Category.pallet: "Pallets",
    Category.routing_carrier: "Routing & carriers",
    Category.appointment: "Appointments",
    Category.documentation: "Documentation",
    Category.chargeback_policy: "Chargeback policy",
    Category.other: "Other",
}
OP_SYMBOLS = {"eq": "=", "lte": "≤", "gte": "≥", "between": "between", "in": "in",
              "before": "before", "exists": "exists"}


def fmt_value(v) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, list):
        return ", ".join(fmt_value(x) for x in v)
    return str(v)


def fmt_param(p: Parameter) -> str:
    field = p.target or f"({p.name}: no shipment field)"
    unit = f" {p.unit}" if p.unit else ""
    if p.operator.value == "between":
        lo, hi = p.value
        return f"{field} between {fmt_value(lo)} and {fmt_value(hi)}{unit}"
    if p.operator.value == "before":
        return f"{field} at least {fmt_value(p.value)}{unit} before {p.reference}"
    if p.operator.value == "exists":
        return f"{field} {'must be present' if p.value else 'must be absent'}"
    return f"{field} {OP_SYMBOLS[p.operator.value]} {fmt_value(p.value)}{unit}"


def fmt_applies(r: Rule) -> str:
    if r.applies_to == "all" or not r.applies_to:
        return "all shipments"
    return "; ".join(f"{c.field} {'=' if c.operator == 'eq' else c.operator.replace('_', ' ')} {fmt_value(c.value)}"
                     for c in r.applies_to)


def _env() -> jinja2.Environment:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=jinja2.select_autoescape(["html"]),
                             trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    env.filters.update(param=fmt_param, applies=fmt_applies, value=fmt_value)
    return env


def checklist_context(rs: RuleSet, run_summary: dict | None = None) -> dict:
    groups = []
    for cat, label in CATEGORY_LABELS.items():
        rules = [r for r in rs.rules if r.category == cat]
        if rules:
            groups.append({"label": label, "rules": rules})
    sources = [s for r in rs.rules for s in r.sources]
    return {
        "rs": rs, "groups": groups, "run": run_summary,
        "stats": {
            "total": len(rs.rules),
            "parameterized": sum(r.status == RuleStatus.parameterized for r in rs.rules),
            "needs_human": sum(r.status == RuleStatus.needs_human for r in rs.rules),
            "with_chargeback": sum(r.chargeback is not None for r in rs.rules),
            "citations": len(sources),
            "verified": sum(s.verified is True for s in sources),
            "unverified": sum(s.verified is False for s in sources),
            "image_only": sum(s.verified is None for s in sources),
        },
    }


def write_checklist(rs: RuleSet, out_base: Path, run_summary: dict | None = None) -> tuple[Path, Path]:
    ctx = checklist_context(rs, run_summary)
    env = _env()
    md, html = out_base.with_suffix(".md"), out_base.with_suffix(".html")
    md.write_text(env.get_template("checklist.md.j2").render(**ctx))
    html.write_text(env.get_template("checklist.html.j2").render(**ctx))
    return md, html


def write_ruleset(rs: RuleSet, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rs.model_dump(mode="json"), indent=2) + "\n")


def write_check_report(report, shipment, out_base: Path) -> tuple[Path, Path]:
    from coreframe.engine import Outcome

    ctx = {
        "r": report, "s": shipment,
        "fails": report.by_outcome(Outcome.FAIL),
        "review": report.by_outcome(Outcome.NEEDS_REVIEW),
        "passed": report.by_outcome(Outcome.PASS),
        "na": report.by_outcome(Outcome.NOT_APPLICABLE),
        "unpriced": sum(x.exposure_usd is None for x in report.by_outcome(Outcome.FAIL)),
    }
    env = _env()
    out_base.parent.mkdir(parents=True, exist_ok=True)
    md, html = (out_base.parent / f"{out_base.name}.md"), (out_base.parent / f"{out_base.name}.html")
    md.write_text(env.get_template("check.md.j2").render(**ctx))
    html.write_text(env.get_template("check.html.j2").render(**ctx))
    return md, html
