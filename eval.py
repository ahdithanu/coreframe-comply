"""Eval + release gate.

    python eval.py                     # evaluate every guide in eval_config.yaml, write eval_report.md
    python eval.py --propose           # (re)propose rule matches for a person to confirm
    python eval.py --reextract --offline   # rebuild rules from cached model responses first (what CI does)

Exit code 0 only if every threshold in eval_config.yaml is met.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import jinja2
import pandas as pd
import yaml

from coreframe import evaluate as ev
from coreframe.schemas import RuleSet

ROOT = Path(__file__).resolve().parent
LABELS = {"recall": "Rule recall", "precision": "Rule precision", "parameter_accuracy": "Parameter accuracy",
          "citation_accuracy": "Citation accuracy (page)", "catch_rate": "Violation catch rate",
          "false_positive_rate": "False-positive rate", "needs_review_rate": "Needs-review rate"}


def pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def reextract(guide: dict, llm_args: dict) -> None:
    from coreframe.chunking import chunk_guide
    from coreframe.cli import data_root
    from coreframe.extract import extract_guide
    from coreframe.ingest import parse_guide
    from coreframe.llm import LLM, RunLog
    from coreframe.render import write_ruleset

    pdf = Path(guide["pdf"])
    root = data_root(pdf)
    parsed = parse_guide(pdf, image_dir=root / "cache/pages")
    log = RunLog(root / "runs" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_eval_{guide['name']}.jsonl")
    llm = LLM(cache_dir=root / "cache/llm", run_log=log, **llm_args)
    write_ruleset(extract_guide(parsed, chunk_guide(parsed), llm).ruleset, Path(guide["rules"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=ROOT / "eval_config.yaml")
    ap.add_argument("--report", type=Path, default=ROOT / "eval_report.md")
    ap.add_argument("--propose", action="store_true", help="propose matches even if a matches CSV exists")
    ap.add_argument("--matcher", choices=["llm", "heuristic"], help="override the config's matcher")
    ap.add_argument("--reextract", action="store_true", help="re-run extraction (from cache) before evaluating")
    ap.add_argument("--offline", action="store_true", help="never call the API; fail on a cache miss")
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text())
    matcher = args.matcher or cfg.get("matcher", "llm")
    from coreframe.llm import DEFAULT_MODEL
    llm_args = {"model": args.model or DEFAULT_MODEL, "offline": args.offline}

    total, checker_total = ev.Counts(), ev.CheckerEval("all", "mixed")
    categories: dict[str, ev.Counts] = {}
    guides, skipped, all_failures, failure_files = [], [], [], []
    unconfirmed = stale = 0

    for g in cfg["guides"]:
        name, rules_path, gt_path = g["name"], Path(g["rules"]), Path(g["ground_truth"])
        if not gt_path.exists():
            skipped.append(f"{name} (no ground truth at {gt_path})")
            continue
        if args.reextract and g.get("pdf") and Path(g["pdf"]).exists():
            reextract(g, llm_args)
        if not rules_path.exists():
            skipped.append(f"{name} (no extracted rules at {rules_path}; run `python -m coreframe extract`)")
            continue
        rs = RuleSet.model_validate_json(rules_path.read_text())
        gt = ev.load_ground_truth(gt_path)
        eval_dir = rules_path.parent.parent / "eval"
        matches_path = Path(g.get("matches") or eval_dir / f"{name}.matches.csv")
        failures_path = Path(g.get("failures") or eval_dir / f"{name}.failures.csv")

        proposals = []
        if args.propose or not matches_path.exists():
            if matcher == "llm":
                from coreframe.llm import LLM, RunLog
                log = RunLog(eval_dir.parent / "runs" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_match_{name}.jsonl")
                proposals = ev.propose_llm(gt, rs, LLM(cache_dir=eval_dir.parent / "cache/llm", run_log=log, **llm_args))
            else:
                proposals = ev.propose_heuristic(gt, rs.rules)
        matches = ev.merge_matches(matches_path, proposals, rs)

        ex = ev.evaluate_extraction(name, gt, rs, matches)
        ref_path = Path(g["reference_rules"]) if g.get("reference_rules") else None
        reference = RuleSet.model_validate_json(ref_path.read_text()) if ref_path and ref_path.exists() else None
        ck = ev.evaluate_checker(name, rs, reference)

        rows = ev.collapse_checker_failures(ex.failures + ck.failures)
        df = ev.write_failures(failures_path, rows)
        all_failures += df.to_dict("records")
        failure_files.append(str(failures_path))

        total.add(ex.total)
        checker_total.add(ck)
        for cat, c in ex.by_category.items():
            categories.setdefault(cat, ev.Counts()).add(c)
        unconfirmed += ex.unconfirmed
        stale += ex.stale
        guides.append({"name": name, "model": rs.model, "prompt": rs.prompt_version, "c": ex.total,
                       "m": {**ex.total.metrics(), **ck.metrics()}, "mode": ck.mode,
                       "matches": str(matches_path)})
        print(f"{name}: " + "  ".join(f"{k}={pct(v)}" for k, v in {**ex.total.metrics(), **ck.metrics()}.items())
              + (f"  [{ex.unconfirmed} matches unconfirmed]" if ex.unconfirmed else ""))

    metrics = {**total.metrics(), **checker_total.metrics()} if guides else {}
    thresholds = cfg.get("thresholds", {})
    problems = ev.gate(metrics, thresholds)
    if cfg.get("require_confirmed_matches", True) and unconfirmed:
        problems.append(f"{unconfirmed} proposed rule matches are unconfirmed"
                        + (f" ({stale} went stale because the extraction changed)" if stale else "")
                        + ". Review the matches CSV and set confirmed to y or n.")

    metric_rows = []
    for key, label in LABELS.items():
        limit = thresholds.get(key, thresholds.get(f"max_{key}"))
        is_max = f"max_{key}" in thresholds
        value = metrics.get(key)
        if limit is None:
            status = ""
        elif value is None:
            status = "not measured"
        else:
            status = "pass" if (value <= limit if is_max else value >= limit) else "**FAIL**"
        metric_rows.append({"label": label, "value": pct(value),
                            "threshold": "" if limit is None else f"{'≤' if is_max else '≥'} {pct(limit)}",
                            "status": status})

    by_type = sorted(Counter(f["type"] for f in all_failures).items(), key=lambda t: -t[1])
    tags = Counter((f.get("failure_tag") or "").strip() or "(untagged)" for f in all_failures)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT / "coreframe/templates"),
                             trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    env.filters["pct"] = pct
    provisional = None
    if unconfirmed:
        provisional = (f"{unconfirmed} proposed matches have not been confirmed by a person, so they are not counted. "
                       "Recall and precision below are lower bounds until the matches CSV is reviewed.")
    args.report.write_text(env.get_template("eval_report.md.j2").render(
        problems=problems, provisional=provisional, skipped=skipped, metric_rows=metric_rows, matcher=matcher,
        guides=guides, categories=sorted(categories.items()), by_type=by_type,
        by_tag=sorted(tags.items(), key=lambda t: -t[1]), failure_files=failure_files, tags=ev.FAILURE_TAGS,
        worst=ev.worst_misses(all_failures)))

    print(f"\nGate: {'PASS' if not problems else 'FAIL'}")
    for p in problems:
        print(f"  - {p}")
    for s in skipped:
        print(f"  skipped: {s}")
    print(f"wrote {args.report}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
