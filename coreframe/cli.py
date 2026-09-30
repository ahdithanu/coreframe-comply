"""Command-line entry point: python -m coreframe <command> ..."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from coreframe.chunking import chunk_guide
from coreframe.ingest import parse_guide


def data_root(guide: Path) -> Path:
    """data/guides/<r>/<v>.pdf -> data ; data/sample/guides/<r>/<v>.pdf -> data/sample."""
    for parent in guide.resolve().parents:
        if parent.name == "guides":
            return parent.parent.relative_to(Path.cwd()) if parent.parent.is_relative_to(Path.cwd()) else parent.parent
    return Path("data")


def cmd_ingest(args: argparse.Namespace) -> int:
    root = data_root(args.guide)
    guide = parse_guide(args.guide, image_dir=root / "cache/pages")
    chunks = chunk_guide(guide)
    out = args.out or root / "chunks" / f"{guide.retailer}_{guide.guide_version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"guide": guide.model_dump(exclude={"pages"}),
                               "chunks": [c.model_dump() for c in chunks]}, indent=2))

    print(f"{guide.retailer} {guide.guide_version}  sha256={guide.sha256[:12]}  "
          f"pages={guide.page_count}  body_font={guide.body_font_size}pt")
    print(f"stripped running lines: {guide.stripped_lines}")
    for p in guide.pages:
        kinds = [b.kind for b in p.blocks]
        flag = f"  IMAGE ({', '.join(p.image_reasons)})" if p.image_reasons else ""
        print(f"  p{p.number}: {kinds.count('heading')} headings, {kinds.count('text')} paragraphs, "
              f"{kinds.count('table')} tables, {p.char_count} chars{flag}")
    print(f"\n{len(chunks)} chunks -> {out}")
    for c in chunks:
        extras = []
        if c.tables:
            extras.append(f"{len(c.tables)} table")
        if c.images:
            extras.append("image p" + ",".join(str(i.page) for i in c.images))
        print(f"  {c.chunk_id}  pp.{'-'.join(map(str, [c.pages[0], c.pages[-1]])) if len(c.pages) > 1 else c.pages[0]:<5} "
              f"{' > '.join(c.section_path):<60} {len(c.text):>5} chars  {'; '.join(extras)}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    from coreframe.extract import DEFAULT_PROMPT, extract_guide
    from coreframe.llm import LLM, RunLog
    from coreframe.render import write_checklist, write_ruleset

    root = data_root(args.guide)
    guide = parse_guide(args.guide, image_dir=root / "cache/pages")
    chunks = chunk_guide(guide)
    name = f"{guide.retailer}_{guide.guide_version}"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = RunLog(root / "runs" / f"{run_id}_extract_{name}.jsonl")
    llm = LLM(model=args.model, cache_dir=root / "cache/llm", run_log=log, effort=args.effort, offline=args.offline)

    report = extract_guide(guide, chunks, llm, prompt_name=args.prompt)
    rs = report.ruleset
    summary = log.summary()

    out_json = root / "rules" / f"{name}.json"
    write_ruleset(rs, out_json)
    md, html = write_checklist(rs, root / "rules" / f"{name}.checklist", summary)
    rejected = [{"chunk_id": r.chunk_id, **bad} for r in report.chunk_results for bad in r.rejected]
    rej_path = root / "rules" / f"{name}.rejected.json"
    if rejected:
        rej_path.write_text(json.dumps(rejected, indent=2))
    elif rej_path.exists():
        rej_path.unlink()
    (log.path.with_suffix(".summary.json")).write_text(json.dumps({
        "run_id": run_id, "guide": str(args.guide), "model": args.model, "effort": args.effort,
        "prompt": args.prompt, "rules": len(rs.rules), **summary}, indent=2))

    print(f"{name}: {len(chunks)} sections -> {len(rs.rules)} rules  (model {args.model}, prompt {args.prompt})")
    for res, c in zip(report.chunk_results, chunks):
        notes = []
        if res.attempts > 1:
            notes.append(f"{res.attempts} attempts")
        if res.demoted:
            notes.append(f"{res.demoted} demoted")
        if res.rejected:
            notes.append(f"{len(res.rejected)} REJECTED")
        print(f"  {c.chunk_id} {c.section[:48]:<48} {len(res.rules):>3} rules  {', '.join(notes)}")
    param = sum(r.status.value == "parameterized" for r in rs.rules)
    srcs = [s for r in rs.rules for s in r.sources]
    print(f"\nparameterized {param}, needs_human {len(rs.rules) - param}; "
          f"merged {len(report.merges)} duplicates; rejected {len(rejected)}")
    print(f"citations: {sum(s.verified is True for s in srcs)} verified, {sum(s.verified is False for s in srcs)} "
          f"not found on page, {sum(s.verified is None for s in srcs)} on image-only pages")
    cost = f"${summary['cost_usd']:.4f}" + ("" if summary["cost_complete"] else " (unpriced model calls excluded)")
    print(f"calls {summary['calls']} ({summary['cache_hits']} cached)  tokens in {summary['input_tokens']:,} "
          f"+ cache write {summary['cache_write_tokens']:,} + cache read {summary['cache_read_tokens']:,}, "
          f"out {summary['output_tokens']:,}  cost {cost}  latency {summary['latency_s']}s")
    print(f"\nwrote {out_json}\n      {md}\n      {html}" + (f"\n      {rej_path}" if rejected else ""))
    return 0


def _rules_root(rules: Path) -> Path:
    """data/sample/rules/x.json -> data/sample."""
    return rules.parent.parent


def cmd_check(args: argparse.Namespace) -> int:
    from coreframe.engine import Outcome, check
    from coreframe.render import write_check_report
    from coreframe.schemas import RuleSet, Shipment

    rs = RuleSet.model_validate_json(args.rules.read_text())
    ship = Shipment.model_validate_json(args.shipment.read_text())
    if ship.retailer.lower() != rs.retailer.lower():
        print(f"error: shipment is for {ship.retailer!r} but rules are for {rs.retailer!r}")
        return 2
    report = check(rs, ship)
    out = args.out or _rules_root(args.rules) / "reports" / f"{ship.shipment_id}.check"
    md, html = write_check_report(report, ship, out)

    fails = report.by_outcome(Outcome.FAIL)
    print(f"{ship.shipment_id}: {len(fails)} FAIL, {len(report.by_outcome(Outcome.NEEDS_REVIEW))} NEEDS_REVIEW, "
          f"{len(report.by_outcome(Outcome.PASS))} PASS, {len(report.by_outcome(Outcome.NOT_APPLICABLE))} N/A"
          + (f"  exposure ${report.known_exposure_usd:,.2f}" if fails else ""))
    for x in fails:
        for pr in sorted(x.params, key=lambda pr: pr.outcome != Outcome.FAIL):
            src = x.rule.sources[0]
            if pr.outcome == Outcome.FAIL:
                money = f"${x.exposure_usd:,.2f}" if x.exposure_usd is not None else (
                    x.rule.chargeback.text if x.rule.chargeback else "no stated fee")
                shared = " *" if x.fee_shared_with else ""
                print(f"  FAIL {money:>22}{shared:2} {pr.expected}  | actual {pr.actual}  [p.{src.page} {src.section}]")
            elif pr.outcome == Outcome.NEEDS_REVIEW:
                print(f"  ???  {'(same rule)':>22}   {pr.expected}  | {pr.actual} ({pr.detail})")
    if any(x.fee_shared_with for x in fails):
        print("  * shares a chargeback line with another failing rule; the total bills each line once")
    print(f"wrote {md}\n      {html}")
    return 1 if fails else 0


def cmd_synth(args: argparse.Namespace) -> int:
    from coreframe.schemas import RuleSet
    from coreframe.synth import generate, mutant_label

    rs = RuleSet.model_validate_json(args.rules.read_text())
    res = generate(rs)
    out = args.out or _rules_root(args.rules) / "synthetic" / args.rules.stem
    ship_dir = out / "shipments"
    ship_dir.mkdir(parents=True, exist_ok=True)
    for old in ship_dir.glob("*.json"):
        old.unlink()
    labels = [{"shipment_id": b.shipment_id, "base_id": b.shipment_id, "seeded_rule_id": None}
              for b in res.bases]
    for b in res.bases:
        (ship_dir / f"{b.shipment_id}.json").write_text(b.model_dump_json(indent=2) + "\n")
    for m in res.mutants:
        (ship_dir / f"{m.shipment.shipment_id}.json").write_text(m.shipment.model_dump_json(indent=2) + "\n")
        labels.append(mutant_label(m, rs))
    (out / "labels.json").write_text(json.dumps({"rules": str(args.rules), "retailer": rs.retailer,
                                                  "guide_version": rs.guide_version, "labels": labels}, indent=2) + "\n")
    print(f"{len(res.bases)} compliant base shipments, {len(res.mutants)} seeded violations -> {out}")
    for sid, rules in res.unrepairable:
        print(f"  could not make {sid} compliant; still failing {rules}")
    for rid, pname, why in res.skipped:
        print(f"  skipped {rid} / {pname}: {why}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from coreframe.diff import diff
    from coreframe.render import write_diff_report
    from coreframe.schemas import RuleSet

    old = RuleSet.model_validate_json(args.old.read_text())
    new = RuleSet.model_validate_json(args.new.read_text())
    d = diff(old, new)
    out = args.out or _rules_root(args.new) / "reports" / f"diff_{new.retailer}_{old.guide_version}_{new.guide_version}"
    md, html = write_diff_report(d, out)
    print(f"{new.retailer} {old.guide_version} -> {new.guide_version}: {len(d.of('changed'))} changed, "
          f"{len(d.of('added'))} added, {len(d.of('removed'))} removed, {len(d.of('unchanged'))} unchanged")
    for e in d.of("changed"):
        print(f"  CHANGED  {e.new.requirement}")
        for c in e.changes:
            print(f"           {c.field}: {c.old}  ->  {c.new}" + (f"  [{c.direction}]" if c.direction else ""))
    for e in d.of("added"):
        s = e.new.sources[0]
        print(f"  ADDED    {e.new.requirement}  [p.{s.page} {s.section}]")
    for e in d.of("removed"):
        s = e.old.sources[0]
        print(f"  REMOVED  {e.old.requirement}  [was p.{s.page} {s.section}]")
    for e in d.of("unchanged"):
        if e.notes:
            print(f"  same     {e.new.requirement}  ({'; '.join(e.notes)})")
    print(f"wrote {md}\n      {html}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coreframe")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="parse a guide PDF into section chunks (debug view of phase 1)")
    p.add_argument("--guide", type=Path, required=True)
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_ingest)

    from coreframe.extract import DEFAULT_PROMPT
    from coreframe.llm import DEFAULT_MODEL

    p = sub.add_parser("extract", help="extract cited rules from a routing guide PDF")
    p.add_argument("--guide", type=Path, required=True, help="data/guides/<retailer>/<version>.pdf")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--prompt", default=DEFAULT_PROMPT, help="prompt file name under prompts/ (versioned)")
    p.add_argument("--offline", action="store_true", help="use cached responses only; fail on a cache miss")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("check", help="check a shipment against extracted rules (exit 1 if any FAIL)")
    p.add_argument("--rules", type=Path, required=True)
    p.add_argument("--shipment", type=Path, required=True)
    p.add_argument("--out", type=Path, help="report path without extension")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("synth", help="generate compliant shipments plus seeded violations with labels")
    p.add_argument("--rules", type=Path, required=True)
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_synth)

    p = sub.add_parser("diff", help="compare rule sets from two versions of the same retailer's guide")
    p.add_argument("--old", type=Path, required=True)
    p.add_argument("--new", type=Path, required=True)
    p.add_argument("--out", type=Path, help="report path without extension")
    p.set_defaults(func=cmd_diff)

    args = parser.parse_args(argv)
    return args.func(args)
