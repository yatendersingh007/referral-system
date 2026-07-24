"""Firm-specific pitch deck generator (14-slide PPTX).

Replaces the static Kingbird PDF with a deck customized per qualified firm:
- 10 fixed slides (the X Analyst model, six services, service detail, why,
  engagement, onboarding, pilot) use the exact Kingbird-deck copy (constants below).
- 4 custom slides (cover, firm profile, where-this-fits, thank-you) are filled
  from the firm's research via PREMIUM_MODEL.

Python assembles a full data JSON and shells out to deck_gen/build_deck.js
(pptxgenjs renderer), which is a pure data-driven renderer.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from typing import Optional

from .. import config, db, models
from ..researcher import search_client

# NOTE on validate.py: pptxgenjs emits <p:notesMasterIdLst> AFTER <p:sldIdLst>,
# which the strict OOXML schema validator (validate.py) flags. We deliberately
# do NOT reorder it: real PowerPoint (verified via COM open), Google Slides, and
# LibreOffice all accept the pptxgenjs ordering, whereas rewriting the XML to the
# "schema-correct" order makes PowerPoint report the file as corrupt. Shipping a
# file the target app opens beats satisfying the strict validator. See caveats.

# ---------------------------------------------------------------------------
# FIXED slide content (verbatim from the Kingbird pitch deck)
# ---------------------------------------------------------------------------
_COMPARISON = {
    "section": "Section 02",
    "title": "Not an agency.  Not a vendor.  Yours.",
    "intro": ("We embed dedicated senior-level professionals directly into your operating "
              "team. They report into your leadership, use your systems, follow your "
              "processes, and know your portfolio — with the cost profile of a global "
              "delivery model."),
    "leftHeader": "Traditional Outsourcing",
    "leftItems": [
        "Shared analysts across multiple clients",
        "Ticket-based workflow, delayed responses",
        "External vendor mindset",
        "Junior staff on your account",
        "Client-side management overhead",
    ],
    "rightHeader": "The X Analyst Approach",
    "rightItems": [
        "One dedicated professional per role, per client",
        "Real-time integration with your team",
        "Extension of your operating team",
        "5+ years senior-level experience minimum",
        "Managed by X Analyst leadership on our side",
    ],
    "result": ("The result: senior-level talent at a fraction of the fully-loaded US cost — "
               "without sacrificing quality, accountability, or cultural fit."),
}

_SIX_FUNCTIONS = {
    "section": "Section 03",
    "title": "Six functions we cover for real estate managers",
    "intro": "Every function below is delivered by a dedicated professional, integrated into your workflow.",
    "cards": [
        {"icon": "calculator", "header": "Fund Accounting & Administration",
         "desc": "Multi-vehicle fund accounting, capital call & distribution processing, waterfall calculations, GAAP financials."},
        {"icon": "layer-group", "header": "Property Accounting",
         "desc": "Property-level GL, monthly close, budget vs actuals, CAM reconciliations, Yardi/MRI/RealPage support."},
        {"icon": "chart-line", "header": "Asset Management Support",
         "desc": "Portfolio performance reporting, hold/sell analysis, budget review, distribution modeling, deal tracking."},
        {"icon": "magnifying-glass-dollar", "header": "Underwriting & Acquisitions",
         "desc": "New deal modeling, market comp analysis, sensitivity runs, sourcing pipeline support, IC memo preparation."},
        {"icon": "file-lines", "header": "Investor Reporting",
         "desc": "Quarterly LP reporting, capital account statements, custom family office reports, investor portal management."},
        {"icon": "coins", "header": "Capital Markets Support",
         "desc": "Debt refi analysis, preferred equity structuring, waterfall verification, lender package preparation."},
    ],
    "footer": "The next three slides expand each function with specifics.",
}

_SERVICE_DETAILS = [
    {
        "header": "Service Detail  |  01 – 02",
        "title": "Fund + Property Accounting",
        "leftIcon": "calculator", "rightIcon": "layer-group",
        "leftHeader": "Fund Accounting & Administration",
        "leftBullets": [
            "Multi-vehicle fund accounting (LP, GP, Co-GP funds, SPVs, blockers, REIT sub-vehicles)",
            "Capital call and distribution notice preparation",
            "European, American, and deal-by-deal waterfall calculations",
            "Preferred equity accrual, PIK interest, hurdle tracking",
            "Monthly and quarterly GAAP financial statements",
            "Audit package preparation and auditor liaison",
            "K-1 and tax return coordination with your tax preparer",
            "Reconciliation of fund GL to portfolio-level records",
        ],
        "rightHeader": "Property Accounting",
        "rightBullets": [
            "Property-level general ledger maintenance",
            "Monthly close, journal entries, accruals",
            "Bank reconciliations across property operating accounts",
            "Budget-to-actual variance analysis for asset managers",
            "CAM (Common Area Maintenance) reconciliations",
            "Property manager and operating partner coordination, invoice review",
            "Yardi, MRI, RealPage, AppFolio, Entrata support",
            "Consolidation from property GL up to fund GL",
        ],
    },
    {
        "header": "Service Detail  |  03 – 04",
        "title": "Asset Management + Underwriting Support",
        "leftIcon": "chart-line", "rightIcon": "magnifying-glass-dollar",
        "leftHeader": "Asset Management Support",
        "leftBullets": [
            "Portfolio performance dashboards and management reporting",
            "Property-level hold/sell analysis and disposition modeling",
            "Annual budget review and mid-year re-forecasting",
            "Distribution and refinancing scenario modeling",
            "Rent roll analysis, occupancy tracking, lease abstraction",
            "Cap-ex tracking, budget variance, ROI on renovations",
            "Loan compliance and covenant monitoring",
            "Data preparation for quarterly IC and investor updates",
        ],
        "rightHeader": "Underwriting & Acquisitions",
        "rightBullets": [
            "Full multifamily deal underwriting from OM through IC memo",
            "Sensitivity analysis on rent growth, cap rates, and exit assumptions",
            "Market and submarket research, rent comp analysis",
            "Sales comp analysis and stabilized-yield calculations",
            "Structuring analysis: LP, GP, Preferred Equity, Mezzanine positions",
            "Preliminary and refined return waterfalls per capital stack scenario",
            "Sourcing pipeline management and screening",
            "IC memo drafting and deal materials preparation",
        ],
    },
    {
        "header": "Service Detail  |  05 – 06",
        "title": "Investor Reporting + Capital Markets Support",
        "leftIcon": "file-lines", "rightIcon": "coins",
        "leftHeader": "Investor Reporting",
        "leftBullets": [
            "Quarterly LP reporting packages",
            "Capital account statements per investor per deal",
            "Custom reporting for family offices and institutional LPs",
            "ILPA-format templates and reporting compliance",
            "Investor portal management (Juniper Square, Investran, Backstop)",
            "Investor onboarding, KYC/AML documentation collection",
            "Distribution and capital call notice distribution",
            "Ad-hoc data pulls and one-off investor requests",
        ],
        "rightHeader": "Capital Markets Support",
        "rightBullets": [
            "Debt refinancing analysis and lender package preparation",
            "Preferred equity structuring support and cap stack modeling",
            "Waterfall verification across LP/GP/Co-GP positions",
            "Subscription line and credit facility documentation",
            "Loan portfolio management and covenant tracking",
            "Rate hedge analysis and interest rate cap monitoring",
            "Lender due diligence responses and PBC schedules",
            "Refinancing scenario analysis and hold period optimization",
        ],
    },
]

_WHY = {
    "title": "Why X Analyst",
    "subtitle": "The four things that make embedded senior talent work at scale.",
    "cards": [
        {"icon": "users", "header": "Senior talent, not juniors",
         "desc": "5+ years of US real estate finance experience minimum. Chartered accountants, MBAs, and CFA charterholders on our team."},
        {"icon": "clock", "header": "US business hours coverage",
         "desc": "Our team works US EST/PST hours. Same-day turnaround on your requests. You never wait for time zones."},
        {"icon": "shield-halved", "header": "Dedicated, not shared",
         "desc": "One professional per role, per client. They know your portfolio, your systems, and your investors — not 15 other clients."},
        {"icon": "coins", "header": "50–70% cost advantage",
         "desc": "Fully-loaded cost is a fraction of a US-based equivalent hire — with the same output quality and English fluency."},
    ],
    "costLine": ("An analyst at your desk has a fully-loaded cost of $145K–$180K.  "
                 "Ours: closer to $55K–$75K, senior quality, dedicated to you."),
}

_ENGAGEMENT = {
    "title": "One engagement model. Fully dedicated.",
    "intro": "Every X Analyst professional is full-time on one client — no shared seats, no ticket queues.",
    "boxTitle": "Dedicated Full-Time",
    "boxSub": "40 hours/week  ·  one role  ·  yours alone",
    "bestForLabel": "Best for",
    "bestFor": "Ongoing functions — property accounting, fund accounting, investor reporting, underwriting, capital markets support.",
    "bullets": [
        "One dedicated professional per role",
        "Full US business hours coverage",
        "Reports into your leadership",
        "Uses your systems & processes",
        "Monthly fixed pricing",
    ],
    "carryHeader": "What X Analyst carries on our side",
    "carry": [
        {"label": "Recruiting & vetting", "desc": "You interview and select from 2–3 pre-vetted senior candidates — we never place without your yes."},
        {"label": "Management & QC", "desc": "Delivery leadership on our side reviews output, runs weekly check-ins, and owns accountability."},
        {"label": "Continuity & backup", "desc": "Documentation standards and cross-training so coverage never depends on one person's laptop."},
        {"label": "Replacement guarantee", "desc": "If a professional isn't working out, we re-run selection at no additional cost."},
    ],
    "footer": "Start with one role. Add seats as the workload proves itself — most clients grow to two or three within the first year.",
}

_TIMELINE = {
    "title": "From first call to fully-integrated in four weeks",
    "intro": "Our onboarding is structured to minimize your team's ramp-up burden.",
    "weeks": [
        {"num": 1, "week": "Week 1", "title": "Requirements & Scoping",
         "desc": "Working session with your team to define role responsibilities, systems access, tools, and success metrics. JD refinement."},
        {"num": 2, "week": "Week 2", "title": "Talent Selection",
         "desc": "We present 2–3 pre-vetted senior candidates. You interview. You pick. We don't move to Week 3 until you've said yes."},
        {"num": 3, "week": "Week 3", "title": "Systems & Access",
         "desc": "IT setup, VPN/systems access, credentials for Yardi/MRI/investor portal/GL. Documentation and process handover starts."},
        {"num": 4, "week": "Week 4", "title": "Shadow & Handover",
         "desc": "Your team member shadows current workflows. Live-fire trial on real tasks with your review. Full production start end of week."},
    ],
    "footer": "You're not managing the hire. We are. You get the output.",
}

_PILOT = {
    "section": "Section 05",
    "title": "A low-risk way to start",
    "intro": "A 90-day pilot on one full-time role — you evaluate, then decide whether to scale.",
    "steps": [
        {"week": "Week 1", "title": "Discovery workshop", "desc": "Deep dive on one workflow with your controller or ops lead"},
        {"week": "Week 2", "title": "Role scoping", "desc": "JD, systems access plan, success metrics"},
        {"week": "Weeks 3–4", "title": "Talent selection & onboarding", "desc": "Interview and select from 2–3 candidates"},
        {"week": "Weeks 5–13", "title": "90-day pilot", "desc": "Live production work. Weekly reviews. Monthly deliverable scorecard."},
        {"week": "Week 14", "title": "Continue, scale, or exit", "desc": "You decide. No long-term commitment beyond the pilot."},
    ],
}

_FOUNDERS = "Harpreet Singh  |  Founder    ·    Baljeet  |  Head of Operations"
_ROLE_CHOICES = ["fund accountants", "property accountants", "underwriters",
                 "asset management analysts", "investor reporting analysts",
                 "capital markets analysts"]


class DeckError(RuntimeError):
    pass


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip())
    return re.sub(r"_+", "_", s).strip("_") or "firm"


def _short_name(name: str) -> str:
    """A short display name: drop entity suffixes, keep the first 1-2 tokens."""
    n = re.sub(r"[,\.]", "", name or "").strip()
    drop = {"llc", "lp", "inc", "incorporated", "corp", "corporation", "company",
            "group", "holdings", "partners", "capital", "management", "investments",
            "investment", "properties", "property", "realty", "&", "the"}
    toks = [t for t in n.split() if t.lower() not in drop]
    if not toks:
        toks = n.split()[:1]
    return " ".join(toks[:2]) if toks else (name or "your firm")


def _roles_line(roles: list[str]) -> str:
    roles = [r for r in (roles or []) if r][:3]
    if not roles:
        roles = ["fund accountants", "property accountants", "asset management analysts"]
    if len(roles) == 1:
        return roles[0]
    return ", ".join(roles[:-1]) + ", and " + roles[-1]


_EXTRACT_PROMPT = """You are preparing a customized pitch deck for X Analyst (offshore \
property/fund accounting and RE finance support) targeting the firm below. From the \
research, extract a firm profile and 3-5 "fit points" mapping the firm's situation to \
X Analyst's six services (fund accounting, property accounting, asset management support, \
underwriting & acquisitions, investor reporting, capital markets support).

Firm: {name}
ICP note: {icp_reason}
Lead service focus for this client: {service_focus}
Research:
{research}

Return a SINGLE JSON object (no prose, no markdown fence):
{{
  "firm_profile": {{
    "lead_sentence": "one vivid sentence describing the firm (asset class + strategy), <= 22 words",
    "headquarters": "city, state/country or empty",
    "history_snippet": "founding/ownership/history one-liner or empty",
    "asset_class_focus": "e.g. workforce & market-rate multifamily plus student housing",
    "investment_strategy": "e.g. value-add through core-plus, deal-by-deal co-investor",
    "deal_structure": "e.g. LP/GP/Co-GP/Preferred Equity or empty",
    "investor_base": "e.g. family offices and institutional investors or empty",
    "geographic_focus": "e.g. US secondary/tertiary markets, Canada, Mexico",
    "bullets": ["4-6 short factual bullets about the firm (<= 18 words each)"],
    "stats": [{{"label": "Gross Asset Value", "value": "$4.2B", "sublabel": "invested since 2008"}}],
    "capital_stack_line": "e.g. LP, GP, Co-GP, Preferred Equity, Mezzanine or empty"
  }},
  "fit_points": [
    {{"icon_label": "waterfall", "bold_label": "Complex waterfall verification",
      "description": "1-2 sentences mapping the firm's situation to a specific X Analyst service."}}
  ],
  "relevant_roles": ["fund accountants", "property accountants"],
  "confidence": "low|medium|high"
}}

Rules:
- bullets: REQUIRE 4-6 bullets. Derive AGGRESSIVELY from the research: founding
  year / ownership, HQ + office locations, portfolio size (units / properties /
  states / sq ft), asset class + strategy, tenant or investor base, and notable
  operational traits (self-managed, vertically integrated, in-house maintenance
  or construction, etc.). Produce fewer than 4 ONLY if the research is genuinely
  near-empty. Do not pad with generic filler.
- Write each bullet as a clean, declarative statement. NO question marks, NO
  hedging words (possibly / maybe / implied / likely / appears), NO meta-commentary
  and NO references to "the research", "stats", or "snippet". If you are not
  confident about a fact, OMIT that bullet rather than hedge.
- stats: 2-4 items. Beyond AUM/GAV, accept: unit count, property count,
  states/markets count, employees, "Since {{founding year}}". Label each properly
  (label = metric name, value = the figure, sublabel = short qualifier). Every
  stat VALUE must contain a real number. NEVER emit placeholder stats such as
  "N/A", "Not specified", "Unknown", or "Multiple" — omit the stat entirely if
  you do not have a figure. NEVER invent a number not in the research or ICP note.
- 3 to 5 fit_points, each tied to a real, specific detail about THIS firm.
- relevant_roles: 2-3 chosen from: fund accountants, property accountants, underwriters,
  asset management analysts, investor reporting analysts, capital markets analysts.
- Leave a field an empty string if the research does not support it. Output valid JSON only.

STRICT DATA RULES:
- stats: ONLY include numbers explicitly stated in the research text. Never round up, infer, extrapolate, or estimate.
- If AUM is tagged "AUM_ESTIMATED" in the research, do NOT include it as a stat box. Instead, mention it softly in the lead sentence ("with an estimated portfolio of ~$X").
- Acceptable stat types: unit count (if stated), property count (if stated), founding year (if stated), employee count (if stated), square footage (if stated), IRR/multiple (only if publicly reported).
- If you can only find 1 verified stat, return 1 stat. An empty stats array is better than a fabricated one.
- For bullets: only state facts directly supported by the research text. Never speculate about ownership structure, investor base, or strategy unless the research explicitly mentions it.
- If research is thin (fewer than 3 substantive facts), set confidence to "low". If 3-5 facts with sources, "medium". If 6+ facts from multiple sources, "high".
"""


def _enrich_research(name: str, research_raw: str) -> str:
    """When stored research is thin, pull extra open-web context (the same
    targeted-search idea /requalify uses) so the profile can carry 4-6 bullets
    and real stats. Best-effort; returns the (possibly augmented) research text."""
    txt = research_raw or ""
    if len(txt) >= 1200:
        return txt[:6000]
    try:
        angles = [
            f'"{name}" real estate (units OR "apartment homes" OR properties OR '
            f'headquarters OR "assets under management" OR states OR founded)',
            f'"{name}" multifamily portfolio size markets employees',
        ]
        results = search_client.layered_search(angles, max_results_each=6)
        extra = "\n".join(f"- {r['title']}: {(r['content'] or '')[:400]}"
                          for r in results[:8] if r.get("content"))
        if extra:
            return (txt + "\n\nAdditional open-web research:\n" + extra)[:6000]
    except Exception:  # noqa: BLE001 - enrichment is best-effort
        pass
    return txt[:6000]


def _extract_custom(company: dict, research_raw: str) -> dict:
    service_focus = "property accounting"
    client = company.get("client_name")
    if client:
        prof = db.get_client_profile(client)
        if prof and prof.get("pitch_service_focus"):
            service_focus = prof["pitch_service_focus"]
    research = _enrich_research(company.get("name", ""),
                               research_raw or company.get("summary") or "")
    prompt = _EXTRACT_PROMPT.format(
        name=company.get("name", ""),
        icp_reason=company.get("icp_reason") or "",
        service_focus=service_focus,
        research=research,
    )
    parsed, _u = models.complete_json(prompt, model=config.PREMIUM_MODEL,
                                      temperature=0.4, max_tokens=2500)
    return parsed


_BULLET_LEAK = re.compile(
    r"\b(implied|snippet|from (the )?research|from stats?|appears? in|possibly|"
    r"likely|maybe|not specified|unknown)\b", re.I)

# Labels denoting a firm's total asset base (the AUM family). When AUM is only
# ESTIMATED, a stat under ANY of these must never render as a hard number — it
# stays soft in the lead. Kept broad so an LLM synonym (esp. "GAV", which the
# extract prompt itself endorses) can't slip an estimate onto slide 3.
_AUM_LABEL_RE = re.compile(
    r"\bAUM\b|\bGAV\b|\bNAV\b|assets? under management|gross asset|net asset value"
    r"|portfolio value|portfolio size|total assets|invested capital|capital deployed"
    r"|equity under management|book value", re.I)
_MONEY_RE = re.compile(r"\$\s?[\d.,]+\s?[BMK]", re.I)
# A million/billion/trillion magnitude in a stat VALUE. For an ESTIMATED-AUM firm
# any such figure is treated as an un-verifiable AUM proxy and stripped, whatever
# its label — the LLM is never told which AUM is estimated, so this is the only
# reliable backstop against an estimate rendering as a hard fact on slide 3.
# Currency-agnostic (or no symbol at all) and covers MM / bn / M / B / T / words,
# because the model emits any of these; counts (no magnitude token) are untouched.
_BIG_MONEY_RE = re.compile(
    r"(?:[$€£]|\bUSD|\bEUR|\bGBP)?\s?[\d.,]+\s?"
    r"(?:MM|MLN|MN|MIL|BLN|BN|BIL|[BMT]|billion|million|trillion)\b",
    re.I)


def _clean_bullet(b: str) -> Optional[str]:
    """Sanitize a model bullet: drop parenthetical meta-notes and reject any
    bullet that hedges, poses a question, or references the research process."""
    b = re.sub(r"\s*\([^)]*\)", "", str(b)).strip()   # strip parenthetical asides
    b = re.sub(r"\s{2,}", " ", b)
    if not b or "?" in b or _BULLET_LEAK.search(b):
        return None
    return b[:120]


def _profile_bullets(fp: dict) -> list[str]:
    # Prefer the model's purpose-written bullets (Part E); fall back to deriving
    # them from the structured fields when the model didn't supply enough.
    _raw_bullets = fp.get("bullets")
    _raw_bullets = _raw_bullets if isinstance(_raw_bullets, list) else []
    llm = [c for c in (_clean_bullet(b) for b in _raw_bullets) if c]
    if len(llm) >= 4:
        return llm[:6]
    out = list(llm)
    if fp.get("history_snippet") and fp["history_snippet"] not in out:
        out.append(fp["history_snippet"])
    hq, geo = fp.get("headquarters"), fp.get("geographic_focus")
    if hq and geo:
        out.append(f"Headquartered in {hq}; {geo}")
    elif hq:
        out.append(f"Headquartered in {hq}")
    elif geo:
        out.append(f"Geographic focus: {geo}")
    if fp.get("asset_class_focus"):
        out.append(f"Focus: {fp['asset_class_focus']}")
    if fp.get("investment_strategy"):
        out.append(f"Strategy: {fp['investment_strategy']}")
    if fp.get("deal_structure"):
        out.append(f"Structure: {fp['deal_structure']}")
    if fp.get("investor_base"):
        out.append(f"Investors: {fp['investor_base']}")
    seen, deduped = set(), []
    for b in out:
        if b not in seen:
            seen.add(b)
            deduped.append(b)
    return deduped[:6]


def _build_data(company: dict, custom: dict, output_path: str,
                research_raw: str = "") -> dict:
    name = company.get("name", "your firm")
    short = _short_name(name)
    year = datetime.now().year
    # Be defensive about the LLM's shape: a non-dict firm_profile or a stats/
    # fit_points value that isn't a list must degrade gracefully, never crash.
    custom = custom if isinstance(custom, dict) else {}
    fp = custom.get("firm_profile")
    fp = fp if isinstance(fp, dict) else {}
    roles = custom.get("relevant_roles")
    roles = roles if isinstance(roles, list) else []
    fit_points = custom.get("fit_points")
    fit_points = fit_points if isinstance(fit_points, list) else []

    # Is the firm's AUM only an estimate? Then it must NEVER appear as a hard
    # stat box (a prospect seeing a wrong AUM about themselves is the top risk).
    aum_estimated = ("ESTIMATED" in (company.get("aum_tag") or "").upper()
                     or re.search(r"\b(est\.?|estimated)\b", company.get("icp_reason") or "", re.I) is not None)

    # The exact estimated $ figure (from the ICP note), so we can strip it even
    # if the LLM labels it with something we didn't anticipate.
    est_money = ""
    if aum_estimated:
        _m = _MONEY_RE.search(company.get("icp_reason") or "")
        est_money = _m.group(0).replace(" ", "").upper() if _m else ""

    raw_stats = []
    _stats = fp.get("stats")
    for s in (_stats if isinstance(_stats, list) else []):
        if not isinstance(s, dict):
            continue
        val = str(s.get("value") or "").strip()
        # Guard: a real stat carries a NON-ZERO digit; drop "N/A"/"Multiple"/"0"/"$0".
        if not re.search(r"[1-9]", val):
            continue
        # Guard: never slice a number mid-digit — drop an over-long value outright
        # (a properly abbreviated figure like "$15.3B" is always well under 12 chars;
        # a long-form "$1,250,000,000" would otherwise render as a mangled number).
        if len(val) > 12:
            continue
        raw_label = (s.get("label") or "").strip()
        # Guard: when AUM is only ESTIMATED, a big-dollar figure must NEVER render as
        # a hard stat box (the #1 prospect risk). Strip on the VALUE ($B/$M magnitude)
        # OR an AUM-family label OR an exact match to the estimated figure. Safe-
        # direction trade-off: a legitimate big-$ transaction stat is also dropped for
        # estimated-AUM firms — counts/years still populate the slide, and the estimate
        # is surfaced softly in the lead instead.
        if aum_estimated and (_BIG_MONEY_RE.search(val)
                              or _AUM_LABEL_RE.search(raw_label)
                              or (est_money and val.replace(" ", "").upper() == est_money)):
            continue
        sub = (s.get("sublabel") or "").strip()
        raw_stats.append({
            "value": val,
            "label": raw_label[:26],
            "sublabel": sub if len(sub) <= 32 else "",   # drop junky/overlong sublabels
        })

    # Guard: drop a sublabel that merely echoes another stat's label (LLM sometimes
    # copies e.g. "assets under management" under a "Properties" count).
    _labels = {st["label"].strip().lower() for st in raw_stats if st["label"].strip()}
    for st in raw_stats:
        if st["sublabel"].strip().lower() in _labels:
            st["sublabel"] = ""

    # Guard: cap at 4. If more than 4 survive, keep the most concrete
    # (counts > founding years > dollar amounts). Preserve order when <= 4.
    def _stat_rank(st):
        v, lab = st["value"], st["label"].lower()
        if "$" in v or re.search(r"aum|asset|value|gav|capital|revenue", lab):
            return 2                                     # dollar amounts last
        if re.fullmatch(r"(19|20)\d{2}\+?", v) or re.search(r"found|since|year", lab):
            return 1                                     # founding years middle
        return 0                                         # counts (units/properties/etc.) first
    if len(raw_stats) > 4:
        raw_stats = sorted(raw_stats, key=_stat_rank)    # stable: concrete first
    stats = raw_stats[:4]
    # NOTE: no estimated-AUM fallback here on purpose — an empty stats array is
    # safer than a fabricated stat box. The layout renders cleanly with 0-2 stats.

    # Lead sentence. When AUM is only estimated, surface it SOFTLY here (never as
    # a hard stat box) so the number reads as a rough figure, not a fact.
    lead = str(fp.get("lead_sentence") or "").strip() or \
        f"A real estate investment manager. {fp.get('asset_class_focus', '')}".strip()
    if aum_estimated:
        m = re.search(r"\$[\d.]+\s?[BMK]", company.get("icp_reason") or "")
        if m and m.group(0) not in lead and "estimat" not in lead.lower():
            lead = lead.rstrip(".") + f", with an estimated portfolio of ~{m.group(0)}."

    bullets = _profile_bullets(fp)
    display_bullets = bullets or ["Real estate investment/operating firm (see research)."]

    # Confidence: start from the LLM's self-assessment, then downgrade to "low"
    # when the surviving data is thin. This is the drafter's review signal.
    _llm_conf = str(custom.get("confidence") or "").strip().lower()
    confidence = _llm_conf if _llm_conf in ("low", "medium", "high") else "medium"
    if len(bullets) < 3 or len(stats) == 0 or len(research_raw or "") < 500:
        confidence = "low"

    agenda_items = [
        {"num": "01", "label": short, "desc": "What we know about your platform"},
        {"num": "02", "label": "Model", "desc": "The X Analyst embedded team approach"},
        {"num": "03", "label": "Services", "desc": "The functions we cover for RE managers"},
        {"num": "04", "label": "Fit", "desc": f"Why this maps to {short}'s operating stack"},
        {"num": "05", "label": "Next Steps", "desc": "How a pilot engagement would look"},
    ]

    pilot = dict(_PILOT)
    contact_title = company.get("contact_title") or "Controller or Head of Operations"
    pilot = {**_PILOT, "nextStep": f"Next step: a 30-minute working session with your {contact_title}"}

    return {
        "output": output_path,
        "firmName": name,
        "firmLegal": name,
        "year": year,
        "confidence": confidence,   # low|medium|high — read by drafter + /status
        "coverTagline": "A dedicated finance & operations team",
        "coverTitle": f"Built for {short}.",
        "coverSubtitle": (f"Embedded senior {_roles_line(roles)} — integrated into your team, "
                          "delivered from our India operations at US market quality."),
        "agenda": {
            "title": "Today's conversation",
            "subtitle": f"A focused introduction to how X Analyst can extend {short}'s operating team.",
            "items": agenda_items,
        },
        "profile": {
            "section": "Section 01",
            "title": f"What we know about {short}",
            "lead": lead,
            "bullets": display_bullets,
            "stats": stats,
            "capitalStack": fp.get("capital_stack_line") or "",
        },
        "comparison": _COMPARISON,
        "sixFunctions": _SIX_FUNCTIONS,
        "serviceDetails": _SERVICE_DETAILS,
        "why": _WHY,
        "engagement": _ENGAGEMENT,
        "timeline": _TIMELINE,
        "fit": {
            "section": "Section 04",
            "title": f"Where this fits {short} specifically",
            "intro": (f"{['','One','Two','Three','Four','Five'][min(len(fit_points),5)]} "
                      "natural entry points based on your platform.").strip()
                     if fit_points else "Natural entry points based on your platform.",
            "points": [{"boldLabel": p.get("bold_label", ""), "description": p.get("description", "")}
                       for p in fit_points[:5] if isinstance(p, dict) and p.get("bold_label")],
        },
        "pilot": pilot,
        "thankYou": {
            "wordmark": "X   A N A L Y S T",
            "title": "Thank you.",
            "line": f"Let's build what {short}'s next chapter of operational leverage looks like.",
            "url": "xanalyst.co",
            "founders": _FOUNDERS,
        },
    }


def _ensure_icons() -> None:
    """Best-effort: pre-render the gold icon PNGs if the cache is missing/empty.
    build_deck.js loads deck_gen/icons/*.png and silently omits any that are
    absent, so a failure here never blocks a deck — icons just don't appear."""
    icons_dir = config.DECK_BUILDER_JS.parent / "icons"
    icons_js = config.DECK_BUILDER_JS.parent / "icons.js"
    try:
        have = list(icons_dir.glob("*.png")) if icons_dir.exists() else []
        if len(have) >= 10 or not icons_js.exists():
            return
        subprocess.run(["node", str(icons_js)], capture_output=True,
                       text=True, timeout=180)
    except Exception:  # noqa: BLE001 - icons are optional decoration
        pass


def generate_deck(company: dict, research_raw: str,
                  fit_points: Optional[list[dict]] = None, force: bool = False) -> str:
    """Generate a firm-specific 14-slide PPTX. Returns the file path.

    Caches by slugged name in DECKS_DIR; reuses an existing deck unless `force`
    or the company was re-researched after the deck was built.
    """
    name = company.get("name") or "firm"
    config.DECKS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.DECKS_DIR / f"XAnalyst_{slugify(name)}_Pitch.pptx"

    if not force and out_path.exists():
        last = company.get("last_researched_at")
        try:
            fresh = last is None or out_path.stat().st_mtime >= last.timestamp()
        except Exception:  # noqa: BLE001
            fresh = True
        if fresh:
            return str(out_path)

    custom = _extract_custom(company, research_raw)
    if fit_points is not None:
        custom["fit_points"] = fit_points
    data = _build_data(company, custom, str(out_path), research_raw=research_raw or "")

    if not config.DECK_BUILDER_JS.exists():
        raise DeckError(f"deck builder not found: {config.DECK_BUILDER_JS}")

    _ensure_icons()

    # persist the data JSON next to the deck (debug / re-render without the LLM)
    json_path = str(config.DECKS_DIR / f"XAnalyst_{slugify(name)}_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    try:
        proc = subprocess.run(
            ["node", str(config.DECK_BUILDER_JS), json_path],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError as e:
        raise DeckError(f"node not available: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise DeckError(f"deck render timed out: {e}") from e

    if proc.returncode != 0 or not out_path.exists():
        raise DeckError(f"deck render failed (rc={proc.returncode}): "
                        f"{(proc.stderr or proc.stdout or '').strip()[:400]}")
    return str(out_path)


def deck_meta(company_name: str) -> dict:
    """Read the confidence flag + stat/bullet counts from a company's deck data
    sidecar. Returns {'confidence','n_stats','n_bullets'}; confidence is
    'unknown' when the sidecar is missing or has no (valid) confidence field.
    Used by the drafter notification and /status; never raises."""
    meta = {"confidence": "unknown", "n_stats": 0, "n_bullets": 0}
    try:
        p = config.DECKS_DIR / f"XAnalyst_{slugify(company_name)}_data.json"
        if not p.exists():
            return meta
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        c = str(d.get("confidence") or "").strip().lower()
        meta["confidence"] = c if c in ("low", "medium", "high") else "unknown"
        prof = d.get("profile") or {}
        meta["n_stats"] = len(prof.get("stats") or [])
        meta["n_bullets"] = len(prof.get("bullets") or [])
    except Exception:  # noqa: BLE001 - reporting helper must never break a flow
        pass
    return meta


def confidence_banner(company_name: str) -> str:
    """Multi-line confidence banner for the post-draft Telegram notification."""
    m = deck_meta(company_name)
    c = m["confidence"]
    detail = f"({m['n_stats']} verified stats, {m['n_bullets']} bullets)"
    if c == "high":
        return f"\U0001F4CA Deck confidence: HIGH {detail}\n✅ Safe to review and send"
    if c == "medium":
        return f"\U0001F4CA Deck confidence: MEDIUM {detail}\n☑️ Review slide 3 before sending"
    if c == "low":
        return (f"\U0001F4CA Deck confidence: LOW {detail}\n"
                f"⚠️ Review slide 3 carefully — thin research data")
    return "\U0001F4CA Deck confidence: UNKNOWN\n⚠️ UNKNOWN — review before sending"


if __name__ == "__main__":
    import sys
    from . import gmail_drafter  # noqa: F401  (ensure package import path)

    nm = " ".join(sys.argv[1:]) or "Kingbird Investments"
    co = db.get_company_by_name(nm) or {"name": nm}
    rr = (co.get("research_raw") if isinstance(co.get("research_raw"), str)
          else json.dumps(co.get("research_raw") or {}))
    print(generate_deck(co, rr, force=True))
