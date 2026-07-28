"""Phase 5b — PPT fill from the provided master (§9). Edits at the <a:t> run
level inside ppt/slides/slideN.xml (preserving <a:rPr>), never rebuilds the
deck and never uses python-pptx's `text_frame.text =` (strips run formatting
— HARD RULE from §4 Phase 5). Reuses Workflow A's proven data-safety guard
(`_BIG_MONEY_RE`) verbatim rather than re-implementing it (HARD RULE 14).

Overflow prevention is proactive: profile_bullets and fit_paragraphs are
capped to the word limits §9 specifies at fill time (never ship an
over-length run) — see _cap_words. Rendering slides to images for pixel-level
overflow detection is out of scope for this build; a best-effort PDF
conversion (via scripts/office/soffice.py) confirms the filled deck still
opens/renders as a well-formed 14-page file, which is the QA signal actually
exercised here.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import escape as _xml_escape

from . import config, fact_check
from lib_snapshot.drafting.pitch_deck import _BIG_MONEY_RE, _short_name

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


class DeckFillError(RuntimeError):
    pass


def load_slot_map() -> dict:
    return json.loads(config.PITCH_SLOTS_JSON.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# HARD RULE 16a preflight: master unzips cleanly AND every slot locator is
# found exactly once (on the wrapped <a:t>...</a:t> form — see
# scripts/locate_pptx_slots.py for why bare inner text isn't the right check).
# ---------------------------------------------------------------------------
def preflight_slots() -> list[str]:
    problems: list[str] = []
    if not config.PITCH_MASTER_PPTX.exists():
        return [f"master PPTX not found: {config.PITCH_MASTER_PPTX}"]
    try:
        z = zipfile.ZipFile(config.PITCH_MASTER_PPTX)
        bad = z.testzip()
        if bad:
            return [f"master PPTX corrupt at member {bad}"]
    except zipfile.BadZipFile as e:
        return [f"master PPTX does not unzip cleanly: {e}"]

    if not config.PITCH_SLOTS_JSON.exists():
        return [f"pitch_slots.json not found: {config.PITCH_SLOTS_JSON} "
                f"(run scripts/locate_pptx_slots.py once)"]
    slot_map = load_slot_map()
    for slide_no, slots in slot_map["slides"].items():
        path = f"ppt/slides/slide{slide_no}.xml"
        try:
            xml = z.read(path).decode("utf-8")
        except KeyError:
            problems.append(f"{path} missing from master PPTX")
            continue
        for slot in slots:
            wrapped = f"<a:t>{slot['locator']}</a:t>"
            n = xml.count(wrapped)
            if n != 1:
                problems.append(
                    f"slide{slide_no} slot {slot['key']!r}: locator found {n}x in "
                    f"slide XML, expected exactly 1 (master may have been re-exported)"
                )
    return problems


# ---------------------------------------------------------------------------
# Overflow prevention (word caps per §9)
# ---------------------------------------------------------------------------
def _cap_words(text: str, max_words: int) -> str:
    words = (text or "").split()
    if len(words) <= max_words:
        return text or ""
    return " ".join(words[:max_words]).rstrip(",.") + "."


# QA finding (visual QA render): slide 1's "PREPARED FOR" full-legal-name
# slot had no length cap at all — §9 itself flags this exact risk ("A firm
# with a longer name... can overflow"). A 72-character stress-test name
# wrapped to 2 lines and crowded the slide's bottom margin against the
# CONFIDENTIAL footer; not yet clipped in that render, but a real risk for
# any longer legal name. Capped at a word boundary, no truncation ellipsis
# needed for a company name.
_FIRM_NAME_DISPLAY_MAX_CHARS = 60


def _cap_chars(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars].rsplit(" ", 1)[0]
    return (truncated or text[:max_chars]) + "…"


# QA finding (adversarial pass, §11.9): HARD RULE 14 / _BIG_MONEY_RE was only
# ever applied to the dedicated aum_value/aum_label slot — NOT to
# profile_bullets or fit_paragraphs. Those are free-form prose the LLM
# writes in real mode, and §11.2's own bug report is precisely this pattern:
# "money forms... reaching deck stats for AUM-estimated firms under non-AUM
# labels" — a bullet like "Manages an estimated $1.2 billion portfolio..."
# carries no "AUM"/"Gross Asset Value" label at all, so _AUM_LABEL_RE alone
# would never have caught it; only a value-level _BIG_MONEY_RE scan across
# EVERY piece of generated text does. Confirmed via a live test: an
# ESTIMATED-tagged fixture's $1.2B leaked straight through profile_bullet_1
# and fit_paragraph_1 verbatim before this fix. A bullet/paragraph
# containing a money figure is DROPPED whole (never partially edited — a
# hand-edited sentence reads as obviously mutilated) when AUM is estimated;
# an empty slot is safer than a fabricated-looking edit, same philosophy as
# the "empty stats array beats a fabricated stat" rule elsewhere.
#
# FIX 36 (four-deck natural experiment, all post-FIX-35 same day: Kingbird
# scrub-OFF shipped 5/5 fit slots; American/Kairos/In-Rel scrub-ON shipped
# 4/5, 1/5, 3/5 -- fill rate tracked whether the scrub was ARMED, not
# content quality): this guard was blunt in one specific way -- a mention
# of the firm's OWN disclosed figure (the exact number the stat tile
# already shows, and the email already states per FIX 10/22) is not an
# "estimate leaking as fact", it's the same fact appearing twice. A live
# repro against Kairos caught this exactly: a fit paragraph citing "$254 M"
# (its own disclosed Capital Invested figure, verbatim) was dropped whole
# by this same check. Whitelisted ONLY when the item's own numeric claims
# are a non-empty SUBSET of the disclosed figure's claims -- an item citing
# any OTHER money figure (the guard's real job; it caught a genuine $1.2B
# leak above) still drops exactly as before. `disclosed_claims` is computed
# once in build_fill_data() from research["disclosed_figure"] alone (NOT
# aum_display -- see that computation's own comment for why) and threaded
# through, never recomputed per item.
def _is_disclosed_figure_only(text: str, disclosed_claims: frozenset) -> bool:
    if not disclosed_claims:
        return False
    item_claims = fact_check.extract_numeric_claims(text)
    return bool(item_claims) and item_claims <= disclosed_claims


def _drop_estimated_money_mentions(items: list[str], must_scrub_money: bool,
                                   disclosed_claims: frozenset = frozenset(),
                                   firm_name: str = "", slot_label: str = "profile_bullet") -> list[str]:
    if not must_scrub_money:
        return items or []
    out = []
    for s in (items or []):
        text = s or ""
        if not _BIG_MONEY_RE.search(text) or _is_disclosed_figure_only(text, disclosed_claims):
            out.append(s)
            continue
        print(f"[DECK DROP] {firm_name}: {slot_label} reason=money_scrub -- dropped text: {text!r}")
    return out


# QA finding (real-mode Kingbird run): a bullet/paragraph restating a number
# with a DIFFERENT value than research (extraction drift) or one invented
# outright is just as much a data-integrity failure as a leaked estimated-
# AUM figure -- drop the whole item rather than patching it in place, same
# philosophy as _drop_estimated_money_mentions above.
def _drop_ungrounded_claims(items: list[str], corpus_text: str,
                            firm_name: str = "", slot_label: str = "profile_bullet") -> list[str]:
    out = []
    for s in (items or []):
        text = s or ""
        ungrounded = fact_check.unverifiable_claims(text, corpus_text)
        if ungrounded:
            print(f"[DECK DROP] {firm_name}: {slot_label} reason=ungrounded:"
                 f"{','.join(sorted(ungrounded))} -- dropped text: {text!r}")
            continue
        out.append(s)
    return out


# FIX 15 (real-mode Kairos run): a bullet identical to firm_oneliner wastes a
# slot restating a fact the deck already shows in its own dedicated
# firm_oneliner slot (slide 3) -- redundant, not a distinct fact, same
# "don't waste a slot on a repeat" philosophy as the email's
# find_redundant_claims. Compares after normalizing whitespace/trailing
# period/case so a trivial formatting difference doesn't hide an otherwise
# verbatim duplicate.
def _drop_oneliner_duplicate(items: list[str], oneliner: str,
                             firm_name: str = "", slot_label: str = "profile_bullet") -> list[str]:
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip().rstrip(".")).lower()

    target = _norm(oneliner)
    if not target:
        return items or []
    out = []
    for s in (items or []):
        if _norm(s) == target:
            print(f"[DECK DROP] {firm_name}: {slot_label} reason=oneliner_duplicate -- dropped text: {s!r}")
            continue
        out.append(s)
    return out


# FIX 15 (real-mode Kairos run): a bullet whose only content is the firm's
# own press-release marketing language ("...raises the bar with a new
# approach to...") is corporate spin, not a fact useful for a pitch -- it
# reads as recycled PR copy, not evidence of homework the way a concrete
# figure/date/structure fact does. Dropped whole, never rewritten -- a
# hand-edited bullet reads as obviously mutilated, same "drop don't patch"
# philosophy as the money-leak/ungrounded-claim guards above.
_MARKETING_LANGUAGE_RE = re.compile(
    r"\bnew approach to\b|\braises? the bar\b|\breimagin(?:e|ing|ed)\b|"
    r"\binnovative approach\b|\bcutting[- ]edge\b|\bstate[- ]of[- ]the[- ]art\b|"
    r"\bnext[- ]level\b|\bgame[- ]chang(?:ing|er)\b|\bbest[- ]in[- ]class\b",
    re.IGNORECASE,
)


def _drop_marketing_language(items: list[str], firm_name: str = "",
                             slot_label: str = "profile_bullet") -> list[str]:
    out = []
    for s in (items or []):
        text = s or ""
        if _MARKETING_LANGUAGE_RE.search(text):
            print(f"[DECK DROP] {firm_name}: {slot_label} reason=marketing_language -- dropped text: {text!r}")
            continue
        out.append(s)
    return out


def _cap_bullets(bullets: list[str], slots: int = 6, max_words: int = 18) -> list[str]:
    out = [_cap_words(b, max_words) for b in (bullets or [])][:slots]
    while len(out) < slots:
        out.append("")
    return out


# FIX 11 (real-mode Kairos Real Estate Partners run): fit_paragraphs used to
# be a plain ordered list, POSITIONALLY paired to these same 5 service
# headings -- when the model returned fewer than 5 (routine: not every
# firm supports all 5 services with a real fact), the remaining paragraphs
# still landed in slots 1..N, silently mismatched against whichever
# heading happened to occupy that position (a paragraph about fund
# structures under the "Property Accounting" heading, etc.), and the tail
# was padded with a fabricated "X Analyst's <heading> work extends
# directly to <firm>'s operations." sentence. Keyed by service now: each
# paragraph is bound to ITS OWN heading regardless of how many total come
# back, and an unfilled key is left BLANK, never padded or shifted into a
# different heading's slot (the 5 headings are FIXED text in the master
# template -- a slot's heading can't be hidden even when its body is
# empty, but an honest blank beats a mismatched or fabricated sentence).
_FIT_PARAGRAPH_KEYS = [
    ("property_accounting", "Property Accounting"),
    ("fund_structures", "Fund Structures"),
    ("investor_reporting", "Investor Reporting"),
    ("underwriting_acquisitions", "Underwriting & Acquisitions"),
    ("capital_markets", "Capital Markets"),
]

# A model sometimes echoes the service name as a redundant lead-in
# ("Fund Structures: With a capital stack...") even though the slide
# already shows that heading separately as its own fixed text run.
_LEADING_LABEL_RE = re.compile(
    r'^\s*(?:Property\s+Accounting|Fund\s+Structures|Investor\s+Reporting|'
    r'Underwriting\s*(?:&|and)\s*Acquisitions|Capital\s+Markets)'
    r'\s*[:\-–—]\s*',
    re.IGNORECASE,
)


def _strip_leading_label(text: str) -> str:
    return _LEADING_LABEL_RE.sub("", text or "").strip()


def _assemble_one_fit_paragraph(key: str, raw: str, must_scrub_money: bool, corpus_text: str,
                                disclosed_claims: frozenset, firm_name: str) -> str:
    """Resolves ONE fit_paragraph key's final text -- leading-label strip,
    word cap, FIX 36 Part A money-scrub whitelist, ungrounded-claims drop --
    shared by the first assembly pass (_assemble_fit_paragraphs) and Part
    C's post-retry re-check so the two can never drift out of sync. Every
    blank outcome prints its own [DECK DROP]/[DECK EMPTY] line (Part B) --
    silent blanking is the same silent-degradation pattern FIX 19 fixed for
    email."""
    raw = raw or ""
    if not raw.strip():
        print(f"[DECK EMPTY] {firm_name}: {key} returned empty by model.")
        return ""
    para = _strip_leading_label(_cap_words(raw, 40))
    if not para.strip():
        print(f"[DECK DROP] {firm_name}: {key} reason=leading_label_empty -- dropped text: {raw!r}")
        return ""
    if must_scrub_money and _BIG_MONEY_RE.search(para):
        if _is_disclosed_figure_only(para, disclosed_claims):
            return para
        print(f"[DECK DROP] {firm_name}: {key} reason=money_scrub -- dropped text: {para!r}")
        return ""
    ungrounded = fact_check.unverifiable_claims(para, corpus_text)
    if ungrounded:
        print(f"[DECK DROP] {firm_name}: {key} reason=ungrounded:{','.join(sorted(ungrounded))} "
             f"-- dropped text: {para!r}")
        return ""
    # FIX 37 Part A: _drop_marketing_language's regex was wired to profile
    # bullets only -- live Kairos ("a new approach to") and In-Rel ("best
    # in class") fit paragraphs shipped press-release spin this same regex
    # already catches for bullets. Same regex, blanked in place here too;
    # a blank from this reason feeds the existing Part C retry exactly like
    # any other blank reason -- no separate wiring needed.
    if _MARKETING_LANGUAGE_RE.search(para):
        print(f"[DECK DROP] {firm_name}: {key} reason=marketing_language -- dropped text: {para!r}")
        return ""
    return para


def _assemble_fit_paragraphs(fit_content: dict, must_scrub_money: bool, corpus_text: str,
                             disclosed_claims: frozenset = frozenset(), firm_name: str = "") -> list[str]:
    """Builds the 5 fixed-position fit_paragraph slots from a {service_key:
    paragraph} dict (FIX 11): each of the 5 canonical keys maps to exactly
    one fixed slide slot, in order -- a missing/blank/disqualified key
    leaves that ONE slot blank, never shifting a different key's content
    into it and never padding with fabricated text."""
    if not isinstance(fit_content, dict):
        fit_content = {}
    return [
        _assemble_one_fit_paragraph(key, fit_content.get(key) or "", must_scrub_money,
                                    corpus_text, disclosed_claims, firm_name)
        for key, _heading in _FIT_PARAGRAPH_KEYS
    ]


# ---------------------------------------------------------------------------
# Deck content (profile bullets + fit paragraphs) — TEST_MODE fixture, or one
# LLM extraction call in real mode (same shape as Workflow A's pitch_deck.py
# _extract_custom, adapted: no client_profile lookup, Phase 2/4 research only)
# ---------------------------------------------------------------------------
_CONTENT_PROMPT = """You are preparing a customized pitch deck for X Analyst
(offshore property/fund accounting and RE finance support) targeting the firm
below. Extract 4-6 factual profile bullets and, for EACH of these 5 X Analyst
service keys, a fit paragraph mapping the firm's situation to that specific
service: property_accounting, fund_structures, investor_reporting,
underwriting_acquisitions, capital_markets.

Firm: {name}
Research: {research}
Personalization hooks: {personalization}

Rules:
- Every bullet and paragraph must be a fact traceable to the research/hooks
  above. Never invent a number or fact.
- Every number (dollar figure, unit/state/property count) must be copied
  VERBATIM from the research/hooks above -- never rounded, re-derived, or
  paraphrased. A bullet or paragraph whose number does not match the
  research object is dropped after generation, so an invented or drifted
  figure just costs you the slot.
- If AUM is tagged AUM_ESTIMATED or AUM_UNVERIFIED and it is NOT a disclosed
  Gross Asset Value (proxy_type != "gav"), do not state it as a hard dollar
  figure in a bullet or paragraph -- it is a genuine internal estimate, not
  a fact the firm discloses.
- fit_paragraphs is an OBJECT keyed by the 5 service keys above, NOT a
  positional list -- if a specific service genuinely has no supportable
  fact for this firm, return an empty string for that key rather than
  inventing one or reusing another service's content. A missing/empty key
  is a real, honest outcome, not a failure.
- Do NOT start a fit paragraph with its own service name as a label (e.g.
  "Fund Structures: ...") -- the slide already shows that heading
  separately.
- The firm's own disclosed figure (research's disclosed_figure/aum_display
  above) MAY be cited verbatim -- the deck's own stat tile already displays
  it, so restating it in a bullet or paragraph is not an estimate leak.
- State research facts in plain prose, never inside quotation marks --
  write $254M in capital invested, never '"$254 M (Capital Invested)"'.
- Bullets: <= 18 words each. Fit paragraphs: <= 40 words each.

Return a SINGLE JSON object, no prose, no markdown fence:
{{"profile_bullets": ["...", "..."],
 "fit_paragraphs": {{"property_accounting": "... or empty string",
                     "fund_structures": "... or empty string",
                     "investor_reporting": "... or empty string",
                     "underwriting_acquisitions": "... or empty string",
                     "capital_markets": "... or empty string"}}}}
"""


def get_deck_content(firm_name: str, research: dict, personalization: dict) -> dict:
    """Returns {"profile_bullets": [...], "fit_paragraphs": {service_key: str,
    ...}}. TEST_MODE returns a canned fixture; real mode makes one LLM
    extraction call."""
    if config.TEST_MODE:
        from . import testmode

        fixture = testmode.deck_content_fixture(firm_name)
        if fixture:
            return fixture
        return {"profile_bullets": [], "fit_paragraphs": {}}

    from lib_snapshot import models
    import json as _json

    prompt = _CONTENT_PROMPT.format(
        name=firm_name,
        research=_json.dumps(research, default=str),
        personalization=_json.dumps(personalization, default=str),
    )

    # FIX 15 (real-mode Kairos run): diagnosed BEFORE fixing. This call had no
    # reasoning_effort and no visibility into finish_reason/reasoning_tokens/
    # which model answered -- unlike icp_gate.py's extraction call (FIX 6/7).
    # A live diagnostic run (debug_capture below, kept permanently for the
    # same reason icp_gate.py's extraction call keeps its own) confirmed the
    # EXACT SAME failure signature FIX 7 fixed for extraction:
    # gpt-5-mini attempt 1: finish_reason='length', reasoning_tokens=2048
    # (the FULL max_completion_tokens budget), response_chars=0 -- the
    # reasoning model spent its entire budget on hidden reasoning before ever
    # writing visible output. Attempt 2: identical. Escalated to DeepSeek on
    # attempt 3, which that time recovered a full response -- but escalation
    # only fires on an EMPTY/unparseable response; a run where gpt-5-mini's
    # reasoning consumes MOST (not literally all) of its budget can still
    # return a technically-valid but sparse JSON on attempt 1 (a real bullet
    # + the oneliner duplicated + 4 blanks; 2 of 5 fit paragraphs) that
    # complete_json_retry has no way to distinguish from a deliberately
    # terse-but-complete answer, so it ships as-is with no escalation at all
    # -- this is the exact shape of the originally-reported defect.
    # reasoning_effort="minimal" applied per FIX 7's own confirmed-live
    # result (reasoning_tokens=0, correct complete output, on the real
    # non-trivial extraction prompt); re-measured here after applying it
    # (see qa_out/QA_REPORT.md FIX 15) before touching _CONTENT_PROMPT itself.
    def _capture(model_name: str, text: str, finish_reason: str, reasoning_tokens: int) -> None:
        if finish_reason == "length" or not text.strip():
            print(f"[DECK CONTENT WARNING] {firm_name}: {model_name} returned "
                 f"finish_reason={finish_reason!r} reasoning_tokens={reasoning_tokens} "
                 f"response_chars={len(text)} -- possible reasoning-budget exhaustion.")

    parsed, usage = models.complete_json_retry(
        prompt, temperature=0.4, max_tokens=1200, debug_capture=_capture,
        reasoning_effort="minimal",
    )
    fit_paragraphs = parsed.get("fit_paragraphs")
    return {
        "profile_bullets": parsed.get("profile_bullets") or [],
        "fit_paragraphs": fit_paragraphs if isinstance(fit_paragraphs, dict) else {},
        # FIX 36 Part C: build_fill_data()'s bounded retry for blank fit
        # keys needs this first-pass call's own token spend to honor
        # config.MAX_LLM_TOKENS_PER_FIRM, the same guard FIX 34 added for
        # email_drafter.py's retry -- exposed here since this is the only
        # place that token count is known.
        "tokens_used": getattr(usage, "total_tokens", 0) or 0,
    }


# ---------------------------------------------------------------------------
# FIX 36 Part C: one bounded retry for whichever fit_paragraph keys are still
# blank after the first assembly pass, mirroring FIX 34's email_drafter.py
# shape -- ask for ONLY the blank keys, explicitly forbidding any dollar
# figure or numeric count (the shape that already survives untouched in the
# American/In-Rel decks' non-numeric fit paragraphs), then re-run the SAME
# per-key assembly (_assemble_one_fit_paragraph) on the retry output so Part
# A's whitelist and Part B's drop logging apply identically to it. A key
# still blank after this stays blank -- FIX 11's honest-blank rule, never
# padded or shifted.
# ---------------------------------------------------------------------------
_FIT_RETRY_PROMPT = """You are preparing a customized pitch deck for X Analyst
(offshore property/fund accounting and RE finance support) targeting {name}.

The fit paragraph(s) below came back blank or unusable on a first attempt.
Write ONE replacement for EACH key listed, grounded in the research/hooks
below.

Rules:
- Do NOT cite any dollar figure or numeric count -- asset class, capital
  stack, markets, capabilities, and named deals/roles are sufficient on
  their own.
- <= 40 words each.
- Do NOT start with the service name as a label (e.g. "Fund Structures: ...").
- State research facts in plain prose, never inside quotation marks --
  write $254M in capital invested, never '"$254 M (Capital Invested)"'.

Keys needing a replacement: {keys}

Research: {research}
Personalization hooks: {personalization}

Return a SINGLE JSON object, no prose, no markdown fence, with EXACTLY these
keys: {{{json_keys}}}
"""


def _retry_blank_fit_keys(firm_name: str, research: dict, personalization: dict,
                          blank_keys: list[str]) -> dict:
    """Makes the ONE Part C follow-up call for `blank_keys` and returns the
    parsed {key: text} dict (empty dict on any parse failure). Callers are
    responsible for the config.MAX_LLM_TOKENS_PER_FIRM guard -- checked
    BEFORE calling this, so a skip never even reaches this function (and
    never logs a misleading [DECK EMPTY] for a key the model was never
    asked about)."""
    from lib_snapshot import models
    import json as _json

    json_keys = ", ".join(f'"{k}": "..."' for k in blank_keys)
    prompt = _FIT_RETRY_PROMPT.format(
        name=firm_name, keys=", ".join(blank_keys),
        research=_json.dumps(research, default=str),
        personalization=_json.dumps(personalization or {}, default=str),
        json_keys=json_keys,
    )
    parsed, _usage = models.complete_json_retry(prompt, temperature=0.4, max_tokens=400,
                                                 reasoning_effort="minimal")
    return parsed if isinstance(parsed, dict) else {}


def _aum_label_for(aum_tag: str, proxy_type: str, proxy_basis: str, founded_year: str = "") -> str:
    """FIX 10 (real-mode Kairos run): the gate's own verified figure must
    reach the stat tile honestly labeled, not blanked just because the
    underlying proxy isn't GAV. Confirmed AUM -> "AUM"; a disclosed GAV
    proxy -> "Gross Asset Value" (a real, publicly disclosed figure);
    every OTHER estimated proxy (floor/non_floor/units) -> an "Est." label
    naming what kind of proxy it is, derived from proxy_basis's own free
    text (icp_scoring_v2_2.md's conventions: "cumulative invested capital,
    proxy" / "portfolio value, proxy" / "fund size, proxy") so the tile
    never claims more certainty than the gate actually established.

    FIX 16 (real-mode Kairos run): "Est. AUM (Invested Capital)" is a
    contradiction -- invested/deployed capital is a CUMULATIVE figure
    summed over the firm's whole operating history, not a snapshot of
    what it currently manages, so calling it any flavor of "AUM" (even
    hedged as an estimate) overclaims what the number actually measures.
    Portfolio-value/fund-size/unit-count proxies ARE snapshot-style
    figures reasonably adjacent to AUM, so those keep an "Est. AUM (...)"
    label; only the invested-capital case gets its own honest label,
    "Capital Invested (since <founded_year>)", naming what the figure
    actually is instead of what it resembles.

    FIX 21 (real-mode In-Rel Properties run): a square-footage-derived
    tertiary proxy gets its own "Est. AUM (Square Footage)" label,
    distinct from "Est. AUM (Unit Count)".

    FIX 22 (real-mode In-Rel Properties run): this function is now a
    FALLBACK ONLY, reached from build_fill_data() when research holds no
    `disclosed_label` at all (un-migrated test fixtures, or a genuinely
    empty model extraction) -- the primary label source is the firm's own
    disclosed_label, verbatim (title-cased for the tile), never this
    keyword-sniffed guess. proxy_basis is plain pass-through from the
    model's own extraction (research_and_gate() no longer overwrites it),
    so this fallback's "sq ft"/"unit" sniffing is best-effort, not
    guaranteed to match -- acceptable since it's a last-resort path."""
    if aum_tag == "AUM_CONFIRMED":
        return "AUM"
    if proxy_type == "gav":
        return "Gross Asset Value"
    basis = (proxy_basis or "").lower()
    if "invested capital" in basis or "deployed" in basis:
        return f"Capital Invested (since {founded_year})" if founded_year else "Capital Invested"
    if "portfolio value" in basis:
        return "Est. AUM (Portfolio Value)"
    if "fund size" in basis:
        return "Est. AUM (Fund Size)"
    if "sq ft" in basis or "square" in basis:
        return "Est. AUM (Square Footage)"
    if "unit" in basis:
        return "Est. AUM (Unit Count)"
    return "Est. AUM"


_LABEL_LOWER_WORDS = {"of", "and", "under", "in", "for", "the", "a", "an", "since", "or"}


def _titlecase_label(label: str) -> str:
    """Title-cases a firm's own disclosed_label (e.g. "gross asset value",
    "of office and retail space under management") for the deck stat tile,
    keeping short connector words lowercase like a normal headline would
    (never the raw model casing, never a naive str.title() that would also
    capitalize "Of"/"And"/"Under"). FIX 22 (real-mode In-Rel Properties
    run): the label itself is never invented -- only its capitalization is
    adjusted for display."""
    words = label.split()
    out = []
    for i, w in enumerate(words):
        if i > 0 and w.lower() in _LABEL_LOWER_WORDS:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(out)


# ---------------------------------------------------------------------------
# FIX 24 (real-mode In-Rel Properties run): the aum_value/aum_label stat
# tile is a fixed-size shape sized for short currency strings ("$4.4B",
# "$254 M") -- a verbatim disclosed quantity like "over 3,000,000 sq ft"
# (21 chars) overruns it, wraps to a second line, and collides with the
# label box directly below (only ~0.02in of vertical gap between them in
# the master). This compaction is SCOPED TO THE TILE ONLY -- disclosed_
# figure/aum_display (email, operator views) keep the full, unabbreviated
# form, which read correctly per the bug report. Same "cap for display,
# never touch the underlying field" philosophy as _cap_chars/
# _FIRM_NAME_DISPLAY_MAX_CHARS above, just a numeric-aware compaction
# instead of a truncate-with-ellipsis one (truncating "3,000,000 sq ft"
# would cut off the unit itself, which _cap_chars was never designed for).
# ---------------------------------------------------------------------------
# Derived from the master's own aum_value/aum_label shape geometry (slide3.xml:
# both boxes are cx=2002536 EMU / 2.19in wide; aum_value's run is 22pt, aum_
# label's is 9.5pt) via the same _AVG_CHAR_WIDTH_EM estimate check_stat_tile_
# fits() uses below -- 2.19in / (0.55 * 22pt/72) ~= 13.0 chars for aum_value,
# 2.19in / (0.55 * 9.5pt/72) ~= 30.2 chars for aum_label. Kept as separate
# named constants (not re-derived from the PPTX at data-assembly time,
# which does no file I/O today) so a master re-export that changes this
# geometry has one obvious place to update both the trigger and the actual
# fit check in sync.
_AUM_VALUE_TILE_MAX_CHARS = 13
_AUM_LABEL_TILE_MAX_CHARS = 30

_QUANTITY_TILE_RE = re.compile(
    r"^(?P<qualifier>over|more than|at least|north of|approximately|about|roughly|~)?\s*"
    r"(?P<number>[\d,]+(?:\.\d+)?)\s*(?P<unit>sq\s*ft|units?)$",
    re.IGNORECASE,
)
_QUALIFIER_TO_TILE_SUFFIX = {"over": "+", "more than": "+", "at least": "+", "north of": "+"}
_QUALIFIER_TO_TILE_PREFIX = {"approximately": "~", "about": "~", "roughly": "~", "~": "~"}


def _format_scaled(number: float, scale: float, suffix: str) -> str:
    """3_000_000 scaled by 1_000_000 -> "3M"; 1_800_000 -> "1.8M". Whether
    to show the decimal is decided from the ROUNDED value (not whether the
    RAW number divides the scale evenly) -- 99,999 / 1,000 = 99.999, which
    rounds to a clean 100.0 at one decimal place; the OLD divisibility
    check (99999 % 1000 != 0) would have kept the decimal anyway, printing
    the spurious "100.0K" instead of "100K" (FIX 24 adversarial review
    finding)."""
    scaled = round(number / scale, 1)
    if scaled == int(scaled):
        return f"{int(scaled)}{suffix}"
    return f"{scaled:.1f}{suffix}"


def _short_number(number: float) -> str:
    """1_000_000 -> "1M"; 1_800_000 -> "1.8M"; 21_794 -> "21.8K"; 800 -> "800".

    FIX 24 (adversarial re-review): the bucket (M vs K vs plain) must be
    decided from the ROUNDED display value, not the raw one -- a number
    like 999,950 rounds to "1000.0" at one decimal place within the
    K-bucket (999,950 / 1,000 = 999.95 -> "1000.0"), which must promote to
    the M-bucket ("1M"), never ship as the longer, nonsensical "1000.0K"
    (which, combined with a qualifier suffix, is long enough to overflow
    the very tile this compaction exists to fit into)."""
    if number >= 1_000_000:
        return _format_scaled(number, 1_000_000, "M")
    if number >= 1_000:
        candidate = _format_scaled(number, 1_000, "K")
        if float(candidate[:-1]) >= 1000:
            return _format_scaled(number, 1_000_000, "M")
        return candidate
    return f"{number:.0f}"


def _compact_quantity_for_tile(value: str) -> str:
    """"over 3,000,000 sq ft" -> "3M+ sq ft"; "over 1,821,794 units" ->
    "1.8M+ units"; already-short currency values ("$4.4B", "$254 M") AND
    already-short quantities ("21,794 units", <= _AUM_VALUE_TILE_MAX_CHARS)
    pass through UNCHANGED (the whole point is to leave already-fitting
    values alone, exactly like _cap_chars leaves a short string alone). An
    unrecognized shape (doesn't match "[qualifier] <number> sq ft|units")
    is left as-is rather than guessed at -- this function only ever touches
    a shape it's confident
    about, never invents an abbreviation for something else."""
    value = (value or "").strip()
    if len(value) <= _AUM_VALUE_TILE_MAX_CHARS:
        return value
    m = _QUANTITY_TILE_RE.match(value)
    if not m:
        return value
    number = float(m.group("number").replace(",", ""))
    unit_short = "sq ft" if "sq" in m.group("unit").lower() else "units"
    qualifier = (m.group("qualifier") or "").strip().lower()
    prefix = _QUALIFIER_TO_TILE_PREFIX.get(qualifier, "")
    suffix = _QUALIFIER_TO_TILE_SUFFIX.get(qualifier, "")
    return f"{prefix}{_short_number(number)}{suffix} {unit_short}"


def _compact_label_for_tile(label: str, tile_value: str) -> str:
    """"Office and Retail Space under Management" wraps to two lines in the
    label's own box and is what "ft" collided with in the reported bug.
    Already-short labels ("Gross Asset Value", "Capital Invested") pass
    through unchanged. For the two known long-label cases this bug
    targets (sq ft / unit quantities), substitutes a precise canonical
    short label rather than guessing at a generic truncation; anything
    else unexpectedly long falls back to _cap_chars, so nothing ships
    un-capped even in a case this fix didn't anticipate."""
    if len(label) <= _AUM_LABEL_TILE_MAX_CHARS:
        return label
    lowered = tile_value.lower()
    if "sq ft" in lowered:
        return "Sq Ft Under Management"
    if "unit" in lowered:
        return "Units Under Management"
    return _cap_chars(label, _AUM_LABEL_TILE_MAX_CHARS)


# ---------------------------------------------------------------------------
# Slot data contract (§9)
# ---------------------------------------------------------------------------
def build_fill_data(firm_name: str, research: dict, deck_content: dict,
                    personalization: Optional[dict] = None) -> dict:
    """Assembles the §9 slot data contract from Phase 2 research + the deck
    content (profile bullets / fit paragraphs). HARD RULE 14: if the gate
    qualified the firm on an ESTIMATED AUM, the big-stat card must never show
    that dollar figure as a hard number — this fixed-layout master has no
    substitute stat slot to swap in (unlike Workflow A's flexible stats
    array), so the safe choice is to blank the AUM card rather than invent or
    smuggle a number through under a different label.

    QA finding (real-mode Kingbird run): that guard was too blunt. It
    conflated two different things that both happen to carry an
    AUM_ESTIMATED tag: a genuinely uncertain proxy (floor/non_floor/units --
    no directly-disclosed figure backs it, must stay blanked) and a GAV
    proxy (proxy_type == "gav" -- the firm PUBLICLY DISCLOSES this exact
    figure as Gross Asset Value; the gate's own leverage-haircut conversion
    to an estimated equity number is an internal analytical construct that
    never gets written back into aum_display, so the raw, disclosed GAV is
    safe -- and required -- to show here). The prior code's gating
    condition (`_AUM_LABEL_RE.search("AUM")`) searched a literal constant
    string and was always true, collapsing this whole distinction away."""
    firm_short = _short_name(firm_name)

    aum_tag = (research.get("aum_tag") or "").upper()
    aum_display = research.get("aum_display") or ""
    proxy_type = (research.get("proxy_type") or "").strip().lower()
    proxy_basis = research.get("proxy_basis") or ""
    aum_estimated = "ESTIMATED" in aum_tag
    disclosed_figure = (research.get("disclosed_figure") or "").strip()
    disclosed_label = (research.get("disclosed_label") or "").strip()

    # FIX 10 (real-mode Kairos run): the gate QUALIFIED this firm on a
    # verified $254M invested-capital proxy, and the deck's stat tile
    # still showed nothing -- the prior guard only ever populated the tile
    # for AUM_CONFIRMED or a disclosed GAV proxy, silently blanking every
    # OTHER estimated proxy (floor/non_floor/units) even though the gate
    # itself already treated that figure as real enough to qualify the
    # firm on. Now: any aum_tag the gate actually produced a display value
    # for gets shown, honestly labeled so a genuine estimate is never
    # presented as if it were confirmed AUM or GAV.
    #
    # FIX 22 (real-mode In-Rel Properties run): the stat tile's PRIMARY
    # source is now disclosed_figure/disclosed_label -- exactly what the
    # firm published, verbatim, no arithmetic, never a haircut/derived
    # number (a $4.4B disclosed GAV shows "$4.4B", never the ~$1.76B
    # internal gating figure; a units/sqft proxy shows the raw quantity,
    # e.g. "3,000,000 sq ft", never a derived "~$240M est." -- reversing
    # FIX 21's choice to synthesize a dollar estimate there). _aum_label_for()
    # and the legacy aum_display field are kept ONLY as a fallback for
    # un-migrated test fixtures that predate this field.
    if disclosed_figure:
        aum_value = disclosed_figure
        aum_label = (_titlecase_label(disclosed_label) if disclosed_label
                    else _aum_label_for(aum_tag, proxy_type, proxy_basis, research.get("founded_year") or ""))
        aum_asof = research.get("aum_asof") or ""
    elif aum_display and aum_tag in ("AUM_CONFIRMED", "AUM_ESTIMATED"):
        aum_value = aum_display
        aum_label = _aum_label_for(aum_tag, proxy_type, proxy_basis, research.get("founded_year") or "")
        aum_asof = research.get("aum_asof") or ""
    else:
        aum_value, aum_label, aum_asof = "", "", ""

    # FIX 24 (real-mode In-Rel Properties run): this dict's aum_value/
    # aum_label feed ONLY the deck stat tile (build_and_convert() is the
    # sole caller of build_fill_data(); email_drafter.py reads research["
    # disclosed_figure"]/["disclosed_label"] directly, never this dict) --
    # safe to compact right here, unconditionally, since both functions are
    # no-ops on an already-short value (an already-short currency figure
    # like "$4.4B"/"$254 M" is returned untouched).
    aum_value = _compact_quantity_for_tile(aum_value)
    aum_label = _compact_label_for_tile(aum_label, aum_value)

    # A genuinely uncertain estimate must still have UNQUALIFIED money
    # mentions scrubbed from free-form bullets/paragraphs (a bullet flatly
    # stating "$254M in assets" implies more certainty than the honestly-
    # labeled stat tile does) -- a confirmed figure or a disclosed GAV proxy
    # does not need scrubbing, it's real and citable as-is.
    must_scrub_money = aum_estimated and proxy_type != "gav"

    # Fix C (real-mode Kingbird run): capital_stack rendered as the
    # degenerate, self-referential "across the capital stack" instead of
    # the actual disclosed tranche list ("LP, GP, Co-GP, Preferred
    # Equity") -- a non-answer the extraction LLM produced when it
    # couldn't find the real structure, rather than the empty string its
    # own prompt schema explicitly allows. No real capital-stack value (a
    # list of tranche names) would ever contain the phrase "capital stack"
    # describing itself, so that phrase is a reliable degenerate-value
    # signal -- reject it and blank the slot rather than ship a fact-free
    # placeholder.
    capital_stack = (research.get("capital_stack") or "").strip()
    if re.search(r'capital\s+stack', capital_stack, re.IGNORECASE):
        capital_stack = ""

    corpus_text = fact_check.grounding_corpus(research, personalization or {})

    # FIX 36 Part A: the set of numeric claims the firm's OWN disclosed
    # figure itself contains -- an item whose claims are a subset of THIS
    # set is citing the same fact the stat tile already shows, not leaking
    # an estimate. See _is_disclosed_figure_only() above.
    #
    # Deliberately NOT falling back to the legacy aum_display here (despite
    # disclosed_figure's own docstring precedent of aum_display as a
    # fallback for un-migrated fixtures): aum_display, in every research
    # object icp_gate.py produces today, is already DERIVED from
    # disclosed_figure (see icp_gate.py's own aum_display construction), so
    # falling back to it changes nothing for live data. For a pre-FIX-22
    # fixture that sets aum_display alone with no disclosed_figure at all
    # (e.g. this file's own "Smuggled Money Co" test), aum_display is
    # standing in for a raw, UNDISCLOSED estimate, not a firm's own
    # published figure -- falling back to it there would whitelist exactly
    # the leak must_scrub_money exists to catch. disclosed_figure alone
    # preserves both: real, firm-disclosed figures are whitelisted; bare
    # AUM_ESTIMATED numbers with no disclosure behind them still drop.
    disclosed_claims = frozenset(fact_check.extract_numeric_claims(disclosed_figure))

    safe_bullets = _drop_estimated_money_mentions(deck_content.get("profile_bullets", []), must_scrub_money,
                                                   disclosed_claims, firm_name)
    safe_bullets = _drop_ungrounded_claims(safe_bullets, corpus_text, firm_name)
    safe_bullets = _drop_oneliner_duplicate(safe_bullets, research.get("firm_oneliner") or "", firm_name)
    safe_bullets = _drop_marketing_language(safe_bullets, firm_name)
    profile_bullets = _cap_bullets(safe_bullets)
    fit_paragraphs = _assemble_fit_paragraphs(deck_content.get("fit_paragraphs") or {},
                                              must_scrub_money, corpus_text, disclosed_claims, firm_name)

    # FIX 36 Part C: exactly one bounded retry for whichever fit_paragraph
    # keys are STILL blank after the pass above, for ANY reason (money_scrub,
    # ungrounded claim, leading-label-empty, or genuinely empty from the
    # model -- all four showed up live across the four-deck evidence).
    # Skipped entirely in TEST_MODE (no fixture firm needs this, and it
    # would otherwise try a real network call under every existing blank-
    # slot unit test) and once the first-pass call's own token spend is
    # already at/over budget.
    blank_keys = [key for (key, _h), val in zip(_FIT_PARAGRAPH_KEYS, fit_paragraphs) if not val]
    if blank_keys and not config.TEST_MODE:
        tokens_so_far = deck_content.get("tokens_used", 0) or 0
        if tokens_so_far >= config.MAX_LLM_TOKENS_PER_FIRM:
            for key in blank_keys:
                print(f"[DECK RETRY] {firm_name}: {key} skipped -- token budget already at/over "
                     f"{config.MAX_LLM_TOKENS_PER_FIRM} ({tokens_so_far}).")
        else:
            retry_result = _retry_blank_fit_keys(firm_name, research, personalization or {}, blank_keys)
            for i, (key, _h) in enumerate(_FIT_PARAGRAPH_KEYS):
                if key not in blank_keys:
                    continue
                retried_raw = (retry_result.get(key) or "").strip()
                if not retried_raw:
                    print(f"[DECK RETRY] {firm_name}: {key} still blank after retry (model returned empty).")
                    continue
                retried_text = _assemble_one_fit_paragraph(key, retried_raw, must_scrub_money, corpus_text,
                                                           disclosed_claims, firm_name)
                if retried_text:
                    fit_paragraphs[i] = retried_text
                    print(f"[DECK RETRY] {firm_name}: {key} filled on retry.")
                else:
                    print(f"[DECK RETRY] {firm_name}: {key} still blank after retry (dropped again).")

    return {
        "firm_name": firm_name,
        "firm_short": firm_short,
        "firm_name_display": _cap_chars(firm_name, _FIRM_NAME_DISPLAY_MAX_CHARS),
        "firm_oneliner": _cap_words(research.get("firm_oneliner") or "", 40),
        "aum_value": aum_value,
        "aum_label": aum_label,
        "aum_asof": aum_asof,
        "founded_year": research.get("founded_year") or "",
        "capital_stack": capital_stack,
        "agenda_section01_label": firm_short,
        "profile_bullet_1": profile_bullets[0],
        "profile_bullet_2": profile_bullets[1],
        "profile_bullet_3": profile_bullets[2],
        "profile_bullet_4": profile_bullets[3],
        "profile_bullet_5": profile_bullets[4],
        "profile_bullet_6": profile_bullets[5],
        "fit_paragraph_1": fit_paragraphs[0],
        "fit_paragraph_2": fit_paragraphs[1],
        "fit_paragraph_3": fit_paragraphs[2],
        "fit_paragraph_4": fit_paragraphs[3],
        "fit_paragraph_5": fit_paragraphs[4],
    }


# ---------------------------------------------------------------------------
# XML-level fill
# ---------------------------------------------------------------------------
def _slot_new_text(slot: dict, fill_data: dict) -> str:
    """The exact <a:t>...</a:t> inner text a slot resolves to, already in
    XML-text-run form (the same escaping convention pitch_slots.json's own
    locator/template strings use -- e.g. "&apos;" not "'") so it is directly
    comparable to a slot's locator and can be written into the output XML
    with no further escaping. Only the SUBSTITUTED variable part (firm_short
    on a name_swap slot; the whole value on full_replace) needs escaping --
    a name_swap template's surrounding text is already pre-escaped in
    pitch_slots.json, and escaping it a second time would double-encode it
    (e.g. "&apos;" -> "&amp;apos;", which renders as the literal text
    "&apos;" instead of an apostrophe).

    Shared by fill_deck() and find_placeholder_leaks() so the two can never
    drift out of sync with each other."""
    if slot["type"] == "name_swap":
        return slot["template"].replace("{{firm_short}}", _xml_escape(fill_data["firm_short"]))
    return _xml_escape(str(fill_data.get(slot["key"], "")))


# ---------------------------------------------------------------------------
# FIX 24 (real-mode In-Rel Properties run): every check above this point is
# TEXT-level (char/word caps, placeholder-leak detection) -- none of them
# says anything about whether the resulting text actually FITS the shape
# geometry it lands in. Best-effort (average-character-width heuristic
# against the box width and font size read straight out of the master's
# own slide XML -- not a real font-metrics/rendering engine), matching this
# module's own documented preference for lightweight checks over slide-
# image rendering (see the module docstring).
# ---------------------------------------------------------------------------
_EXT_RE = re.compile(r'<a:ext cx="(\d+)" cy="(\d+)"/>')
_RPR_SZ_RE = re.compile(r'<a:rPr[^>]*\bsz="(\d+)"')
_EMU_PER_INCH = 914400
# Deliberately conservative (errs toward flagging a borderline value rather
# than missing a real overflow) -- tuned against the master's own known-good
# values ("$4.4B", "$254 M", "Gross Asset Value", "Capital Invested" all fit
# comfortably) and known-bad ones ("3,000,000 sq ft", "Office and Retail
# Space under Management" both correctly exceed their box at this ratio).
_AVG_CHAR_WIDTH_EM = 0.55


def _shape_box_and_font_before(xml: str, wrapped_locator: str) -> Optional[tuple[int, int, int]]:
    """Returns (box_width_emu, box_height_emu, font_size_hundredths_pt) for
    the shape/run enclosing `wrapped_locator`'s <a:t> tag -- the NEAREST
    preceding <a:ext cx=".." cy=".."/> (the enclosing shape's box) and
    <a:rPr ... sz="..."> (the run's font size) in the raw XML. Plain
    string/regex search, matching fill_deck()'s own plain-string XML
    editing approach rather than introducing an XML tree parser. None if
    either can't be found -- best-effort, the overflow check below simply
    skips (never raises) when geometry can't be confidently located."""
    idx = xml.find(wrapped_locator)
    if idx == -1:
        return None
    head = xml[:idx]
    ext_matches = list(_EXT_RE.finditer(head))
    sz_matches = list(_RPR_SZ_RE.finditer(head))
    if not ext_matches or not sz_matches:
        return None
    cx, cy = (int(g) for g in ext_matches[-1].groups())
    sz = int(sz_matches[-1].group(1))
    return cx, cy, sz


def _estimated_text_width_emu(text: str, font_size_hundredths_pt: int) -> float:
    font_size_pt = font_size_hundredths_pt / 100
    return len(text) * font_size_pt * _AVG_CHAR_WIDTH_EM / 72 * _EMU_PER_INCH


# Slots this check applies to -- the exact ones the reported bug hit. Not
# every slot in pitch_slots.json is a fixed-size single-line stat tile
# (bulleted/paragraph slots are already word/char capped with room to
# spare and are expected to wrap across multiple lines by design), so this
# is deliberately scoped rather than blanket-applied to every slot.
_STAT_TILE_SLOT_KEYS = {"aum_value", "aum_label"}


def check_stat_tile_fits(firm_name: str, fill_data: dict, slide_xml_before_fill: dict[str, str],
                        slot_map: dict) -> list[str]:
    """Requirement 4: assert the aum_value/aum_label stat tile's filled text
    fits on one line at the slot's own font size, per the box geometry read
    from the master. `slide_xml_before_fill` maps slide_no -> the slide's
    XML as it was BEFORE this fill's replacements (so the locator search
    below still finds the ORIGINAL "$4.4B"/"Gross Asset Value" placeholder
    text, whose box/font this new value must fit into)."""
    problems: list[str] = []
    for slide_no, slots in slot_map["slides"].items():
        xml = slide_xml_before_fill.get(slide_no)
        if xml is None:
            continue
        for slot in slots:
            if slot["key"] not in _STAT_TILE_SLOT_KEYS:
                continue
            text = str(fill_data.get(slot["key"], ""))
            if not text:
                continue
            geo = _shape_box_and_font_before(xml, f"<a:t>{slot['locator']}</a:t>")
            if geo is None:
                continue
            box_width_emu, _box_height_emu, font_sz = geo
            needed = _estimated_text_width_emu(text, font_sz)
            if needed > box_width_emu:
                problems.append(
                    f"{firm_name}: slot {slot['key']!r} value {text!r} is estimated to overflow "
                    f"its box (~{needed / _EMU_PER_INCH:.2f}in needed vs "
                    f"{box_width_emu / _EMU_PER_INCH:.2f}in available at {font_sz / 100:.1f}pt) -- "
                    f"would wrap to a second line and risks colliding with an adjacent slot"
                )
    return problems


def fill_deck(firm_name: str, fill_data: dict, output_path: Path) -> Path:
    """Unzips the master, replaces each slot's <a:t> run text in place
    (preserving every <a:rPr>), re-zips to output_path. Never touches a slide
    not listed in pitch_slots.json."""
    problems = preflight_slots()
    if problems:
        raise DeckFillError("preflight failed: " + "; ".join(problems))

    slot_map = load_slot_map()
    with zipfile.ZipFile(config.PITCH_MASTER_PPTX, "r") as zin:
        names = zin.namelist()
        contents = {n: zin.read(n) for n in names}

    slide_xml_before_fill: dict[str, str] = {}
    for slide_no, slots in slot_map["slides"].items():
        path = f"ppt/slides/slide{slide_no}.xml"
        xml = contents[path].decode("utf-8")
        slide_xml_before_fill[slide_no] = xml
        for slot in slots:
            if slot["type"] == "fixed":
                continue
            locator = slot["locator"]
            wrapped_old = f"<a:t>{locator}</a:t>"
            if xml.count(wrapped_old) != 1:
                raise DeckFillError(
                    f"{firm_name}: slot {slot['key']!r} locator not unique at fill "
                    f"time on slide {slide_no} ({xml.count(wrapped_old)} occurrences)"
                )
            new_text = _slot_new_text(slot, fill_data)
            xml = xml.replace(wrapped_old, f"<a:t>{new_text}</a:t>", 1)
        contents[path] = xml.encode("utf-8")

    # FIX 24: check BEFORE writing the output file -- a deck that fails this
    # check must never reach output_path at all, same "raise before ship"
    # posture as the placeholder-leak check below.
    tile_problems = check_stat_tile_fits(firm_name, fill_data, slide_xml_before_fill, slot_map)
    if tile_problems:
        raise DeckFillError("stat tile overflow: " + "; ".join(tile_problems))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in names:
            zout.writestr(n, contents[n])

    # QA finding (real-mode Kingbird review): fill_deck() always performs an
    # ACTIVE replace for every non-fixed slot, even when the new value is
    # empty -- so a bare master placeholder surviving into client-facing
    # output should be structurally impossible. Don't just assume that:
    # verify it, every fill, for every firm. If a future change (a new slot
    # type, a locator that stops matching, a slide the slot map doesn't
    # cover) ever lets a placeholder survive, this is the check that catches
    # it instead of a human spotting a wrong number on a client deck.
    leaks = find_placeholder_leaks(firm_name, fill_data, output_path)
    if leaks:
        raise DeckFillError("master placeholder leaked into output: " + "; ".join(leaks))
    return output_path


def find_placeholder_leaks(firm_name: str, fill_data: dict, output_path: Path) -> list[str]:
    """For every non-fixed slot whose resolved value legitimately differs
    from the master's own sample locator text, confirms that locator text
    is actually gone from the corresponding slide in output_path. A firm
    whose real value happens to equal the master's sample (e.g. the exact
    same GAV figure) is not a leak -- only a MISMATCH between what fill_data
    says and what the deck actually shows counts."""
    problems: list[str] = []
    slot_map = load_slot_map()
    with zipfile.ZipFile(output_path) as z:
        for slide_no, slots in slot_map["slides"].items():
            path = f"ppt/slides/slide{slide_no}.xml"
            xml = z.read(path).decode("utf-8")
            for slot in slots:
                if slot["type"] == "fixed":
                    continue
                locator = slot["locator"]
                new_text = _slot_new_text(slot, fill_data)
                if new_text == locator:
                    continue
                if f"<a:t>{locator}</a:t>" in xml:
                    problems.append(
                        f"{firm_name}: slide{slide_no} slot {slot['key']!r} -- master "
                        f"placeholder {locator!r} still present in output (expected {new_text!r})"
                    )
    return problems


# ---------------------------------------------------------------------------
# PDF conversion (attach the flat PDF, not the editable deck) + best-effort QA
# ---------------------------------------------------------------------------
def convert_to_pdf(pptx_path: Path, progress: Progress = _noop) -> Optional[Path]:
    soffice_wrapper = config.SOFFICE_SCRIPT
    if not soffice_wrapper.exists():
        progress("soffice wrapper not found; shipping the deck without a PDF conversion.")
        return None
    out_dir = pptx_path.parent
    try:
        proc = subprocess.run(
            [sys.executable, str(soffice_wrapper), "--headless", "--convert-to", "pdf",
             "--outdir", str(out_dir), str(pptx_path)],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        progress(f"PDF conversion failed ({e}); shipping the deck without a PDF conversion.")
        return None
    pdf_path = out_dir / (pptx_path.stem + ".pdf")
    if proc.returncode != 0 or not pdf_path.exists():
        progress(f"PDF conversion failed (rc={proc.returncode}); shipping the deck without a PDF conversion.")
        return None
    return pdf_path


def build_and_convert(firm_name: str, research: dict, deck_content: dict,
                      progress: Progress = _noop, personalization: Optional[dict] = None) -> dict:
    """Full Phase 5b entry point: build fill data, fill the deck, convert to
    PDF. Returns {"pptx_path", "pdf_path" (may be None), "fill_data"}."""
    config.GENERATED_DECKS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", firm_name).strip("_") or "firm"
    pptx_path = config.GENERATED_DECKS_DIR / f"XAnalyst_{slug}_Pitch.pptx"

    fill_data = build_fill_data(firm_name, research, deck_content, personalization)
    fill_deck(firm_name, fill_data, pptx_path)
    pdf_path = convert_to_pdf(pptx_path, progress=progress)
    return {"pptx_path": pptx_path, "pdf_path": pdf_path, "fill_data": fill_data}
