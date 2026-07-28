"""TEST_MODE fixtures for every external call Workflow C makes: LLM, Tavily,
Exa, Apollo, Gmail. When config.TEST_MODE is true, every phase module short-
circuits to these fixtures instead of touching the network — see the
TEST_MODE check at the top of each phase's public entry point.

Fixture data here is SYNTHETIC, built for the §7 step 12 end-to-end scenario:
Kingbird Investment Management (qualify -> draft), Blackstone (disqualify,
AUM far over ceiling), Aliya Capital Partners (park, AUM unverifiable), Singh
Capital Partners (referrer's own firm, excluded before the gate), and any
firm not in the connections file (no_connection).

Note on Kingbird's AUM: the provided master deck (templates/Pitch_Master.pptx)
shows a real prior Kingbird engagement with AUM $4.4B — which would DISQUALIFY
under Workflow C's $200M-$2B band. That master is reused here only as the PPT
*layout/locator* reference (§9); this fixture deliberately uses a
within-band AUM (~$1.1B) so the qualify -> draft path is actually exercised
end-to-end in TEST_MODE, rather than reproducing the master's literal number.
"""
from __future__ import annotations

import copy
from typing import Optional

from .matching.normalize import normalize as _normalize

# ---------------------------------------------------------------------------
# Phase 2 — ICP gate / firm research fixtures
# ---------------------------------------------------------------------------
_FIRM_RESEARCH: dict[str, dict] = {
    "kingbird investment management": {
        "icp_result": "qualified",
        "icp_reason": "RE investment manager, workforce/market-rate multifamily, "
                      "AUM ~$1.1B confirmed (firm investor page), US-based (TX & Southeast), qualifies",
        "aum_tag": "AUM_CONFIRMED",
        "confidence": 0.85,
        "source_count": 2,
        "search_calls": 3,
        "tokens_used": 1800,
        "research_cost_usd": 0.004,
        "research": {
            "firm_oneliner": "A vertically integrated real estate investment manager "
                             "specializing in workforce and market-rate multifamily "
                             "housing across US secondary markets.",
            "aum_value_usd": 1_100_000_000,
            "aum_display": "$1.1B",
            "disclosed_figure": "$1.1B",
            "disclosed_label": "assets under management",
            "aum_asof": "as of Dec 31, 2025",
            "aum_tag": "AUM_CONFIRMED",
            "geography": "US secondary markets, concentrated in Texas and the Southeast",
            "capital_stack": "LP, GP, Co-GP, Preferred Equity",
            "founded_year": "2008",
            "domain": "kingbirdinvestments.example",
            "recent_event": "Appointed a Director of Capital Markets in 2019 to expand investment services.",
            "sources": [
                "https://kingbirdinvestments.example/about",
                "https://kingbirdinvestments.example/investor-relations",
            ],
        },
    },
    "blackstone": {
        "icp_result": "disqualified",
        "icp_reason": "AUM ~$1.1T (confirmed, public filings), far exceeds $2B ceiling, disqualified",
        "aum_tag": "AUM_CONFIRMED",
        "confidence": 0.95,
        "source_count": 3,
        "search_calls": 2,
        "tokens_used": 900,
        "research_cost_usd": 0.002,
        "research": {
            "firm_oneliner": "A global alternative asset manager spanning real estate, "
                             "private equity, credit, and hedge fund solutions.",
            "aum_value_usd": 1_100_000_000_000,
            "aum_display": "$1.1T",
            "disclosed_figure": "$1.1T",
            "disclosed_label": "assets under management",
            "aum_tag": "AUM_CONFIRMED",
            "geography": "Global, headquartered in the United States",
            "capital_stack": "",
            "founded_year": "1985",
            "domain": "blackstone.example",
            "recent_event": "",
            "sources": ["https://blackstone.example/about"],
        },
    },
    "aliya capital partners": {
        "icp_result": "parked",
        "icp_reason": "AUM not found, no dollar proxies in sources, parked",
        "aum_tag": "AUM_UNVERIFIED",
        "park_reason": "aum_unverified",
        "park_source": "research",
        "confidence": 0.2,
        "source_count": 1,
        "search_calls": 4,
        "tokens_used": 1200,
        "research_cost_usd": 0.003,
        "research": {
            "firm_oneliner": "A real estate investment firm focused on multifamily acquisitions.",
            "aum_value_usd": None,
            "aum_display": "",
            "disclosed_figure": "",
            "disclosed_label": "",
            "aum_tag": "AUM_UNVERIFIED",
            "geography": "United States",
            "capital_stack": "",
            "founded_year": "",
            "domain": "",
            "recent_event": "",
            "sources": ["https://aliyacapital.example/about"],
        },
    },
}

_DEFAULT_PARKED_RESEARCH = {
    "icp_result": "parked",
    "icp_reason": "AUM not found, no dollar proxies in sources, parked",
    "aum_tag": "AUM_UNVERIFIED",
    "park_reason": "aum_unverified",
    "park_source": "research",
    "confidence": 0.1,
    "source_count": 0,
    "search_calls": 1,
    "tokens_used": 200,
    "research_cost_usd": 0.001,
    "research": {
        "firm_oneliner": "", "aum_value_usd": None, "aum_display": "",
        "disclosed_figure": "", "disclosed_label": "",
        "aum_tag": "AUM_UNVERIFIED", "geography": "", "capital_stack": "",
        "founded_year": "", "domain": "", "recent_event": "", "sources": [],
    },
}


def firm_research_fixture(firm_name: str) -> dict:
    key = _normalize(firm_name)
    fixture = _FIRM_RESEARCH.get(key, _DEFAULT_PARKED_RESEARCH)
    return copy.deepcopy(fixture)


# ---------------------------------------------------------------------------
# Phase 3 — Apollo email enrichment fixtures. Keyed by company only (not the
# full POC name string): real LinkedIn exports carry messy name fields (e.g.
# "Lei" / "Farrand, CFP®" with a credential suffix baked into the Last Name
# column), so matching on company is the robust, realistic TEST_MODE lookup —
# the point is exercising "Apollo was called and returned an email", not
# reproducing exact-string POC matching.
# ---------------------------------------------------------------------------
_APOLLO_EMAILS: dict[str, tuple[str, str]] = {
    "kingbird investment management": ("lei.farrand@kingbirdinvestments.example", "verified"),
}


def apollo_email_fixture(poc_name: str, company: str) -> Optional[tuple[str, str]]:
    return _APOLLO_EMAILS.get(_normalize(company))


# ---------------------------------------------------------------------------
# Phase 4 — personalization research fixtures
# ---------------------------------------------------------------------------
_PERSONALIZATION: dict[str, dict] = {
    "kingbird investment management": {
        "hook_1": "You run roughly 15,000 workforce and market-rate multifamily units "
                  "across Texas and the Southeast, with LP, GP, and co-GP capital on every deal.",
        "hook_1_source": "https://kingbirdinvestments.example/portfolio",
        "hook_2": "Kingbird appointed a Director of Capital Markets in 2019 to expand its investment services.",
        "hook_2_source": "https://kingbirdinvestments.example/news/2019-capital-markets",
        "firm_niche": "workforce and market-rate multifamily housing, US secondary markets",
        "recent_event": "Director of Capital Markets appointment (2019)",
        "pain_point_angle": "multi-entity LP/GP/co-GP structures across a geographically "
                            "dispersed multifamily portfolio",
        "pain_point_angle_source": "https://kingbirdinvestments.example/portfolio",
        "service_fit": "property accounting and fund accounting for a multi-entity capital stack",
        "thin_personalization": False,
    },
}


def personalization_fixture(firm_name: str) -> dict:
    key = _normalize(firm_name)
    if key in _PERSONALIZATION:
        return copy.deepcopy(_PERSONALIZATION[key])
    return {
        "hook_1": "", "hook_1_source": "", "hook_2": "", "hook_2_source": "",
        "firm_niche": "", "recent_event": "", "pain_point_angle": "",
        "pain_point_angle_source": "", "service_fit": "", "thin_personalization": True,
    }


# ---------------------------------------------------------------------------
# Phase 5a — email generation fixtures (Block 2 / Block 4)
# ---------------------------------------------------------------------------
_EMAIL_BLOCKS: dict[str, dict] = {
    "kingbird investment management": {
        "block_2": "You run roughly 15,000 workforce and market-rate multifamily units "
                  "across Texas and the Southeast, with LP, GP, and co-GP capital on every deal.",
        "block_4": "With LP, GP, and co-GP capital on every deal across a dispersed "
                  "multifamily portfolio, your controller is either paying a fund admin "
                  "who does not know your waterfalls, or building it in house at month end. "
                  "That inertia costs you every close.",
    },
}


def email_blocks_fixture(firm_name: str) -> Optional[dict]:
    key = _normalize(firm_name)
    return copy.deepcopy(_EMAIL_BLOCKS.get(key))


# ---------------------------------------------------------------------------
# Phase 5b — deck content fixtures (profile bullets + fit paragraphs)
# ---------------------------------------------------------------------------
_DECK_CONTENT: dict[str, dict] = {
    "kingbird investment management": {
        "profile_bullets": [
            "Vertically integrated multifamily investment manager operating since 2008.",
            "Confirmed AUM of approximately $1.1 billion across workforce and market-rate housing.",
            "Concentrated portfolio across Texas and the Southeast United States.",
            "Uses LP, GP, and co-GP structures alongside preferred equity on individual deals.",
            "Appointed a dedicated Director of Capital Markets in 2019.",
        ],
        "fit_paragraphs": {
            "property_accounting": (
                "With a multifamily portfolio spread across Texas and the Southeast, Kingbird "
                "needs property-level books that stay current across many entities without "
                "adding headcount at every close."
            ),
            "fund_structures": (
                "Kingbird's LP, GP, and co-GP structures on each deal call for accurate capital "
                "allocations and waterfall tracking across the full stack."
            ),
            "investor_reporting": (
                "A confirmed base of roughly $1.1B under management means Kingbird's investors "
                "expect reporting that holds up to institutional scrutiny every quarter."
            ),
            "underwriting_acquisitions": (
                "Continued acquisitions across secondary markets keep Kingbird's underwriting "
                "team busy modeling new deals against the existing portfolio."
            ),
            "capital_markets": (
                "Since standing up a dedicated Director of Capital Markets role in 2019, "
                "Kingbird has leaned further into debt placement and refinancing work that a "
                "dedicated analyst can support directly."
            ),
        },
        "confidence": "high",
        "relevant_roles": ["property accountants", "fund accountants", "capital markets analysts"],
    },
}


def deck_content_fixture(firm_name: str) -> Optional[dict]:
    key = _normalize(firm_name)
    v = _DECK_CONTENT.get(key)
    return copy.deepcopy(v) if v else None


# ---------------------------------------------------------------------------
# Gmail fake service — records calls, never sends, returns fake draft ids
# ---------------------------------------------------------------------------
class FakeGmailService:
    """Mimics the googleapiclient `service.users().drafts().create(...)`
    surface. Records every call so tests/grep can confirm .send() is never
    reachable through this object at all (it doesn't even implement it)."""

    def __init__(self):
        self.created_drafts: list[dict] = []
        self._next_id = 1

    def users(self):
        return self

    def drafts(self):
        return self

    def create(self, userId: str, body: dict):
        draft_id = f"TESTMODE-DRAFT-{self._next_id}"
        self._next_id += 1
        record = {"userId": userId, "body": body, "id": draft_id}
        self.created_drafts.append(record)
        return _Executable({"id": draft_id, "message": body.get("message", {})})


class _Executable:
    def __init__(self, result: dict):
        self._result = result

    def execute(self):
        return self._result
