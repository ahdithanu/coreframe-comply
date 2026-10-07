"""Eval: matching CSV workflow, metrics, failure analysis, checker eval, gate."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from coreframe import evaluate as ev
from coreframe.extract import assign_ids
from coreframe.schemas import Parameter, Rule, RuleSet

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data/sample"


def reference(version="v2025.1") -> RuleSet:
    return RuleSet.model_validate_json((SAMPLE / f"rules/northwind_{version}.reference.json").read_text())


def ground_truth(version="v2025.1"):
    return ev.load_ground_truth(SAMPLE / f"ground_truth/northwind_{version}.csv")


def reviewed(gt, rs, reject=()) -> pd.DataFrame:
    """Heuristic proposals as a careful reviewer would leave them: wrong ones rejected, and the
    second half of the appointment rule (which the extractor split) added by hand."""
    df = pd.DataFrame(ev.propose_heuristic(gt, rs.rules), columns=ev.MATCH_COLUMNS)
    df["confirmed"] = "y"
    df.loc[df.gt_rule_id.isin(reject), "confirmed"] = "n"
    appt = next(r for r in rs.rules if "require a scheduled appointment" in r.requirement)
    g = next(g for g in gt if g.rule_id == "NW-028")
    df.loc[len(df)] = [g.rule_id, g.requirement, appt.rule_id, appt.requirement, "human", "", "split", "y", ""]
    return df


def simulated() -> RuleSet:
    """Reference rules with known errors: 2 rules dropped, 1 wrong value, 1 wrong page, 1 invented rule."""
    rs = reference()
    rules = [r for r in rs.rules if "banded" not in r.requirement and "Monday through Friday" not in r.requirement]
    for r in rules:
        if "between 1 and 50 lbs" in r.requirement:
            r.parameters[0].value = [1, 55]
        if "72 inches" in r.requirement:
            r.sources[0].page = 4
    rules.append(Rule(rule_id="x", retailer="northwind", guide_version="v2025.1", category="pallet",
                      requirement="Pallets must be painted blue.", parameters=[],
                      sources=[{"page": 3, "section": "5 Pallet Requirements", "snippet": "GMA pallets"}],
                      confidence="low", status="needs_human", review_note="invented"))
    assign_ids(rules, "northwind", "v2025.1")
    rs.rules = rules
    return rs


@pytest.mark.parametrize("version", ["v2025.1", "v2025.2"])
def test_reference_rules_score_perfectly_against_ground_truth(version):
    """Three-way consistency: ground truth CSV, reference rule set and scorer agree."""
    rs, gt = reference(version), ground_truth(version)
    ex = ev.evaluate_extraction(version, gt, rs, reviewed(gt, rs))
    assert ex.total.metrics() == {"recall": 1.0, "precision": 1.0, "parameter_accuracy": 1.0, "citation_accuracy": 1.0}
    assert ex.failures == [] and ex.unconfirmed == 0
    ck = ev.evaluate_checker(version, rs, rs)
    assert ck.metrics()["catch_rate"] == 1.0 and ck.metrics()["false_positive_rate"] == 0.0


def test_unconfirmed_proposals_do_not_count():
    rs, gt = reference(), ground_truth()
    df = pd.DataFrame(ev.propose_heuristic(gt, rs.rules), columns=ev.MATCH_COLUMNS)   # nobody has reviewed
    ex = ev.evaluate_extraction("g", gt, rs, df)
    assert ex.total.metrics()["recall"] == 0.0 and ex.unconfirmed == len(df)


def test_planted_errors_are_measured():
    rs, gt = simulated(), ground_truth()
    ex = ev.evaluate_extraction("g", gt, rs, reviewed(gt, rs, reject=["NW-013"]))
    c = ex.total
    assert (c.gt_rules, c.gt_matched) == (33, 31)                    # 2 dropped
    assert (c.extracted_rules, c.extracted_matched) == (33, 32)      # 1 invented
    assert c.params - c.params_correct == 1                          # weight 1-55 instead of 1-50
    assert c.citations - c.citations_correct == 1                    # wrong page
    kinds = sorted((f["type"], f["gt_rule_id"]) for f in ex.failures)
    assert kinds == [("missed_rule", "NW-013"), ("missed_rule", "NW-023"), ("spurious_rule", ""),
                     ("wrong_citation", "NW-019"), ("wrong_parameter", "NW-010")]
    by_type = {f["type"]: f for f in ex.failures}
    assert by_type["wrong_parameter"]["suggested_tag"] == "table parsing"       # from the GT notes
    assert "expected page 3, cited [4]" in by_type["wrong_citation"]["detail"]
    assert ex.by_category["carton"].metrics()["recall"] == pytest.approx(6 / 7, abs=1e-3)


def test_checker_eval_is_end_to_end_with_reference_labels():
    rs = simulated()
    ck = ev.evaluate_checker("g", rs, reference())
    assert ck.mode == "end_to_end" and ck.seeded == 180
    assert ck.caught == 174                       # 55 lb cartons pass the wrong 1-55 limit on 6 base shipments
    rows = ev.collapse_checker_failures(ck.failures)
    assert [r["type"] for r in rows] == ["missed_violation"] and "(+5 more shipments)" in rows[0]["detail"]
    assert ev.evaluate_checker("g", rs, None).mode == "self_consistency"


@pytest.mark.parametrize("gt_value, param, ok", [
    ("1-50", dict(operator="between", value=[1, 50]), True),
    ("1-50", dict(operator="between", value=[1, 55]), False),
    ("35", dict(operator="lte", value=35.0), True),
    ("2000", dict(operator="lte", value=2000), True),
    ("UPS|FedEx Ground", dict(operator="in", value=["FedEx Ground", "UPS"]), True),
    ("UPS|FedEx Ground", dict(operator="in", value=["UPS"]), False),
    ("BOL", dict(operator="in", value=["BOL", "packing_list"]), True),     # extractor merged two rules
    ("true", dict(operator="exists", value=True), True),
    ("false", dict(operator="eq", value=False), True),
    ("GS1-128", dict(operator="eq", value="gs1 128"), True),
    ("40x48", dict(operator="eq", value="40x48"), True),
])
def test_value_matching(gt_value, param, ok):
    assert ev.value_matches(gt_value, Parameter(name="p", target=None, **param)) is ok


def test_unit_must_match_when_ground_truth_states_one():
    r = reference().rules
    weight = next(x for x in r if "between 1 and 50 lbs" in x.requirement)
    assert ev.param_correct(("w", "1-50", "lb"), [weight])           # lb == lbs
    assert not ev.param_correct(("w", "1-50", "kg"), [weight])


def test_confirmations_survive_reruns_and_go_stale_when_the_rule_changes(tmp_path):
    rs, gt = reference(), ground_truth()
    path = tmp_path / "m.csv"
    df = ev.merge_matches(path, ev.propose_heuristic(gt, rs.rules), rs)
    df["confirmed"] = "y"
    df.to_csv(path, index=False)

    again = ev.merge_matches(path, ev.propose_heuristic(gt, rs.rules), rs)        # re-proposing keeps decisions
    assert (again.confirmed == "y").all() and len(again) == len(df)

    changed = rs.model_copy(deep=True)
    victim = changed.rules[0]
    victim.requirement = "A different rule now has this id."
    stale = ev.merge_matches(path, [], changed)
    row = stale[stale.extracted_rule_id == victim.rule_id].iloc[0]
    assert row.confirmed == "" and row.notes.startswith("STALE")


def test_failure_tags_survive_reruns(tmp_path):
    rs, gt = simulated(), ground_truth()
    ex = ev.evaluate_extraction("g", gt, rs, reviewed(gt, rs, reject=["NW-013"]))
    path = tmp_path / "f.csv"
    df = ev.write_failures(path, ex.failures)
    df.loc[df.gt_rule_id == "NW-010", "failure_tag"] = "table parsing"
    df.to_csv(path, index=False)
    again = ev.write_failures(path, ev.evaluate_extraction("g", gt, rs, reviewed(gt, rs, reject=["NW-013"])).failures)
    assert again[again.gt_rule_id == "NW-010"].failure_tag.iloc[0] == "table parsing"
    assert list(again.columns) == ev.FAILURE_COLUMNS


def test_gate():
    t = {"recall": 0.85, "max_false_positive_rate": 0.02}
    assert ev.gate({"recall": 0.9, "false_positive_rate": 0.0}, t) == []
    assert "below threshold" in ev.gate({"recall": 0.8, "false_positive_rate": 0.0}, t)[0]
    assert "above maximum" in ev.gate({"recall": 0.9, "false_positive_rate": 0.1}, t)[0]
    assert "not measured" in ev.gate({}, {"recall": 0.85})[0]             # no data is not a pass


def test_worst_misses_ranks_missed_rules_and_money_first():
    rows = [{"type": "wrong_citation", "chargeback": "$500 per shipment"},
            {"type": "missed_rule", "chargeback": "$3.00 per carton"},
            {"type": "missed_rule", "chargeback": "$250 per shipment"},
            {"type": "wrong_parameter", "chargeback": ""}]
    assert [r["chargeback"] for r in ev.worst_misses(rows, 3)] == ["$250 per shipment", "$3.00 per carton", ""]


def test_eval_script_end_to_end(tmp_path, monkeypatch):
    """eval.py: fails while matches are unconfirmed, then gates on the thresholds."""
    import eval as eval_script

    rules = tmp_path / "rules/northwind_v2025.1.json"
    rules.parent.mkdir()
    rules.write_text(simulated().model_dump_json())
    cfg = {"thresholds": {"recall": 0.85, "precision": 0.90, "parameter_accuracy": 0.90, "catch_rate": 0.95},
           "require_confirmed_matches": True, "matcher": "heuristic",
           "guides": [{"name": "northwind_v2025.1", "rules": str(rules),
                       "ground_truth": str(SAMPLE / "ground_truth/northwind_v2025.1.csv"),
                       "reference_rules": str(SAMPLE / "rules/northwind_v2025.1.reference.json")},
                      {"name": "absent", "rules": "nope.json", "ground_truth": "nope.csv"}]}
    (tmp_path / "cfg.yaml").write_text(yaml.safe_dump(cfg))
    argv = ["--config", str(tmp_path / "cfg.yaml"), "--report", str(tmp_path / "report.md")]

    assert eval_script.main(argv) == 1
    report = (tmp_path / "report.md").read_text()
    assert "Gate: FAIL" in report and "have not been confirmed" in report and "absent (no ground truth" in report

    m = tmp_path / "eval/northwind_v2025.1.matches.csv"
    df = pd.read_csv(m, dtype=str, keep_default_na=False)
    df["confirmed"] = df.gt_rule_id.map(lambda g: "n" if g == "NW-013" else "y")
    df.to_csv(m, index=False)
    assert eval_script.main(argv) == 0                               # 93.9 / 93.9 / 96.6 / 96.7: all above threshold
    report = (tmp_path / "report.md").read_text()
    assert "Gate: PASS" in report and "## Worst misses" in report and "missed_violation" in report

    cfg["thresholds"]["catch_rate"] = 0.99
    (tmp_path / "cfg.yaml").write_text(yaml.safe_dump(cfg))
    assert eval_script.main(argv) == 1
