"""Extraction pipeline against a stubbed Anthropic client (no network, no key)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coreframe.chunking import chunk_guide
from coreframe.extract import extract_guide
from coreframe.ingest import parse_guide
from coreframe.llm import LLM, CacheMiss, RunLog, StalePromptError
from coreframe.render import write_checklist
from coreframe.schemas import RuleStatus

SAMPLE = Path(__file__).resolve().parents[1] / "data/sample/guides/northwind/v2025.1.pdf"


def rule(category, requirement, page, section, snippet, params=(), status="parameterized", **kw):
    return {"category": category, "requirement": requirement, "parameters": list(params),
            "applies_to": kw.get("applies_to", []), "chargeback": kw.get("chargeback"),
            "sources": [{"page": page, "section": section, "snippet": snippet}],
            "confidence": kw.get("confidence", "high"), "status": status,
            "review_note": kw.get("review_note")}


def p(target, op, value, unit=None, reference=None, name="p"):
    return {"name": name, "target": target, "operator": op, "value": value, "unit": unit, "reference": reference}


CB3 = {"amount": 3.0, "basis": "per_carton", "text": "$3.00 per carton"}
CANNED = {
    "3.1 Dimensions and Weight": [
        rule("carton", "Carton length must be 6-24 in.", 2, "3.1 Dimensions and Weight", "Length 6 in 24 in",
             [p("cartons[].length_in", "between", [6, 24], "in")], chargeback=CB3),
        rule("carton", "Carton weight must be 1-50 lbs.", 3, "3.1 Dimensions and Weight", "Weight 1 lb 50 lbs",
             [p("cartons[].weight_lbs", "between", [1, 50], "lbs")], chargeback=CB3),
        rule("carton", "Parcel cartons must not exceed 35 lbs.", 3, "3.1 Dimensions and Weight",
             "must not exceed 35 lbs", [p("cartons[].weight_lbs", "lte", 35, "lbs")],
             applies_to=[{"field": "shipment_type", "operator": "eq", "value": "parcel"}], chargeback=CB3),
    ],
    "3.2 Carton Construction": [
        # model says parameterized but can't map burst strength -> code must demote it
        rule("carton", "Cartons must have 200 psi burst strength.", 3, "3.2 Carton Construction",
             "minimum burst strength of 200 psi", [p(None, "gte", 200, "psi")]),
    ],
    "2.3 ASN Content": [
        rule("asn_edi", "Each carton needs an SSCC-18.", 2, "2.3 ASN Content", "a paraphrase that is not in the guide",
             [p("cartons[].sscc_present", "eq", True)]),
    ],
    "4.1 Carton Labels": [
        rule("labeling", "Labels must be legible and not over seams or under tape.", 3, "4.1 Carton Labels",
             "Labels must be legible", status="needs_human", review_note="Vague."),
    ],
    "4.2 Pallet Labels": [
        # near-duplicate of the 4.1 needs_human rule -> merged, both sources kept
        rule("labeling", "Labels must be legible and not placed over seams or under tape.", 3, "4.2 Pallet Labels",
             "formatted as specified in Section 4.1", status="needs_human", review_note="Cross-reference to 4.1."),
    ],
    "[page 6: image only]": [
        rule("pallet", "DC 6012: pallets must not exceed 60 in.", 6, "Appendix B DC-Specific Requirements",
             "Maximum pallet height is 60 inches.", [p("pallets[].height_in", "lte", 60, "in")],
             applies_to=[{"field": "destination_dc", "operator": "eq", "value": "6012"}]),
    ],
}


class FakeClient:
    """Mimics client.messages.stream(...) -> ctx manager -> get_final_message()."""

    def __init__(self, responder):
        self.responder, self.calls = responder, []
        self.messages = self

    def stream(self, **kw):
        self.calls.append(kw)
        text = self.responder(kw, len(self.calls))
        msg = SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=1000),
            to_dict=lambda: {"stub": True}, _request_id="req_stub")

        class Ctx:
            def __enter__(self_): return SimpleNamespace(get_final_message=lambda: msg)
            def __exit__(self_, *a): return False
        return Ctx()


def section_of(kw):
    text = kw["messages"][0]["content"][-1]["text"]
    return text.split("\n")[0].removeprefix("Section: ").split(" > ")[-1]


def canned(kw, n):
    return json.dumps({"rules": CANNED.get(section_of(kw), [])})


@pytest.fixture()
def setup(tmp_path):
    guide = parse_guide(SAMPLE, image_dir=tmp_path / "pages")
    chunks = chunk_guide(guide)

    def make_llm(responder=canned, offline=False):
        llm = LLM("claude-sonnet-5", tmp_path / "llm", RunLog(tmp_path / "run.jsonl"), offline=offline)
        llm._client = FakeClient(responder)
        return llm
    return guide, chunks, make_llm, tmp_path


def by_req(rs, text):
    return next(r for r in rs.rules if text in r.requirement)


def test_end_to_end(setup):
    guide, chunks, make_llm, tmp = setup
    llm = make_llm()
    rep = extract_guide(guide, chunks, llm)
    rs = rep.ruleset
    assert len(llm.client.calls) == len(chunks)
    assert len(rs.rules) == 7  # 8 canned, 1 merged

    # system prompt is identical across chunks (cacheable) and carries the chargeback table
    systems = {json.dumps(c["system"]) for c in llm.client.calls}
    assert len(systems) == 1 and "Unapproved carrier" in rep.system_prompt
    assert llm.client.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}

    # scanned page is sent as an image
    scan_call = next(c for c in llm.client.calls if section_of(c) == "[page 6: image only]")
    assert scan_call["messages"][0]["content"][0]["type"] == "image"

    # ids
    assert [r.rule_id for r in rs.rules if r.category.value == "carton"][:2] == [
        "northwind_v2025.1_carton_001", "northwind_v2025.1_carton_002"]

    # demotion
    burst = by_req(rs, "burst strength")
    assert burst.status == RuleStatus.needs_human and "Auto-demoted" in burst.review_note

    # citation verification (table row on the continued page; paraphrase; image page)
    assert by_req(rs, "1-50 lbs").sources[0].verified is True
    assert by_req(rs, "SSCC-18").sources[0].verified is False
    assert by_req(rs, "DC 6012").sources[0].verified is None

    # dedup keeps both sources
    legible = by_req(rs, "legible")
    assert [s.section for s in legible.sources] == ["4.1 Carton Labels", "4.2 Pallet Labels"]
    assert len(rep.merges) == 1

    # parcel override is NOT merged into the general weight rule
    assert by_req(rs, "Parcel").applies_to[0].value == "parcel"


def test_rerun_is_served_from_cache(setup):
    guide, chunks, make_llm, tmp = setup
    first = extract_guide(guide, chunks, make_llm()).ruleset
    llm = make_llm(responder=lambda kw, n: pytest.fail("network call on cached rerun"), offline=True)
    second = extract_guide(guide, chunks, llm).ruleset
    assert [r.model_dump(exclude={"extracted_at"}) for r in second.rules] == \
           [r.model_dump(exclude={"extracted_at"}) for r in first.rules]
    assert llm.run_log.summary()["cost_usd"] == 0.0


def test_offline_cache_miss(setup):
    guide, chunks, make_llm, _ = setup
    with pytest.raises(CacheMiss):
        extract_guide(guide, chunks, make_llm(offline=True))


def test_stale_prompt_detected(setup, monkeypatch):
    guide, chunks, make_llm, _ = setup
    extract_guide(guide, chunks, make_llm())
    import coreframe.extract as ex
    orig = ex.render_system_prompt
    monkeypatch.setattr(ex, "render_system_prompt", lambda *a: orig(*a) + "\nedited without a version bump")
    with pytest.raises(StalePromptError):
        extract_guide(guide, chunks, make_llm())


def test_validation_retry_sends_errors_back(setup):
    guide, chunks, make_llm, _ = setup
    target = next(c for c in chunks if c.section == "3.2 Carton Construction")
    good = rule("carton", "Do not band individual cartons.", 3, "3.2 Carton Construction",
                "Do not use banding or strapping", status="needs_human", review_note="No field.")
    bad = {**good, "sources": [{**good["sources"][0], "page": 9}]}

    def responder(kw, n):
        return json.dumps({"rules": [bad] if len(kw["messages"]) == 1 else [good]})
    llm = make_llm(responder)
    rep = extract_guide(guide, [target], llm)
    res = rep.chunk_results[0]
    assert res.attempts == 2 and len(res.rules) == 1 and not res.rejected
    feedback = llm.client.calls[1]["messages"][-1]["content"]
    assert "source page 9 is not in this section" in feedback


def test_rules_rejected_after_max_attempts(setup):
    guide, chunks, make_llm, _ = setup
    target = next(c for c in chunks if c.section == "3.2 Carton Construction")
    bad = rule("carton", "x", 3, "3.2", " ".join(["word"] * 30), status="needs_human", review_note="n")
    rep = extract_guide(guide, [target], make_llm(lambda kw, n: json.dumps({"rules": [bad]})))
    res = rep.chunk_results[0]
    assert res.attempts == 3 and res.rules == [] and "under 25 words" in res.rejected[0]["problems"][0]


def test_checklist_render(setup):
    guide, chunks, make_llm, tmp = setup
    rs = extract_guide(guide, chunks, make_llm()).ruleset
    md, html = write_checklist(rs, tmp / "checklist")
    md_text, html_text = md.read_text(), html.read_text()
    assert "northwind_v2025.1_carton_001" in md_text and "needs human review" in md_text
    assert "cartons[].weight_lbs between 1 and 50 lbs" in md_text
    assert "p. 3, 3.1 Dimensions and Weight" in md_text
    assert "<script>" in html_text and "http" not in html_text.split("<body>")[1]  # self-contained
