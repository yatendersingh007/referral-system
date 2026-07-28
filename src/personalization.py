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

from typing import Callable, Optional

from . import config, icp_gate, testmode
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

X Analyst's own offering, for the two fields below that reference it: X
Analyst is an EMBEDDED FINANCE TEAM — dedicated India-based operators (top
0.5% CA/CPAs) working inside the client's own systems and close calendar. It
is NOT software, NOT a platform, and NOT a product — there is nothing for
the client to install, onboard to, migrate to, or pilot. The full service
stack, and the ONLY services that exist to select from, is exactly:
- property-level books and month-end close
- bank reconciliations
- AP/AR and tenant billing
- CAM reconciliations and true-ups
- budget vs. actual variance reporting
- lender packages and covenant reporting
- owner distributions and monthly owner reports
All of this runs across Yardi, RealPage, and Entrata.

Return a SINGLE JSON object, no prose, no markdown fence:
{{
  "hook_1": "1-2 sentences, verifiable fact, or empty string",
  "hook_1_source": "url or empty string",
  "hook_2": "1-2 sentences, verifiable fact, or empty string",
  "hook_2_source": "url or empty string",
  "firm_niche": "short phrase",
  "recent_event": "one dated fact or empty string",
  "pain_point_angle": "ground this in the choice between overpaying an outsourced accounting provider and burning the in-house controller's nights on close, reconciliations, and reporting -- tied to THIS firm's entity count, fund structures, asset class, or reporting cadence. Never frame it as switching, onboarding, migrating, implementing, a pilot, product adoption, or vendor friction -- X Analyst is a team replacing manual/outsourced accounting work, not software being adopted. Never use a semicolon in this value.",
  "pain_point_angle_source": "url this is grounded in, or empty string",
  "service_fit": "select 2-3 services VERBATIM from X Analyst's service stack above (never invent a new one) that best fit this firm's asset class, entity count, fund structure, and reporting complexity -- never describe X Analyst as software, a platform, or a tool. Never use a semicolon in this value."
}}

Search results:
{results}
"""


# FIX 13 (real-mode Kairos Real Estate Partners run): this module runs its
# OWN independent Tavily search (_run_search/_ANGLES above), separate from
# icp_gate.py's Phase 2 research -- with no entity filter at all until now.
# A real run's hook_1/hook_2 carried correct kairosrep.com sources, but
# pain_point_angle had no source field to check at all, so a wrong-entity
# fact could reach the email/deck through that one field with nothing to
# catch it. Applies the SAME own_domain / other-entity / rejected-CRD logic
# icp_gate.py already uses, preferring the identity Phase 2 already
# established (passed in via `research`) over re-deriving it from this
# phase's differently-worded search angles, which could disagree.
def _filter_results(results: list[dict], firm_name: str, research: Optional[dict]) -> list[dict]:
    research = research or {}
    own_domain = research.get("own_domain") or icp_gate._infer_own_domain(results)
    rejected_crds = research.get("rejected_crds") or []
    return icp_gate._filter_out_other_entities(results, own_domain, firm_name, rejected_crds)


def research_personalization(firm_name: str, poc_name: str, research: Optional[dict] = None,
                             progress: Progress = _noop) -> dict:
    """Phase 4 entry point. TEST_MODE returns a canned fixture. Real mode
    searches, filters out other-entity sources, then has the LLM extract
    hooks with sources. thin_personalization is True whenever no usable hook
    was found — the draft still proceeds using firm-level facts from Phase 2
    only (HARD RULE per §4 Phase 4). `research` is Phase 2's research dict
    (own_domain/rejected_crds if present, per FIX 13) — optional so existing
    callers/tests that don't have it yet still work, falling back to a fresh
    own_domain inference from this phase's own results."""
    if config.TEST_MODE:
        return testmode.personalization_fixture(firm_name)

    from lib_snapshot import models

    results, _calls = _run_search(firm_name, poc_name)
    if not results:
        return {
            "hook_1": "", "hook_1_source": "", "hook_2": "", "hook_2_source": "",
            "firm_niche": "", "recent_event": "", "pain_point_angle": "",
            "pain_point_angle_source": "", "service_fit": "", "thin_personalization": True,
        }

    filtered_results = _filter_results(results, firm_name, research)
    snippets = "\n".join(
        f"- [{r.get('url')}] {r.get('title', '')}: {(r.get('content') or '')[:400]}"
        for r in filtered_results[:15]
    ) or "(no results found)"
    prompt = _EXTRACT_PROMPT.format(firm=firm_name, poc=poc_name, results=snippets)

    # FIX 15 follow-up (real-mode Kairos run): this call originally had no
    # reasoning_effort applied -- a live diagnostic run showed it succeeding
    # cleanly (finish_reason='stop'), unlike deck_filler.py's get_deck_content()
    # (which was hitting the full 2048-token reasoning ceiling and returning
    # empty). NOT applied at first, since "succeeds" != "no risk". Re-examined:
    # across 3 consecutive diagnostic runs this call consumed 1600/1344/1600
    # reasoning tokens against the SAME 2048 ceiling that produced empty
    # responses elsewhere -- 66-78% of budget, margin, not (yet) a failure.
    # Applied reasoning_effort="minimal" on that basis and re-measured hook
    # completeness before/after (see qa_out/QA_REPORT.md) rather than assuming
    # a lower reasoning budget is free of quality cost for an EXTRACTION task
    # (unlike deck_filler's generative task, this call's job is finding and
    # citing real facts from search snippets -- less "thinking" could
    # plausibly mean thinner or misattributed hooks, not just faster success).
    def _capture(model_name: str, text: str, finish_reason: str, reasoning_tokens: int) -> None:
        if finish_reason == "length" or not text.strip():
            print(f"[PERSONALIZATION WARNING] {firm_name}: {model_name} returned "
                 f"finish_reason={finish_reason!r} reasoning_tokens={reasoning_tokens} "
                 f"response_chars={len(text)} -- possible reasoning-budget exhaustion.")

    parsed, _usage = models.complete_json_retry(
        prompt, temperature=0.3, max_tokens=1000, debug_capture=_capture,
        reasoning_effort="minimal",
    )

    # Belt-and-suspenders: strip any hook whose source slipped through as a
    # linkedin.com URL (HARD RULE 2) rather than trusting the prompt alone.
    for hook_key, src_key in (("hook_1", "hook_1_source"), ("hook_2", "hook_2_source"),
                             ("pain_point_angle", "pain_point_angle_source")):
        if "linkedin.com" in (parsed.get(src_key) or "").lower():
            parsed[hook_key] = ""
            parsed[src_key] = ""

    thin = not (parsed.get("hook_1") or parsed.get("hook_2"))
    parsed["thin_personalization"] = thin
    return parsed
