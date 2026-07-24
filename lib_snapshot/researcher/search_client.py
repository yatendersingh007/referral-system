"""Tavily wrapper: layered queries (search + news + site angles) + extract.

Single provider, key-POOL capable (HARD RULE: all research uses open-web
search APIs; research never visits LinkedIn). This is the clean interface a
browser-agent researcher could replace later without touching callers.

Feature A (key-pool rotation, §11.15/HARD RULE 15 companion): a 1,000-firm
batch run can burn through one free-tier Tavily key's monthly quota well
before the batch finishes. `_pool` holds an ordered list of keys (built from
config.TAVILY_API_KEYS, itself backward-compatible with a single
TAVILY_API_KEY). When a response signals THIS KEY is exhausted, `_post`
transparently advances to the next key and retries the SAME request — the
caller (icp_gate.py, personalization.py) never sees an error and the firm
being researched never fails or parks because of it. Only when every key in
the pool is exhausted does `_post` finally raise SearchAPIError, which
(via icp_gate.InfrastructureError) is what feeds HARD RULE 15's circuit
breaker — so the breaker now only ever trips when the WHOLE pool is dead,
not when a single key dies (a single-key death is invisible above this
module by design).

Built generically (`_KeyPool` has no Tavily-specific code) so another
provider (Exa) could adopt the same rotation pattern later — see
HANDOVER.md.
"""
from __future__ import annotations

import hashlib
import sys
import time
from typing import Callable, Optional

import requests

from .. import config

SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"
_TIMEOUT = 40

# QA finding / documented incident (BUILD_SPEC_C.md, from a real Workflow A
# quota-exhaustion incident): Tavily's own quota-exceeded status is HTTP 432,
# not 429 (429 is Tavily's ordinary rate-limit, already handled below by the
# existing same-key sleep-and-retry — rotating keys over a merely-rate-limited
# request would be wasteful, since the SAME key works again moments later).
# 401/403 (auth failure — a revoked/invalid key) are treated the same as
# quota exhaustion: the key doesn't work, so rotate away from it. This is the
# strongest available evidence in this codebase (a real prior incident,
# documented in three places) rather than a guess — but it has not been
# confirmed against a LIVE Tavily response in this session. `_post` below
# logs the raw response body (truncated) on every exhaustion event
# specifically so the signature can be confirmed/adjusted in the first
# real-mode run. Security review finding (M2): the success path logs only a
# compact summary (query hash, result count, key index, latency) — never a
# full response body or the raw query text — full detail is reserved for
# this error/exhaustion path.
_KEY_EXHAUSTION_STATUS = {401, 403, 432}


class SearchError(RuntimeError):
    pass


class SearchAPIError(SearchError):
    """A HARD, reportable search-provider error (quota/auth/explicit error body).
    Carries the provider + real message so the caller can tell the operator the
    truth instead of silently reporting 'Found 0 candidates'."""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        self.message = message
        super().__init__(f"{provider} error: {message}")


class _KeyExhausted(Exception):
    """Internal signal only: this key returned the quota/auth-dead signature.
    Always caught inside _post to trigger pool rotation — never escapes this
    module."""

    def __init__(self, response: "requests.Response"):
        self.response = response


class _KeyPool:
    """Generic ordered pool of API keys with quota-aware rotation. One
    instance per provider (Tavily today); a future Exa pool could reuse this
    class unchanged."""

    def __init__(self, keys: list[str]):
        self._keys = [k for k in (keys or []) if k]
        self._idx = 0

    def current(self) -> Optional[str]:
        if not self._keys:
            return None
        return self._keys[self._idx]

    def advance(self) -> bool:
        """Moves to the next key. Returns False (pool exhausted, every key
        already tried) or True (a fresh key is now current)."""
        if self._idx + 1 >= len(self._keys):
            return False
        self._idx += 1
        return True

    def position(self) -> tuple[int, int]:
        """(1-based current key number, total key count) for messaging."""
        return self._idx + 1, len(self._keys)

    def size(self) -> int:
        return len(self._keys)

    def reset(self, keys: list[str]) -> None:
        """Reload from a fresh key list (e.g. /referral resume re-reading
        .env) — starts back at key 1."""
        self._keys = [k for k in (keys or []) if k]
        self._idx = 0


_pool = _KeyPool(config.TAVILY_API_KEYS)

# Telegram-facing notify hook (Feature A: "Tavily key 1 exhausted, switched to
# key 2 of 3, continuing."). This module never imports telegram_bot directly
# — the caller that owns the Telegram `notify` channel (src/pipeline.py's
# drain_queue) registers it once via set_notify() before draining.
_notify: Optional[Callable[[str], None]] = None


def set_notify(fn: Optional[Callable[[str], None]]) -> None:
    global _notify
    _notify = fn


def _notify_rotation(msg: str) -> None:
    if _notify:
        try:
            _notify(msg)
        except Exception:  # noqa: BLE001 — a notify failure must never break a real search
            pass


def reload_keys() -> None:
    """Feature A mid-run key top-up: /referral resume calls this so a key the
    operator added to .env while the run was paused is picked up, without a
    process restart and without using a stale in-memory pool."""
    _pool.reset(config.reload_tavily_keys())


def pool_size() -> int:
    return _pool.size()


def _log(msg: str) -> None:
    """Per-key debug detail: stderr/journalctl only, never Telegram (matches
    §11.7 — 'which key served each firm's searches' is for debugging, not an
    operator-facing message)."""
    print(f"[tavily-pool] {msg}", file=sys.stderr, flush=True)


def _query_hash(q: str) -> str:
    """A short, stable fingerprint for a query string — enough to correlate
    log lines for the same query without printing the query text itself
    (which can carry a firm/POC name) on every successful request."""
    return hashlib.sha256(q.encode("utf-8")).hexdigest()[:12]


def _error_message(r: "requests.Response") -> str:
    """Pull a human error string out of a Tavily error response body."""
    try:
        j = r.json()
        if isinstance(j, dict):
            d = j.get("detail")
            if isinstance(d, dict) and d.get("error"):
                return str(d["error"])
            if isinstance(d, str) and d.strip():
                return d.strip()
            if j.get("error"):
                return str(j["error"])
    except ValueError:
        pass
    return (r.text or f"HTTP {r.status_code}").strip()[:200]


def _post(url: str, payload: dict, retries: int = 3) -> dict:
    if not _pool.current():
        raise SearchAPIError("Tavily", "No Tavily API key configured. Set TAVILY_API_KEY "
                             "or TAVILY_API_KEYS in .env.")
    while True:
        key = _pool.current()
        full_payload = {"api_key": key, **payload}
        idx, total = _pool.position()
        qhash = _query_hash(payload.get("query", url))
        last_err: Optional[Exception] = None
        try:
            for attempt in range(retries):
                try:
                    start = time.monotonic()
                    r = requests.post(url, json=full_payload, timeout=_TIMEOUT)
                    latency_ms = int((time.monotonic() - start) * 1000)
                    if r.status_code == 429:
                        time.sleep(2 ** attempt + 1)
                        continue
                    if r.status_code in _KEY_EXHAUSTION_STATUS:
                        raise _KeyExhausted(r)
                    # Other client errors (bad request, etc.) are PERMANENT —
                    # do not retry, do not swallow: raise the real message so
                    # the operator sees it.
                    if 400 <= r.status_code < 500:
                        raise SearchAPIError("Tavily", _error_message(r))
                    r.raise_for_status()   # 5xx -> HTTPError -> retried, same key
                    data = r.json()
                    if isinstance(data, dict) and (data.get("error")
                                                   or isinstance(data.get("detail"), dict)):
                        raise SearchAPIError("Tavily", _error_message(r))
                    # Success path: a compact summary line only (query hash,
                    # not the query text itself, which can carry a firm/POC
                    # name) — never the full response body. Full detail is
                    # reserved for the error/exhaustion path below.
                    result_count = len(data.get("results", [])) if isinstance(data, dict) else 0
                    _log(f"key {idx}/{total} query={qhash} results={result_count} "
                        f"latency_ms={latency_ms}")
                    return data
                except SearchAPIError:
                    raise
                except _KeyExhausted:
                    raise
                except requests.RequestException as e:  # noqa: PERF203
                    last_err = e
                    if attempt < retries - 1:
                        time.sleep(2 ** attempt)
        except _KeyExhausted as exhausted:
            body = (exhausted.response.text or "")[:300]
            _log(f"key {idx}/{total} query={qhash} EXHAUSTED (HTTP {exhausted.response.status_code}): {body}")
            if _pool.advance():
                new_idx, _ = _pool.position()
                _notify_rotation(f"Tavily key {idx} exhausted, switched to key {new_idx} of "
                                 f"{total}, continuing.")
                continue   # retry the SAME url/payload, transparently, on the new key
            raise SearchAPIError("Tavily", f"All {total} Tavily key(s) exhausted.") from None
        raise SearchError(f"Tavily request failed: {last_err}")


def search(
    query: str,
    max_results: int = 8,
    topic: str = "general",       # 'general' or 'news'
    search_depth: str = "basic",  # 'basic' or 'advanced'
    include_raw_content: bool = False,
    days: Optional[int] = None,   # for topic='news'
) -> list[dict]:
    """Run one Tavily query. Returns normalized [{title, url, content, score}]."""
    payload: dict = {
        "query": query,
        "max_results": max_results,
        "topic": topic,
        "search_depth": search_depth,
        "include_raw_content": include_raw_content,
    }
    if days is not None:
        payload["days"] = days
    data = _post(SEARCH_URL, payload)
    out = []
    for r in data.get("results", []):
        out.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("raw_content") or r.get("content", ""),
            "score": r.get("score", 0.0),
        })
    return out


def layered_search(angles: list[str], max_results_each: int = 6,
                   news_angles: Optional[list[str]] = None) -> list[dict]:
    """Run several query angles, merge, and dedupe by URL. Preserves best score.

    `angles` run as general search; `news_angles` run against the news topic
    (recent entrants / signals). Returns a single deduped result list.
    """
    seen: dict[str, dict] = {}

    def _absorb(results: list[dict]) -> None:
        for r in results:
            url = r.get("url")
            if not url:
                continue
            if url not in seen or r.get("score", 0) > seen[url].get("score", 0):
                seen[url] = r

    for q in angles:
        try:
            _absorb(search(q, max_results=max_results_each, topic="general"))
        except SearchAPIError:
            raise                       # HARD error (quota/auth): surface the truth
        except SearchError:
            continue                    # soft/transient: skip this one angle
    for q in (news_angles or []):
        try:
            _absorb(search(q, max_results=max_results_each, topic="news", days=180))
        except SearchAPIError:
            raise
        except SearchError:
            continue

    return sorted(seen.values(), key=lambda r: r.get("score", 0), reverse=True)


def extract(urls: list[str]) -> dict[str, str]:
    """Pull clean text for specific URLs. Returns {url: text}. Best-effort."""
    if not urls:
        return {}
    try:
        data = _post(EXTRACT_URL, {"urls": urls[:20]})
    except SearchError:
        return {}
    out = {}
    for r in data.get("results", []):
        out[r.get("url", "")] = r.get("raw_content", "")
    return out
