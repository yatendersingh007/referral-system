"""Phase 4 — personalization research (§4 Phase 4). Public-web hooks on the
POC and firm: interviews, podcasts, panels, blog posts, press quotes,
announced deals. Every hook carries a source URL. If a search result URL is
linkedin.com, only ever the search-SNIPPET text is used — the page itself is
never fetched (HARD RULE 2). This module never calls a provider's `.extract()`
/content-fetch function on ANY url, linkedin or otherwise, precisely so that
guarantee needs no per-URL branch to enforce: search results already contain
enough snippet text, and there's no code path that could reach linkedin.com.
"""
from __future__ import annotations

from typing import Callable

from . import config, testmode
from .icp_gate import InfrastructureError

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def assert_no_linkedin_fetch(url: str) -> None:
    """Defensive guard: any code path that is about to fetch a URL's full page
    content (as opposed to using a search-result snippet) must call this
    first. It always raises for linkedin.com — this module never calls it in
    a context where it could fire, which is exactly the point."""
    if "linkedin.com" in (url or "").lower():
        raise RuntimeError(
            "HARD RULE 2 violation: attempted to fetch a linkedin.com page. "
            "Search-result snippet text only, never a page fetch."
        )


_ANGLES = [
    '"{poc}" "{firm}" interview OR podcast OR panel OR quoted',
    '"{firm}" blog OR press release OR announcement OR "case study"',
]


def _run_search(firm_name: str, poc_name: str) -> tuple[list[dict], int]:
    from lib_snapshot.researcher import search_client
    from lib_snapshot.researcher.search_client import SearchAPIError

    calls = 0
    results: list[dict] = []
    for angle in _ANGLES:
        if calls >= config.MAX_SEARCH_CALLS_PER_FIRM:
            break
        query = angle.format(poc=poc_name, firm=firm_name)
        try:
            results.extend(search_client.search(query, max_results=5))
            calls += 1
        except SearchAPIError as e:
            raise InfrastructureError("Tavily", e.message) from e
    return results, calls


_EXTRACT_PROMPT = """From the search results below about {firm} (and its POC {poc}),
extract public personalization hooks for an outreach email and pitch deck.
Every hook MUST carry a source URL from the results — never invent one. If a
result's url contains "linkedin.com", you may use its snippet text as
context but its url may never be used as a hook's source (LinkedIn is never
fetched or cited for outreach — HARD RULE 2).

Return a SINGLE JSON object, no prose, no markdown fence:
{{
  "hook_1": "1-2 sentences, verifiable fact, or empty string",
  "hook_1_source": "url or empty string",
  "hook_2": "1-2 sentences, verifiable fact, or empty string",
  "hook_2_source": "url or empty string",
  "firm_niche": "short phrase",
  "recent_event": "one dated fact or empty string",
  "pain_point_angle": "how switching-inertia pain maps to this firm's actual operations",
  "service_fit": "which X Analyst service(s) fit best"
}}

Search results:
{results}
"""


def research_personalization(firm_name: str, poc_name: str, progress: Progress = _noop) -> dict:
    """Phase 4 entry point. TEST_MODE returns a canned fixture. Real mode
    searches, then has the LLM extract hooks with sources. thin_personalization
    is True whenever no usable hook was found — the draft still proceeds using
    firm-level facts from Phase 2 only (HARD RULE per §4 Phase 4)."""
    if config.TEST_MODE:
        return testmode.personalization_fixture(firm_name)

    from lib_snapshot import models

    results, _calls = _run_search(firm_name, poc_name)
    if not results:
        return {
            "hook_1": "", "hook_1_source": "", "hook_2": "", "hook_2_source": "",
            "firm_niche": "", "recent_event": "", "pain_point_angle": "",
            "service_fit": "", "thin_personalization": True,
        }

    snippets = "\n".join(
        f"- [{r.get('url')}] {r.get('title', '')}: {(r.get('content') or '')[:400]}"
        for r in results[:15]
    )
    prompt = _EXTRACT_PROMPT.format(firm=firm_name, poc=poc_name, results=snippets)
    parsed, _usage = models.complete_json_retry(prompt, temperature=0.3, max_tokens=1000)

    # Belt-and-suspenders: strip any hook whose source slipped through as a
    # linkedin.com URL (HARD RULE 2) rather than trusting the prompt alone.
    for hook_key, src_key in (("hook_1", "hook_1_source"), ("hook_2", "hook_2_source")):
        if "linkedin.com" in (parsed.get(src_key) or "").lower():
            parsed[hook_key] = ""
            parsed[src_key] = ""

    thin = not (parsed.get("hook_1") or parsed.get("hook_2"))
    parsed["thin_personalization"] = thin
    return parsed
