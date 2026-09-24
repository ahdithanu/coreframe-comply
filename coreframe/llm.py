"""Anthropic client wrapper: disk cache, structured JSON output, cost/token/latency log.

Every raw model response is cached as JSON under
    <cache_dir>/<guide_sha[:16]>/<prompt_version>/<model>/<key>.json
so reruns and evals replay for free. The cache entry records the SHA of the
rendered system prompt. If the prompt file changed without a version bump, the
lookup fails loudly instead of returning a stale answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# $ per million tokens: (input, output). Cache writes bill at 1.25x input, reads at 0.1x.
PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
DEFAULT_MODEL = os.environ.get("COREFRAME_MODEL", "claude-sonnet-5")


class CacheMiss(RuntimeError):
    pass


class StalePromptError(RuntimeError):
    pass


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def cost_usd(model: str, usage: dict[str, int]) -> float | None:
    if model not in PRICING:
        return None
    p_in, p_out = PRICING[model]
    return (usage.get("input_tokens", 0) * p_in
            + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
            + usage.get("cache_read_input_tokens", 0) * p_in * 0.10
            + usage.get("output_tokens", 0) * p_out) / 1e6


@dataclass
class CallRecord:
    key: str
    cached: bool
    latency_s: float
    usage: dict[str, int]
    cost_usd: float | None
    stop_reason: str | None


@dataclass
class RunLog:
    path: Path
    records: list[CallRecord] = field(default_factory=list)

    def add(self, rec: CallRecord, **extra: Any) -> None:
        self.records.append(rec)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({**rec.__dict__, **extra}) + "\n")

    def summary(self) -> dict[str, Any]:
        live = [r for r in self.records if not r.cached]
        tok = lambda k: sum(r.usage.get(k, 0) for r in live)  # noqa: E731
        costs = [r.cost_usd for r in live]
        return {
            "calls": len(self.records),
            "cache_hits": len(self.records) - len(live),
            "input_tokens": tok("input_tokens"),
            "cache_write_tokens": tok("cache_creation_input_tokens"),
            "cache_read_tokens": tok("cache_read_input_tokens"),
            "output_tokens": tok("output_tokens"),
            "cost_usd": round(sum(c for c in costs if c is not None), 4) if live else 0.0,
            "cost_complete": all(c is not None for c in costs),
            "latency_s": round(sum(r.latency_s for r in live), 1),
        }


class LLM:
    def __init__(self, model: str, cache_dir: Path, run_log: RunLog, effort: str = "high",
                 offline: bool = False, max_tokens: int = 32_000):
        self.model, self.effort, self.offline, self.max_tokens = model, effort, offline, max_tokens
        self.cache_dir, self.run_log = cache_dir, run_log
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic

            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise SystemExit("ANTHROPIC_API_KEY is not set (or pass --offline to use cached responses only).")
            # Explicit key + base URL so an ambient ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL
            # from some other tool never gets picked up silently.
            self._client = anthropic.Anthropic(
                api_key=key,
                base_url=os.environ.get("COREFRAME_API_BASE_URL", "https://api.anthropic.com"),
            )
        return self._client

    def json_call(self, *, namespace: str, key: str, system: list[dict], messages: list[dict],
                  schema: dict, log_extra: dict | None = None) -> str:
        """Return the model's JSON text for this request, from cache when possible."""
        system_sha = sha(json.dumps(system, sort_keys=True))
        path = self.cache_dir / namespace / self.model / f"{key}.json"
        if path.exists():
            entry = json.loads(path.read_text())
            if entry["system_sha"] != system_sha:
                raise StalePromptError(
                    f"{path}: cached under a different system prompt. The prompt file or guide context "
                    "changed without a prompt version bump. Bump the version (prompts/extract_vN.md) "
                    "or delete this cache directory.")
            self.run_log.add(CallRecord(key, True, 0.0, entry["usage"], 0.0, entry["stop_reason"]),
                             **(log_extra or {}))
            return entry["text"]
        if self.offline:
            raise CacheMiss(f"offline mode and no cached response at {path}")

        t0 = time.monotonic()
        with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        ) as stream:
            msg = stream.get_final_message()
        latency = time.monotonic() - t0

        if msg.stop_reason == "refusal":
            raise RuntimeError(f"model refused request {key}")
        if msg.stop_reason == "max_tokens":
            raise RuntimeError(f"hit max_tokens={self.max_tokens} on {key}; section too large or raise the limit")
        text = next(b.text for b in msg.content if b.type == "text")
        usage = {k: getattr(msg.usage, k) or 0 for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "key": key, "model": self.model, "effort": self.effort, "system_sha": system_sha,
            "request_id": getattr(msg, "_request_id", None), "stop_reason": msg.stop_reason,
            "usage": usage, "latency_s": round(latency, 2), "text": text,
            "raw": msg.to_dict(),
        }, indent=1))
        self.run_log.add(CallRecord(key, False, round(latency, 2), usage, cost_usd(self.model, usage),
                                    msg.stop_reason), **(log_extra or {}))
        return text
