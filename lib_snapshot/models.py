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
from typing import Any, Callable, Optional

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
                                  "timeout": 90, "max_retries": 1,
                                  # Azure AI Foundry (services.ai.azure.com/models) checks
                                  # authentication via an "api-key" HEADER, not the
                                  # "Authorization: Bearer <key>" header the OpenAI SDK sends
                                  # from api_key= alone -- Bearer-only auth 401s against
                                  # Foundry even with a correct, working key (confirmed via a
                                  # direct httpx POST to the same endpoint/key returning 200).
                                  # api_key= is kept so the SDK's own client-side validation
                                  # still passes; this header is what Foundry actually checks.
                                  "default_headers": {"api-key": ep["api_key"]}}
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
    # Diagnosis tooling (FIX 6, real-mode Kairos review): reasoning models
    # (gpt-5*/o-series) spend tokens on HIDDEN reasoning before any visible
    # completion text -- reasoning_tokens (from the API's own
    # completion_tokens_details, when the provider reports it) is what lets
    # a caller distinguish "the model reasoned a lot and ran out of budget
    # before writing anything visible" from "the model just declined to
    # answer". 0 when a provider doesn't report this breakdown at all,
    # which is itself a fact worth surfacing, not silently assumed to mean
    # zero reasoning happened.
    reasoning_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cost_usd += other.cost_usd
        self.reasoning_tokens += other.reasoning_tokens


@dataclass
class LLMResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    # Diagnosis tooling (FIX 6): the API's own finish_reason for this
    # completion ("stop", "length", "content_filter", ...) -- "length" with
    # empty text is the specific, confirmable signature of a reasoning
    # model exhausting its token budget on hidden reasoning before ever
    # producing visible output, as opposed to some other empty-response
    # cause (a content filter, a genuine refusal, etc.).
    finish_reason: str = ""


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
    reasoning_effort: Optional[str] = None,
) -> LLMResult:
    """One chat completion with backoff retry. Returns text + token usage/cost.

    Routes to the endpoint that serves `model` and adapts parameters for
    reasoning models (gpt-5*/o-series): max_completion_tokens (with headroom for
    hidden reasoning) and no custom temperature.

    `reasoning_effort` (only meaningful for reasoning models; ignored
    otherwise): one of "minimal"/"low"/"medium"/"high"/etc, passed straight
    through to the API. FIX 7 (real-mode Kairos run): confirmed live against
    this project's actual Azure gpt-5-mini deployment (not assumed from
    memory) that it accepts this parameter -- "minimal" reasoning effort
    against the real, non-trivial extraction prompt produced
    reasoning_tokens=0 (vs the FULL max_completion_tokens budget consumed
    with the provider's own default effort, which produced
    finish_reason="length" and empty visible output) while still returning
    a correct, complete result. The default (None) omits the parameter
    entirely, leaving the provider's own default effort for every OTHER
    caller unchanged -- this is a targeted, per-call opt-in, not a global
    behavior change."""
    model = model or config.DEFAULT_MODEL
    reasoning = _is_reasoning(model)
    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if reasoning:
        # Reasoning tokens are spent BEFORE visible output; give real headroom
        # or content comes back empty with finish_reason='length'.
        kwargs["max_completion_tokens"] = max(max_tokens, 2048)
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = temperature
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = client_for(model).chat.completions.create(**kwargs)
            choice = resp.choices[0]
            text = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", "") or ""
            u = resp.usage
            pt = getattr(u, "prompt_tokens", 0) or 0
            ct = getattr(u, "completion_tokens", 0) or 0
            details = getattr(u, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0 if details else 0
            usage = Usage(
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=getattr(u, "total_tokens", pt + ct) or (pt + ct),
                cost_usd=_estimate_cost(model, pt, ct),
                reasoning_tokens=reasoning_tokens,
            )
            return LLMResult(text=text, usage=usage, finish_reason=finish_reason)
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
    reasoning_effort: Optional[str] = None,
) -> LLMResult:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat(messages, model=model, temperature=temperature,
                max_tokens=max_tokens, json_mode=json_mode,
                reasoning_effort=reasoning_effort)


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
                        temperature: float = 0.2, max_tokens: int = 2000,
                        debug_capture: Optional[Callable[[str, str, str, int], None]] = None,
                        reasoning_effort: Optional[str] = None,
                        ) -> tuple[Any, Usage]:
    """Like complete_json, but an EMPTY / unparseable response is retried once on
    the same model, then ESCALATED to the premium model — because an empty
    completion is a success at the HTTP layer (chat's retries never fire) yet is
    useless. Accumulates usage across attempts. Raises LLMError if all fail.

    `debug_capture`, if given, is called as debug_capture(model_name,
    raw_response_text, finish_reason, reasoning_tokens) immediately after
    each attempt's HTTP call succeeds -- a pure side-effect for diagnostics
    (e.g. a caller dumping the exact raw response it received to a file, or
    warning when a reasoning model returns empty because it exhausted its
    token budget on hidden reasoning -- finish_reason == "length" with an
    empty response is the confirmable signature of that specific failure
    mode, as opposed to a refusal or content filter). Fires for every
    attempt made, including one that later fails to parse. Never affects
    retry/escalation behavior or the returned value; the default (None) is
    a complete no-op."""
    model = model or config.DEFAULT_MODEL
    premium = premium_model or config.PREMIUM_MODEL
    total = Usage()
    last: Optional[Exception] = None
    for m in (model, model, premium):     # try, retry, escalate
        try:
            res = complete(prompt, system=system, model=m, temperature=temperature,
                           max_tokens=max_tokens, json_mode=True,
                           reasoning_effort=reasoning_effort)
            total.add(res.usage)
            if debug_capture is not None:
                debug_capture(m, res.text, res.finish_reason, res.usage.reasoning_tokens)
            return parse_json(res.text), total          # parse_json raises on empty
        except (ValueError, LLMError) as e:
            last = e
    raise LLMError(f"empty/invalid JSON after retry + premium escalation: {last}")


def ping() -> str:
    """Tiny liveness check used by /status and startup diagnostics."""
    res = complete("reply with the single word: ok", max_tokens=5, temperature=0)
    return res.text.strip()
