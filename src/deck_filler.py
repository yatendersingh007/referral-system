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
def _drop_estimated_money_mentions(items: list[str], must_scrub_money: bool) -> list[str]:
    if not must_scrub_money:
        return items or []
    return [s for s in (items or []) if not _BIG_MONEY_RE.search(s or "")]


# QA finding (real-mode Kingbird run): a bullet/paragraph restating a number
# with a DIFFERENT value than research (extraction drift) or one invented
# outright is just as much a data-integrity failure as a leaked estimated-
# AUM figure -- drop the whole item rather than patching it in place, same
# philosophy as _drop_estimated_money_mentions above.
def _drop_ungrounded_claims(items: list[str], corpus_text: str) -> list[str]:
    return [s for s in (items or []) if not fact_check.unverifiable_claims(s or "", corpus_text)]


def _cap_bullets(bullets: list[str], slots: int = 6, max_words: int = 18) -> list[str]:
    out = [_cap_words(b, max_words) for b in (bullets or [])][:slots]
    while len(out) < slots:
        out.append("")
    return out


def _cap_fit_paragraphs(paragraphs: list[str], firm_short: str, headings: list[str],
                        n: int = 5, max_words: int = 40) -> list[str]:
    out = [_cap_words(p, max_words) for p in (paragraphs or [])][:n]
    while len(out) < n:
        idx = len(out)
        heading = headings[idx] if idx < len(headings) else "this"
        out.append(f"X Analyst's {heading} work extends directly to {firm_short}'s operations.")
    return out


# ---------------------------------------------------------------------------
# Deck content (profile bullets + fit paragraphs) — TEST_MODE fixture, or one
# LLM extraction call in real mode (same shape as Workflow A's pitch_deck.py
# _extract_custom, adapted: no client_profile lookup, Phase 2/4 research only)
# ---------------------------------------------------------------------------
_CONTENT_PROMPT = """You are preparing a customized pitch deck for X Analyst
(offshore property/fund accounting and RE finance support) targeting the firm
below. Extract 4-6 factual profile bullets and exactly 5 fit paragraphs
mapping the firm's situation to these X Analyst service cards, in this order:
Property Accounting, Fund Structures, Investor Reporting, Underwriting &
Acquisitions, Capital Markets.

Firm: {name}
Research: {research}
Personalization hooks: {personalization}

Rules:
- Every bullet and paragraph must be a fact traceable to the research/hooks
  above. Never invent a number or fact.
- Every number (dollar figure, unit/state/property count) must be copied
  VERBATIM from the research/hooks above -- never rounded, re-derived, or
  paraphrased. A bullet whose number does not match the research object is
  dropped after generation, so an invented or drifted figure just costs you
  the slot.
- If AUM is tagged AUM_ESTIMATED or AUM_UNVERIFIED and it is NOT a disclosed
  Gross Asset Value (proxy_type != "gav"), do not state it as a hard dollar
  figure in a bullet -- it is a genuine internal estimate, not a fact the
  firm discloses.
- Bullets: <= 18 words each. Fit paragraphs: <= 40 words each, exactly 5.

Return a SINGLE JSON object, no prose, no markdown fence:
{{"profile_bullets": ["...", "..."], "fit_paragraphs": ["...", "...", "...", "...", "..."]}}
"""


def get_deck_content(firm_name: str, research: dict, personalization: dict) -> dict:
    """Returns {"profile_bullets": [...], "fit_paragraphs": [...]}. TEST_MODE
    returns a canned fixture; real mode makes one LLM extraction call."""
    if config.TEST_MODE:
        from . import testmode

        fixture = testmode.deck_content_fixture(firm_name)
        if fixture:
            return fixture
        return {"profile_bullets": [], "fit_paragraphs": []}

    from lib_snapshot import models
    import json as _json

    prompt = _CONTENT_PROMPT.format(
        name=firm_name,
        research=_json.dumps(research, default=str),
        personalization=_json.dumps(personalization, default=str),
    )
    parsed, _usage = models.complete_json_retry(prompt, temperature=0.4, max_tokens=1200)
    return {
        "profile_bullets": parsed.get("profile_bullets") or [],
        "fit_paragraphs": parsed.get("fit_paragraphs") or [],
    }


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
    aum_estimated = "ESTIMATED" in aum_tag
    disclosed_confirmed = aum_tag == "AUM_CONFIRMED" or (aum_estimated and proxy_type == "gav")
    if not disclosed_confirmed or not aum_display:
        aum_value, aum_label, aum_asof = "", "", ""
    else:
        aum_value = aum_display
        aum_label = "Gross Asset Value"
        aum_asof = research.get("aum_asof") or ""

    # A genuinely uncertain estimate must still have money mentions scrubbed
    # from free-form bullets/paragraphs; a confirmed disclosed figure
    # (including a GAV proxy) does not need scrubbing -- it's real and
    # citable.
    must_scrub_money = aum_estimated and not disclosed_confirmed

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
    headings = ["property accounting", "fund structure", "investor reporting",
               "underwriting", "capital markets"]
    safe_bullets = _drop_estimated_money_mentions(deck_content.get("profile_bullets", []), must_scrub_money)
    safe_fit_paragraphs = _drop_estimated_money_mentions(deck_content.get("fit_paragraphs", []), must_scrub_money)
    safe_bullets = _drop_ungrounded_claims(safe_bullets, corpus_text)
    safe_fit_paragraphs = _drop_ungrounded_claims(safe_fit_paragraphs, corpus_text)
    profile_bullets = _cap_bullets(safe_bullets)
    fit_paragraphs = _cap_fit_paragraphs(safe_fit_paragraphs, firm_short, headings)

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

    for slide_no, slots in slot_map["slides"].items():
        path = f"ppt/slides/slide{slide_no}.xml"
        xml = contents[path].decode("utf-8")
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
