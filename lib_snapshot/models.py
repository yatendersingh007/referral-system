"""LLM client wrapper (OpenAI-compatible), retry, cost logging.

Two models, each routed to its own endpoint (config.MODEL_ENDPOINTS):
  DEFAULT_MODEL  - research, classification, ICP gating, dedup
  PREMIUM_MODEL  - final outbound drafts only

DEFAULT_MODEL and PREMIUM_MODEL can live on different Azure resources with
different URL patterns / api-versions. One OpenAI client is built and cached per
(base_url, api_version, api_key) tuple. Reasoning models (gpt-5*/o-series) need
max_completion_tokens and reject a custom temperature; that is handled here.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import OpenAI

from . import config

_clients: dict[tuple, OpenAI] = {}

# Reasoning-model families: use max_completion_tokens, only default temperature.
_REASONING_PREFIXES = ("gpt-5", "gpt5", "o1", "o3", "o4")


def _is_reasoning(model: str) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _REASONING_PREFIXES)


def _endpoint_for(model: str) -> dict:
    return config.MODEL_ENDPOINTS.get(model, config.PRIMARY_ENDPOINT)


def client_for(model: Optional[str] = None) -> OpenAI:
    """Return the cached OpenAI client for the endpoint that serves `model`."""
    model = model or config.DEFAULT_MODEL
    ep = _endpoint_for(model)
    if not ep.get("api_key"):
        raise RuntimeError(
            f"No API key for model {model!r}. Set LLM_API_KEY (and SECONDARY_LLM_API_KEY "
            "if using a secondary endpoint) in .env."
        )
    key = (ep["base_url"], ep.get("api_version"), ep["api_key"])
    if key not in _clients:
        # No LLM call may hang a run (Fix 7): bound each request (timeout) and cap
        # the SDK's own retries so a stalled endpoint can't stack ~3x90s per call
        # (chat() already runs its own retry loop on top).
        kwargs: dict[str, Any] = {"base_url": ep["base_url"], "api_key": ep["api_key"],
                                  "timeout": 90, "max_retries": 1}
        if ep.get("api_version"):
            kwargs["default_query"] = {"api-version": ep["api_version"]}
        _clients[key] = OpenAI(**kwargs)
    return _clients[key]


def client() -> OpenAI:
    """Backwards-compatible accessor: the DEFAULT_MODEL's client."""
    return client_for(config.DEFAULT_MODEL)


def endpoint_label(model: str) -> str:
    """Human-readable endpoint host for `model` (for diagnostics; no secrets)."""
    ep = _endpoint_for(model)
    return f"{ep['base_url']} (api-version={ep.get('api_version') or 'none'})"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cost_usd += other.cost_usd


@dataclass
class LLMResult:
    text: str
    usage: Usage = field(default_factory=Usage)


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price = config.MODEL_PRICES.get(model)
    if not price:
        return 0.0
    return (prompt_tokens * price["in"] + completion_tokens * price["out"]) / 1_000_000


class LLMError(RuntimeError):
    pass


def chat(
    messages: list[dict],
    model: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 2000,
    json_mode: bool = False,
    retries: int = 4,
) -> LLMResult:
    """One chat completion with backoff retry. Returns text + token usage/cost.

    Routes to the endpoint that serves `model` and adapts parameters for
    reasoning models (gpt-5*/o-series): max_completion_tokens (with headroom for
    hidden reasoning) and no custom temperature.
    """
    model = model or config.DEFAULT_MODEL
    reasoning = _is_reasoning(model)
    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if reasoning:
        # Reasoning tokens are spent BEFORE visible output; give real headroom
        # or content comes back empty with finish_reason='length'.
        kwargs["max_completion_tokens"] = max(max_tokens, 2048)
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = temperature
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = client_for(model).chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
            u = resp.usage
            pt = getattr(u, "prompt_tokens", 0) or 0
            ct = getattr(u, "completion_tokens", 0) or 0
            usage = Usage(
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=getattr(u, "total_tokens", pt + ct) or (pt + ct),
                cost_usd=_estimate_cost(model, pt, ct),
            )
            return LLMResult(text=text, usage=usage)
        except Exception as e:  # noqa: BLE001 - surface a clean, retried error
            last_err = e
            msg = str(e).lower()
            # Do not burn retries on unrecoverable auth/balance errors.
            if any(s in msg for s in ("insufficient balance", "invalid api key",
                                      "unauthorized", "authentication")):
                raise LLMError(f"LLM call failed (unrecoverable): {e}") from e
            # Adapt to per-model parameter constraints, then retry immediately.
            if "max_tokens" in msg and "max_completion_tokens" not in kwargs:
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = max(max_tokens, 2048)
                continue
            if "temperature" in msg and "temperature" in kwargs:
                kwargs.pop("temperature", None)
                continue
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise LLMError(f"LLM call failed after {retries} attempts: {last_err}")


def complete(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 2000,
    json_mode: bool = False,
) -> LLMResult:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat(messages, model=model, temperature=temperature,
                max_tokens=max_tokens, json_mode=json_mode)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_json(text: str) -> Any:
    """Best-effort JSON extraction from a model response.

    Handles ```json fences and leading/trailing prose. Raises ValueError if no
    parseable JSON object/array is found.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty LLM response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            text = m.group(1)
    # Grab the outermost {...} or [...] span.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON found in LLM response: {text[:200]!r}")


def complete_json(prompt: str, system: Optional[str] = None,
                  model: Optional[str] = None, temperature: float = 0.2,
                  max_tokens: int = 2000) -> tuple[Any, Usage]:
    """Convenience: request JSON and return (parsed, usage)."""
    res = complete(prompt, system=system, model=model, temperature=temperature,
                   max_tokens=max_tokens, json_mode=True)
    return parse_json(res.text), res.usage


def complete_json_retry(prompt: str, system: Optional[str] = None,
                        model: Optional[str] = None, premium_model: Optional[str] = None,
                        temperature: float = 0.2, max_tokens: int = 2000) -> tuple[Any, Usage]:
    """Like complete_json, but an EMPTY / unparseable response is retried once on
    the same model, then ESCALATED to the premium model — because an empty
    completion is a success at the HTTP layer (chat's retries never fire) yet is
    useless. Accumulates usage across attempts. Raises LLMError if all fail."""
    model = model or config.DEFAULT_MODEL
    premium = premium_model or config.PREMIUM_MODEL
    total = Usage()
    last: Optional[Exception] = None
    for m in (model, model, premium):     # try, retry, escalate
        try:
            res = complete(prompt, system=system, model=m, temperature=temperature,
                           max_tokens=max_tokens, json_mode=True)
            total.add(res.usage)
            return parse_json(res.text), total          # parse_json raises on empty
        except (ValueError, LLMError) as e:
            last = e
    raise LLMError(f"empty/invalid JSON after retry + premium escalation: {last}")


def ping() -> str:
    """Tiny liveness check used by /status and startup diagnostics."""
    res = complete("reply with the single word: ok", max_tokens=5, temperature=0)
    return res.text.strip()
