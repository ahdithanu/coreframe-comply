# Eval report

**Gate: FAIL**
- recall: not measured (threshold 0.85)
- precision: not measured (threshold 0.9)
- parameter_accuracy: not measured (threshold 0.9)
- catch_rate: not measured (threshold 0.95)

> Not evaluated: northwind_v2025.1 (no extracted rules at data/sample/rules/northwind_v2025.1.json; run `python -m coreframe extract`); northwind_v2025.2 (no extracted rules at data/sample/rules/northwind_v2025.2.json; run `python -m coreframe extract`).

## Metrics

| Metric | Value | Threshold | |
|---|---|---|---|
| Rule recall | n/a | ≥ 85.0% | not measured |
| Rule precision | n/a | ≥ 90.0% | not measured |
| Parameter accuracy | n/a | ≥ 90.0% | not measured |
| Citation accuracy (page) | n/a |  |  |
| Violation catch rate | n/a | ≥ 95.0% | not measured |
| False-positive rate | n/a |  |  |
| Needs-review rate | n/a |  |  |

Rule matches are proposed by `llm` and count only once a person confirms them in the matches CSV. Parameter accuracy requires both value and unit to be right. Citation accuracy requires the cited page to be right.
