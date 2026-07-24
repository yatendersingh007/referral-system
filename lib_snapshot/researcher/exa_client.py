"""Exa search wrapper: semantic 'find similar firms' for competitor discovery.

Second search provider behind the same interface as search_client (same
layered_search signature and normalized {title, url, content, score} results),
so competitor_engine can swap it in with no other changes.

Exa's strength is embeddings/semantic search ("firms similar to X"), which fits
the discovery step (find owner/operator competitors). Per-firm research stays on
Tavily (company_research.py). Selection is by key presence: if EXA_API_KEY is
set, discovery uses Exa; otherwise it falls back to Tavily. No env flag.
"""
from __future__ import annotations

from typing import Optional

from .. import config
from .search_client import SearchAPIError, SearchError  # shared hard-error type

# Company-focused discovery: bias results to firm homepages.
_CATEGORY = "company"
_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.EXA_API_KEY:
            raise SearchAPIError("Exa", "EXA_API_KEY is empty. Set it in .env.")
        from exa_py import Exa  # imported lazily so the module loads without the key
        _client = Exa(config.EXA_API_KEY)
    return _client


def search(query: str, max_results: int = 8, **_ignored) -> list[dict]:
    """One Exa semantic search. Returns [{title, url, content, score}].

    Extra kwargs (topic/search_depth/days) are accepted and ignored for
    drop-in compatibility with search_client.search.
    """
    client = _get_client()
    try:
        resp = client.search_and_contents(
            query,
            type="auto",              # let Exa pick neural vs keyword
            category=_CATEGORY,       # company pages, not brokers' blog posts
            num_results=max_results,
            text={"max_characters": 800},
        )
    except Exception as e:  # noqa: BLE001
        raise SearchAPIError("Exa", str(e)) from e

    out = []
    for r in getattr(resp, "results", []) or []:
        out.append({
            "title": getattr(r, "title", "") or "",
            "url": getattr(r, "url", "") or "",
            "content": (getattr(r, "text", "") or "")[:800],
            "score": getattr(r, "score", 0.0) or 0.0,
        })
    return out


def find_similar(url: str, max_results: int = 10) -> list[dict]:
    """Find firms semantically similar to a known company URL."""
    client = _get_client()
    try:
        resp = client.find_similar_and_contents(
            url, num_results=max_results, text={"max_characters": 800},
            exclude_source_domain=True,
        )
    except Exception as e:  # noqa: BLE001
        raise SearchAPIError("Exa", str(e)) from e
    out = []
    for r in getattr(resp, "results", []) or []:
        out.append({
            "title": getattr(r, "title", "") or "",
            "url": getattr(r, "url", "") or "",
            "content": (getattr(r, "text", "") or "")[:800],
            "score": getattr(r, "score", 0.0) or 0.0,
        })
    return out


def layered_search(angles: list[str], max_results_each: int = 6,
                   news_angles: Optional[list[str]] = None) -> list[dict]:
    """Run several semantic query angles, merge, dedupe by URL. Same shape as
    search_client.layered_search so it is a drop-in discovery provider.

    news_angles are run as ordinary semantic searches (Exa has no news topic
    dimension for this use); they still surface recent entrants.
    """
    seen: dict[str, dict] = {}

    def _absorb(results: list[dict]) -> None:
        for r in results:
            url = r.get("url")
            if not url:
                continue
            if url not in seen or r.get("score", 0) > seen[url].get("score", 0):
                seen[url] = r

    for q in list(angles) + list(news_angles or []):
        try:
            _absorb(search(q, max_results=max_results_each))
        except SearchAPIError:
            raise                       # Exa is down (key/quota) -> caller falls back
        except SearchError:
            continue

    return sorted(seen.values(), key=lambda r: r.get("score", 0), reverse=True)
