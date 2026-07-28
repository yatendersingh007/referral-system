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

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
from rapidfuzz import fuzz

from . import config
# FIX 28: reuse the Phase 1 firm matcher's own tokenizer/normalizer (rapidfuzz
# is already a dependency there too) for own_domain name-similarity scoring,
# rather than writing a third, independent name-cleaning implementation.
from .matching import normalize as name_normalize

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
    park_source: Optional[str] = None  # research | infrastructure | match
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


# QA finding (real-mode dry run, Kingbird): GAV (Gross Asset Value) is a
# GROSS figure — it includes debt/leverage — so it is NEVER a 1:1 stand-in
# for AUM and must never be gated against the $2B ceiling as-is. A large,
# leveraged real estate firm's GAV routinely exceeds $2B while its actual
# equity/AUM sits well within the $200M-$2B band. 60% is the midpoint of
# typical 50-70% real estate leverage (icp_scoring_v2_2.md proxy rule e).
GAV_LEVERAGE_HAIRCUT = 0.60

# Tertiary proxy (icp_scoring_v2_2.md "Tertiary proxy" section): when
# exhaustive research finds NO dollar figure of any kind but a unit count
# is disclosed, estimate a conservative GROSS value per unit, then apply
# the SAME leverage haircut as the GAV proxy. $120,000/unit is a
# deliberately conservative baseline for standard workforce/market-rate
# multifamily (below typical national per-unit multifamily pricing, which
# often runs well above this for institutional-grade product) — a
# judgment call flagged here since no exact figure was specified; adjust
# if a better benchmark becomes available.
PER_UNIT_GROSS_ESTIMATE_USD = 120_000

# FIX 20 (real-mode In-Rel Properties run): a SEPARATE per-square-foot rate,
# never the same constant as PER_UNIT_GROSS_ESTIMATE_USD above -- that one is
# calibrated for a per-APARTMENT-UNIT gross value; naively reusing it for a
# square-footage count would produce a wildly wrong estimate (3,000,000 "units"
# at $120,000/unit = $360B, an absurd overestimate for a commercial
# office/retail portfolio). $200/sq ft is a deliberately conservative
# baseline for a mixed commercial (office/retail) portfolio -- below typical
# prime-market office pricing, in line with many secondary-market
# office/retail valuations -- a judgment call flagged here since no exact
# figure was specified, same caveat as the per-unit constant above; adjust
# if a better benchmark becomes available.
PER_SQFT_GROSS_ESTIMATE_USD = 200

# FIX 21 (real-mode In-Rel Properties run): a units/sqft-derived estimate is
# MANUFACTURED by this codebase from a hardcoded rate, not a figure the firm
# itself disclosed -- unlike a stated floor (the firm's OWN claim about a
# lower bound), our own rate assumption could easily be off enough in
# EITHER direction to have mis-classified a genuinely qualifying firm.
# Arithmetic that motivated this: PER_SQFT_GROSS_ESTIMATE_USD=200 with a 60%
# haircut is $80/sq ft net, so the $200M floor sits at 2.5M sq ft -- a firm
# at 2M sq ft (a plausible, unremarkable commercial owner-operator) computes
# to $160M and would have been DISQUALIFIED outright on a number this
# codebase invented, not one the firm stated. Below the floor: PARK
# unconditionally (see the floor-proxy branch below, same reasoning).
# Above the ceiling: an estimate merely somewhat over $2B could easily be a
# rate-assumption artifact, not genuine oversized AUM -- PARK unless the
# estimate clears the ceiling by such a wide margin that no plausible rate
# error would bring it back under. 2x the ceiling is a judgment call (no
# authoritative benchmark specified, same caveat as the rate constants
# above) -- adjust if a better threshold becomes available.
DERIVED_PROXY_CEILING_MULTIPLIER = 2.0


def _tertiary_proxy_conversion(unit_count: Optional[int],
                               sqft_count: Optional[float]) -> tuple[Optional[float], str]:
    """Converts a disclosed unit count or square-footage figure to an
    estimated equity/AUM value + a human-readable detail string describing
    the exact rate/haircut used, or (None, "") if neither quantity is
    present. unit_count takes priority if both are somehow present (rare)
    -- the pre-existing, more-specific residential path. Used ONLY by
    `apply_gate()`'s internal gating decision (FIX 22: the returned dollar
    value is `gate_value_usd`-internal-only material -- it must NEVER be
    used for anything a prospect reads; see `_disclosed_figure_and_label()`
    below for what actually reaches the deck/email for this proxy type)."""
    if unit_count:
        gross_estimate = unit_count * PER_UNIT_GROSS_ESTIMATE_USD
        aum_value_usd = gross_estimate * (1 - GAV_LEVERAGE_HAIRCUT)
        detail = (f"{unit_count:,} units x ~${PER_UNIT_GROSS_ESTIMATE_USD:,.0f}/unit "
                 f"conservative estimate, {int(GAV_LEVERAGE_HAIRCUT * 100)}% leverage haircut")
        return aum_value_usd, detail
    if sqft_count:
        gross_estimate = sqft_count * PER_SQFT_GROSS_ESTIMATE_USD
        aum_value_usd = gross_estimate * (1 - GAV_LEVERAGE_HAIRCUT)
        detail = (f"{sqft_count:,.0f} sq ft x ~${PER_SQFT_GROSS_ESTIMATE_USD:,.0f}/sq ft "
                 f"conservative estimate, {int(GAV_LEVERAGE_HAIRCUT * 100)}% leverage haircut")
        return aum_value_usd, detail
    return None, ""


# FIX 24 (real-mode In-Rel Properties run): "over three million square
# feet" was extracted as unit_count/sqft_count=3_000_000 and rendered as
# "3,000,000 sq ft" -- correct on the number, but two things the source
# text never claimed crept in: (1) the "over" qualifier was dropped, and
# (2) the bare number implies an EXACTNESS ("exactly 3,000,000", not "at
# least 3,000,000" or "roughly 3,000,000") the firm never asserted. Mapped
# to a canonical PREFIX word (not the model's own possibly-inconsistent
# phrasing) so disclosed_figure is deterministic and groundable against
# whichever exact word the model reported.
_QUANTITY_QUALIFIER_PREFIXES = {
    "over": "over ",
    "more_than": "more than ",
    "more than": "more than ",
    "at_least": "at least ",
    "at least": "at least ",
    "north_of": "north of ",
    "north of": "north of ",
    "approximately": "approximately ",
    "about": "about ",
    "roughly": "roughly ",
    "~": "approximately ",
}


def _normalize_quantity_qualifier(raw: Optional[str]) -> str:
    key = (raw or "").strip().lower()
    return _QUANTITY_QUALIFIER_PREFIXES.get(key, "")


# FIX 22 (real-mode In-Rel Properties run): "aum_value_usd" was being asked
# to do two incompatible jobs -- drive the band comparison AND be the source
# of what the prospect reads. Decision made: prospect-facing artifacts show
# ONLY what the firm itself published, verbatim, with the firm's own label.
# Haircuts and derivations (GAV's 60% LTV haircut, the units/sqft tertiary
# proxy's per-unit/per-sq-ft rate) inform the GATING DECISION ONLY
# (gate_value_usd, see apply_gate() above) and must never leave that
# internal path. This function computes the SEPARATE, verbatim pair that
# DOES reach deck_filler.py/email_drafter.py.
def _disclosed_figure_and_label(parsed: dict) -> tuple[str, str]:
    """Returns (disclosed_figure, disclosed_label) -- exactly what the firm
    published, no arithmetic. For every proxy type EXCEPT the units/sqft
    tertiary proxy, this is the model's own `disclosed_figure`/
    `disclosed_label` extraction (there is no clean structured field to
    derive it from programmatically -- e.g. "$4.4B"/"gross asset value" or
    "$254M"/"capital invested" is exactly what the model read from the
    source text). For the units/sqft tertiary proxy specifically,
    `disclosed_figure` is instead built DETERMINISTICALLY from
    `unit_count`/`sqft_count` -- the SAME structured quantity used for
    gating (FIX 20/21) -- rather than trusted to the model's own free-text
    re-transcription of the same fact, which risks drifting from the
    number actually used for the gate decision. `disclosed_label` for this
    case is still the model's own source-grounded description (e.g. "of
    office and retail space under management") with a generic, honest
    (never fabricated) fallback if the model left it empty.

    FIX 24 (real-mode In-Rel Properties run): if the source stated an
    approximator ("over", "approximately", ...) alongside the quantity
    (`quantity_qualifier`), it's prepended here -- "over 3,000,000 sq ft",
    never the bare "3,000,000 sq ft" that implies more precision than the
    firm actually claimed."""
    proxy_type = (parsed.get("proxy_type") or "").strip().lower()
    if proxy_type == "units":
        unit_count = parsed.get("unit_count")
        sqft_count = parsed.get("sqft_count")
        label = (parsed.get("disclosed_label") or "").strip()
        qualifier = _normalize_quantity_qualifier(parsed.get("quantity_qualifier"))
        if unit_count:
            return f"{qualifier}{unit_count:,} units", (label or "units under management")
        if sqft_count:
            return f"{qualifier}{sqft_count:,.0f} sq ft", (label or "square feet under management")
        return "", ""
    return (parsed.get("disclosed_figure") or "").strip(), (parsed.get("disclosed_label") or "").strip()


def apply_gate(firm_type_ok: bool, region_ok: bool, gate_value_usd: Optional[float],
              aum_tag: str, proxy_type: Optional[str] = None,
              proxy_basis: str = "", source_note: str = "",
              unit_count: Optional[int] = None,
              sqft_count: Optional[float] = None) -> tuple[str, str]:
    """Pure function, no I/O. Returns (icp_result, icp_reason).

    FIX 22 (real-mode In-Rel Properties run): `gate_value_usd` (renamed from
    `aum_value_usd`) is EXPLICITLY internal-only -- the number this function
    compares against the $200M-$2B band, which may be haircut (GAV) or
    entirely manufactured (units/sqft tertiary proxy) from what was actually
    disclosed. It must NEVER be confused with, or reach, what a prospect
    reads -- that's `disclosed_figure`/`disclosed_label` (verbatim, no
    arithmetic), computed separately by `research_and_gate()` and never
    passed through this function at all. This function's caller passes the
    RAW disclosed figure (or None) as `gate_value_usd`'s starting point;
    this function may then convert it internally (GAV haircut, units/sqft
    derivation) purely for the gating decision.

    `aum_tag`: AUM_CONFIRMED | AUM_ESTIMATED | AUM_UNVERIFIED.
    `proxy_type` (only meaningful when aum_tag == AUM_ESTIMATED): 'floor' for
    floor-proxy LANGUAGE ONLY ("over $2B", "north of $X", "at least $X" — an
    EXPLICIT stated lower bound) — true AUM could be higher, so below-$200M
    PARKS rather than disqualifies; 'non_floor' for proxy rule (b) (invested/
    deployed capital — a specific, dated cumulative figure, NOT itself phrased
    as a lower bound, despite the real possibility that current AUM differs
    from it either way) or proxy rules (c)/(d) (portfolio value, fund size) —
    all three are closer to actual value than a stated floor, so below-$200M
    DISQUALIFIES; QA finding (real-mode Kairos run): proxy rule (b) used to be
    grouped with floor-proxy LANGUAGE here, so a firm invested-capital-proxied
    below $200M parked instead of disqualifying — corrected in the extraction
    prompt (icp_scoring_v2_2.md + _EXTRACT_PROMPT_TMPL below), NOT in this
    function's own branching, which is unchanged;
    'gav' for a disclosed Gross Asset Value (proxy rule e) — `gate_value_usd`
    must be the RAW GAV number (never pre-converted by the caller); this
    function applies the leverage haircut before any threshold comparison,
    entirely internally -- the raw GAV is what a prospect reads, never this
    haircut result;
    'units' for the last-resort unit-count/sq-ft tertiary proxy — pass
    `gate_value_usd=None` and EITHER `unit_count` (a disclosed
    residential/multifamily unit count) OR `sqft_count` (a disclosed square-
    footage figure), never both interpreted as the same thing — this
    function estimates a converted equity figure using the rate specific to
    whichever quantity was actually reported (FIX 20, real-mode In-Rel
    Properties run: "owns and manages over three million square feet" was
    previously written straight into the gating field as if it were a dollar
    figure — $3,000,000 — disqualifying a firm plausibly well inside the
    $200M-$2B band on a fabricated number; a square-footage/unit-count/
    property-count/employee-count figure is NEVER a dollar amount).
    FIX 21: unlike proxy rules (b)/(c)/(d) (a real, specific figure someone
    actually stated), a units/sqft estimate is a number THIS CODEBASE
    manufactured from a hardcoded rate — outside the clean $200M-$2B band,
    it PARKS for manual verification rather than disqualifying (below the
    floor, unconditionally; above the ceiling, unless the estimate clears
    it by DERIVED_PROXY_CEILING_MULTIPLIER, in which case it's confidently
    too big regardless of rate uncertainty and still disqualifies).
    """
    if not firm_type_ok:
        return "disqualified", "Not an accepted firm type (RE/family office/PE-RE/property management), disqualified"
    if not region_ok:
        return "disqualified", "Outside North America, disqualified"

    # Tertiary proxy: no dollar figure at all, only a unit count or square-
    # footage figure on file. Must run BEFORE the "no value -> park" check
    # below, since this is exactly the case where gate_value_usd starts out
    # None. `derived_detail` (FIX 21) is kept SEPARATE from `proxy_basis` --
    # it always carries the actual rate/haircut mechanics for the PARK
    # message below regardless of whether a caller supplied its own
    # (possibly terse) proxy_basis, since a human asked to "verify manually"
    # needs the mechanics, not just a label.
    derived_detail = ""
    if aum_tag == "AUM_ESTIMATED" and proxy_type == "units" and gate_value_usd is None:
        converted, derived_detail = _tertiary_proxy_conversion(unit_count, sqft_count)
        if converted is not None:
            gate_value_usd = converted
            if not proxy_basis:
                proxy_basis = derived_detail

    if aum_tag == "AUM_UNVERIFIED" or gate_value_usd is None:
        return "parked", "AUM not found, no dollar proxies in sources, parked"

    if aum_tag == "AUM_CONFIRMED":
        if gate_value_usd < config.AUM_MIN_USD:
            return "disqualified", f"AUM ~{_fmt_usd(gate_value_usd)}, below $200M floor, disqualified"
        if gate_value_usd > config.AUM_MAX_USD:
            return "disqualified", f"AUM ~{_fmt_usd(gate_value_usd)}, exceeds $2B ceiling, disqualified"
        basis = f" confirmed ({source_note})" if source_note else " confirmed"
        return "qualified", f"AUM ~{_fmt_usd(gate_value_usd)}{basis}, US-based, qualifies"

    if aum_tag == "AUM_ESTIMATED":
        # GAV (proxy rule e): GROSS figure, includes debt/leverage. Convert
        # to an estimated equity/AUM proxy BEFORE any threshold comparison
        # — raw GAV must never be compared to the ceiling directly (see
        # module docstring on GAV_LEVERAGE_HAIRCUT above). This haircut
        # value is INTERNAL to the gating decision only (FIX 22) -- the
        # prospect always reads the RAW, undiscounted GAV figure instead.
        if proxy_type == "gav":
            gate_value_usd = gate_value_usd * (1 - GAV_LEVERAGE_HAIRCUT)
            if not proxy_basis:
                proxy_basis = f"GAV-derived equity proxy ({int(GAV_LEVERAGE_HAIRCUT * 100)}% LTV)"
        basis = proxy_basis or "proxy"

        # FIX 21 (real-mode In-Rel Properties run): a units/sqft-derived
        # estimate is a number THIS CODEBASE manufactured from a hardcoded
        # rate (PER_UNIT_GROSS_ESTIMATE_USD / PER_SQFT_GROSS_ESTIMATE_USD),
        # never one the firm disclosed -- treated as its OWN, more lenient
        # asymmetric case, separate from the generic disqualify-outside-band
        # logic below. Below the floor: PARK unconditionally, same branch
        # and same reasoning as the floor-proxy case just below (the
        # manufactured figure is a lower-bound GUESS; true AUM could be far
        # higher) -- never permanently delete a firm on a number the system
        # computed, not one the firm stated. Above the ceiling: PARK too,
        # UNLESS the estimate clears the ceiling by DERIVED_PROXY_CEILING_
        # MULTIPLIER -- at that point no plausible rate error would bring it
        # back under, so it's confidently, not just nominally, too big.
        if proxy_type == "units":
            if gate_value_usd > config.AUM_MAX_USD * DERIVED_PROXY_CEILING_MULTIPLIER:
                return "disqualified", (f"DISQUALIFIED (est. {_fmt_usd(gate_value_usd)} — {basis}, "
                                        f"exceeds ceiling by a wide margin)")
            if config.AUM_MIN_USD <= gate_value_usd <= config.AUM_MAX_USD:
                return "qualified", f"QUALIFIED (est. {_fmt_usd(gate_value_usd)} — {basis})"
            detail = derived_detail or basis
            return "parked", (f"PARKED (est. {_fmt_usd(gate_value_usd)} from {detail} — "
                              f"derived, not disclosed; verify manually)")

        if gate_value_usd > config.AUM_MAX_USD:
            return "disqualified", f"DISQUALIFIED (est. {_fmt_usd(gate_value_usd)} — {basis}, exceeds ceiling)"
        if gate_value_usd >= config.AUM_MIN_USD:
            return "qualified", f"QUALIFIED (est. {_fmt_usd(gate_value_usd)} — {basis})"
        # Below $200M: asymmetric by proxy type (icp_scoring_v2_2.md). Branching
        # unchanged by the FIX 8 reclassification below -- only the message
        # text changed (was hardcoded to "invested capital", which is no
        # longer what reaches this branch: proxy rule (b) invested-capital
        # figures are now classified "non_floor" upstream in the extraction
        # prompt, so only genuine floor-proxy LANGUAGE reaches "floor" here).
        if proxy_type == "floor":
            return "parked", (f"PARKED (proxy {_fmt_usd(gate_value_usd)} below floor — "
                              f"stated as a floor, true AUM could be higher, verify manually)")
        return "disqualified", f"DISQUALIFIED (est. {_fmt_usd(gate_value_usd)} — {basis}, below floor)"

    return "parked", f"AUM confidence tag {aum_tag!r} unrecognized, parked for manual review"


# ---------------------------------------------------------------------------
# Real-mode path
# ---------------------------------------------------------------------------
# QA finding (real-mode dry run, Kingbird): the original 4 angles skewed
# toward the firm's OWN domain — an "about us"/investor-page-style query
# naturally ranks a firm's own site highest, so a real run against Kingbird
# surfaced 4 kingbird.com/LinkedIn-company results and nothing else, missing
# Kingbird's own publicly disclosed ~$4.4B GAV / 21,794 units / 1.8M sq ft.
# Widened to explicitly target the size/AUM metrics real estate firms
# actually disclose (AUM, GAV, portfolio value, unit counts, square footage,
# scale language like "billion"), not just firm-site "about us" content.
_CORE_ANGLES = [
    '"{name}" "assets under management" OR AUM OR "regulatory assets under management"',
    '"{name}" "gross asset value" OR GAV',
    '"{name}" "portfolio value" OR assets OR billion',
    '"{name}" units multifamily OR "square feet" OR "square footage"',
    '"{name}" headquarters OR founded OR "real estate" OR multifamily',
    '"{name}" "invested since" OR "capital deployed" OR "fund closed" OR "raised"',
]

# Escalation: only runs when the core pass above finds no size/dollar
# signal at all (checked via _has_size_signal, a cheap regex pre-check —
# not the LLM's own judgment, so escalating never costs an extra LLM call).
# Leans harder on open-web / third-party phrasing (industry press, deal
# announcements) that a firm's own "about us" page won't contain; one angle
# explicitly uses Tavily's news topic to bias toward press/journalism
# domains rather than corporate sites.
_ESCALATION_ANGLES = [
    ('"{name}" billion real estate', "general"),
    ('"{name}" GAV OR "gross asset value" OR "portfolio value"', "general"),
    ('"{name}" acquisition OR acquires OR closes OR announces OR raised', "news"),
    ('"{name}" assets under management OR "gross asset value"', "news"),
]

_SIZE_SIGNAL_RE = re.compile(
    r'\$\s?[\d][\d,.]*\s*(?:million|billion|mm|bn)\b'
    r'|\b[\d][\d,.]*\s*(?:million|billion)\s*(?:dollars)?\b'
    r'|\bGAV\b|\bgross asset value\b|\bAUM\b|\bassets under management\b'
    r'|\b[\d,]+\s*units\b'
    r'|\b[\d,.]+\s*(?:sq\.?\s*ft\.?|square\s*feet|square\s*footage)\b',
    re.IGNORECASE,
)

# Adversarial review (FIX 27 follow-up): _SIZE_SIGNAL_RE deliberately covers
# BOTH genuine dollar/AUM disclosures AND bare unit/sq-ft counts (needed for
# the legitimate "units" tertiary proxy, e.g. a firm with no dollar figure at
# all, only "3,000,000 sq ft under management"). But a bare unit/sq-ft match
# is also exactly what nav-menu/listing chrome tends to contain ("Browse
# 1,200 sq ft office suites") -- reproduced live: this let a degraded
# full-text shell's incidental "sq ft" mention (1) satisfy sub-change A's
# "does full_text still have a signal" guard and replace a snippet whose OWN
# real dollar disclosure was lost, and (2) let _windowed_slice center on that
# same irrelevant mention instead of a real disclosure sitting later in the
# text. _CURRENCY_SIGNAL_RE is the narrower, more authoritative subset (a
# genuine dollar amount or an explicit AUM/GAV keyword, never a bare
# unit/sq-ft count) -- used to prefer a currency-class match over a
# units-class one wherever "which specific match matters" (both below),
# while _SIZE_SIGNAL_RE/_has_size_signal themselves are UNCHANGED (still used
# as-is for escalation and the display-cap sort, where "any signal at all,
# including units-only" is exactly the intended, broader check).
_CURRENCY_SIGNAL_RE = re.compile(
    r'\$\s?[\d][\d,.]*\s*(?:million|billion|mm|bn)\b'
    r'|\b[\d][\d,.]*\s*(?:million|billion)\s*(?:dollars)?\b'
    r'|\bGAV\b|\bgross asset value\b|\bAUM\b|\bassets under management\b',
    re.IGNORECASE,
)

_SIGNAL_RANK = {"none": 0, "units": 1, "currency": 2}


def _signal_class(text: str) -> str:
    """'currency' if a dollar amount or an explicit AUM/GAV keyword is
    present, 'units' if only a bare unit/sq-ft count is, else 'none'."""
    if _CURRENCY_SIGNAL_RE.search(text or ""):
        return "currency"
    if _SIZE_SIGNAL_RE.search(text or ""):
        return "units"
    return "none"


def _has_size_signal(results: list[dict]) -> bool:
    """Cheap regex pre-check on raw search-result text — NOT the LLM's own
    judgment — used only to decide whether escalation is warranted, so
    checking it never costs an extra LLM call."""
    for r in results:
        text = f"{r.get('title', '')} {r.get('content', '')}"
        if _SIZE_SIGNAL_RE.search(text):
            return True
    return False


# FIX 27 sub-change B: a real Atlas run showed a 915-char PRNewswire body's
# head-slice(500) landing entirely inside the page's own nav-menu preamble —
# the disclosure sentence ("$2.5 billion in assets under management") sat at
# chars [675:687], one word past where the head-slice cut off. Windowing the
# SAME width around the first currency/size match (reusing _SIZE_SIGNAL_RE,
# not a second regex) instead of always starting at character 0 fixes this
# with zero token-budget change — same width, just re-centred. No match ->
# identical to the previous head-slice behaviour (falls through to [:width]).
# Applied to BOTH the third-party 500-char cap and the _FULL_TEXT_MAX_CHARS
# 2,500-char cap — the same failure mode (a real disclosure sitting past a
# naive head-slice cutoff) reproduced identically on a fetched full-text page
# whose first ~3,800 chars were a login/nav shell before the real content.
#
# Adversarial review follow-up: centering on the FIRST _SIZE_SIGNAL_RE match
# is itself defeatable by an earlier, irrelevant units/sq-ft mention (nav
# chrome) sitting ahead of the real disclosure -- reproduced live. Now prefers
# the first _CURRENCY_SIGNAL_RE match (a genuine dollar/AUM/GAV signal,
# wherever it sits in the text) and only falls back to the broader
# _SIZE_SIGNAL_RE (which can be a bare unit/sq-ft match) when NO currency-
# class match exists anywhere -- preserving the legitimate all-units-proxy
# case while no longer letting a units-only red herring win over a real
# dollar disclosure appearing later in the same text.
def _windowed_slice(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    m = _CURRENCY_SIGNAL_RE.search(text) or _SIZE_SIGNAL_RE.search(text)
    if not m:
        return text[:width]
    center = (m.start() + m.end()) // 2
    half = width // 2
    start = max(0, center - half)
    end = min(len(text), start + width)
    start = max(0, end - width)  # re-clamp so a match near the END still yields a full-width window
    return text[start:end]


def _run_research(firm_name: str, progress: Progress) -> tuple[list[dict], int]:
    """Layered Tavily search (Exa as a secondary angle source). Raises
    InfrastructureError on a hard provider error; caps at MAX_SEARCH_CALLS_PER_FIRM
    (HARD RULE 9, 12 calls/firm).

    Two tiers: CORE angles (_CORE_ANGLES) always run first. If NONE of the
    aggregated results contain even a rough size/dollar signal, escalate
    with _ESCALATION_ANGLES (wider, more third-party-leaning, including a
    news-topic search) until either a signal turns up or the budget is
    exhausted. This is the fix for a real-mode dry run that parked a firm
    with real, easily-findable public GAV/unit-count disclosures after only
    4 searches, all against the firm's own domain — never park on an
    under-budget search count when a signal genuinely wasn't looked for."""
    from lib_snapshot.researcher import search_client
    from lib_snapshot.researcher.search_client import SearchAPIError

    calls = 0
    results: list[dict] = []

    def _run_angle(angle_tmpl: str, topic: str = "general") -> None:
        nonlocal calls
        try:
            results.extend(search_client.search(
                angle_tmpl.format(name=firm_name), max_results=6, topic=topic,
            ))
            calls += 1
        except SearchAPIError as e:
            raise InfrastructureError("Tavily", e.message) from e

    for angle in _CORE_ANGLES:
        if calls >= config.MAX_SEARCH_CALLS_PER_FIRM:
            return results, calls
        _run_angle(angle)

    if _has_size_signal(results):
        return results, calls

    progress(f"{firm_name}: no size/AUM signal in {calls} core searches — escalating "
            f"to wider/third-party angles before parking.")
    for angle, topic in _ESCALATION_ANGLES:
        if calls >= config.MAX_SEARCH_CALLS_PER_FIRM:
            break
        _run_angle(angle, topic=topic)
        if _has_size_signal(results):
            break

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
  "aum_value_usd": <the AUM or proxy number (including a disclosed RAW Gross
                    Asset Value / GAV figure — report it RAW, do not convert
                    it yourself) as a plain number, or null if aum_tag is
                    AUM_UNVERIFIED — NEVER leave this null when a proxy rule
                    (a-e) or floor-proxy language applies. NEVER a non-currency
                    quantity: square footage, unit counts, property counts,
                    employee counts, or any other count of "how many"/"how
                    much space" is NOT a dollar amount, no matter how large
                    the number looks — "over three million square feet" is a
                    SIZE, not 3000000 dollars; that case belongs in proxy_type
                    "units" with the quantity in unit_count/sqft_count below,
                    with aum_value_usd left null>,
  "proxy_type": "floor" ONLY if this figure came from floor-proxy LANGUAGE
                ("over $X", "north of $X", "at least $X" — an EXPLICIT stated
                lower bound) — the true AUM could be higher; "non_floor" if
                from proxy rule (b) ("$X invested/deployed/in acquisitions" —
                a specific, dated cumulative figure, NOT itself phrased as a
                lower bound, even though current AUM could genuinely differ
                from it either way) or proxy rule (c) or (d) (portfolio
                value, fund size) — none of these three are a stated floor,
                so they are closer to actual value; "gav" if this is a
                disclosed Gross Asset Value (proxy
                rule e) — a GROSS figure that includes debt/leverage; report
                the RAW GAV number in aum_value_usd, a deterministic function
                applies the leverage haircut, do NOT do that conversion
                yourself; "units" if NO dollar figure of any kind was found
                but a unit count OR a square-footage figure is disclosed (see
                unit_count/sqft_count below) — leave aum_value_usd null in
                that case; empty string if aum_tag is not AUM_ESTIMATED,
  "proxy_basis": "e.g. 'invested capital proxy', 'fund size proxy',
                 'regulatory AUM (Form ADV)', or empty string if AUM_CONFIRMED
                 from a direct source statement",
  "unit_count": <ONLY if proxy_type is "units" AND a residential/multifamily
                UNIT count was disclosed (e.g. "21,794 units"): the count as a
                plain integer, so a deterministic function can estimate from
                it — never estimate a dollar figure from this yourself, and
                never put a square-footage number here. null/omit otherwise>,
  "sqft_count": <ONLY if proxy_type is "units" AND a SQUARE-FOOTAGE figure was
                disclosed (e.g. "1.8M square feet", "over three million
                square feet") — NOT a unit count: the square footage as a
                plain number (e.g. 3000000), so a deterministic function can
                estimate from it at its OWN per-square-foot rate — never
                estimate a dollar figure from this yourself, and never put a
                unit count here. null/omit otherwise>,
  "quantity_qualifier": "ONLY if proxy_type is 'units': the approximator
                        word the SOURCE TEXT used alongside unit_count/
                        sqft_count, if any — 'over' (\"over three million
                        square feet\", \"over 21,794 units\"), 'more_than',
                        'at_least', 'north_of', 'approximately', 'about', or
                        'roughly' (\"approximately 1.8M square feet\"). This
                        preserves the firm's own claimed precision — a firm
                        that said 'over X' never claimed exactly X, and the
                        deck/email must not silently upgrade that to an
                        exact-sounding bare number. Empty string if the
                        source stated the quantity as a plain, unqualified
                        number, or if proxy_type is not 'units'.",
  "source_note": "e.g. 'firm website' or 'SEC Form ADV' — empty if not applicable",
  "disclosed_figure": "EXACTLY what the firm itself published, verbatim, no
                      arithmetic, no haircut, no conversion — this is what a
                      PROSPECT will read, so it must be defensible against the
                      source text word for word. e.g. '$4.4B' for a disclosed
                      GAV (NEVER the haircut equity value), '$254M' for a
                      disclosed invested-capital figure. If proxy_type is
                      'units', leave this EMPTY — it is built separately from
                      unit_count/sqft_count, not from your own re-transcription.
                      Empty string if aum_tag is AUM_UNVERIFIED or nothing was
                      disclosed",
  "disclosed_label": "the firm's OWN word(s) for what the figure above is, in
                     the firm's own framing — e.g. 'gross asset value',
                     'capital invested', 'assets under management'. For a
                     'units' proxy_type, describe what the unit_count/
                     sqft_count actually measures, e.g. 'of office and retail
                     space under management' or 'units under management' — do
                     NOT restate the number itself here. Empty string if you
                     have no genuine source-grounded description",
  "aum_display": "legacy/operator-view-only convenience string, e.g. $1.1B —
                 downstream deck/email code no longer reads this field, only
                 disclosed_figure/disclosed_label above",
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

Some snippets below may describe a DIFFERENT company that merely shares a
similar name, brand word, or market with {name} — a snippet prefixed with a
bracketed tag like [Company Name, Location] names who that specific snippet
is actually about. Treat the COMPANY NAME as the primary signal: only ignore
the snippet if the tagged name is clearly a different company than {name} —
a differing secondary office or wire-dateline city alone, with a matching or
absent name, is NOT by itself a reason to discard a snippet that otherwise
matches. When the name is genuinely a different company, IGNORE every
figure in that snippet entirely — do not attribute it to {name}. A snippet
tagged [unverified source] could describe ANY company — do not assume it is
about {name} unless the snippet's own text makes that explicit. An
UN-prefixed snippet is {name}'s own verified site/filing and can be trusted
directly.

Research snippets:
{snippets}
"""


# DIAGNOSIS-ONLY instrumentation (not extraction/gating logic): dumps exactly
# what the extraction LLM call received and returned to qa_out/debug/, so a
# human can inspect the real-mode research pipeline's actual behavior without
# re-deriving it from stderr logs. Guarded on `not config.TEST_MODE`
# explicitly -- NOT just "only called from a real-mode path", because
# _extract() itself has no TEST_MODE check (only its caller,
# research_and_gate(), does) and at least one existing test
# (test_extract_prioritizes_signal_bearing_results_so_escalation_finding_survives_truncation)
# calls _extract() directly with a mocked complete_json_retry — a real,
# confirmed case of the dump firing during a normal pytest run before this
# guard existed, writing a real file to disk from a test with a mocked LLM
# response. Purely additive otherwise: does not change what gets extracted,
# parsed, or gated, only observes and records it.
def _dump_extraction_debug(firm_name: str, prompt: str, results: list[dict],
                           attempts: list[tuple[str, str]],
                           filter_info: Optional[dict] = None) -> Optional[Path]:
    if config.TEST_MODE:
        return None
    debug_dir = config.ROOT / "qa_out" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", firm_name).strip("_") or "firm"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = debug_dir / f"extraction_input_{slug}_{ts}.txt"

    lines = [
        f"# Extraction debug dump — {firm_name}",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Written from: {__file__}",
        "",
    ]
    if filter_info is not None:
        lines += [
            "=" * 78,
            "0. FIX 5 ENTITY-FILTERING DECISION",
            "=" * 78,
            f"own_domain resolved: {filter_info.get('own_domain')!r}",
            f"other-entity domains dropped: {sorted(filter_info.get('other_domains') or [])}",
            f"SEC ADV rejected CRDs (plausibly-confusable, confirmed-different): "
            f"{filter_info.get('rejected_crds') or []}",
        ]
        # FIX 28 sub-change C: the one diagnostic artifact built for exactly
        # this situation (a human opening this dump to understand why a
        # firm parked as firm_identity_ambiguous) was silently missing the
        # actual candidate domains -- present via progress()'s log line and
        # GateResult.research, but never here.
        if filter_info.get("ambiguous_candidates"):
            lines.append(f"AMBIGUOUS CANDIDATE DOMAINS (park, no LLM call made): "
                         f"{filter_info['ambiguous_candidates']}")
        lines += [
            f"URLs dropped ({len(filter_info.get('dropped_urls') or [])}):",
        ] + [f"  - {u}" for u in (filter_info.get("dropped_urls") or [])] + [""]
    lines += [
        "=" * 78,
        "1. EXACT, FULL, UNTRUNCATED PROMPT HANDED TO THE EXTRACTION LLM",
        "=" * 78,
        prompt,
        "",
        "=" * 78,
        f"2. SOURCE URLS COLLECTED FOR THIS FIRM ({len(results)} total, from "
        "_run_research -- before the 24-result display cap applied when "
        "building the prompt above)",
        "=" * 78,
    ] + [f"- {r.get('url', '(no url)')}" for r in results] + [
        "",
        "=" * 78,
        "3. PER-SOURCE BODY LENGTH (character length of the 'content' field "
        "Tavily's search() call actually returned for that URL -- 0 if empty; "
        "this is the RAW length before _extract()'s own 500-char-per-result "
        "truncation when building the prompt above)",
        "=" * 78,
    ] + [f"- {r.get('url', '(no url)')}  [content_length_chars={len(r.get('content') or '')}]"
        for r in results] + [
        "",
        "=" * 78,
        f"4. RAW, UNPARSED LLM EXTRACTION RESPONSE ({len(attempts)} attempt(s) made)",
        "=" * 78,
    ]
    for i, (model_name, raw_text) in enumerate(attempts, start=1):
        lines += [f"--- attempt {i}, model={model_name} ---", raw_text, ""]
    if not attempts:
        lines.append("(no attempt recorded -- complete_json_retry raised before any "
                     "HTTP call reported success)")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[EXTRACTION DEBUG DUMP] written from {__file__} -> {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# FIX 1/3 — own-domain inference + high-value source classification
# ---------------------------------------------------------------------------
# QA finding (Kairos Real Estate Partners real-mode run): well-known
# aggregator/data-broker/press-wire/social domains never carry a firm's own
# primary disclosures (a RocketReach or ZoomInfo blurb is a summary of public
# records, not a source of truth) -- excluded so they can never be
# mistaken for "the firm's own site" no matter how often they're hit.
_THIRD_PARTY_DOMAINS = {
    "linkedin.com", "pr.linkedin.com", "rocketreach.co", "zoominfo.com",
    "crunchbase.com", "bloomberg.com", "prnewswire.com", "businesswire.com",
    "sec.gov", "adviserinfo.sec.gov", "aum13f.com", "preqin.com",
    "pitchbook.com", "builderonline.com", "newprivatemarkets.com",
    "redfin.com", "capdex.com", "altss.com", "multihousingnews.com",
}

# QA finding (this fix's own verification run): a live Kairos run's search
# results included a 48,232-char PDF (a mirrored copy of an unrelated firm's
# SEC ADV brochure, surfaced via general web search, not this module's own
# ADV lookup). Left fully uncapped, full-text retrieval blew the total
# prompt to 213,634 chars and the extraction LLM returned completely empty
# on both attempts (a reasoning model spends tokens digesting the input
# BEFORE producing visible output -- an oversized prompt starves that
# budget). An 8,000-char cap fixed the empty-response failure but still
# pushed total usage (prompt + hidden reasoning + completion) to 52,638
# tokens against this project's existing 25,000/firm budget
# (config.MAX_LLM_TOKENS_PER_FIRM, HARD RULE 9 -- a pre-existing budget cap
# left untouched, not a "gate threshold" this fix was asked to change).
# Re-verified live at 2,000 chars/source: still 28,501 tokens, over budget
# by 3,501. Token cost is NOT linear with character count for a reasoning
# model (cutting characters saves a disproportionate number of hidden
# reasoning tokens, not just a proportional share) -- reduced to 1,000 to
# fit budget, but this ALSO cut off a legitimate fact: the FactSheet PDF's
# "Since our founding in 2013" sentence sits near the END of its 2,198-char
# full text, past a 1,000-char cutoff -- founded_year came back empty as a
# direct, confirmed result.
#
# FIX 5 (entity filtering) changed the math: with 7 wrong-entity sources
# dropped before full-text retrieval ever runs, the same firm's total
# prompt fell to ~21,000 chars at 6,755 tokens (73% of the 25,000/firm
# budget unused) even at the old 1,000-char cap -- real headroom to raise
# this. Re-tested against the cached research payload (FIX 4, no new
# Tavily searches) at three values: 6,000 chars -> both gpt-5-mini attempts
# returned empty (finish_reason="length", reasoning_tokens=2048 -- FIX 6's
# diagnosed failure mode, triggered here by the LARGER prompt itself) and
# DeepSeek's escalation attempt misattributed an unrelated $1B "Kairos
# Living" press mention as the firm's GAV; 2,200 chars -> both gpt-5-mini
# attempts again empty, DeepSeek escalated but this time landed on the
# correct $254M; 2,500 chars -> gpt-5-mini's FIRST attempt still empty, but
# its own RETRY (same model, no escalation needed) succeeded, correctly
# returning $254M/founded 2013, at 15,037 of 25,000 tokens. Settled on
# 2,500: comfortably includes the FactSheet's full 2,198-char text (the
# fact this cap exists to capture) while empirically causing fewer
# gpt-5-mini failures than either alternative tested. gpt-5-mini's
# remaining attempt-1 empty-response rate at this size is a separate,
# unresolved finding -- see FIX 6's diagnosis -- not something this cap
# value alone eliminates.
_FULL_TEXT_MAX_CHARS = 2500


def _extract_domain(url: str) -> str:
    netloc = (urlparse(url or "").netloc or "").lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


# ---------------------------------------------------------------------------
# FIX 28 — own_domain resolution: name similarity outranks raw hit count
# ---------------------------------------------------------------------------
# QA finding (Kairos Real Estate Partners, deployed-VM run): the same firm,
# the same code, opposite verdicts across two environments. Local run: 12
# kairosrep.com hits vs 2 kimc.com -> correct. VM run: kimc.com 5,
# kairosrep.com 3, merriam-webster.com 5 ("kairos" is an English dictionary
# word), youtube.com 4 -> own_domain resolved to kimc.com, PURELY on hit
# count. That one bad vote then compounded: _identify_other_entity_domains
# dropped kairosrep.com itself (it shares the brand token "kairos" with the
# WRONG anchor), and the extraction attributed a real, sourced, confirmed
# $2.4B AUM figure -- Kairos INVESTMENT MANAGEMENT COMPANY's, a real but
# different firm -- to the target, disqualifying it on someone else's
# balance sheet. Hit count is a noisy, environment-dependent signal for a
# firm whose brand word is also a common English word; the firm's OWN NAME
# is not -- "kairosrep" is literally Kairos + R/E/P from "Real Estate
# Partners", true at any hit count. Name similarity now wins outright when
# strong; hit count is the tie-break for weak/absent name signals, not the
# primary vote.
_OWN_DOMAIN_NAME_MATCH_THRESHOLD = 85.0   # score >= this: a real, name-derived match
_OWN_DOMAIN_AMBIGUITY_MARGIN = 10.0       # two strong matches this close -> ambiguous, park
_OWN_DOMAIN_MODERATE_NAME_MATCH = 60.0    # a real, but sub-"strong", signal -- still worth weighing
_WEAK_DOMAIN_NAME_SIGNAL_CAP = 70.0       # a bare fuzzy resemblance alone never counts as "strong"
# FIX 28 sub-change D: how close a REJECTED SEC ADV hit's own name must be
# to the target's before treating the rejection itself as suspicious (see
# _classify_sec_adv_hits/the _extract() call site). Set well above the
# real, CONFIRMED-different "Redwood-Kairos Real Estate Partners" case --
# 81.8, measured live via rapidfuzz against the ACTUAL production firm_name
# string ("Kairos Real Estate Partners, LLC", the matched-company name
# research_and_gate() actually receives; the bare name without the ", LLC"
# suffix scores 87.1, which is what an earlier draft of this comment cited
# before the discrepancy was caught) -- so a genuinely different firm
# sharing most-but-not-all of the target's words is never flagged -- only
# a near-exact name the strict exact-match check narrowly missed.
_SEC_ADV_SUSPICIOUS_REJECTION_THRESHOLD = 95.0

# Pure corporate-STRUCTURE words (as opposed to descriptive business-type
# words like "Capital"/"Partners"/"Real"/"Estate", which legitimately show
# up in real initialisms/domains) -- excluded from the initials/leftover-
# explanation logic below. QA finding while implementing this fix: passing
# "Kairos Real Estate Partners, LLC" (the ", LLC" suffix appears on some
# call sites, not others) changed the answer for BOTH mechanisms at once --
# the required initials became "krepl" instead of "krep" (breaking the
# correct kairosrep.com match), AND "kairosgroupllc.com"'s leftover
# "groupllc" started matching via the literal substring "llc" (wrongly
# re-admitting a confuser domain the existing test suite already expects to
# stay droppable). Nobody builds a domain/initialism that includes the "L"
# for "LLC" -- these words are correctly excluded from the containment/
# initials math while `meaningful_tokens()`'s own broader SUFFIX_TOKENS
# stripping (unchanged, used elsewhere) is left alone.
#
# Adversarial review follow-up: "and" joins this set for the identical
# reason -- src/matching/normalize.py's normalize() expands "&" to " and "
# BEFORE tokenizing, so "H&R" becomes ["h","and","r"], not one joined
# token. Left in, "and" would (a) count as its own letter in the full-
# initials fallback, turning "H&R Real Estate Investment Trust"'s correct
# initials "hreit" into the wrong "hareit", and (b) satisfy the anchor/
# leftover math as a 3-letter "meaningful" word on its own. Neither is a
# real part of the firm's brand identity -- it is punctuation, re-spelled.
_PURE_CORPORATE_SUFFIX = {
    "llc", "lp", "l.p.", "inc", "incorporated", "corp", "corporation",
    "co", "ltd", "limited", "the", "company", "and",
}

# Adversarial review finding (severe, live-reproduced): the ORIGINAL leftover-
# explanation check accepted ANY substring overlap in EITHER direction
# between the leftover and a remaining token, with no length floor on
# either side. Two independently-reproduced false-positive classes:
# (1) a firm name with an ampersand-joined short segment ("H&R Real Estate
#     Investment Trust" -> tokens include bare "h"/"r") makes a
#     single-character token trivially "contained in" almost any leftover
#     of nontrivial length -- e.g. harborview.com/harmony.com/hershey.com
#     all scored a false 100.0 for "H&R...". Fixed with _MIN_ANCHOR_LEN: a
#     token this short can never anchor the substring search that starts
#     the whole mechanism.
# (2) even for an ORDINARY name with no unusual punctuation, a leftover
#     could coincidentally be a substring of a much longer remaining word
#     ("pit" inside "capital", "art"/"ers" inside "partners") -- e.g.
#     kairospit.com/kairosart.com falsely scored 100.0 against "Kairos
#     Capital Partners". Fixed by replacing "is either a substring of the
#     other" with a narrow, specific relaxation: the leftover must be
#     EXACTLY one remaining token (any length -- a real, deliberate
#     correspondence), or that token pluralized by a trailing "s" (only
#     for tokens >=4 chars, so pluralizing a short/coincidental fragment
#     can't manufacture a match). This still supports the ORIGINAL
#     required case (leftover "investments" == remaining token
#     "investment" + "s") while rejecting coincidental fragment overlap.
_MIN_ANCHOR_LEN = 3     # a shorter anchor token risks matching almost any domain by coincidence
_MIN_SPELLOUT_LEN = 4   # a shorter remaining token risks a coincidental pluralization match


def _initialism_tokens(firm_name: str) -> list[str]:
    return [t for t in name_normalize.tokenize(firm_name) if t not in _PURE_CORPORATE_SUFFIX]


def _leftover_explained(leftover: str, remaining_tokens: list[str]) -> bool:
    """Is `leftover` (the domain characters left after removing the anchor
    token) accounted for by the firm's OTHER tokens? Two mechanisms, EITHER
    is sufficient: exact equality to the initials of every remaining token
    (a true initialism, e.g. "rep" == r+e+p from "real"/"estate"/
    "partners"), or exact equality (optionally pluralized, for tokens long
    enough that pluralizing them can't coincidentally manufacture a match)
    to ONE remaining token spelled out in full (e.g. "investments" ==
    "investment" + "s"). Deliberately NOT a general substring-containment
    check in either direction -- see the module-level comment above
    _MIN_ANCHOR_LEN/_MIN_SPELLOUT_LEN for the two real, live-reproduced
    false-positive classes that check used to admit."""
    if not leftover:
        return True
    initials = "".join(t[0] for t in remaining_tokens if t)
    if initials and leftover == initials:
        return True
    for t in remaining_tokens:
        if not t:
            continue
        if leftover == t:
            return True
        if len(t) >= _MIN_SPELLOUT_LEN and (leftover == t + "s" or t == leftover + "s"):
            return True
    return False


def _domain_name_similarity(domain: str, firm_name: str) -> float:
    """0-100: how strongly `domain` looks DERIVED from `firm_name`'s own
    words, not just a raw string resemblance. Two exact-match mechanisms
    (either alone scores 100), reusing src/matching/normalize.py's tokenizer
    (the SAME one the Phase 1 firm matcher already uses) rather than a
    third, independent name-cleaning implementation:

    (1) full meaningful-token containment, with the LEFTOVER explained: find
        one of the firm's own suffix-stripped tokens (e.g. "kingbird" from
        "Kingbird Investment Management"), at least _MIN_ANCHOR_LEN chars
        long, as a substring of the domain, then require whatever
        characters remain to be accounted for by the firm's OTHER tokens
        (see _leftover_explained) -- spelled out (handles
        "kingbirdinvestments", leftover "investments" == the remaining
        token "investment" pluralized) or reduced to initials (handles
        "kairosrep", leftover "rep" == initials of the remaining
        "real"/"estate"/"partners"). This is what distinguishes a genuine
        derived domain from a domain that merely contains the SAME short
        brand word attached to unrelated letters -- "kairoscm.com"/
        "kairosgroupllc.com" (real confuser examples this module already
        treats as a different entity, see _identify_other_entity_domains)
        contain "kairos" too, but their leftover ("cm"/"groupllc") explains
        nothing in the firm's own name, so they do NOT score as a strong
        match here -- unexplained containment is exactly the brand-token-
        collision risk this fix exists to avoid re-introducing.
    (2) fully-abbreviated fallback: the WHOLE domain, with nothing spelled
        out at all, equals the initials of EVERY token in the firm's name.

    Anything short of an exact mechanism match falls back to a plain
    rapidfuzz ratio against the meaningful tokens, capped well below the
    "strong" threshold (_WEAK_DOMAIN_NAME_SIGNAL_CAP) -- a bare fuzzy
    resemblance (e.g. "kairoscm" vs "kairos") is a real, weak signal, never
    strong enough to win outright or to protect a domain from being
    dropped as a different entity.

    Known, accepted residual (adversarial review, not fully closable
    without breaking the required "kingbirdinvestments" case): a domain
    that is the brand word plus ONE generic real-estate/finance descriptor
    spelled out (e.g. "kairosestates.com" via "estate"+"s") can still score
    100 even for a firm this isn't actually about -- the same mechanism
    that must accept "investment"/"management" as legitimate spell-outs
    cannot, by word-list membership alone, distinguish them from
    "estate"/"realty" as illegitimate ones; both are equally generic
    English business words. Mitigated, not eliminated, by two OTHER
    layers: sub-change B's hit-count-corroboration requirement (a
    low-frequency coincidental match is not protected from being dropped)
    and sub-change C's comparable-score + hit-count-dominance tie-break (a
    coincidental confuser competing against the real, dominant domain
    triggers ambiguity handling rather than silently losing outright)."""
    if not domain or not firm_name:
        return 0.0
    domain_root = (domain.split(".")[0] or "").lower()
    domain_alpha = re.sub(r"[^a-z0-9]", "", domain_root)
    if not domain_alpha:
        return 0.0

    all_tokens = _initialism_tokens(firm_name)
    if not all_tokens:
        return 0.0
    meaningful = [t for t in name_normalize.meaningful_tokens(firm_name) if t not in _PURE_CORPORATE_SUFFIX]

    for tok in meaningful:
        if tok and len(tok) >= _MIN_ANCHOR_LEN and tok in domain_alpha:
            idx = domain_alpha.find(tok)
            leftover = domain_alpha[:idx] + domain_alpha[idx + len(tok):]
            remaining = [t for t in all_tokens if t != tok]
            if _leftover_explained(leftover, remaining):
                return 100.0

    full_initials = "".join(t[0] for t in all_tokens if t)
    if full_initials and domain_alpha == full_initials:
        return 100.0

    joined = "".join(meaningful) if meaningful else ""
    weak = float(fuzz.ratio(domain_alpha, joined)) if joined else 0.0
    return min(weak, _WEAK_DOMAIN_NAME_SIGNAL_CAP)


@dataclass
class OwnDomainResolution:
    """Richer sibling of _infer_own_domain's plain Optional[str] return --
    `ambiguous`/`candidates` let _extract() decide to PARK (sub-change C)
    instead of silently picking a domain when the signals genuinely
    conflict. _infer_own_domain stays a thin wrapper so its own existing
    callers/tests (none of which exercise the ambiguous case) are
    unaffected."""
    domain: Optional[str]
    ambiguous: bool = False
    candidates: list[str] = field(default_factory=list)


def _resolve_own_domain(results: list[dict], firm_name: str = "") -> OwnDomainResolution:
    """Best-effort: the firm's own domain is whichever candidate looks most
    NAME-DERIVED from `firm_name` (see _domain_name_similarity), falling
    back to raw hit-count plurality only when no domain shows a real name
    signal at all. `firm_name` is optional (default "") so this reduces to
    the original hit-count-only behavior when omitted -- existing callers/
    tests that never had a firm name to pass keep their exact prior
    behavior. Requires >=2 hits for the hit-count-only fallback path, same
    as before, to avoid a false positive from a single coincidental match."""
    counts = Counter(
        d for r in results
        if (d := _extract_domain(r.get("url", ""))) and d not in _THIRD_PARTY_DOMAINS
    )
    if not counts:
        return OwnDomainResolution(domain=None)

    if not firm_name:
        domain, count = counts.most_common(1)[0]
        return OwnDomainResolution(domain=domain if count >= 2 else None)

    name_scores = {d: _domain_name_similarity(d, firm_name) for d in counts}
    strong = sorted(
        (d for d, s in name_scores.items() if s >= _OWN_DOMAIN_NAME_MATCH_THRESHOLD),
        key=lambda d: name_scores[d], reverse=True,
    )
    if strong:
        top_score = name_scores[strong[0]]
        comparable = [d for d in strong if top_score - name_scores[d] <= _OWN_DOMAIN_AMBIGUITY_MARGIN]
        if len(comparable) > 1:
            # QA finding (real-mode Atlas Real Estate run): "Atlas" is a
            # common enough word/brand that FOUR different domains
            # (atlas.co, atlas.so, atlasre.com, realatlas.com) all scored a
            # clean 100 -- but three of them were single, incidental
            # bare-root mentions (count=1) against realatlas.com's 4
            # independently-surfaced hits. Equal NAME scores do not mean
            # equal EVIDENCE -- hit count is explicitly the tie-break among
            # comparably-name-scoring candidates (not the primary signal,
            # which stays name similarity): if one candidate's hit count
            # clearly dominates the others', it wins outright rather than
            # forcing a park over what is really just word-coincidence
            # noise with no corroborating frequency behind it.
            by_count = sorted(comparable, key=lambda d: counts[d], reverse=True)
            top_count, runner_up_count = counts[by_count[0]], counts[by_count[1]]
            if top_count >= 2 * max(runner_up_count, 1):
                return OwnDomainResolution(domain=by_count[0])
            return OwnDomainResolution(domain=None, ambiguous=True,
                                      candidates=sorted(comparable))
        return OwnDomainResolution(domain=strong[0])

    # No strong name match anywhere -- fall back to hit-count plurality,
    # but first check for a genuine hit-count-vs-name disagreement: a
    # MODERATE (not yet "strong") name signal on a DIFFERENT domain than
    # the hit-count winner, where the hit-count winner isn't overwhelmingly
    # ahead, is a real conflict between the two signals -- neither
    # dominates, so park rather than silently trust either one alone.
    hitcount_domain, hitcount_n = counts.most_common(1)[0]
    if hitcount_n >= 2:
        moderate = [d for d in name_scores
                    if name_scores[d] >= _OWN_DOMAIN_MODERATE_NAME_MATCH and d != hitcount_domain]
        if moderate:
            best_moderate = max(moderate, key=lambda d: name_scores[d])
            hitcount_dominates = hitcount_n >= 2 * counts.get(best_moderate, 0)
            if not hitcount_dominates:
                return OwnDomainResolution(domain=None, ambiguous=True,
                                          candidates=sorted({hitcount_domain, best_moderate}))
        return OwnDomainResolution(domain=hitcount_domain)
    return OwnDomainResolution(domain=None)


def _infer_own_domain(results: list[dict], firm_name: str = "") -> Optional[str]:
    """Best-effort: the firm's own domain is the single most-frequent
    domain among collected results, once well-known third-party domains
    are excluded, requiring at least 2 hits to avoid a false positive from
    a single coincidental match. Thin wrapper around _resolve_own_domain --
    see FIX 28 above for the name-similarity-first logic; this function's
    own contract (a plain Optional[str], no ambiguity signal) is unchanged
    for existing callers."""
    return _resolve_own_domain(results, firm_name).domain


# ---------------------------------------------------------------------------
# FIX 5 — entity filtering: DROP other-entity sources, don't just deprioritize
# ---------------------------------------------------------------------------
# QA finding (live Kairos run): resolving own_domain and rejecting a false
# SEC ADV match were both computed correctly -- and then IGNORED. The wrong
# entity's sources still reached the prompt (just ranked lower), and the
# extraction LLM used them anyway, attributing a different real company's
# $4.4B/2005-founding to the target firm. A source that reaches the prompt
# gets used regardless of its rank -- the fix is exclusion, not ordering.
_GENERIC_NAME_WORDS = {
    "the", "and", "of", "llc", "inc", "l.l.c", "lp", "l.p", "corp",
    "corporation", "co", "company", "real", "estate", "partners",
    "partnership", "capital", "investment", "investments", "management",
    "group", "advisors", "advisers", "holdings", "properties", "property",
}


def _brand_tokens(firm_name: str) -> set[str]:
    """The distinctive, non-generic word(s) in a firm name -- e.g. {"kairos"}
    from "Kairos Real Estate Partners, LLC". Deliberately looser than
    _normalize_firm_name_for_match (which is for exact SEC identity
    matching): this is only used to spot ANOTHER domain that spells the same
    distinctive brand word into its own name (kairoscm.com,
    kairosgroupllc.com) so it can be dropped as a likely different entity."""
    tokens = re.findall(r"[a-z0-9]+", (firm_name or "").lower())
    return {t for t in tokens if t not in _GENERIC_NAME_WORDS and len(t) > 2}


def _is_bare_root_url(url: str) -> bool:
    """True if `url` is a domain's bare root -- no path beyond an optional
    trailing slash, no query string -- e.g. "https://kimc.com" or
    "https://kimc.com/", but NOT "https://kimc.com/associate-asset-management".
    A root-URL hit is a much stronger "this domain IS someone's own
    homepage" signal than a deep article path on a third-party press
    site -- strong enough to act on even at a single occurrence (FIX 9),
    unlike the frequency-based signal below, which needs >=2 hits."""
    parsed = urlparse(url or "")
    return (parsed.path or "") in ("", "/") and not parsed.query and not parsed.fragment


def _identify_other_entity_domains(results: list[dict], own_domain: Optional[str],
                                   firm_name: str) -> set[str]:
    """Domains that look like a DIFFERENT real entity's own site, once
    own_domain is resolved. Three independent signals, any one is sufficient:
    (a) the domain spells out the firm's distinctive brand token but isn't
        own_domain -- catches a differently-named company that still uses
        the shared brand word in ITS domain (kairoscm.com,
        kairosgroupllc.com are both literally "kairos...");
    (b) the domain independently clears the same >=2-hit bar
        _infer_own_domain itself uses, but lost the pick to own_domain --
        a domain with several of its OWN distinct pages is acting like
        someone's dedicated site, not incidental press coverage. Catches
        kimc.com, which shares no literal substring with "kairos" at all
        (KIMC is Kairos Investment Management Company's INITIALS, not the
        word spelled out) -- (a) alone would have missed it; a real run
        confirmed kimc.com tied own_domain 3-3 on distinct pages;
    (c) FIX 9 (real-mode Kairos run): a domain hit via its OWN bare root
        URL even just ONCE -- a real run found kimc.com surfaced only a
        single hit that run (below (b)'s 2-hit bar), and that one hit was
        literally https://kimc.com titled "Kairos Investment Management:
        Home" -- a root-URL hit on a non-aggregator domain that isn't
        own_domain is itself strong enough evidence of "someone else's own
        site" to act on at count=1, unlike incidental press coverage
        (a news article about the target is a deep path on the NEWS
        domain, not a root-URL hit on a domain claiming to BE the firm)."""
    if not own_domain:
        return set()
    brand_tokens = _brand_tokens(firm_name)
    counts: Counter = Counter()
    bare_root_domains: set[str] = set()
    for r in results:
        url = r.get("url", "")
        domain = _extract_domain(url)
        if not domain or domain in _THIRD_PARTY_DOMAINS:
            continue
        counts[domain] += 1
        if _is_bare_root_url(url):
            bare_root_domains.add(domain)

    own_domain_count = counts.get(own_domain, 0)
    other: set[str] = set()
    for domain, count in counts.items():
        if domain == own_domain:
            continue
        # FIX 28 sub-change B: a domain scoring a strong NAME-DERIVED match
        # (see _domain_name_similarity) can never be dropped as "other
        # entity", regardless of brand-token sharing or hit count. QA
        # finding: own_domain resolution picking the WRONG anchor (on raw
        # hit count) then let this exact function drop the TARGET's own
        # site, kairosrep.com, because it shares the brand token "kairos"
        # with the wrong anchor -- dropping the target's own site is a bug
        # class, not an edge case to weigh against other signals.
        #
        # Adversarial review follow-up: the strong-match exemption alone,
        # with no reference to hit count, is itself exploitable -- a
        # genuinely different company's own site (e.g. "kairosrealty.com",
        # a single incidental bare-root hit) can ALSO score >=85 (see
        # _domain_name_similarity's own documented residual for how a
        # brand-word-plus-generic-descriptor domain can coincidentally
        # score a full match) and would then be shielded from exclusion
        # even while own_domain is resolved confidently and correctly,
        # with dramatically more corroborating hits. Requires the
        # candidate's own hit count to be at least half of own_domain's
        # (the same 2x-dominance ratio _resolve_own_domain already applies
        # when picking between comparably-scoring domains) before granting
        # the exemption -- a strong name match with real, comparable
        # search-frequency support is protected; a strong name match
        # riding on a single incidental mention is not.
        strong_name_match = _domain_name_similarity(domain, firm_name) >= _OWN_DOMAIN_NAME_MATCH_THRESHOLD
        has_comparable_support = count * 2 >= own_domain_count
        if strong_name_match and has_comparable_support:
            continue
        bare = domain.replace(".", "").replace("-", "")
        shares_brand_token = any(tok in bare for tok in brand_tokens)
        also_entity_like = count >= 2
        is_own_site_at_any_hit_count = domain in bare_root_domains
        if shares_brand_token or also_entity_like or is_own_site_at_any_hit_count:
            other.add(domain)
    return other


def _filter_out_other_entities(results: list[dict], own_domain: Optional[str],
                               firm_name: str, rejected_crds: list[str]) -> list[dict]:
    """Drops (not reorders) any source that belongs to a different,
    identified entity: a different candidate's own domain
    (_identify_other_entity_domains), or a URL that references a CRD number
    the SEC ADV lookup already confirmed is a DIFFERENT, rejected firm (this
    catches reports.adviserinfo.sec.gov/reports/ADV/<crd>/... -- a mirrored
    copy of the wrong entity's ADV brochure that a general web search
    surfaced independently of this module's own SEC lookup, on a domain
    that shares neither a brand token nor enough raw hits to be caught by
    the domain-level filter above)."""
    other_domains = _identify_other_entity_domains(results, own_domain, firm_name)
    kept = []
    for r in results:
        url = r.get("url", "") or ""
        if _extract_domain(url) in other_domains:
            continue
        if any(crd and crd in url for crd in rejected_crds):
            continue
        kept.append(r)
    return kept


def _is_high_value_source(url: str, own_domain: Optional[str]) -> bool:
    """A source worth full-text retrieval (FIX 1) and priority ranking
    ahead of aggregators under the display cap (FIX 3): the firm's own
    domain, any PDF (fact sheets, brochures, decks routinely carry the real
    numbers a homepage teaser doesn't), or its SEC ADV record."""
    domain = _extract_domain(url)
    if domain == "adviserinfo.sec.gov":
        return True
    if (url or "").lower().endswith(".pdf"):
        return True
    return bool(own_domain) and domain == own_domain


# ---------------------------------------------------------------------------
# FIX 1 — full-document retrieval for high-value sources (own domain / PDF)
# ---------------------------------------------------------------------------
def _fetch_full_text_for_high_value_sources(results: list[dict],
                                            own_domain: Optional[str]) -> dict[str, str]:
    """Calls search_client.extract() (Tavily's /extract endpoint -- full
    parsed page text, not the short /search snippet) for every distinct
    own-domain or .pdf URL in `results`. Returns {url: full_text}; empty on
    any failure (extract() itself already degrades to {} on a soft/API
    error -- see lib_snapshot.researcher.search_client.extract). ADV
    (adviserinfo.sec.gov) is handled separately by _lookup_sec_adv/FIX 2,
    since it isn't retrievable via Tavily's extract at all (a JS-rendered
    SPA -- confirmed by direct probe: a plain fetch of any
    adviserinfo.sec.gov page returns the same empty Angular app shell
    regardless of path)."""
    from lib_snapshot.researcher import search_client

    urls = sorted({
        r["url"] for r in results
        if r.get("url") and _is_high_value_source(r["url"], own_domain)
        and _extract_domain(r["url"]) != "adviserinfo.sec.gov"
    })
    if not urls:
        return {}
    return search_client.extract(urls)


# ---------------------------------------------------------------------------
# FIX 27 sub-change D — entity attribution on third-party snippets
# ---------------------------------------------------------------------------
# QA finding (real-mode Atlas Real Estate run): _filter_out_other_entities
# only drops a source that looks like ANOTHER identified entity's own site
# (own-domain-style signals) or a rejected SEC CRD -- it correctly does NOT
# drop ordinary third-party press/directory coverage, since that is exactly
# what the escalation angles exist to surface (dropping it by domain would
# break wide research). But a real run showed THREE different companies'
# dollar figures reaching the same extraction prompt with nothing telling
# the model which snippet was about which: junipersquare.com described a
# DIFFERENT "Atlas Real Estate Partners" (New York, "over $1B AUM") --
# the identical name, wrong company, so a name-only or domain-only signal
# could never disambiguate it; waterfallam.com's press release covered
# Waterfall Asset Management's "Waterfall Atlas Fund" ($485M fund, $1.2B
# deployable, Waterfall's own $4.8B AUM) -- a fund merely NAMED "Atlas".
# Rather than trying to decide correctness here, this prefixes each
# non-own-domain snippet with whatever name/location the SOURCE ITSELF
# names (best-effort regex, not NLP) so the extraction prompt's own new
# disambiguation instruction (see _EXTRACT_PROMPT_TMPL) has something
# concrete to compare against the already-known target identity. Kept
# deliberately short (a bracketed tag, not a sentence) to stay token-
# neutral across the ~15-20 non-own-domain sources typically present.
_ENTITY_LOCATION_RE = re.compile(
    r"\b([A-Z][A-Za-z.]+(?:[ -][A-Z][A-Za-z.]+){0,2})-based\b"
    r"|\b([A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?,\s*[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?)"
    r"\s*[–—-]\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d"
)
# QA finding (real-mode In-Rel Properties run): a hyphenated compound name
# ("In-Rel Properties") was mis-extracted as "Rel Properties" -- the
# character class excluded "-", so \b's word-boundary anchor started
# matching right after the hyphen instead of at the true start of the name.
# Widened to include "-" in each name token, verified this does NOT change
# any previously-correct match (Waterfall Asset Management, Atlas Real
# Estate Partners, Atlas Capital Group all still match identically).
#
# Adversarial review follow-up: the suffix list omitted "Estate" -- so a
# TARGET whose own name is bare "X Real Estate" (this pipeline's own target
# vertical, an extremely common naming pattern) could never get its OWN name
# tagged on a third-party source, while a confuser ending in an already-
# whitelisted word (e.g. "X Real Estate PARTNERS") got the full, confident
# tag on the SAME run -- reproduced live: the real Atlas Real Estate run's
# own genuine $2.5B disclosure (re-hosted on prnewswire.com) tagged only
# "[Denver]" (location-only), while a same-named but different New York
# "Atlas Real Estate Partners" got the complete "[Atlas Real Estate
# Partners, New York]" tag -- backwards from the intended effect. Added
# "Estate" (not "Real Estate" as one unit -- the existing multi-word group
# above already captures "Real" as a middle word).
_ENTITY_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z&.'-]+(?:\s[A-Z][A-Za-z&.'-]+){0,4}\s"
    r"(?:Partners|Capital|Management|Advisors|Adviser|Group|Holdings|Ventures|"
    r"Investments?|Properties|Realty|Estate|LLC|Inc\.?|LP))\b"
)

# A snippet's entity tag exists ONLY to help the LLM decide whether a figure
# belongs to the target -- if the tagger genuinely can't tell (empty prefix),
# that must never look identical to "verified own-domain, trusted directly"
# (see _EXTRACT_PROMPT_TMPL). This fixed marker distinguishes the two states
# cheaply (one short constant, not a per-source sentence).
_UNVERIFIED_SOURCE_TAG = "[unverified source] "


def _entity_prefix(text: str, firm_name: str = "") -> str:
    """Best-effort "[Name, Location]" tag from whatever a third-party source
    names itself/its subject as — empty string if neither is extractable
    (a real, expected outcome for generic forum/aggregator text, e.g.
    Reddit or a dictionary definition, confirmed against live Atlas data;
    never a hard requirement).

    Adversarial review follow-up: a co-mention snippet naming BOTH the
    target and some other company (e.g. "Blackstone Real Estate Partners
    and Atlas Real Estate Partners both bid on...") would tag with only
    whichever name regex.search() (leftmost match) happened to hit first --
    if that's the OTHER company, the prompt's own "ignore every figure in
    that snippet" instruction could wrongly discard a snippet that's ALSO
    genuinely about the target. Checks EVERY name match (not just the
    first) via finditer; if the target's OWN name (normalized via
    _normalize_firm_name_for_match -- the SAME strict, exact-identity
    comparison already used for SEC ADV matching, deliberately NOT the
    looser _brand_tokens check) is found among them VERBATIM, suppresses
    the tag entirely -- this snippet genuinely, exactly names the target
    too, so it must not carry a tag implying it's confidently about someone
    else. Exact-identity (not brand-token overlap) matters here: a
    confuser like "Atlas Real Estate Partners" sharing just the word
    "Atlas" with target "Atlas Real Estate" must NOT suppress its own
    tag -- that's precisely the disambiguation sub-change D exists to
    provide; only the target's OWN full name appearing verbatim should."""
    normalized_target = _normalize_firm_name_for_match(firm_name) if firm_name else ""
    name_matches = list(_ENTITY_NAME_RE.finditer(text or ""))
    if normalized_target and any(
        _normalize_firm_name_for_match(m.group(1)) == normalized_target for m in name_matches
    ):
        return ""
    loc_match = _ENTITY_LOCATION_RE.search(text or "")
    name = name_matches[0].group(1) if name_matches else None
    location = None
    if loc_match:
        location = loc_match.group(1) or loc_match.group(2)
    if name and location:
        return f"[{name}, {location}] "
    if name:
        return f"[{name}] "
    if location:
        return f"[{location}] "
    return ""


# ---------------------------------------------------------------------------
# FIX 2 — deterministic SEC IAPD (ADV) lookup, independent of Tavily
# ---------------------------------------------------------------------------
# Confirmed via direct live probe (not guessed from memory): adviserinfo.sec.gov
# itself is a JS-rendered SPA (every path returns the same static app shell to
# a plain fetch); its backend search API lives on a DIFFERENT host,
# api.adviserinfo.sec.gov, and IS a plain JSON REST endpoint.
_SEC_ADV_SEARCH_URL = "https://api.adviserinfo.sec.gov/search/firm"
_SEC_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_CORPORATE_SUFFIX_RE = re.compile(r"\b(llc|inc|l\.l\.c|l\.p|lp|corp|corporation|co)\b")


def _normalize_firm_name_for_match(name: str) -> str:
    """Strict identity normalization for SEC IAPD matching -- NOT the loose
    substring style used elsewhere in this module. IAPD's own name search is
    fuzzy and returns many firms sharing only a generic word; a real,
    confirmed false-positive risk this was built to avoid: a search for
    "Kairos Real Estate Partners" returns, as its only KAIROS-containing
    hit, a firm registered as "KAIROS" / "Redwood-Kairos Real Estate
    Partners" / "Kairos Investment Management Company" (kimc.com) -- a
    real, but DIFFERENT, similarly-named company. Token-overlap matching
    after stripping generic real-estate words ("real", "estate",
    "partners") would falsely accept this, since both names would then
    reduce to just {"kairos"}. Exact-string identity after normalizing
    punctuation/corporate suffixes correctly rejects it (their full names
    differ: "kairos real estate partners" != "redwood kairos real estate
    partners" and != "kairos")."""
    n = (name or "").lower()
    n = re.sub(r"[,.]", " ", n)
    n = _CORPORATE_SUFFIX_RE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def _lookup_sec_adv_hits(firm_name: str) -> list[dict]:
    """Raw SEC IAPD search hits for `firm_name`, unfiltered -- the shared
    network fetch behind both _lookup_sec_adv() (below) and FIX 28
    sub-change D (checking whether a REJECTED hit's own name actually
    matches the target better than the exact-match check gave it credit
    for, a sign the wrong own_domain anchor might have been chosen).
    Factored out so sub-change D never issues a second, duplicate live
    query for the same firm_name. Empty list on ANY network/parse failure
    -- callers already treat that identically to "no hits found"."""
    try:
        r = requests.get(_SEC_ADV_SEARCH_URL, params={"query": firm_name},
                         headers=_SEC_HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    return (data.get("hits") or {}).get("hits") or []


def _classify_sec_adv_hits(firm_name: str, hits: list[dict]) -> tuple[Optional[dict], list[str], list[dict]]:
    """Shared decision logic behind _lookup_sec_adv(): exact-normalized-name
    acceptance, brand-token-based rejection. Returns (match, rejected_crds,
    rejected_details) -- rejected_details additionally carries each
    rejected hit's own names, its best fuzzy similarity to the TARGET name,
    and the SPECIFIC name that produced that score (FIX 28 sub-change D),
    reusing the same rapidfuzz + matching/normalize.py machinery as
    _domain_name_similarity rather than a fourth name-comparison
    implementation.

    Adversarial review follow-up: the similarity search is restricted to
    the SAME names that actually share a brand token with the target --
    i.e. the reason this hit was flagged "plausibly confusable" in the
    first place -- not every name field a hit happens to carry. Scoring
    across ALL names (including ones sharing nothing with the target)
    could otherwise produce a "suspiciously close" signal driven entirely
    by an unrelated other_name, disconnected from why the hit was ever
    rejected."""
    query_norm = _normalize_firm_name_for_match(firm_name)
    if not query_norm:
        return None, [], []
    brand_tokens = _brand_tokens(firm_name)
    query_tokens_joined = " ".join(name_normalize.tokenize(firm_name))
    rejected_crds: list[str] = []
    rejected_details: list[dict] = []
    for hit in hits:
        src = hit.get("_source") or {}
        names = [src.get("firm_name", "")] + list(src.get("firm_other_names") or [])
        if any(_normalize_firm_name_for_match(n) == query_norm for n in names if n):
            crd = src.get("firm_source_id", "")
            return {
                "crd": crd,
                "matched_name": src.get("firm_name", ""),
                "all_names": names,
                "scope": src.get("firm_ia_scope", ""),
                "address": src.get("firm_ia_address_details", ""),
                "summary_url": f"https://adviserinfo.sec.gov/firm/summary/{crd}",
            }, [], []
        crd = src.get("firm_source_id", "")
        brand_sharing_names = [n for n in names if n and any(tok in n.lower() for tok in brand_tokens)]
        if crd and brand_sharing_names:
            rejected_crds.append(crd)
            scored = [
                (fuzz.token_sort_ratio(query_tokens_joined, " ".join(name_normalize.tokenize(n))), n)
                for n in brand_sharing_names
            ]
            best_similarity, best_name = max(scored, key=lambda pair: pair[0])
            rejected_details.append({
                "crd": crd, "names": names, "best_name": best_name,
                "similarity_to_target": best_similarity,
            })
    return None, rejected_crds, rejected_details


def _lookup_sec_adv(firm_name: str) -> tuple[Optional[dict], list[str]]:
    """Best-effort, deterministic SEC IAPD firm lookup by name -- never
    depends on Tavily surfacing an adviserinfo.sec.gov URL on its own.
    Returns (match, rejected_crds). match is None on ANY network/parse
    failure or when no confidently (exact-normalized-name) matching firm is
    found -- both a genuine "no ADV record" (many small RE operators are
    not SEC-registered investment advisers at all) and an infrastructure
    hiccup are treated identically here: this is a soft, best-effort
    enrichment, not a hard dependency, and must never raise or block the
    research pipeline.

    rejected_crds (FIX 5) lists CRD/firm_source_id values from hits that
    share the firm's distinctive brand token (see _brand_tokens) but did
    NOT pass the strict exact-name check -- i.e. a plausibly-confusable but
    CONFIRMED-different entity. A real run showed this matters beyond the
    adviserinfo.sec.gov/firm/summary/<crd> page itself: a general web
    search independently surfaced a MIRRORED copy of that same rejected
    entity's ADV brochure at reports.adviserinfo.sec.gov/reports/ADV/<crd>/...
    -- a different domain, unrelated to this lookup, that a caller can
    still recognize and drop by checking for the same CRD substring.

    Thin wrapper: the actual fetch/decision logic lives in
    _lookup_sec_adv_hits()/_classify_sec_adv_hits() (FIX 28), so callers
    needing sub-change D's extra rejected-hit similarity signal can reuse
    the SAME already-fetched hits without a second live query. This
    function's own observable behavior is unchanged."""
    hits = _lookup_sec_adv_hits(firm_name)
    match, rejected_crds, _rejected_details = _classify_sec_adv_hits(firm_name, hits)
    return match, rejected_crds


def _format_adv_result(firm_name: str, match: Optional[dict]) -> tuple[str, str]:
    """Returns (url, text) for the synthetic ADV "source" folded into the
    extraction snippets -- always present, so a missing ADV record is a
    recorded fact, never a silent omission (FIX 2's explicit requirement).
    KNOWN LIMITATION: this reports the firm's basic IAPD registration
    record (name/other names, registration scope, address) -- it does NOT
    include a parsed numeric AUM figure from the Form ADV Part 1 schedule
    itself. Retrieving that requires an additional, unverified
    report-generation step (IAPD serves the actual filed schedule as a
    generated PDF keyed by a report "editionID", not as a plain JSON field)
    that was not confirmed working during this fix -- flagged here rather
    than silently claimed as covered."""
    if match is None:
        url = f"https://adviserinfo.sec.gov/search?query={firm_name}"
        text = (f"No SEC Investment Adviser (ADV) record found on IAPD for "
               f"{firm_name!r} (searched by name; no confidently-matching "
               f"firm in the results).")
        return url, text
    other = [n for n in match["all_names"] if n and n != match["matched_name"]]
    text = (f"SEC IAPD record found: firm_name={match['matched_name']!r}, "
           f"CRD/firm_source_id={match['crd']}, registration scope={match['scope']!r}, "
           f"other registered names={other}, address={match['address']!r}. "
           f"Note: numeric AUM from the Form ADV schedule itself was not retrievable "
           f"through this lookup (see code comment) -- registration status/identity only.")
    return match["summary_url"], text


# ---------------------------------------------------------------------------
# FIX 4 — cache the raw research payload; replay without calling Tavily
# ---------------------------------------------------------------------------
def _research_payload_cache_path(firm_name: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", firm_name).strip("_") or "firm"
    return config.ROOT / "qa_out" / "debug" / f"research_payload_{slug}.json"


def _save_research_payload(firm_name: str, results: list[dict]) -> None:
    """DIAGNOSIS-ONLY caching (not extraction/gating logic): never runs in
    TEST_MODE, same reasoning as _dump_extraction_debug."""
    if config.TEST_MODE:
        return
    path = _research_payload_cache_path(firm_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"firm_name": firm_name, "results": results}, indent=2),
                    encoding="utf-8")


def _load_research_payload(firm_name: str) -> Optional[list[dict]]:
    path = _research_payload_cache_path(firm_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data.get("results")


def _extract(firm_name: str, results: list[dict]) -> tuple[dict, int, float, Optional[str], list[str], list[str]]:
    """Returns (parsed, tokens, cost, own_domain, rejected_crds,
    ambiguous_candidates). ambiguous_candidates is non-empty ONLY when FIX
    28 sub-change C parks on a conflicting-identity signal -- in that case
    parsed={}, tokens=0, cost=0.0, own_domain=None, and NO LLM call was
    made at all (see the early-return below)."""
    from lib_snapshot import models

    icp_prompt = config.load_prompt(config.ICP_PROMPT_NAME)

    # De-duplicate by URL before anything else: the SAME url (e.g. a fact
    # sheet PDF) routinely appears multiple times across different search
    # angles. Once FIX 1 substitutes a high-value source's FULL text, an
    # un-deduplicated repeat would show the LLM the identical full document
    # 3-4x over, wasting slots the 24-result display cap needs for OTHER
    # distinct sources.
    #
    # FIX 27 sub-change C: keep the LONGEST `content` copy for a repeated
    # URL, not whichever happened to be LAST in iteration order (the
    # original `deduped[u] = r` unconditional overwrite). A real Atlas run
    # showed the same own-domain press-release URL surfacing 3x with
    # content lengths 135/144/156 chars, all short stub fragments -- an
    # accident of which search angle's snippet Tavily returned last, not a
    # deliberate "best copy" choice.
    #
    # Adversarial review follow-up: "longest wins" alone is itself blind to
    # WHICH copy actually carries the disclosure -- reproduced live: a
    # longer, pure-boilerplate duplicate (nav/footer/social-links, no
    # figures at all) discarded a SHORTER duplicate that had the real dollar
    # signal, for a third-party URL with no full-text-fetch recovery path
    # (only own-domain/.pdf/ADV sources get full-text fetched at all). Now
    # ranks by _signal_class first (currency > units > none) and only
    # breaks a tie within the SAME rank by length -- a signal-bearing copy
    # can never lose to a signal-less one merely for being shorter, while
    # among equally-relevant copies the longest (most complete) one still
    # wins, matching the real Atlas case above (135/144/156-char stubs
    # split currency/units/currency -- the two currency-rank copies tie-
    # break to the longest, 156, the same outcome as before this follow-up).
    deduped: dict[str, dict] = {}
    for r in results:
        u = r.get("url")
        if not u:
            continue
        existing = deduped.get(u)
        if existing is None:
            deduped[u] = r
            continue
        r_content = r.get("content") or ""
        existing_content = existing.get("content") or ""
        r_rank = _SIGNAL_RANK[_signal_class(r_content)]
        existing_rank = _SIGNAL_RANK[_signal_class(existing_content)]
        if r_rank > existing_rank or (r_rank == existing_rank and len(r_content) > len(existing_content)):
            deduped[u] = r
    dedup_results = list(deduped.values())

    # QA finding (this fix's own live verification run): inferring own-domain
    # from the DEDUPLICATED list counts "how many distinct pages", which
    # produced a genuine 3-vs-3 tie between kairosrep.com (the real target,
    # Kairos Real Estate Partners) and kimc.com (a different, similarly-named
    # company, Kairos Investment Management Company) -- broken incorrectly,
    # by insertion order, in kimc.com's favor. The UN-deduplicated `results`
    # counts "how many distinct SEARCH ANGLES surfaced this domain" instead --
    # a much stronger signal, since the firm's own site is naturally
    # rediscovered by many differently-worded angles, while an incidental
    # same-named company is more likely to be surfaced by only one or two.
    own_resolution = _resolve_own_domain(results, firm_name)
    own_domain = own_resolution.domain
    adv_hits = _lookup_sec_adv_hits(firm_name)
    adv_match, rejected_crds, rejected_adv_details = _classify_sec_adv_hits(firm_name, adv_hits)

    # FIX 28 sub-change D: a rejected ADV record whose own name is a
    # SUSPICIOUSLY close match to the target (near-exact, not just sharing
    # a brand token) is itself evidence the resolved own_domain anchor might
    # be wrong -- either that record actually IS the target under a minor
    # formatting variant the strict exact-match missed, or it's genuinely
    # a different, very-similarly-named firm and the situation is
    # ambiguous either way. Threshold set well above the real, CONFIRMED-
    # different "Redwood-Kairos Real Estate Partners" case -- 81.8 against
    # the exact production firm_name string ("Kairos Real Estate Partners,
    # LLC", confirmed via qa_out/realmode_Kairos_Real_Estate_Partners.md
    # and this exact fixture in tests/test_icp_gate.py), verified via live
    # diagnosis before implementing this (adversarial review follow-up:
    # the originally-documented 87.1 was computed against the SAME name
    # without its ", LLC" suffix -- a different input than what the tests
    # and production path actually use; corrected here to the real figure,
    # still comfortably below this threshold either way) -- so a genuinely
    # different firm sharing most-but-not-all of the target's words is
    # never flagged, only a near-exact name the strict check narrowly missed.
    #
    # Adversarial review follow-up: the OLD "and not own_domain" gate meant
    # a CONFIDENTLY-but-WRONGLY-resolved own_domain (a domain that scores a
    # full, syntactic match yet isn't actually the target's site) completely
    # silenced a simultaneous near-exact SEC ADV rejection -- the two
    # signals are orthogonal (domain-string derivation vs. SEC registrant
    # name) and neither validates the other, so a confident-looking
    # own_domain should never be allowed to suppress this independent
    # safety net.
    suspicious_adv_rejection = any(
        d["similarity_to_target"] >= _SEC_ADV_SUSPICIOUS_REJECTION_THRESHOLD
        for d in rejected_adv_details
    )
    ambiguous = own_resolution.ambiguous or suspicious_adv_rejection
    if ambiguous:
        candidates = sorted(set(own_resolution.candidates) | (
            {d["best_name"] for d in rejected_adv_details
            if d["similarity_to_target"] >= _SEC_ADV_SUSPICIOUS_REJECTION_THRESHOLD and d.get("best_name")}
            if suspicious_adv_rejection else set()
        ))
        # FIX 28 sub-change C: conflicting signals PARK, they do not
        # proceed -- a visible park is strictly better than a confident
        # wrong answer. Returns BEFORE the LLM call: an ambiguous entity
        # identity makes any extraction unreliable regardless of prompt
        # quality, so there is nothing worth spending the call on (same
        # "fail cheap" principle as HARD RULE 5's infrastructure-error
        # parks elsewhere in this pipeline).
        _dump_extraction_debug(firm_name, "(ambiguity park -- no prompt built, no LLM call made)",
                              [], [], filter_info={
                                  "own_domain": None,
                                  "other_domains": set(),
                                  "rejected_crds": rejected_crds,
                                  "dropped_urls": [],
                                  "ambiguous_candidates": candidates,
                              })
        return {}, 0, 0.0, None, rejected_crds, candidates

    # FIX 5: DROP other-entity sources entirely, not just deprioritize them.
    # A real run showed own_domain and the ADV rejection were both computed
    # correctly and then ignored -- the wrong entity's sources still reached
    # the prompt (just ranked lower) and the extraction LLM used them
    # anyway, attributing a different real company's AUM/founding year to
    # the target firm.
    other_domains = _identify_other_entity_domains(dedup_results, own_domain, firm_name)
    filtered_results = _filter_out_other_entities(dedup_results, own_domain, firm_name, rejected_crds)
    dropped_urls = [r.get("url", "") for r in dedup_results
                    if r.get("url", "") not in {f.get("url", "") for f in filtered_results}]

    full_text_by_url = _fetch_full_text_for_high_value_sources(filtered_results, own_domain)

    adv_url, adv_text = _format_adv_result(firm_name, adv_match)
    adv_result = {"url": adv_url, "title": "SEC IAPD (ADV) record", "content": adv_text, "score": 1.0}
    combined_results = filtered_results + [adv_result]

    # Prioritize (a) results with an actual size/dollar/unit signal, THEN
    # (b) high-value sources without one (own domain / PDF / ADV) ahead of
    # generic third-party/aggregator noise -- so a real source can never be
    # cut by the 24-result display cap in favor of a RocketReach blurb.
    prioritized = sorted(
        combined_results,
        key=lambda r: (not _has_size_signal([r]), not _is_high_value_source(r.get("url", ""), own_domain)),
    )
    snippet_lines = []
    for r in prioritized[:24]:
        url = r.get("url", "")
        content = r.get("content") or ""
        full_text = full_text_by_url.get(url) or ""
        if url == adv_url:
            body = content                          # FIX 2: ADV text/no-match note, never truncated
            source_text = content
        else:
            # FIX 1: full parsed text -- but NOT literally unbounded. QA
            # finding (this fix's own verification run): a live Kairos run
            # surfaced a 48,232-char PDF via general web search (it happened
            # to be the WRONG "Kairos" entity's SEC ADV brochure -- a real,
            # off-target source, but any single huge legitimate source would
            # have caused the exact same failure). Left fully uncapped, this
            # blew the total prompt to 213,634 chars and both extraction
            # attempts came back completely empty (confirmed: verified via
            # the debug dump's own raw-response section) -- a reasoning
            # model spending its token budget digesting one oversized source
            # before ever reaching visible output. Deliberate, evidence-based
            # deviation from "include the FULL text" taken literally:
            # capped per source, generous enough that every genuinely-
            # relevant source actually observed (731-6,418 chars) passes
            # through completely untouched.
            #
            # FIX 27 sub-change A: a fetched full text can ALSO be a
            # degraded page shell -- a real Atlas run's /extract call
            # returned a login/nav-links shell for an own-domain URL whose
            # search-result `content` was merely a short (but real) stub.
            # The previous code substituted full_text unconditionally
            # whenever present; now it wins only when it is actually
            # BETTER -- longer than `content`, and not losing a currency/
            # size signal `content` already had (reusing _SIZE_SIGNAL_RE,
            # not a second regex). A shell that's shorter, or that drops a
            # signal `content` had, is discarded in favor of `content`.
            #
            # Adversarial review follow-up: a plain "does full_text have ANY
            # signal" boolean check has its own blind spot -- reproduced
            # live: a degraded shell containing only an incidental, unrelated
            # units/sq-ft mention (nav chrome, e.g. "Browse 1,200 sq ft
            # office suites") satisfied the OLD boolean check and replaced a
            # `content` that had the REAL currency disclosure. Ranked via
            # _signal_class (currency > units > none) instead of a plain
            # bool: full_text now wins only when its signal class is AT
            # LEAST as strong as content's -- a units-only full_text can
            # never outrank a currency-bearing content, while a genuinely
            # all-units-proxy source (content and full_text both units-only,
            # or both none) is unaffected.
            content_rank = _SIGNAL_RANK[_signal_class(content)]
            full_text_rank = _SIGNAL_RANK[_signal_class(full_text)]
            use_full_text = bool(full_text) and len(full_text) > len(content) and (
                full_text_rank >= content_rank
            )
            if use_full_text:
                source_text = full_text
                body = _windowed_slice(full_text, _FULL_TEXT_MAX_CHARS)
            else:
                source_text = content
                body = _windowed_slice(content, 500)  # unchanged width for third-party pages
        # FIX 27 sub-change D: a non-own-domain snippet may describe a
        # DIFFERENT company (see _entity_prefix's own docstring) -- tag it
        # with whatever name/location the source itself names, extracted
        # from the same (pre-window) source_text so the tag survives even
        # when the disambiguating text itself sits outside the windowed
        # body. Own-domain sources and the ADV record are the target by
        # definition and are never tagged.
        #
        # Adversarial review follow-up: when _entity_prefix can't find a
        # name/location at all (returns ""), an un-prefixed third-party
        # snippet was visually IDENTICAL to a genuine own-domain one --
        # reproduced live against a real, wrong-entity, dollar-figure-
        # bearing source (calcalistech.com, an unrelated company called
        # "Atlas Invest") that the prompt's own "an un-prefixed snippet can
        # be trusted directly" instruction would have silently endorsed.
        # Falls back to _UNVERIFIED_SOURCE_TAG so "own-domain, verified" and
        # "third-party, tagger found nothing" are never the same signal.
        prefix = ""
        if url != adv_url and _extract_domain(url) != own_domain:
            prefix = _entity_prefix(source_text, firm_name) or _UNVERIFIED_SOURCE_TAG
        snippet_lines.append(f"- [{url}] {r.get('title', '')}: {prefix}{body}")
    snippets = "\n".join(snippet_lines) or "(no results found)"

    prompt = _EXTRACT_PROMPT_TMPL.format(icp_prompt=icp_prompt, name=firm_name, snippets=snippets)
    debug_attempts: list[tuple[str, str]] = []

    # FIX 6: emit a VISIBLE warning the moment the primary model returns
    # empty, instead of only recording it silently in the debug dump --
    # this must never again pass unnoticed. finish_reason == "length" with
    # empty text is the confirmable signature of a reasoning model
    # exhausting its token budget on hidden reasoning before ever producing
    # visible output (see models.complete_json_retry's docstring); reported
    # here whenever it's what actually happened, not assumed.
    def _capture(model_name: str, text: str, finish_reason: str, reasoning_tokens: int) -> None:
        debug_attempts.append((model_name, text))
        if not text.strip():
            print(f"[EXTRACTION WARNING] {firm_name}: {model_name} returned EMPTY "
                 f"(finish_reason={finish_reason!r}, reasoning_tokens={reasoning_tokens}) "
                 f"-- falling through to the next attempt/escalation.")

    try:
        # FIX 7: gpt-5-mini's default reasoning effort was consuming its
        # ENTIRE max_completion_tokens budget on hidden reasoning before
        # writing any visible output (confirmed: finish_reason="length",
        # reasoning_tokens=2048 -- the exact budget). Confirmed live
        # against this project's actual Azure deployment (not assumed) that
        # "minimal" is accepted and, on the real non-trivial extraction
        # prompt, produces reasoning_tokens=0 with a correct, complete
        # result -- more reliable headroom than raising the token budget,
        # per instruction. Scoped to this call only, not a global default.
        parsed, usage = models.complete_json_retry(
            prompt, temperature=0.1, max_tokens=1500, debug_capture=_capture,
            reasoning_effort="minimal",
        )
    finally:
        _dump_extraction_debug(firm_name, prompt, combined_results, debug_attempts,
                              filter_info={
                                  "own_domain": own_domain,
                                  "other_domains": other_domains,
                                  "rejected_crds": rejected_crds,
                                  "dropped_urls": dropped_urls,
                              })
    # FIX 13 (real-mode Kairos run): own_domain/rejected_crds were computed
    # here and then only ever written to the debug dump -- Phase 4
    # (personalization.py) runs its OWN separate, unfiltered search and had
    # no way to reuse this phase's already-verified entity identity, so a
    # wrong-entity source could reach the email/deck via the personalization
    # hooks even after this module correctly excluded it here. Returned so
    # research_and_gate() can persist them onto the research object Phase 4
    # receives, instead of Phase 4 re-deriving (and potentially
    # disagreeing) from a differently-worded set of search results.
    return parsed, usage.total_tokens, usage.cost_usd, own_domain, rejected_crds, []


def research_and_gate(firm_name: str, progress: Progress = _noop) -> GateResult:
    """Phase 2 entry point. TEST_MODE returns a canned fixture; otherwise
    researches the firm and applies the binary gate via the LLM, following
    icp_scoring_v2_2.md's proxy taxonomy and asymmetric gate logic verbatim."""
    if config.TEST_MODE:
        return _test_mode_gate(firm_name)

    # FIX 4: replay from a cached raw research payload instead of spending
    # live Tavily search budget, when explicitly enabled and a cache exists
    # for this firm. Falls back to a live run (and caches its result) if no
    # cache is found — never a hard requirement.
    if config.REPLAY_RESEARCH_FROM_CACHE:
        cached = _load_research_payload(firm_name)
        if cached is not None:
            progress(f"{firm_name}: replaying research from cached payload "
                     f"(qa_out/debug/research_payload_*.json) — no live Tavily calls.")
            results, search_calls = cached, 0
        else:
            progress(f"{firm_name}: REPLAY_RESEARCH_FROM_CACHE set but no cache found — "
                     f"running live research and caching the result.")
            try:
                results, search_calls = _run_research(firm_name, progress)
            except InfrastructureError:
                raise
            _save_research_payload(firm_name, results)
    else:
        try:
            results, search_calls = _run_research(firm_name, progress)
        except InfrastructureError:
            raise
        _save_research_payload(firm_name, results)

    if search_calls >= config.MAX_SEARCH_CALLS_PER_FIRM and not results:
        return GateResult(icp_result="parked", icp_reason="budget_cap_hit before any result",
                          park_reason="budget_cap_hit", park_source="research",
                          search_calls=search_calls)

    parsed, tokens, cost, own_domain, rejected_crds, ambiguous_candidates = _extract(firm_name, results)
    if ambiguous_candidates:
        # FIX 28 sub-change C: own_domain resolution found two or more
        # candidate domains with comparable evidence (or a suspiciously
        # close SEC ADV near-match) and neither the hit-count winner nor
        # the name-similarity winner clearly dominates. Reusing the SAME
        # park shape the batch pipeline already emits for CSV-level
        # firm-match ambiguity (pipeline.py's "firm_match_ambiguous",
        # park_source="research") -- here park_source="match" instead,
        # since this ambiguity is about WHICH real-world entity a search
        # result belongs to, not which CSV row matched a connection.
        progress(f"{firm_name}: firm identity ambiguous ({len(ambiguous_candidates)} candidate "
                f"domains: {', '.join(ambiguous_candidates)}); parking rather than guessing.")
        return GateResult(
            icp_result="parked",
            icp_reason=f"firm identity ambiguous — {len(ambiguous_candidates)} candidate domains",
            park_reason="firm_identity_ambiguous", park_source="match",
            search_calls=search_calls,
            research={"ambiguous_candidate_domains": ambiguous_candidates},
        )
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
        gate_value_usd=parsed.get("aum_value_usd"),
        aum_tag=aum_tag,
        proxy_type=parsed.get("proxy_type") or None,
        proxy_basis=parsed.get("proxy_basis", ""),
        source_note=parsed.get("source_note", ""),
        unit_count=parsed.get("unit_count"),
        sqft_count=parsed.get("sqft_count"),
    )
    park_reason = None
    if icp_result == "parked":
        park_reason = "aum_unverified" if aum_tag == "AUM_UNVERIFIED" else "gate_parked"

    # FIX 22 (real-mode In-Rel Properties run): disclosed_figure/
    # disclosed_label are the ONLY AUM-related fields deck_filler.py/
    # email_drafter.py may read going forward -- verbatim, no arithmetic,
    # never the gate_value_usd apply_gate() used internally above (that
    # value -- a GAV haircut or a units/sqft derivation -- never appears
    # anywhere in this `research` dict at all; it exists only as a local
    # variable inside apply_gate()'s own call frame, already returned and
    # gone by this point).
    disclosed_figure, disclosed_label = _disclosed_figure_and_label(parsed)
    if icp_result == "qualified" and not disclosed_figure:
        print(f"[DISCLOSED FIGURE WARNING] {firm_name}: qualified (aum_tag={aum_tag!r} "
             f"proxy_type={parsed.get('proxy_type')!r}) but disclosed_figure came back EMPTY "
             f"-- every qualifying path should have SOME disclosed signal to show a prospect. "
             f"This is a bug, not an expected blank -- the deck/email stat tile will be blank; "
             f"investigate the extraction output for this firm.")

    # aum_display is kept ONLY for operator-facing views (status.py's
    # prospect brief, scripts/realmode_smoke.py's report) -- a
    # human-readable combination of the two verbatim fields above, NEVER
    # the FIX-21-era derived-dollar string this superseded.
    if disclosed_figure and disclosed_label:
        aum_display = f"{disclosed_figure} ({disclosed_label})"
    else:
        aum_display = disclosed_figure or disclosed_label

    research = {
        "firm_oneliner": parsed.get("firm_oneliner", ""),
        "aum_value_usd": parsed.get("aum_value_usd"),
        "aum_display": aum_display,
        "disclosed_figure": disclosed_figure,
        "disclosed_label": disclosed_label,
        "aum_asof": parsed.get("aum_asof", ""),
        "aum_tag": aum_tag,
        "proxy_type": parsed.get("proxy_type") or "",
        "proxy_basis": parsed.get("proxy_basis") or "",
        "geography": parsed.get("geography", ""),
        "capital_stack": parsed.get("capital_stack", ""),
        "founded_year": parsed.get("founded_year", ""),
        "domain": parsed.get("domain", ""),
        "recent_event": parsed.get("recent_event", ""),
        "sources": parsed.get("sources") or [],
        "own_domain": own_domain or "",
        "rejected_crds": rejected_crds or [],
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
