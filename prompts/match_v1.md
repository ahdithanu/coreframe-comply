You are helping evaluate a rule-extraction system. You get two lists as JSON:

- `ground_truth`: rules a person extracted by hand from a retailer's routing guide.
- `extracted`: rules a model extracted from the same guide.

For every ground-truth rule, list the extracted rule or rules that state the same requirement. Your output is a set of proposals that a person will confirm or correct, so be accurate and say why.

Guidelines:
- Match on meaning: the same obligation on the same thing under the same conditions. Wording will differ.
- A match does not require the extracted values to be right. If the extracted rule is clearly about the same requirement but has a wrong number, unit or page, still propose it: scoring the values is a separate step. Mention the discrepancy in `reason`.
- One ground-truth rule may map to several extracted rules when the extractor split it (for example a label position rule and a separate edge-clearance rule). List all of them.
- Several ground-truth rules may map to the same extracted rule when the extractor merged them (for example one rule requiring both a BOL and a packing list).
- A rule that applies only under a condition (parcel only, one DC only) does not match the unconditional version of the rule, and vice versa.
- If nothing in `extracted` states the requirement, return an empty `extracted_rule_ids` list. Do not force a match.
- `confidence`: high when requirement, conditions and section all line up; med when you had to interpret; low when it is a guess.

Return one entry per ground-truth rule, in the same order.
