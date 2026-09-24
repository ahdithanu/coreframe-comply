"""Section chunks -> validated, cited, deduplicated Rules.

Per chunk:
  1. send the section (plus page images for scans / heavy tables) with a system
     prompt that is identical for every chunk of a guide, so it's prompt-cached
  2. force JSON output against the RuleOut schema (structured outputs)
  3. validate each rule against the strict Rule model plus chunk-level checks
     (cited page must be in the section); on failure, send the errors back and
     retry, up to MAX_ATTEMPTS
Then, deterministically:
  4. demote 'parameterized' rules with unmapped parameters to needs_human
  5. verify each snippet appears on its cited page (SourceRef.verified)
  6. merge duplicate rules across sections, keeping every source
"""

from __future__ import annotations

import base64
import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Union

import jinja2
from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from coreframe.chunking import Chunk
from coreframe.ingest import ParsedGuide
from coreframe.llm import LLM, sha
from coreframe.schemas import (
    CONDITION_FIELDS, TARGETS, Category, Confidence, Rule, RuleSet, RuleStatus,
)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
DEFAULT_PROMPT = "extract_v1"
MAX_ATTEMPTS = 3
DEDUP_TEXT_RATIO = 0.85
CHARGEBACK_HEADER_RE = re.compile(r"charge|fee|penalt|fine|deduct", re.IGNORECASE)

# --------------------------------------------------------------------------- #
# Model-facing output schema: shape only. Every field is required (nullable where
# optional) so structured outputs can enforce it; semantics are checked by Rule.
# --------------------------------------------------------------------------- #

TargetName = Literal[tuple(TARGETS)]  # type: ignore[valid-type]
Scalar = Union[float, str, bool]


class ParamOut(BaseModel):
    name: str
    target: Optional[TargetName]
    operator: Literal["eq", "lte", "gte", "between", "in", "before", "exists"]
    value: Union[Scalar, list[Scalar]]
    unit: Optional[str]
    reference: Optional[TargetName]


class ConditionOut(BaseModel):
    field: Literal[tuple(sorted(CONDITION_FIELDS))]  # type: ignore[valid-type]
    operator: Literal["eq", "in", "not_in"]
    value: Union[str, list[str]]


class ChargebackOut(BaseModel):
    amount: Optional[float]
    basis: Optional[Literal["flat", "per_carton", "per_pallet", "per_unit", "per_shipment",
                            "percent_of_invoice", "other"]]
    text: str


class SourceOut(BaseModel):
    page: int
    section: str
    snippet: str


class RuleOut(BaseModel):
    category: Literal[tuple(c.value for c in Category)]  # type: ignore[valid-type]
    requirement: str
    parameters: list[ParamOut]
    applies_to: list[ConditionOut]
    chargeback: Optional[ChargebackOut]
    sources: list[SourceOut]
    confidence: Literal["low", "med", "high"]
    status: Literal["parameterized", "needs_human"]
    review_note: Optional[str]


class ExtractionOut(BaseModel):
    rules: list[RuleOut]


OUTPUT_SCHEMA = transform_schema(ExtractionOut)

# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def _targets_table() -> str:
    rows = ["| target | type | unit | meaning |", "|---|---|---|---|"]
    rows += [f"| `{t}` | {typ} | {unit or ''} | {desc} |" for t, (typ, unit, desc) in TARGETS.items()]
    return "\n".join(rows)


def guide_context(chunks: list[Chunk]) -> str:
    """Table of contents plus any chargeback/fee tables, shared by every chunk's prompt."""
    toc = ["## Sections"]
    for c in chunks:
        pages = f"p.{c.pages[0]}" if len(c.pages) == 1 else f"pp.{c.pages[0]}-{c.pages[-1]}"
        toc.append(f"- {c.section} ({pages})")
    parts = ["\n".join(toc)]
    for c in chunks:
        for t in c.tables:
            if any(CHARGEBACK_HEADER_RE.search(h) for h in t.header):
                parts.append(f"## Chargeback table from \"{c.section}\" (page {t.page})\n\n{t.to_markdown()}")
    return "\n\n".join(parts)


def render_system_prompt(prompt_name: str, chunks: list[Chunk]) -> str:
    src = (PROMPTS_DIR / f"{prompt_name}.md").read_text()
    return jinja2.Template(src, undefined=jinja2.StrictUndefined).render(
        targets_table=_targets_table(), guide_context=guide_context(chunks))


def _user_content(chunk: Chunk) -> list[dict]:
    content: list[dict] = []
    for img in chunk.images:
        data = base64.standard_b64encode(Path(img.path).read_bytes()).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
        content.append({"type": "text", "text": f"(Above: image of page {img.page}; reason: {', '.join(img.reasons)}.)"})
    pages = ", ".join(map(str, chunk.pages))
    content.append({"type": "text", "text": (
        f"Section: {' > '.join(chunk.section_path)}\nPages: {pages}\n\n{chunk.text}\n\n"
        "Extract every rule in this section.")})
    return content

# --------------------------------------------------------------------------- #
# Validation and deterministic post-processing
# --------------------------------------------------------------------------- #


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def page_texts(guide: ParsedGuide) -> dict[int, str]:
    out = {}
    for p in guide.pages:
        bits = []
        for b in p.blocks:
            bits.append(b.text)
            if b.table:
                bits += [" ".join(b.table.header)] + [" ".join(r) for r in b.table.rows]
        out[p.number] = _norm(" ".join(bits))
    return out


@dataclass
class ChunkResult:
    chunk_id: str
    rules: list[Rule] = field(default_factory=list)
    attempts: int = 0
    demoted: int = 0
    rejected: list[dict] = field(default_factory=list)


def _validate(raw: list[dict], chunk: Chunk, retailer: str, version: str) -> tuple[list[Rule], list[str], int, list[dict]]:
    rules, errors, bad, demoted = [], [], [], 0
    for i, r in enumerate(raw):
        r = json.loads(json.dumps(r))  # private copy
        unmapped = [p["name"] for p in r.get("parameters", []) if p.get("target") is None]
        if r.get("status") == "parameterized" and (unmapped or not r.get("parameters")):
            r["status"] = "needs_human"
            r["review_note"] = r.get("review_note") or (
                f"Auto-demoted: parameter(s) {unmapped} have no checkable shipment field." if unmapped
                else "Auto-demoted: marked parameterized but has no parameters.")
            demoted += 1
        problems = [f"source page {s['page']} is not in this section (pages {chunk.pages})"
                    for s in r.get("sources", []) if s.get("page") not in chunk.pages]
        try:
            rule = Rule(rule_id=f"tmp_{i}", retailer=retailer, guide_version=version, **r)
        except ValidationError as e:
            problems += [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
            rule = None
        if problems:
            errors.append(f"rules[{i}] ({r.get('requirement', '')[:80]!r}): " + "; ".join(problems))
            bad.append({"raw": r, "problems": problems})
        else:
            rules.append(rule)
    return rules, errors, demoted, bad


def extract_chunk(llm: LLM, system: list[dict], chunk: Chunk, guide: ParsedGuide, namespace: str) -> ChunkResult:
    res = ChunkResult(chunk.chunk_id)
    messages: list[dict] = [{"role": "user", "content": _user_content(chunk)}]
    base_key = f"{chunk.chunk_id}_{sha(chunk.text)[:8]}"
    for attempt in range(MAX_ATTEMPTS):
        res.attempts = attempt + 1
        text = llm.json_call(namespace=namespace, key=f"{base_key}_a{attempt}", system=system,
                             messages=messages, schema=OUTPUT_SCHEMA,
                             log_extra={"chunk_id": chunk.chunk_id, "section": chunk.section, "attempt": attempt})
        raw = json.loads(text)["rules"]
        rules, errors, demoted, bad = _validate(raw, chunk, guide.retailer, guide.guide_version)
        res.rules, res.demoted, res.rejected = rules, demoted, bad
        if not errors:
            break
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "These rules failed validation:\n" + "\n".join(f"- {e}" for e in errors)
             + "\n\nReturn the complete corrected list of rules for this section (all rules, not only the fixed ones)."},
        ]
    return res


def verify_snippets(rules: list[Rule], texts: dict[int, str]) -> None:
    for r in rules:
        for s in r.sources:
            page = texts.get(s.page, "")
            s.verified = (_norm(s.snippet) in page) if page else None


_CONF_RANK = {Confidence.low: 0, Confidence.med: 1, Confidence.high: 2}


def _param_key(r: Rule) -> tuple:
    return tuple(sorted((p.target, p.operator.value, json.dumps(p.value), (p.unit or "").lower(), p.reference)
                        for p in r.parameters))


def _cond_key(r: Rule) -> str:
    if r.applies_to == "all" or not r.applies_to:
        return "all"
    return json.dumps(sorted(json.dumps(c.model_dump(), sort_keys=True) for c in r.applies_to))


def _same_rule(a: Rule, b: Rule) -> bool:
    if a.category != b.category or _cond_key(a) != _cond_key(b):
        return False
    if a.status == b.status == RuleStatus.parameterized:
        return _param_key(a) == _param_key(b)
    ratio = difflib.SequenceMatcher(None, a.requirement.lower(), b.requirement.lower()).ratio()
    return ratio >= DEDUP_TEXT_RATIO


def dedupe(rules: list[Rule]) -> tuple[list[Rule], list[tuple[str, str]]]:
    """Merge rules stated in more than one place. Returns (kept, [(kept_requirement, merged_requirement)])."""
    kept: list[Rule] = []
    merges: list[tuple[str, str]] = []
    for r in rules:
        match = next((k for k in kept if _same_rule(k, r)), None)
        if match is None:
            kept.append(r)
            continue
        seen = {(s.page, _norm(s.snippet)) for s in match.sources}
        match.sources += [s for s in r.sources if (s.page, _norm(s.snippet)) not in seen]
        if _CONF_RANK[r.confidence] > _CONF_RANK[match.confidence]:
            match.confidence = r.confidence
        match.chargeback = match.chargeback or r.chargeback
        if r.review_note and r.review_note != match.review_note:
            match.review_note = "; ".join(filter(None, [match.review_note, r.review_note]))
        merges.append((match.requirement, r.requirement))
    return kept, merges


def assign_ids(rules: list[Rule], retailer: str, version: str) -> None:
    counters: dict[str, int] = {}
    for r in rules:
        n = counters[r.category.value] = counters.get(r.category.value, 0) + 1
        r.rule_id = f"{retailer}_{version}_{r.category.value}_{n:03d}"


@dataclass
class ExtractionReport:
    ruleset: RuleSet
    chunk_results: list[ChunkResult]
    merges: list[tuple[str, str]]
    system_prompt: str


def extract_guide(guide: ParsedGuide, chunks: list[Chunk], llm: LLM, prompt_name: str = DEFAULT_PROMPT) -> ExtractionReport:
    system_text = render_system_prompt(prompt_name, chunks)
    system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    namespace = f"{guide.sha256[:16]}/{prompt_name}"

    results = [extract_chunk(llm, system, c, guide, namespace) for c in chunks]
    all_rules = [r for res in results for r in res.rules]
    verify_snippets(all_rules, page_texts(guide))
    rules, merges = dedupe(all_rules)
    assign_ids(rules, guide.retailer, guide.guide_version)

    ruleset = RuleSet(retailer=guide.retailer, guide_version=guide.guide_version, guide_sha256=guide.sha256,
                      prompt_version=prompt_name, model=llm.model,
                      extracted_at=datetime.now(timezone.utc).replace(microsecond=0), rules=rules)
    return ExtractionReport(ruleset, results, merges, system_text)
