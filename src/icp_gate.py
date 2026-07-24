"""Phase 2 — ICP gate (§4 Phase 2, prompts/icp_scoring_v2_2.md, §10a, §10f).

Researches the FIRM (not the POC) and applies the binary ICP gate: AUM
$200M-$2B, US/North America, one of the accepted firm types. Three-state
outcome — qualified / disqualified / parked — persisted with a proxy-basis
reason string so the gate's judgment is auditable (HARD RULE 6).

TEST_MODE short-circuits to fixtures (src/testmode.py) — no real Tavily/Exa/
LLM call happens. The real-mode path below exists for production readiness
and follows the AUM proxy taxonomy + source-priority order exactly as
specified in icp_scoring_v2_2.md; it is not exercised by the TEST_MODE
end-to-end run.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from . import config

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


class InfrastructureError(RuntimeError):
    """A quota/rate-limit/auth failure from a research provider — NOT a
    genuine 'couldn't verify' outcome (HARD RULE 15). Callers must distinguish
    this from a normal park and feed it to the circuit breaker."""

    def __init__(self, service: str, message: str):
        self.service = service
        self.message = message
        super().__init__(f"{service}: {message}")


class BudgetExceeded(RuntimeError):
    """Per-firm search/token cap hit (HARD RULE 9) — park as budget_cap_hit."""


@dataclass
class GateResult:
    icp_result: str                    # qualified | disqualified | parked
    icp_reason: str
    aum_tag: Optional[str] = None      # AUM_CONFIRMED | AUM_ESTIMATED | AUM_UNVERIFIED
    park_reason: Optional[str] = None
    park_source: Optional[str] = None  # research | infrastructure
    confidence: float = 0.0
    source_count: int = 0
    search_calls: int = 0
    tokens_used: int = 0
    research_cost_usd: float = 0.0
    research: dict = field(default_factory=dict)  # firm_oneliner, aum_value, domain, ...

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# TEST_MODE path
# ---------------------------------------------------------------------------
def _test_mode_gate(firm_name: str) -> GateResult:
    from . import testmode

    fixture = testmode.firm_research_fixture(firm_name)
    return GateResult(**fixture)


# ---------------------------------------------------------------------------
# Deterministic gate (icp_scoring_v2_2.md Conditions 1-3 + AUM proxy
# taxonomy + asymmetric gate table). QA finding: this used to be delegated
# entirely to one LLM call's own judgment for icp_result/icp_reason — but
# icp_scoring_v2_2.md itself says the floor-proxy rule "is a deterministic
# rule — do not second-guess floor statements", and the asymmetric
# below-$200M table is written as an exact decision table, not something to
# ask an LLM to weigh holistically. The LLM's job is EXTRACTION ONLY (find
# the firm type, region, AUM figure, and which proxy category it came from);
# the qualify/disqualify/park decision and the exact threshold comparisons
# are pure Python so a boundary case (e.g. exactly $200M, or a floor
# statement of exactly $2B) can never be misapplied by model whim.
# ---------------------------------------------------------------------------
def _fmt_usd(n: float) -> str:
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.1f}B".replace(".0B", "B")
    return f"${n/1_000_000:.0f}M"


def apply_gate(firm_type_ok: bool, region_ok: bool, aum_value_usd: Optional[float],
              aum_tag: str, proxy_type: Optional[str] = None,
              proxy_basis: str = "", source_note: str = "") -> tuple[str, str]:
    """Pure function, no I/O. Returns (icp_result, icp_reason).

    `aum_tag`: AUM_CONFIRMED | AUM_ESTIMATED | AUM_UNVERIFIED.
    `proxy_type` (only meaningful when aum_tag == AUM_ESTIMATED): 'floor' for
    floor-proxy LANGUAGE ("over $2B") or proxy rule (b) (invested/deployed
    capital) — true AUM could be higher, so below-$200M PARKS rather than
    disqualifies; 'non_floor' for proxy rules (c)/(d) (portfolio value, fund
    size) — those are closer to actual value, so below-$200M DISQUALIFIES.
    """
    if not firm_type_ok:
        return "disqualified", "Not an accepted firm type (RE/family office/PE-RE/property management), disqualified"
    if not region_ok:
        return "disqualified", "Outside North America, disqualified"

    if aum_tag == "AUM_UNVERIFIED" or aum_value_usd is None:
        return "parked", "AUM not found, no dollar proxies in sources, parked"

    if aum_tag == "AUM_CONFIRMED":
        if aum_value_usd < config.AUM_MIN_USD:
            return "disqualified", f"AUM ~{_fmt_usd(aum_value_usd)}, below $200M floor, disqualified"
        if aum_value_usd > config.AUM_MAX_USD:
            return "disqualified", f"AUM ~{_fmt_usd(aum_value_usd)}, exceeds $2B ceiling, disqualified"
        basis = f" confirmed ({source_note})" if source_note else " confirmed"
        return "qualified", f"AUM ~{_fmt_usd(aum_value_usd)}{basis}, US-based, qualifies"

    if aum_tag == "AUM_ESTIMATED":
        basis = proxy_basis or "proxy"
        if aum_value_usd > config.AUM_MAX_USD:
            return "disqualified", f"DISQUALIFIED (est. {_fmt_usd(aum_value_usd)} — {basis}, exceeds ceiling)"
        if aum_value_usd >= config.AUM_MIN_USD:
            return "qualified", f"QUALIFIED (est. {_fmt_usd(aum_value_usd)} — {basis})"
        # Below $200M: asymmetric by proxy type (icp_scoring_v2_2.md).
        if proxy_type == "floor":
            return "parked", (f"PARKED (proxy {_fmt_usd(aum_value_usd)} below floor — "
                              f"invested capital is a floor, verify manually)")
        return "disqualified", f"DISQUALIFIED (est. {_fmt_usd(aum_value_usd)} — {basis}, below floor)"

    return "parked", f"AUM confidence tag {aum_tag!r} unrecognized, parked for manual review"


# ---------------------------------------------------------------------------
# Real-mode path
# ---------------------------------------------------------------------------
_SEARCH_ANGLES = [
    '"{name}" "assets under management" OR "regulatory assets under management" OR AUM',
    '"{name}" investor page OR "about us" gross asset value OR portfolio value',
    '"{name}" headquarters OR founded OR "real estate" OR multifamily',
    '"{name}" "invested since" OR "capital deployed" OR "fund closed" OR "raised"',
]


def _run_research(firm_name: str, progress: Progress) -> tuple[list[dict], int]:
    """Layered Tavily search (Exa as a secondary angle source). Raises
    InfrastructureError on a hard provider error; caps at MAX_SEARCH_CALLS_PER_FIRM."""
    from lib_snapshot.researcher import search_client
    from lib_snapshot.researcher.search_client import SearchAPIError

    angles = [a.format(name=firm_name) for a in _SEARCH_ANGLES]
    calls = 0
    results: list[dict] = []
    for angle in angles:
        if calls >= config.MAX_SEARCH_CALLS_PER_FIRM:
            break
        try:
            results.extend(search_client.search(angle, max_results=6))
            calls += 1
        except SearchAPIError as e:
            raise InfrastructureError("Tavily", e.message) from e
    return results, calls


_EXTRACT_PROMPT_TMPL = """{icp_prompt}

---
EXTRACT ONLY — do not decide QUALIFIED/DISQUALIFIED/PARKED yourself. A
deterministic function applies the gate table above to whatever you extract
here; your job is finding and correctly categorizing the facts, not weighing
the threshold. Read the research snippets below for this firm and return a
SINGLE JSON object, no prose, no markdown fence:
{{
  "firm_type_ok": <true if the firm is RE owner/operator/developer/REIT,
                   family office with a RE allocation, RE-focused or
                   RE-holding PE, or property management — else false>,
  "region_ok": <true if headquartered or primarily operating in the US,
                Canada, or Mexico — else false>,
  "aum_tag": "AUM_CONFIRMED|AUM_ESTIMATED|AUM_UNVERIFIED",
  "aum_value_usd": <the AUM or proxy number as a plain number, or null if
                    aum_tag is AUM_UNVERIFIED — NEVER leave this null when a
                    proxy rule (a-d) or floor-proxy language applies>,
  "proxy_type": "floor" if this figure came from floor-proxy LANGUAGE
                ("over $X", "north of $X", "at least $X") or from proxy rule
                (b) ("$X invested/deployed/in acquisitions") — the true AUM
                could be higher; "non_floor" if from proxy rule (c) or (d)
                (portfolio value, fund size) — these are closer to actual
                value; empty string if aum_tag is not AUM_ESTIMATED,
  "proxy_basis": "e.g. 'invested capital proxy', 'fund size proxy',
                 'regulatory AUM (Form ADV)', or empty string if AUM_CONFIRMED
                 from a direct source statement",
  "source_note": "e.g. 'firm website' or 'SEC Form ADV' — empty if not applicable",
  "aum_display": "e.g. $1.1B",
  "aum_asof": "e.g. as of Dec 31, 2025 or empty string",
  "firm_oneliner": "<=22 words, asset class + strategy",
  "geography": "e.g. US secondary markets",
  "capital_stack": "e.g. LP, GP, Co-GP, Preferred Equity or empty string",
  "founded_year": "e.g. 2008 or empty string",
  "domain": "firm website domain, e.g. kingbird.com, or empty string",
  "recent_event": "one dated fact or empty string",
  "sources": ["url", ...]
}}

Firm: {name}
Research snippets:
{snippets}
"""


def _extract(firm_name: str, results: list[dict]) -> tuple[dict, int, float]:
    from lib_snapshot import models

    icp_prompt = config.load_prompt(config.ICP_PROMPT_NAME)
    snippets = "\n".join(
        f"- [{r.get('url')}] {r.get('title', '')}: {(r.get('content') or '')[:500]}"
        for r in results[:20]
    ) or "(no results found)"
    prompt = _EXTRACT_PROMPT_TMPL.format(icp_prompt=icp_prompt, name=firm_name, snippets=snippets)
    parsed, usage = models.complete_json_retry(prompt, temperature=0.1,
                                               max_tokens=1500)
    return parsed, usage.total_tokens, usage.cost_usd


def research_and_gate(firm_name: str, progress: Progress = _noop) -> GateResult:
    """Phase 2 entry point. TEST_MODE returns a canned fixture; otherwise
    researches the firm and applies the binary gate via the LLM, following
    icp_scoring_v2_2.md's proxy taxonomy and asymmetric gate logic verbatim."""
    if config.TEST_MODE:
        return _test_mode_gate(firm_name)

    try:
        results, search_calls = _run_research(firm_name, progress)
    except InfrastructureError:
        raise

    if search_calls >= config.MAX_SEARCH_CALLS_PER_FIRM and not results:
        return GateResult(icp_result="parked", icp_reason="budget_cap_hit before any result",
                          park_reason="budget_cap_hit", park_source="research",
                          search_calls=search_calls)

    parsed, tokens, cost = _extract(firm_name, results)
    if tokens > config.MAX_LLM_TOKENS_PER_FIRM:
        progress(f"{firm_name}: token budget exceeded ({tokens}); parking.")
        return GateResult(icp_result="parked", icp_reason="budget_cap_hit (LLM tokens)",
                          park_reason="budget_cap_hit", park_source="research",
                          search_calls=search_calls, tokens_used=tokens,
                          research_cost_usd=cost)

    aum_tag = parsed.get("aum_tag")
    icp_result, icp_reason = apply_gate(
        firm_type_ok=bool(parsed.get("firm_type_ok", False)),
        region_ok=bool(parsed.get("region_ok", False)),
        aum_value_usd=parsed.get("aum_value_usd"),
        aum_tag=aum_tag,
        proxy_type=parsed.get("proxy_type") or None,
        proxy_basis=parsed.get("proxy_basis", ""),
        source_note=parsed.get("source_note", ""),
    )
    park_reason = None
    if icp_result == "parked":
        park_reason = "aum_unverified" if aum_tag == "AUM_UNVERIFIED" else "gate_parked"

    research = {
        "firm_oneliner": parsed.get("firm_oneliner", ""),
        "aum_value_usd": parsed.get("aum_value_usd"),
        "aum_display": parsed.get("aum_display", ""),
        "aum_asof": parsed.get("aum_asof", ""),
        "aum_tag": aum_tag,
        "geography": parsed.get("geography", ""),
        "capital_stack": parsed.get("capital_stack", ""),
        "founded_year": parsed.get("founded_year", ""),
        "domain": parsed.get("domain", ""),
        "recent_event": parsed.get("recent_event", ""),
        "sources": parsed.get("sources") or [],
    }
    return GateResult(
        icp_result=icp_result,
        icp_reason=icp_reason,
        aum_tag=aum_tag,
        park_reason=park_reason,
        park_source="research" if icp_result == "parked" else None,
        confidence=0.8 if aum_tag == "AUM_CONFIRMED" else (0.5 if aum_tag == "AUM_ESTIMATED" else 0.2),
        source_count=len(research["sources"]),
        search_calls=search_calls,
        tokens_used=tokens,
        research_cost_usd=cost,
        research=research,
    )
