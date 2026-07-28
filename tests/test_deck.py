"""QA Part 5 — deck fill & QA against §9's slot map and HARD RULE 14. Real
master PPTX is never mutated; corruption tests copy it to tmp_path first.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.reset_testmode import reset as reset_testmode
from lib_snapshot.drafting.pitch_deck import _short_name
from src import config, deck_filler, testmode

KINGBIRD = "Kingbird Investment Management"


@pytest.fixture(autouse=True)
def _clean_wc_state():
    """This file writes wc_runs/wc_firms/wc_drafts rows directly (bypassing
    the matcher) to test pipeline.process_referral_firm's graceful-degradation
    path. Reset first so a prior test file's leftover contacted_pocs entry for
    Lei Farrand doesn't turn a fresh 'drafted' assertion into a false
    'skipped_duplicate' — exactly what happened before this fixture existed."""
    reset_testmode(verbose=False)
    yield


def _copy_master(tmp_path: Path) -> Path:
    dest = tmp_path / "Corrupted_Master.pptx"
    dest.write_bytes(config.PITCH_MASTER_PPTX.read_bytes())
    return dest


def _rewrite_zip_entry(pptx_path: Path, member: str, old: str, new: str, expected_count: int = 1) -> None:
    with zipfile.ZipFile(pptx_path, "r") as zin:
        names = zin.namelist()
        contents = {n: zin.read(n) for n in names}
    xml = contents[member].decode("utf-8")
    assert xml.count(old) == expected_count, f"expected {old!r} exactly {expected_count}x, found {xml.count(old)}"
    contents[member] = xml.replace(old, new).encode("utf-8")
    with zipfile.ZipFile(pptx_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in names:
            zout.writestr(n, contents[n])


def _kingbird_research():
    return testmode.firm_research_fixture(KINGBIRD)["research"]


def _kingbird_deck_content():
    return testmode.deck_content_fixture(KINGBIRD)


# ---------------------------------------------------------------------------
# Preflight (HARD RULE 16a)
# ---------------------------------------------------------------------------
def test_preflight_passes_on_the_real_master():
    problems = deck_filler.preflight_slots()
    assert problems == []


def test_preflight_fails_loud_on_a_renamed_locator(tmp_path, monkeypatch):
    """Simulates the master being re-exported and a slot's text shifting —
    §11.8: 'fail loud at preflight, not at firm 12.'"""
    corrupted = _copy_master(tmp_path)
    _rewrite_zip_entry(corrupted, "ppt/slides/slide3.xml", "$4.4B", "$9.9B")
    monkeypatch.setattr(config, "PITCH_MASTER_PPTX", corrupted)

    problems = deck_filler.preflight_slots()
    assert problems, "corrupting a slot locator must produce a non-empty preflight problem list"
    assert any("aum_value" in p and "slide3" in p for p in problems), problems


def test_preflight_fails_loud_when_locator_duplicated(tmp_path, monkeypatch):
    """A locator string appearing 0 or 2+ times in its slide is equally
    unsafe for a plain-text replace — both must fail preflight, not just the
    'missing' case."""
    corrupted = _copy_master(tmp_path)
    # Make "2008" (founded_year) appear a second time on slide 3 by injecting
    # a duplicate run inside the same slide's XML.
    with zipfile.ZipFile(corrupted, "r") as zin:
        names = zin.namelist()
        contents = {n: zin.read(n) for n in names}
    xml = contents["ppt/slides/slide3.xml"].decode("utf-8")
    assert xml.count("<a:t>2008</a:t>") == 1
    xml = xml.replace("<a:t>2008</a:t>", "<a:t>2008</a:t><a:t>2008</a:t>", 1)
    contents["ppt/slides/slide3.xml"] = xml.encode("utf-8")
    with zipfile.ZipFile(corrupted, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in names:
            zout.writestr(n, contents[n])
    monkeypatch.setattr(config, "PITCH_MASTER_PPTX", corrupted)

    problems = deck_filler.preflight_slots()
    assert problems
    assert any("founded_year" in p for p in problems), problems


def test_preflight_fails_loud_on_missing_master(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PITCH_MASTER_PPTX", tmp_path / "does_not_exist.pptx")
    problems = deck_filler.preflight_slots()
    assert problems
    assert "not found" in problems[0]


def test_preflight_fails_loud_on_corrupt_zip(tmp_path, monkeypatch):
    bad = tmp_path / "not_a_zip.pptx"
    bad.write_bytes(b"this is not a zip file at all")
    monkeypatch.setattr(config, "PITCH_MASTER_PPTX", bad)
    problems = deck_filler.preflight_slots()
    assert problems
    assert "unzip" in problems[0].lower() or "corrupt" in problems[0].lower()


def test_fill_deck_raises_when_master_corrupted_mid_run(tmp_path, monkeypatch):
    """fill_deck() must ALSO refuse (not just preflight_slots() in isolation)
    — a caller that skips the standalone preflight check must still be safe."""
    corrupted = _copy_master(tmp_path)
    _rewrite_zip_entry(corrupted, "ppt/slides/slide3.xml", "$4.4B", "$9.9B")
    monkeypatch.setattr(config, "PITCH_MASTER_PPTX", corrupted)

    fill_data = deck_filler.build_fill_data(KINGBIRD, _kingbird_research(), _kingbird_deck_content())
    with pytest.raises(deck_filler.DeckFillError):
        deck_filler.fill_deck(KINGBIRD, fill_data, tmp_path / "out.pptx")


# ---------------------------------------------------------------------------
# Slot correctness: name-swap vs full-replace
# ---------------------------------------------------------------------------
def _slide_texts(pptx_path: Path, slide_no: int) -> list[str]:
    import re

    with zipfile.ZipFile(pptx_path) as z:
        xml = z.read(f"ppt/slides/slide{slide_no}.xml").decode("utf-8")
    return re.findall(r"<a:t>(.*?)</a:t>", xml)


def test_name_swap_slots_replace_firm_short_everywhere(tmp_path):
    firm = "Riverside Bay Partners"
    research = {"firm_oneliner": "A regional multifamily manager.", "aum_display": "$500M",
               "aum_tag": "AUM_CONFIRMED", "aum_asof": "as of 2026", "founded_year": "2015",
               "capital_stack": "LP, GP"}
    deck_content = {"profile_bullets": ["Fact one about Riverside.", "Fact two about Riverside."],
                    "fit_paragraphs": {"property_accounting": "Para one.", "fund_structures": "Para two.",
                                      "investor_reporting": "Para three.",
                                      "underwriting_acquisitions": "Para four.",
                                      "capital_markets": "Para five."}}
    fill_data = deck_filler.build_fill_data(firm, research, deck_content)
    assert fill_data["firm_short"] == "Riverside Bay"
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck(firm, fill_data, out)

    slide1 = _slide_texts(out, 1)
    assert "Built for Riverside Bay." in slide1
    assert "Kingbird" not in " ".join(slide1)

    slide2 = _slide_texts(out, 2)
    assert any("extend Riverside Bay" in t for t in slide2)
    assert "Kingbird" not in " ".join(slide2)

    slide12 = _slide_texts(out, 12)
    assert "Where this fits Riverside Bay specifically" in slide12

    slide14 = _slide_texts(out, 14)
    assert any("Riverside Bay" in t for t in slide14)


def test_full_replace_slots_take_research_values(tmp_path):
    firm = "Northgate Realty Partners"
    research = {"firm_oneliner": "A vertically integrated industrial REIT.", "aum_display": "$750M",
               "aum_tag": "AUM_CONFIRMED", "aum_asof": "as of Jan 1, 2026", "founded_year": "2011",
               "capital_stack": "LP, GP, Preferred Equity",
               "geography": "Owns 40 industrial properties across the Midwest."}
    deck_content = {"profile_bullets": ["Owns 40 industrial properties.", "Focused on the Midwest."],
                    "fit_paragraphs": {"property_accounting": "Para A.", "fund_structures": "Para B.",
                                      "investor_reporting": "Para C.",
                                      "underwriting_acquisitions": "Para D.",
                                      "capital_markets": "Para E."}}
    fill_data = deck_filler.build_fill_data(firm, research, deck_content)
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck(firm, fill_data, out)

    slide3 = _slide_texts(out, 3)
    assert "A vertically integrated industrial REIT." in slide3
    assert "$750M" in slide3
    assert "AUM" in slide3  # FIX 10: plain AUM_CONFIRMED (no gav proxy) labels the tile "AUM"
    assert "as of Jan 1, 2026" in slide3
    assert "2011" in slide3
    assert "LP, GP, Preferred Equity" in slide3
    assert "Owns 40 industrial properties." in slide3
    assert "Focused on the Midwest." in slide3

    slide12 = _slide_texts(out, 12)
    for p in ("Para A.", "Para B.", "Para C.", "Para D.", "Para E."):
        assert p in slide12
    # the 5 card headings are FIXED — never touched regardless of firm
    assert "Scalable Property Accounting" in slide12
    assert "Capital Markets Expertise" in slide12


def test_fixed_slides_4_to_11_and_13_never_touched(tmp_path):
    firm = "Cedar Point Capital"
    research = {"firm_oneliner": "x", "aum_display": "$300M", "aum_tag": "AUM_CONFIRMED"}
    deck_content = {"profile_bullets": ["b1"], "fit_paragraphs": {"property_accounting": "p1", "fund_structures": "p2", "investor_reporting": "p3", "underwriting_acquisitions": "p4", "capital_markets": "p5"}}
    fill_data = deck_filler.build_fill_data(firm, research, deck_content)
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck(firm, fill_data, out)

    for slide_no in list(range(4, 12)) + [13]:
        with zipfile.ZipFile(config.PITCH_MASTER_PPTX) as zmaster, zipfile.ZipFile(out) as zout:
            master_xml = zmaster.read(f"ppt/slides/slide{slide_no}.xml")
            out_xml = zout.read(f"ppt/slides/slide{slide_no}.xml")
        assert master_xml == out_xml, f"slide{slide_no}.xml should be byte-identical to the master (fixed slide)"


# ---------------------------------------------------------------------------
# HARD RULE 14 (FIX 10 reinterpretation, real-mode Kairos run): an estimated
# AUM must never be presented AS IF confirmed -- but that no longer means
# blanked outright. The gate itself already verified this figure well enough
# to QUALIFY the firm on it; the old blanket "blank unless CONFIRMED or GAV"
# guard meant a real, gate-verified figure (e.g. Kairos's $254M
# invested-capital proxy) never reached the deck at all. Now: any figure the
# gate produced a display value for is always shown, honestly labeled by
# _aum_label_for() so the tile never claims more certainty than the gate
# actually established (see the GAV-proxy and non-gav-proxy tests below for
# the specific label each proxy_type gets).
# ---------------------------------------------------------------------------
def test_aum_estimated_shows_an_honestly_labeled_estimate_not_blank():
    research = {"firm_oneliner": "x", "aum_display": "$1.2B", "aum_tag": "AUM_ESTIMATED",
               "aum_asof": "as of 2026", "founded_year": "2010"}
    fill_data = deck_filler.build_fill_data("Estimated AUM Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "$1.2B"
    assert fill_data["aum_label"] == "Est. AUM"  # no proxy_basis text to name a specific proxy kind
    assert fill_data["aum_asof"] == "as of 2026"
    assert fill_data["founded_year"] == "2010"


def test_aum_estimated_stat_card_renders_honestly_labeled_end_to_end_in_xml(tmp_path):
    research = {"firm_oneliner": "An estimated-AUM test firm.", "aum_display": "$1.2B",
               "aum_tag": "AUM_ESTIMATED", "aum_asof": "as of 2026", "founded_year": "2012",
               "capital_stack": "LP, GP"}
    deck_content = {"profile_bullets": ["A fact."], "fit_paragraphs": {"property_accounting": "p1", "fund_structures": "p2", "investor_reporting": "p3", "underwriting_acquisitions": "p4", "capital_markets": "p5"}}
    fill_data = deck_filler.build_fill_data("Estimated Big Money LLC", research, deck_content)
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck("Estimated Big Money LLC", fill_data, out)
    slide3 = _slide_texts(out, 3)
    assert "$1.2B" in slide3, "a gate-verified estimated figure must reach the deck's stat tile (FIX 10)"
    assert "Est. AUM" in slide3, "must be honestly labeled as an estimate, never as flat/unqualified AUM"
    assert "2012" in slide3  # founded year still renders — it's a count/year, not money


def test_aum_estimated_strips_money_mentions_from_bullets_and_fit_paragraphs():
    """QA finding (adversarial pass, §11.9/§11.2): the dedicated aum_value
    slot was correctly blanked for AUM_ESTIMATED, but the SAME dollar figure
    leaked straight through free-form profile_bullets/fit_paragraphs
    (exactly the 'money forms reaching deck stats under non-AUM labels'
    pattern §11.2 already names). A bullet/paragraph containing a money
    figure must be dropped whole (never partially edited) when AUM is
    estimated; a clean bullet still survives and shifts up to fill slots
    (profile_bullets stays a plain list). fit_paragraphs is now keyed by
    service (FIX 11) -- a money-leaking key is blanked IN PLACE, never
    shifted into a different key's slot."""
    research = {"firm_oneliner": "x", "aum_display": "$1.2B", "aum_tag": "AUM_ESTIMATED"}
    deck_content = {
        "profile_bullets": [
            "Manages an estimated $1.2 billion portfolio across residential assets.",
            "Founded in 2015.",
            "Operates in 5 states.",
        ],
        "fit_paragraphs": {
            "property_accounting": "With $1.2B in assets under management, this firm needs robust reporting.",
            "fund_structures": "A clean paragraph with no dollar figure at all.",
        },
    }
    fill_data = deck_filler.build_fill_data("Smuggled Money Co", research, deck_content)
    all_bullets = " ".join(fill_data[f"profile_bullet_{i}"] for i in range(1, 7))
    all_fit = " ".join(fill_data[f"fit_paragraph_{i}"] for i in range(1, 6))
    assert "1.2" not in all_bullets, f"a money figure leaked through a bullet: {all_bullets!r}"
    assert "1.2" not in all_fit, f"a money figure leaked through a fit paragraph: {all_fit!r}"
    assert fill_data["profile_bullet_1"] == "Founded in 2015."  # clean bullet shifted up
    # property_accounting is slot 1 -- blanked in place, NOT shifted
    assert fill_data["fit_paragraph_1"] == ""
    # fund_structures is slot 2 -- its own clean paragraph, still in its own slot
    assert fill_data["fit_paragraph_2"] == "A clean paragraph with no dollar figure at all."


def test_aum_confirmed_does_not_strip_money_mentions_from_bullets():
    """The strip is specific to ESTIMATED (unverified) figures — a CONFIRMED
    AUM is a verified fact and a bullet is free to state it."""
    research = {"firm_oneliner": "x", "aum_display": "$500M", "aum_tag": "AUM_CONFIRMED"}
    deck_content = {"profile_bullets": ["Manages a confirmed $500M portfolio."], "fit_paragraphs": {}}
    fill_data = deck_filler.build_fill_data("Confirmed Money Co", research, deck_content)
    assert fill_data["profile_bullet_1"] == "Manages a confirmed $500M portfolio."


def test_aum_unverified_also_blanks_stat_card():
    """A qualified-on-manual-override firm could in principle still carry an
    UNVERIFIED tag from before the operator's /qualify — the guard must not
    depend on the firm having reached 'qualified' status, only on the tag."""
    research = {"firm_oneliner": "x", "aum_display": "", "aum_tag": "AUM_UNVERIFIED"}
    fill_data = deck_filler.build_fill_data("Unverified Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == ""


# ---------------------------------------------------------------------------
# Fix A (real-mode Kingbird run): a GAV proxy is a CONFIRMED, publicly
# disclosed figure -- the gate's own leverage-haircut conversion to an
# estimated equity number is an internal analytical construct, never written
# back into aum_display. The prior guard's `_AUM_LABEL_RE.search("AUM")`
# condition searched a literal constant string and was always true, so it
# blanked the GAV stat card too, even though it's real and must show.
# ---------------------------------------------------------------------------
def test_gav_proxy_estimated_shows_the_confirmed_stat_card():
    research = {"firm_oneliner": "x", "aum_display": "$4.4B", "aum_tag": "AUM_ESTIMATED",
               "proxy_type": "gav", "aum_asof": "as of Dec 31, 2025"}
    fill_data = deck_filler.build_fill_data("GAV Disclosed Co", research,
                                            {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "$4.4B"
    assert fill_data["aum_label"] == "Gross Asset Value"
    assert fill_data["aum_asof"] == "as of Dec 31, 2025"


def test_gav_proxy_estimated_does_not_strip_money_mentions_from_bullets():
    research = {"firm_oneliner": "x", "aum_display": "$4.4B", "aum_tag": "AUM_ESTIMATED", "proxy_type": "gav"}
    deck_content = {"profile_bullets": ["Manages $4.4 billion in gross asset value."], "fit_paragraphs": {}}
    fill_data = deck_filler.build_fill_data("GAV Bullet Co", research, deck_content)
    assert fill_data["profile_bullet_1"] == "Manages $4.4 billion in gross asset value."


def test_gav_proxy_estimated_stat_card_renders_end_to_end_in_xml(tmp_path):
    """Uses a GAV figure deliberately different from the master's own literal
    "$4.4B" locator text, so the assertion proves the value came from
    research, not a no-op match against the unfilled master."""
    research = {"firm_oneliner": "A GAV-disclosing test firm.", "aum_display": "$3.4B",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "gav", "aum_asof": "as of Dec 31, 2025",
               "founded_year": "2012", "capital_stack": "LP, GP, Preferred Equity"}
    deck_content = {"profile_bullets": ["A fact."], "fit_paragraphs": {"property_accounting": "p1", "fund_structures": "p2", "investor_reporting": "p3", "underwriting_acquisitions": "p4", "capital_markets": "p5"}}
    fill_data = deck_filler.build_fill_data("GAV Render Co", research, deck_content)
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck("GAV Render Co", fill_data, out)
    slide3 = _slide_texts(out, 3)
    assert "$3.4B" in slide3
    assert "Gross Asset Value" in slide3


def test_non_gav_estimated_proxy_shows_with_a_generic_est_label():
    """FIX 10: a floor/non_floor/units proxy has no directly-disclosed figure
    behind it the way a GAV proxy does, but the gate still verified it well
    enough to qualify the firm -- it must reach the deck too, just labeled as
    an estimate rather than "Gross Asset Value" or plain "AUM"."""
    research = {"firm_oneliner": "x", "aum_display": "$1.7B", "aum_tag": "AUM_ESTIMATED", "proxy_type": "floor"}
    fill_data = deck_filler.build_fill_data("Floor Proxy Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "$1.7B"
    assert fill_data["aum_label"] == "Est. AUM"


# ---------------------------------------------------------------------------
# Fix C (real-mode Kingbird run): capital_stack rendered as the degenerate,
# self-referential "across the capital stack" instead of the real disclosed
# tranche list. No real capital-stack value would ever contain the phrase
# "capital stack" describing itself.
# ---------------------------------------------------------------------------
def test_degenerate_capital_stack_value_is_blanked():
    research = {"firm_oneliner": "x", "aum_display": "$1.1B", "aum_tag": "AUM_CONFIRMED",
               "capital_stack": "across the capital stack"}
    fill_data = deck_filler.build_fill_data("Degenerate Stack Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["capital_stack"] == ""


def test_real_capital_stack_value_passes_through():
    research = {"firm_oneliner": "x", "aum_display": "$1.1B", "aum_tag": "AUM_CONFIRMED",
               "capital_stack": "LP, GP, Co-GP, Preferred Equity"}
    fill_data = deck_filler.build_fill_data("Real Stack Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["capital_stack"] == "LP, GP, Co-GP, Preferred Equity"


# ---------------------------------------------------------------------------
# Fix B (real-mode Kingbird run): a bullet/paragraph whose number does not
# trace to the research/personalization object is a data-integrity failure
# (extraction drift or an outright hallucination), not just an estimated-AUM
# leak -- dropped whole, same "never partially edit" philosophy.
# ---------------------------------------------------------------------------
def test_ungrounded_numeric_claim_in_bullet_is_dropped():
    research = {"firm_oneliner": "x", "aum_display": "$1.1B", "aum_tag": "AUM_CONFIRMED",
               "geography": "US (17 states) and Puerto Rico"}
    deck_content = {
        "profile_bullets": [
            "Operates across 12 states and Puerto Rico.",  # drifted from research's 17
            "A fiduciary for family offices and third-party LPs.",  # no numbers, clean
        ],
        "fit_paragraphs": {},
    }
    fill_data = deck_filler.build_fill_data("Drifted Numbers Co", research, deck_content)
    assert fill_data["profile_bullet_1"] == "A fiduciary for family offices and third-party LPs."
    assert "12 states" not in fill_data["profile_bullet_2"]


def test_grounded_numeric_claim_matching_research_survives():
    research = {"firm_oneliner": "x", "aum_display": "$1.1B", "aum_tag": "AUM_CONFIRMED",
               "geography": "US (17 states) and Puerto Rico"}
    deck_content = {"profile_bullets": ["Operates across 17 states and Puerto Rico."], "fit_paragraphs": {}}
    fill_data = deck_filler.build_fill_data("Matching Numbers Co", research, deck_content)
    assert fill_data["profile_bullet_1"] == "Operates across 17 states and Puerto Rico."


# ---------------------------------------------------------------------------
# General placeholder-leak safety net (QA ask, follow-up review): an empty
# fill value must actively blank a slot, never leave the master's own sample
# text showing through on a client-facing deck. fill_deck() always performs
# an active replace for every non-fixed slot (even an empty one), so this
# should be structurally impossible -- these tests verify it directly rather
# than assuming the replace logic is airtight.
# ---------------------------------------------------------------------------
def test_empty_fill_value_leaves_no_master_placeholder_end_to_end(tmp_path):
    research = {"firm_oneliner": "x", "aum_display": "", "aum_tag": "AUM_UNVERIFIED"}
    deck_content = {"profile_bullets": [], "fit_paragraphs": {}}
    fill_data = deck_filler.build_fill_data("Blank Slots Co", research, deck_content)
    assert fill_data["aum_value"] == ""
    assert fill_data["aum_label"] == ""
    assert fill_data["founded_year"] == ""
    assert fill_data["capital_stack"] == ""

    out = tmp_path / "out.pptx"
    deck_filler.fill_deck("Blank Slots Co", fill_data, out)
    slide3 = " ".join(_slide_texts(out, 3))
    for master_placeholder in ("$4.4B", "Gross Asset Value", "as of Dec 31, 2025", "2008",
                               "LP, GP, Co-GP, Preferred Equity"):
        assert master_placeholder not in slide3, f"master placeholder {master_placeholder!r} leaked into output"
    assert deck_filler.find_placeholder_leaks("Blank Slots Co", fill_data, out) == []


def test_find_placeholder_leaks_detects_a_reintroduced_master_value(tmp_path):
    """Direct test of the general safety net itself: if a slot's master
    placeholder somehow survives into output (simulated here by injecting it
    back in after a clean fill), find_placeholder_leaks must catch it."""
    firm = "Northgate Realty Partners"
    research = {"firm_oneliner": "A vertically integrated industrial REIT.", "aum_display": "$750M",
               "aum_tag": "AUM_CONFIRMED", "aum_asof": "as of Jan 1, 2026", "founded_year": "2011",
               "capital_stack": "LP, GP, Preferred Equity"}
    deck_content = {"profile_bullets": [], "fit_paragraphs": {}}
    fill_data = deck_filler.build_fill_data(firm, research, deck_content)
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck(firm, fill_data, out)
    assert deck_filler.find_placeholder_leaks(firm, fill_data, out) == []

    _rewrite_zip_entry(out, "ppt/slides/slide3.xml", "<a:t>2011</a:t>",
                       "<a:t>2011</a:t><a:t>2008</a:t>", expected_count=1)
    leaks = deck_filler.find_placeholder_leaks(firm, fill_data, out)
    assert any("founded_year" in p and "2008" in p for p in leaks), leaks


def test_fill_deck_raises_when_a_placeholder_would_leak(tmp_path, monkeypatch):
    """fill_deck() itself must refuse to ship output with a leaked
    placeholder, not just report it via a separate function a caller might
    forget to call. find_placeholder_leaks' own detection logic is proven
    separately above; this test proves fill_deck is actually WIRED to it and
    treats a non-empty result as a hard failure."""
    firm = "Leak Guard Co"
    research = {"firm_oneliner": "x", "aum_display": "$300M", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data(firm, research, {"profile_bullets": [], "fit_paragraphs": {}})

    monkeypatch.setattr(deck_filler, "find_placeholder_leaks",
                        lambda *a, **kw: ["simulated leak for this test"])
    with pytest.raises(deck_filler.DeckFillError, match="placeholder leaked"):
        deck_filler.fill_deck(firm, fill_data, tmp_path / "out.pptx")


# ---------------------------------------------------------------------------
# Escaping regression guard (found during this fix's own review): sharing
# slot-resolution logic between fill_deck() and find_placeholder_leaks()
# risked double-escaping a name_swap template's already-XML-escaped
# apostrophe entity (&apos; -> &amp;apos;, which renders as the literal text
# "&apos;" on the slide instead of an apostrophe).
# ---------------------------------------------------------------------------
def test_name_swap_slot_preserves_xml_entity_escaping_no_double_escape(tmp_path):
    firm = "Riverside Bay Partners"
    research = {"firm_oneliner": "x", "aum_display": "$500M", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data(firm, research, {"profile_bullets": [], "fit_paragraphs": {}})
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck(firm, fill_data, out)
    combined = " ".join(_slide_texts(out, 2))
    assert "Riverside Bay&apos;s" in combined, combined
    assert "&amp;apos;" not in combined, f"apostrophe entity was double-escaped: {combined!r}"


def test_grounded_numeric_claim_from_personalization_survives():
    """The grounding corpus includes personalization too -- a fact Phase 4
    surfaced (e.g. a unit count in a hook) that never made it into a Phase 2
    research field must still count as grounded, not get dropped as an
    invented number."""
    research = {"firm_oneliner": "x", "aum_display": "$1.1B", "aum_tag": "AUM_CONFIRMED"}
    personalization = {"hook_1": "Since 2008 the firm has invested in over 21,000 multifamily units."}
    deck_content = {"profile_bullets": ["Manages a portfolio of 21,000 multifamily units."], "fit_paragraphs": {}}
    fill_data = deck_filler.build_fill_data("Personalization Grounded Co", research, deck_content, personalization)
    assert fill_data["profile_bullet_1"] == "Manages a portfolio of 21,000 multifamily units."


# ---------------------------------------------------------------------------
# Overflow prevention (§9 caps)
# ---------------------------------------------------------------------------
def test_overflow_bullets_capped_at_6_slots_18_words():
    long_bullets = [f"This is a very long bullet number {i} that goes on and on well past eighteen words "
                    f"to force truncation of the sentence." for i in range(8)]
    research = {"firm_oneliner": "x", "aum_display": "$300M", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data("Overflow Co", research,
                                            {"profile_bullets": long_bullets, "fit_paragraphs": {}})
    bullet_keys = [f"profile_bullet_{i}" for i in range(1, 7)]
    assert "profile_bullet_7" not in fill_data  # only 6 slots exist in the data contract at all
    for k in bullet_keys:
        assert len(fill_data[k].split()) <= 19, f"{k} exceeds the ~18-word cap: {fill_data[k]!r}"  # +1 for trailing period tolerance


def test_overflow_fit_paragraphs_capped_at_5_slots_40_words():
    """FIX 11: fit_paragraphs is keyed by the 5 canonical service keys, not a
    positional list -- there's no '6th item' overflow scenario anymore (a
    dict simply can't hold more than these 5 keys). What still needs
    covering is the per-paragraph 40-word cap, independently for each key."""
    service_keys = ["property_accounting", "fund_structures", "investor_reporting",
                    "underwriting_acquisitions", "capital_markets"]
    long_paragraphs = {key: f"This is the {key} paragraph " + ("word " * 60) for key in service_keys}
    research = {"firm_oneliner": "x", "aum_display": "$300M", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data("Overflow Fit Co", research,
                                            {"profile_bullets": [], "fit_paragraphs": long_paragraphs})
    for i in range(1, 6):
        key = f"fit_paragraph_{i}"
        assert key in fill_data
        assert len(fill_data[key].split()) <= 41, f"{key} exceeds the ~40-word cap: {fill_data[key]!r}"
    assert "fit_paragraph_6" not in fill_data


def test_overflow_long_legal_firm_name_capped_on_cover_slide(tmp_path):
    """QA finding (visual render): slide 1's 'PREPARED FOR' full-legal-name
    slot had NO length cap at all — §9 explicitly names this exact risk
    ('A firm with a longer name... can overflow'). A 72-character stress
    name wrapped to 2 lines and crowded the bottom margin against the
    CONFIDENTIAL footer in the pre-fix render. Fixed via _cap_chars (a
    truncate, one of §9's two sanctioned remediations: 'auto-fit... or
    truncate... to fit'). Deliberately ends in nothing but legal/generic
    words (Holdings/Group/LLC) so _short_name's trailing-strip (FIX 12)
    reduces firm_short to something short ("Meridian Bridge") -- a name
    whose long tail is instead a non-generic descriptive phrase (e.g. "...of
    the Southeastern United States") would leave firm_short equal to the
    full name, which is a real, separate stress case but not what THIS test
    is checking (the full-legal-name slot's own _cap_chars truncation)."""
    long_name = "Meridian Bridge Capital Investment Partners Holdings Group, LLC"
    research = {"firm_oneliner": "x", "aum_display": "$1.4B", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data(long_name, research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert len(fill_data["firm_name_display"]) <= deck_filler._FIRM_NAME_DISPLAY_MAX_CHARS + 1  # +1 for the ellipsis char
    assert fill_data["firm_name_display"].endswith("…")

    out = tmp_path / "out.pptx"
    deck_filler.fill_deck(long_name, fill_data, out)
    slide1 = _slide_texts(out, 1)
    assert fill_data["firm_name_display"] in slide1
    assert long_name not in " ".join(slide1)  # the un-truncated name must never appear


def test_short_firm_name_never_truncated():
    fill_data = deck_filler.build_fill_data(KINGBIRD, {"firm_oneliner": "x", "aum_tag": "AUM_CONFIRMED"},
                                            {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["firm_name_display"] == KINGBIRD
    assert not fill_data["firm_name_display"].endswith("…")


def test_overflow_firm_oneliner_capped_at_40_words():
    six_line_oneliner = " ".join(["word"] * 80)  # deliberately way over any reasonable single-line length
    research = {"firm_oneliner": six_line_oneliner, "aum_display": "$300M", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data("Long Oneliner Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert len(fill_data["firm_oneliner"].split()) <= 41


def test_fewer_than_6_bullets_blanks_remaining_slots_not_placeholder_text():
    research = {"firm_oneliner": "x", "aum_display": "$300M", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data("Few Bullets Co", research,
                                            {"profile_bullets": ["Only one fact."], "fit_paragraphs": {}})
    assert fill_data["profile_bullet_1"] == "Only one fact."
    for i in range(2, 7):
        assert fill_data[f"profile_bullet_{i}"] == ""


# ---------------------------------------------------------------------------
# Output validity + visual QA (render + inspect, using a deliberately
# stress-testing firm name/content set, not just re-confirming Kingbird)
# ---------------------------------------------------------------------------
def test_output_converts_to_a_valid_14_page_pdf(tmp_path):
    firm = "Meridian Bridge Capital Partners of the Southeastern United States"  # deliberately long name
    research = {"firm_oneliner": ("A multi-strategy real estate investment platform spanning "
                                  "industrial, multifamily, and self-storage assets across the "
                                  "Southeast and Mid-Atlantic regions of the United States."),
               "aum_display": "$1.4B", "aum_tag": "AUM_CONFIRMED", "aum_asof": "as of Q4 2025",
               "founded_year": "1998", "capital_stack": "LP, GP, Co-GP, Preferred Equity, Mezzanine Debt"}
    deck_content = {
        "profile_bullets": [
            "Operates across industrial, multifamily, and self-storage asset classes regionally.",
            "Vertically integrated platform with in-house property management services.",
            "Concentrated in the Southeast and Mid-Atlantic United States markets.",
            "Uses a complex multi-tier LP, GP, and co-GP capital structure.",
            "Adds mezzanine debt alongside preferred equity on larger transactions.",
            "Founded in 1998 by a team of former institutional investors.",
        ],
        "fit_paragraphs": {
            "property_accounting": (
                "With three distinct asset classes under one roof, Meridian Bridge needs property "
                "accounting that can flex across very different operating models without adding headcount."
            ),
            "fund_structures": (
                "A five-layer capital stack across industrial, multifamily, and storage deals means "
                "fund accounting has to track waterfalls correctly for every entity and every asset class."
            ),
            "investor_reporting": (
                "A confirmed $1.4B platform spanning three asset classes needs investor reporting "
                "that can roll up cleanly across very different property types for the same investor base."
            ),
            "underwriting_acquisitions": (
                "Continued acquisitions across three asset classes keep underwriting busy modeling "
                "very different deal types side by side against the existing multi-asset-class portfolio."
            ),
            "capital_markets": (
                "Mezzanine debt alongside preferred equity on larger deals means capital markets work "
                "spans a wider set of instruments than a single-asset-class shop would ever need to manage."
            ),
        },
    }
    result = deck_filler.build_and_convert(firm, research, deck_content, progress=lambda m: None)
    assert result["pptx_path"].exists()
    with zipfile.ZipFile(result["pptx_path"]) as z:
        assert z.testzip() is None  # valid zip, no corrupt members

    if result["pdf_path"] is None:
        pytest.skip("soffice not available in this environment — PDF conversion untestable here")
    assert result["pdf_path"].exists()

    try:
        import fitz  # PyMuPDF, dev-only QA dependency (see HANDOVER.md)
    except ImportError:
        pytest.skip("pymupdf not installed — page-count/visual QA untestable here")

    doc = fitz.open(str(result["pdf_path"]))
    assert doc.page_count == 14

    # Render slides 1/3/12 (the cover, firm-profile, and firm-fit slides —
    # the ones with a long name and long researched content to stress) for
    # visual inspection. Saved under qa_out/ so they can be opened/reviewed;
    # this test only asserts they render without raising and are non-trivial
    # images (a blank/corrupt render would be suspiciously small).
    qa_dir = Path(__file__).resolve().parents[1] / "qa_out" / "deck_visual_qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    for slide_no in (1, 3, 12):
        pix = doc[slide_no - 1].get_pixmap(dpi=150)
        out_png = qa_dir / f"stress_test_slide_{slide_no}.png"
        pix.save(str(out_png))
        assert out_png.stat().st_size > 20_000, f"slide {slide_no} render suspiciously small/blank"


# ---------------------------------------------------------------------------
# Graceful degradation (§3): master PPTX missing -> email-only, summary says so
# ---------------------------------------------------------------------------
def test_pipeline_runs_email_only_when_master_missing(tmp_path, monkeypatch):
    from src import dbc, pipeline

    monkeypatch.setattr(config, "PITCH_MASTER_PPTX", tmp_path / "missing_master.pptx")

    run = dbc.create_wc_run("single_firm", query_firm=KINGBIRD)
    firm = dbc.create_wc_firm(run["id"], KINGBIRD)
    result = pipeline.process_referral_firm(firm["id"])
    assert result["outcome"] == "drafted"
    assert result["ppt_attached"] is False

    draft = dbc.wc_draft_for_firm(firm["id"])
    assert draft["email_status"] == "drafted"
    assert draft["ppt_attached"] is False

    # cleanup this test's own rows
    with dbc.conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM contacted_pocs WHERE firm_name = %s", (KINGBIRD,))
            cur.execute("DELETE FROM wc_drafts WHERE firm_id = %s", (firm["id"],))
            cur.execute("DELETE FROM wc_firms WHERE id = %s", (firm["id"],))
            cur.execute("DELETE FROM wc_runs WHERE id = %s", (run["id"],))
        c.commit()


def test_summary_reports_ppt_skipped_when_master_missing(tmp_path, monkeypatch):
    from src import dbc, pipeline, telegram_bot

    monkeypatch.setattr(config, "PITCH_MASTER_PPTX", tmp_path / "missing_master.pptx")

    run = dbc.create_wc_run("single_firm", query_firm=KINGBIRD)
    firm = dbc.create_wc_firm(run["id"], KINGBIRD)
    pipeline.process_referral_firm(firm["id"])

    summary = telegram_bot._fmt_summary(run["id"], {"drafted": 1, "disqualified": 0, "parked": 0,
                                                    "no_email_found": 0, "skipped_duplicate": 0})
    assert "PPT skipped: master template not found." in summary
    assert "(0 with PPT attached)" in summary

    with dbc.conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM contacted_pocs WHERE firm_name = %s", (KINGBIRD,))
            cur.execute("DELETE FROM wc_drafts WHERE firm_id = %s", (firm["id"],))
            cur.execute("DELETE FROM wc_firms WHERE id = %s", (firm["id"],))
            cur.execute("DELETE FROM wc_runs WHERE id = %s", (run["id"],))
        c.commit()


# ---------------------------------------------------------------------------
# FIX 24 (adversarial re-review): DeckFillError (preflight failure, a leaked
# placeholder, or the new stat-tile-overflow render check) was previously
# uncaught in pipeline.py's Phase 5b -- it propagated to drain_queue()'s
# generic exception handler, marking the WHOLE RUN "crashed" and aborting
# every remaining queued firm for one firm's foreseeable data-shape issue
# (e.g. a verbatim disclosed_figure this build's compaction doesn't
# recognize), and /referral resume would retry the SAME firm and hit the
# identical error again, wedging the run indefinitely. A deck that can't be
# safely built now degrades exactly like the master-missing case above:
# email-only for THIS firm, run continues.
# ---------------------------------------------------------------------------
def test_pipeline_degrades_to_email_only_when_deck_fill_error_raised(monkeypatch):
    from src import dbc, pipeline

    def _raise_deck_fill_error(*args, **kwargs):
        raise deck_filler.DeckFillError("stat tile overflow: simulated for this test")

    monkeypatch.setattr(deck_filler, "build_and_convert", _raise_deck_fill_error)

    run = dbc.create_wc_run("single_firm", query_firm=KINGBIRD)
    firm = dbc.create_wc_firm(run["id"], KINGBIRD)
    result = pipeline.process_referral_firm(firm["id"])
    assert result["outcome"] == "drafted"
    assert result["ppt_attached"] is False

    draft = dbc.wc_draft_for_firm(firm["id"])
    assert draft["email_status"] == "drafted"
    assert draft["ppt_attached"] is False

    with dbc.conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM contacted_pocs WHERE firm_name = %s", (KINGBIRD,))
            cur.execute("DELETE FROM wc_drafts WHERE firm_id = %s", (firm["id"],))
            cur.execute("DELETE FROM wc_firms WHERE id = %s", (firm["id"],))
            cur.execute("DELETE FROM wc_runs WHERE id = %s", (run["id"],))
        c.commit()


def test_pipeline_reraises_non_deckfillerror_exceptions_from_deck_build(monkeypatch):
    """The graceful-degradation catch is scoped to DeckFillError only -- a
    genuinely unexpected exception (not a foreseeable "couldn't safely
    build this deck" case) must still surface as a real failure, not be
    silently swallowed as if it were the same class of situation."""
    from src import dbc, pipeline

    def _raise_value_error(*args, **kwargs):
        raise ValueError("something genuinely unexpected")

    monkeypatch.setattr(deck_filler, "build_and_convert", _raise_value_error)

    run = dbc.create_wc_run("single_firm", query_firm=KINGBIRD)
    firm = dbc.create_wc_firm(run["id"], KINGBIRD)
    with pytest.raises(ValueError, match="something genuinely unexpected"):
        pipeline.process_referral_firm(firm["id"])

    with dbc.conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM contacted_pocs WHERE firm_name = %s", (KINGBIRD,))
            cur.execute("DELETE FROM wc_drafts WHERE firm_id = %s", (firm["id"],))
            cur.execute("DELETE FROM wc_firms WHERE id = %s", (firm["id"],))
            cur.execute("DELETE FROM wc_runs WHERE id = %s", (run["id"],))
        c.commit()


# ---------------------------------------------------------------------------
# FIX 10 (real-mode Kairos Real Estate Partners run): _aum_label_for's
# proxy_basis-derived labels -- the exact scenario the gate hit ($254M
# qualified via an invested-capital proxy) plus the other proxy_basis
# conventions icp_scoring_v2_2.md names.
# ---------------------------------------------------------------------------
def test_aum_label_for_invested_capital_proxy_basis():
    """FIX 16 (real-mode Kairos run): "Est. AUM (Invested Capital)" is a
    contradiction -- invested capital is a cumulative figure, not AUM, even
    hedged as an estimate. Labeled "Capital Invested (since <founded_year>)"
    instead, naming what the figure actually is."""
    research = {"firm_oneliner": "x", "aum_display": "$254M", "aum_tag": "AUM_ESTIMATED",
               "proxy_type": "non_floor", "proxy_basis": "cumulative invested capital, proxy",
               "founded_year": "2013"}
    fill_data = deck_filler.build_fill_data("Invested Capital Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "$254M"
    assert fill_data["aum_label"] == "Capital Invested (since 2013)"


def test_aum_label_for_deployed_capital_wording_also_matches_invested_capital():
    research = {"firm_oneliner": "x", "aum_display": "$254M", "aum_tag": "AUM_ESTIMATED",
               "proxy_type": "non_floor", "proxy_basis": "capital deployed across acquisitions",
               "founded_year": "2013"}
    fill_data = deck_filler.build_fill_data("Deployed Capital Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_label"] == "Capital Invested (since 2013)"


def test_aum_label_for_invested_capital_without_founded_year_omits_since_clause():
    research = {"firm_oneliner": "x", "aum_display": "$254M", "aum_tag": "AUM_ESTIMATED",
               "proxy_type": "non_floor", "proxy_basis": "cumulative invested capital, proxy"}
    fill_data = deck_filler.build_fill_data("No Founded Year Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_label"] == "Capital Invested"


def test_aum_label_for_portfolio_value_proxy_basis():
    research = {"firm_oneliner": "x", "aum_display": "$900M", "aum_tag": "AUM_ESTIMATED",
               "proxy_type": "non_floor", "proxy_basis": "portfolio value, proxy"}
    fill_data = deck_filler.build_fill_data("Portfolio Value Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_label"] == "Est. AUM (Portfolio Value)"


def test_aum_label_for_fund_size_proxy_basis():
    research = {"firm_oneliner": "x", "aum_display": "$500M", "aum_tag": "AUM_ESTIMATED",
               "proxy_type": "non_floor", "proxy_basis": "fund size, proxy"}
    fill_data = deck_filler.build_fill_data("Fund Size Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_label"] == "Est. AUM (Fund Size)"


def test_aum_label_for_unit_count_proxy_basis():
    research = {"firm_oneliner": "x", "aum_display": "$300M", "aum_tag": "AUM_ESTIMATED",
               "proxy_type": "units", "proxy_basis": "unit count, proxy"}
    fill_data = deck_filler.build_fill_data("Unit Count Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_label"] == "Est. AUM (Unit Count)"


def test_aum_label_for_square_footage_proxy_basis():
    """FIX 21 (real-mode In-Rel Properties run): a square-footage-derived
    tertiary proxy gets its own label naming the derivation, distinct from
    "Est. AUM (Unit Count)"."""
    research = {"firm_oneliner": "x", "aum_display": "~$240M est.", "aum_tag": "AUM_ESTIMATED",
               "proxy_type": "units", "proxy_basis": "3,000,000 sq ft x ~$200/sq ft "
                                                     "conservative estimate, 60% leverage haircut"}
    fill_data = deck_filler.build_fill_data("Square Footage Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "~$240M est."
    assert fill_data["aum_label"] == "Est. AUM (Square Footage)"


def test_aum_label_for_confirmed_is_plain_aum_not_gross_asset_value():
    """A plain AUM_CONFIRMED (not a GAV proxy) is labeled just "AUM" --
    "Gross Asset Value" is reserved for a disclosed GAV proxy specifically,
    which is a real but grosser-than-equity figure, not the same thing as a
    confirmed net AUM number."""
    research = {"firm_oneliner": "x", "aum_display": "$1.1B", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data("Plain Confirmed Co", research, {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_label"] == "AUM"


# ---------------------------------------------------------------------------
# FIX 22 (real-mode In-Rel Properties run): disclosed_figure/disclosed_label
# are now the PRIMARY source for the stat tile -- exactly what the firm
# published, verbatim, no arithmetic, never the internal haircut/derived
# gating value. aum_display/proxy_basis-derived _aum_label_for() remain
# ONLY a fallback for un-migrated fixtures (exercised by every test above
# this section, none of which set disclosed_figure).
# ---------------------------------------------------------------------------
def test_disclosed_figure_shows_raw_gav_never_the_haircut_equity_value():
    """A $4.4B disclosed GAV firm gates at ~$1.76B internally (60% leverage
    haircut) and QUALIFIES -- but the deck tile must show "$4.4B", the raw
    disclosed figure, never the internal ~$1.76B gating number."""
    research = {"firm_oneliner": "x", "aum_tag": "AUM_ESTIMATED", "proxy_type": "gav",
               "disclosed_figure": "$4.4B", "disclosed_label": "gross asset value"}
    fill_data = deck_filler.build_fill_data("Synthetic GAV Co", research,
                                            {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "$4.4B"
    assert fill_data["aum_label"] == "Gross Asset Value"
    assert "1.76" not in fill_data["aum_value"]


def test_disclosed_figure_for_sqft_proxy_shows_raw_quantity_never_a_dollar_estimate():
    """In-Rel Properties: the tile must read the square footage, never a
    derived "~$240M est." dollar figure (FIX 21's display choice, which
    this fix reverses as the wrong one for a prospect-facing deck).

    FIX 24 (real-mode In-Rel Properties run): the RAW verbatim quantity
    ("3,000,000 sq ft") overflows the tile's box and wraps into the label
    below it -- the tile specifically (not disclosed_figure/the email)
    compacts to a short form ("3M sq ft"), and the verbose label compacts
    to a short canonical one ("Sq Ft Under Management") to match."""
    research = {"firm_oneliner": "x", "aum_tag": "AUM_ESTIMATED", "proxy_type": "units",
               "disclosed_figure": "3,000,000 sq ft",
               "disclosed_label": "of office and retail space under management"}
    fill_data = deck_filler.build_fill_data("In-Rel Properties", research,
                                            {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "3M sq ft"
    assert fill_data["aum_label"] == "Sq Ft Under Management"
    assert "$" not in fill_data["aum_value"]
    assert "est." not in fill_data["aum_value"]


def test_disclosed_figure_present_but_label_empty_falls_back_to_aum_label_for():
    """disclosed_figure can be present with disclosed_label empty (a source
    that discloses a number but not the model's own descriptive phrase) --
    the figure is still shown verbatim, and only the LABEL falls back to
    the keyword-sniffed _aum_label_for()."""
    research = {"firm_oneliner": "x", "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": "2013",
               "disclosed_figure": "$254M", "disclosed_label": ""}
    fill_data = deck_filler.build_fill_data("Kairos Real Estate Partners", research,
                                            {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "$254M"
    assert fill_data["aum_label"] == "Capital Invested (since 2013)"


def test_titlecase_label_keeps_connector_words_lowercase():
    assert deck_filler._titlecase_label("of office and retail space under management") == \
        "Of Office and Retail Space under Management"
    assert deck_filler._titlecase_label("capital invested") == "Capital Invested"
    assert deck_filler._titlecase_label("gross asset value") == "Gross Asset Value"


# ---------------------------------------------------------------------------
# FIX 11 (real-mode Kairos Real Estate Partners run): _assemble_fit_paragraphs
# pairs by key, drops unfilled keys (never pads, never shifts), and strips a
# model's redundant leading label.
# ---------------------------------------------------------------------------
def test_assemble_fit_paragraphs_pairs_by_key_regardless_of_dict_order():
    fit_content = {
        "capital_markets": "Capital markets paragraph.",
        "property_accounting": "Property accounting paragraph.",
    }
    out = deck_filler._assemble_fit_paragraphs(fit_content, must_scrub_money=False, corpus_text="")
    assert out[0] == "Property accounting paragraph."  # slot 1 = property_accounting
    assert out[4] == "Capital markets paragraph."       # slot 5 = capital_markets
    assert out[1] == out[2] == out[3] == ""             # unfilled keys are blank, never padded


def test_assemble_fit_paragraphs_missing_keys_leave_blank_not_fabricated():
    out = deck_filler._assemble_fit_paragraphs({"fund_structures": "Only this one."},
                                                must_scrub_money=False, corpus_text="")
    assert out == ["", "Only this one.", "", "", ""]
    assert all("extends directly to" not in (p or "") for p in out), (
        "a missing key must be blank, never padded with a fabricated "
        "'X Analyst's ... work extends directly to ...' sentence"
    )


def test_strip_leading_label_removes_models_own_service_name_prefix():
    assert (deck_filler._strip_leading_label("Fund Structures: With a capital stack across many deals...")
           == "With a capital stack across many deals...")
    assert (deck_filler._strip_leading_label("Capital Markets - debt placement work fits here.")
           == "debt placement work fits here.")
    assert (deck_filler._strip_leading_label("No label here, just the paragraph.")
           == "No label here, just the paragraph.")


def test_assemble_fit_paragraphs_strips_leading_label_per_key():
    fit_content = {"property_accounting": "Property Accounting: needs multi-entity books."}
    out = deck_filler._assemble_fit_paragraphs(fit_content, must_scrub_money=False, corpus_text="")
    assert out[0] == "needs multi-entity books."


def test_assemble_fit_paragraphs_blanks_money_leak_in_place_not_shifted():
    """A money-leaking key is blanked IN PLACE -- unlike the old positional
    list design, a later key's content never shifts up to fill the gap."""
    fit_content = {
        "property_accounting": "Manages an estimated $1.2 billion portfolio.",
        "fund_structures": "A clean paragraph with no dollar figure at all.",
    }
    out = deck_filler._assemble_fit_paragraphs(fit_content, must_scrub_money=True, corpus_text="")
    assert out[0] == ""  # property_accounting's slot, blanked -- not shifted away
    assert out[1] == "A clean paragraph with no dollar figure at all."  # fund_structures stays in ITS slot


def test_assemble_fit_paragraphs_blanks_ungrounded_claim_in_place():
    fit_content = {
        "investor_reporting": "Reports across 12 states.",  # not in corpus -> drifted/invented
        "capital_markets": "Debt placement work with no numbers at all.",
    }
    out = deck_filler._assemble_fit_paragraphs(fit_content, must_scrub_money=False,
                                                corpus_text='{"geography": "17 states"}')
    assert out[2] == ""  # investor_reporting's slot
    assert out[4] == "Debt placement work with no numbers at all."  # capital_markets' own slot


# ---------------------------------------------------------------------------
# FIX 12 (real-mode Kairos Real Estate Partners run): _short_name strips
# trailing legal/generic words iteratively from the END, never truncating to
# a fixed word count.
# ---------------------------------------------------------------------------
def test_short_name_strips_legal_suffix_and_generic_industry_words():
    """The actual QA finding: "Kairos Real Estate Partners, LLC" was
    shortening to "Kairos Real" under the old first-2-tokens design, since
    "real"/"estate" weren't dropped from anywhere and the cap grabbed them
    anyway. Iterative trailing-strip removes LLC/Partners/Estate/Real one at
    a time, correctly stopping at the actual brand name."""
    assert _short_name("Kairos Real Estate Partners, LLC") == "Kairos"


def test_short_name_multi_word_brand_with_no_generic_tail_keeps_both_words():
    """Not a word-count cap: a genuine two-word brand with nothing generic to
    strip from the end is left as both words, not cut down to one."""
    assert _short_name("Riverside Bay Partners") == "Riverside Bay"


def test_short_name_strips_only_from_the_end_stops_at_first_non_generic():
    assert _short_name("Blackstone Real Estate Advisors LLC") == "Blackstone"


def test_short_name_leaves_a_name_with_no_generic_words_at_all_unchanged():
    assert _short_name("Related Companies") == "Related Companies"


def test_short_name_single_token_name_is_never_stripped_to_empty():
    assert _short_name("LLC") == "LLC"


# ---------------------------------------------------------------------------
# FIX 15 (real-mode Kairos Real Estate Partners run): profile_bullets quality
# guards -- a bullet identical to firm_oneliner wastes a slot restating a
# fact the deck already shows in its own dedicated slot; a bullet that's
# pure press-release marketing spin isn't a fact useful for a pitch.
# ---------------------------------------------------------------------------
def test_drop_oneliner_duplicate_removes_exact_match():
    oneliner = "Commercial owner-operator and property manager focusing on office, medical, industrial"
    items = [oneliner, "Founded in 2013.", "Operates in the Mid-Atlantic."]
    out = deck_filler._drop_oneliner_duplicate(items, oneliner)
    assert oneliner not in out
    assert out == ["Founded in 2013.", "Operates in the Mid-Atlantic."]


def test_drop_oneliner_duplicate_ignores_trivial_formatting_differences():
    oneliner = "Commercial owner-operator and property manager."
    items = ["commercial   owner-operator and property manager", "A distinct fact."]
    out = deck_filler._drop_oneliner_duplicate(items, oneliner)
    assert out == ["A distinct fact."]


def test_drop_oneliner_duplicate_keeps_bullets_that_merely_overlap_not_match():
    oneliner = "Commercial owner-operator and property manager focusing on office, medical, industrial"
    items = ["Commercial owner-operator and property manager"]  # a prefix, not an exact match
    out = deck_filler._drop_oneliner_duplicate(items, oneliner)
    assert out == items


def test_drop_oneliner_duplicate_no_op_when_oneliner_blank():
    items = ["A fact.", "Another fact."]
    assert deck_filler._drop_oneliner_duplicate(items, "") == items


def test_drop_marketing_language_removes_press_release_spin():
    items = [
        "Announced a new approach to property management in January 2024, indicating expansion.",
        "Founded in 2013 with a value-add strategy across the Mid-Atlantic.",
    ]
    out = deck_filler._drop_marketing_language(items)
    assert out == ["Founded in 2013 with a value-add strategy across the Mid-Atlantic."]


def test_drop_marketing_language_catches_other_cliche_phrasings():
    assert deck_filler._drop_marketing_language(["Raises the bar with a bold new vision."]) == []
    assert deck_filler._drop_marketing_language(["A cutting-edge, state-of-the-art platform."]) == []
    assert deck_filler._drop_marketing_language(["Reimagining property management for tenants."]) == []


def test_drop_marketing_language_keeps_factual_bullets():
    items = ["Founded in 2013.", "LP/GP capital stack across 12 properties.", "Manages $254M invested capital."]
    assert deck_filler._drop_marketing_language(items) == items


def test_build_fill_data_drops_oneliner_duplicate_and_marketing_bullet_end_to_end():
    """The actual observed defect: slide 3 had 1 real bullet + firm_oneliner
    duplicated into bullet_1 + 4 blanks, and the one other filled bullet was
    pure press-release marketing copy."""
    oneliner = "Commercial owner-operator and property manager focusing on office, medical, industrial"
    research = {"firm_oneliner": oneliner, "aum_display": "$254M", "aum_tag": "AUM_ESTIMATED",
               "proxy_type": "non_floor", "proxy_basis": "invested capital, proxy"}
    deck_content = {
        "profile_bullets": [
            oneliner,  # duplicate of firm_oneliner -- must be dropped
            "Announced a new approach to property management in January 2024, indicating expansion.",  # marketing spin -- must be dropped
            "Founded in 2013, based in the Mid-Atlantic and Southeast Florida.",  # a real, distinct fact -- must survive
        ],
        "fit_paragraphs": {},
    }
    fill_data = deck_filler.build_fill_data("Kairos Real Estate Partners, LLC", research, deck_content)
    all_bullets = [fill_data[f"profile_bullet_{i}"] for i in range(1, 7)]
    assert oneliner not in all_bullets
    assert not any("new approach to" in (b or "").lower() for b in all_bullets)
    assert "Founded in 2013, based in the Mid-Atlantic and Southeast Florida." in all_bullets


# ---------------------------------------------------------------------------
# FIX 15 (real-mode Kairos run): diagnosed the SAME gpt-5-mini reasoning-
# budget-exhaustion failure FIX 7 fixed for icp_gate.py's extraction call --
# confirmed live (finish_reason='length', reasoning_tokens=2048 == the full
# budget, response_chars=0, twice, before escalating to DeepSeek). Applied
# reasoning_effort="minimal" to get_deck_content()'s complete_json_retry
# call; re-measured live and confirmed attempt 1 then succeeds
# (finish_reason='stop', reasoning_tokens=0, non-empty, no escalation).
# ---------------------------------------------------------------------------
def test_get_deck_content_real_mode_passes_reasoning_effort_minimal(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)
    captured = {}

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete_json_retry(prompt, **kwargs):
        captured.update(kwargs)
        return ({"profile_bullets": ["A fact."], "fit_paragraphs": {}}, _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)

    deck_filler.get_deck_content(KINGBIRD, {"firm_oneliner": "x"}, {})
    assert captured.get("reasoning_effort") == "minimal"


# ---------------------------------------------------------------------------
# FIX 24 (real-mode In-Rel Properties run): the deck stat tile overflowed
# with a non-currency disclosed figure -- "3,000,000 sq ft" (16 chars, plus
# the dropped "over" qualifier making it 21) wraps to a second line in a
# box the master sized for a short currency string ("$4.4B"), and that
# second line collides with the label box directly below (only ~0.02in of
# gap between them in the master). Four fixes tested below:
#   1. quantity_qualifier preserves an approximator ("over"/"~"/...) in
#      disclosed_figure (tested in test_icp_gate.py, not here).
#   2. _compact_quantity_for_tile() compacts the TILE value only.
#   3. _compact_label_for_tile() compacts the TILE label only.
#   4. check_stat_tile_fits() / fill_deck() catch an overflow that
#      somehow still reaches fill time, as a safety net.
# ---------------------------------------------------------------------------

# --- 2. Tile value compaction -----------------------------------------------

def test_short_number_formats_millions_thousands_and_plain():
    assert deck_filler._short_number(3_000_000) == "3M"
    assert deck_filler._short_number(1_800_000) == "1.8M"
    assert deck_filler._short_number(21_794) == "21.8K"
    assert deck_filler._short_number(800) == "800"


# FIX 24 (adversarial re-review): the bucket (M vs K vs plain) must be
# picked from the ROUNDED display value, not the raw one -- a number in
# [999_950, 999_999] rounds to "1000.0" within the K-bucket at one decimal
# place and must promote to the M-bucket ("1M"), never ship as the longer,
# nonsensical "1000.0K" (which, combined with a qualifier suffix, was long
# enough to overflow the tile and raise a real DeckFillError). Same defect
# recurs one order of magnitude down ([99_950, 99_999] -> spurious "100.0K"
# instead of a clean "100K") without the K/M bucket confusion, purely a
# cosmetic rounding-decimal artifact.
@pytest.mark.parametrize("number,expected", [
    (999_950, "1M"),
    (999_999, "1M"),
    (999_500, "999.5K"),
    (999_949, "999.9K"),
    (99_999, "100K"),
    (99_950, "100K"),
    (100_000, "100K"),
    (999_000, "999K"),
    (1_000_000, "1M"),
])
def test_short_number_rounds_across_bucket_boundaries_correctly(number, expected):
    assert deck_filler._short_number(number) == expected


@pytest.mark.parametrize("value,expected", [
    ("$4.4B", "$4.4B"),                          # already short -- unchanged
    ("$254 M", "$254 M"),                         # already short -- unchanged
    ("$1.1T", "$1.1T"),                           # already short -- unchanged
    ("3,000,000 sq ft", "3M sq ft"),              # long, no qualifier
    ("over 3,000,000 sq ft", "3M+ sq ft"),        # "over" -> "+" suffix
    ("more than 3,000,000 sq ft", "3M+ sq ft"),
    ("at least 3,000,000 sq ft", "3M+ sq ft"),
    ("approximately 1,800,000 sq ft", "~1.8M sq ft"),
    ("about 1,800,000 sq ft", "~1.8M sq ft"),
    ("21,794 units", "21,794 units"),             # 12 chars -- already fits, unchanged
    ("1,821,794 units", "1.8M units"),             # long -- compacted
    ("over 21,794 units", "21.8K+ units"),
    ("north of 3,000,000 sq ft", "3M+ sq ft"),     # FIX 24 adversarial re-review: untested qualifiers
    ("roughly 1,800,000 sq ft", "~1.8M sq ft"),
    ("~1,800,000 sq ft", "~1.8M sq ft"),
    # FIX 24 adversarial re-review: rounding-across-a-bucket-boundary cases
    ("over 999,950 sq ft", "1M+ sq ft"),           # was "1000.0K+ sq ft" (14 chars, overflowed the tile)
    ("999,999 sq ft", "999,999 sq ft"),            # 13 chars -- already fits, unchanged
    ("north of 999,999 sq ft", "1M+ sq ft"),       # long -- was "1000.0M+ sq ft"-shaped overflow risk
])
def test_compact_quantity_for_tile(value, expected):
    assert deck_filler._compact_quantity_for_tile(value) == expected


def test_compact_quantity_for_tile_unrecognized_long_shape_passes_through():
    """A long value that doesn't match the "[qualifier] <number> sq ft|
    units" shape is left as-is rather than guessed at -- this function only
    ever touches a shape it's confident about."""
    weird = "Approximately $4.4 Billion In Gross Assets"
    assert deck_filler._compact_quantity_for_tile(weird) == weird


# --- 3. Tile label compaction ------------------------------------------------

@pytest.mark.parametrize("label,tile_value,expected", [
    ("Gross Asset Value", "$4.4B", "Gross Asset Value"),
    ("Capital Invested", "$254M", "Capital Invested"),
    ("Capital Invested (since 2013)", "$254M", "Capital Invested (since 2013)"),
    ("Of Office and Retail Space under Management", "3M+ sq ft", "Sq Ft Under Management"),
    ("Units under Management", "21.8K units", "Units under Management"),  # already fits, unchanged
    ("Units under Management for the Full Residential Portfolio", "1.8M units", "Units Under Management"),
])
def test_compact_label_for_tile(label, tile_value, expected):
    assert deck_filler._compact_label_for_tile(label, tile_value) == expected


def test_compact_label_for_tile_unexpected_long_label_falls_back_to_cap_chars():
    """A long label that isn't the sq-ft/units case this bug targets still
    gets SOME cap applied (via the pre-existing _cap_chars), rather than
    shipping uncapped just because this fix didn't anticipate the shape."""
    long_label = "A Very Long And Unexpected Descriptive Label That Nobody Anticipated"
    result = deck_filler._compact_label_for_tile(long_label, "$4.4B")
    assert result == deck_filler._cap_chars(long_label, deck_filler._AUM_LABEL_TILE_MAX_CHARS)
    assert len(result) <= deck_filler._AUM_LABEL_TILE_MAX_CHARS + 1  # +1 for the ellipsis char


def test_build_fill_data_compaction_leaves_short_currency_values_unchanged():
    """VERIFY (Kairos): a short, already-fitting currency value/label must
    be completely unaffected by this fix."""
    research = {"firm_oneliner": "x", "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "disclosed_figure": "$254 M", "disclosed_label": "Capital Invested"}
    fill_data = deck_filler.build_fill_data("Kairos Real Estate Partners", research,
                                            {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "$254 M"
    assert fill_data["aum_label"] == "Capital Invested"


def test_build_fill_data_compacts_gav_figure_only_if_actually_long():
    """A disclosed GAV figure is currency-shaped and normally short ("$4.4B")
    -- confirms the compaction path doesn't accidentally mangle a real GAV
    value just because it's routed through the same function."""
    research = {"firm_oneliner": "x", "aum_tag": "AUM_ESTIMATED", "proxy_type": "gav",
               "disclosed_figure": "$4.4B", "disclosed_label": "gross asset value"}
    fill_data = deck_filler.build_fill_data("Synthetic GAV Co", research,
                                            {"profile_bullets": [], "fit_paragraphs": {}})
    assert fill_data["aum_value"] == "$4.4B"
    assert fill_data["aum_label"] == "Gross Asset Value"


# --- 4. Render/overflow check ------------------------------------------------

def test_shape_box_and_font_before_reads_real_master_geometry():
    """Locks in the actual master geometry this whole fix's math is derived
    from -- if the master is ever re-exported with different box/font
    sizes, this test (not just a silent behavior change) is what notices."""
    import zipfile as _zipfile
    with _zipfile.ZipFile(config.PITCH_MASTER_PPTX) as z:
        xml = z.read("ppt/slides/slide3.xml").decode("utf-8")
    value_geo = deck_filler._shape_box_and_font_before(xml, "<a:t>$4.4B</a:t>")
    label_geo = deck_filler._shape_box_and_font_before(xml, "<a:t>Gross Asset Value</a:t>")
    assert value_geo == (2002536, 384048, 2200)
    assert label_geo == (2002536, 365760, 950)


def test_shape_box_and_font_before_returns_none_for_missing_locator():
    assert deck_filler._shape_box_and_font_before("<a:t>irrelevant</a:t>", "<a:t>not here</a:t>") is None


def test_estimated_text_width_emu_known_good_values_fit_their_real_boxes():
    """The master's own pre-fix locator values, and this fix's compacted
    forms, must all be estimated to fit -- the exact "does it fit" check
    check_stat_tile_fits() relies on."""
    value_box_cx, _, value_sz = 2002536, 384048, 2200
    label_box_cx, _, label_sz = 2002536, 365760, 950
    for value in ("$4.4B", "$254 M", "$1.1T", "3M+ sq ft", "~1.8M sq ft", "21.8K units"):
        assert deck_filler._estimated_text_width_emu(value, value_sz) <= value_box_cx, value
    for label in ("Gross Asset Value", "Capital Invested", "Capital Invested (since 2013)",
                 "Sq Ft Under Management", "Units Under Management"):
        assert deck_filler._estimated_text_width_emu(label, label_sz) <= label_box_cx, label


def test_estimated_text_width_emu_known_bad_values_exceed_their_real_boxes():
    """The EXACT reported bug: the raw, uncompacted forms must be estimated
    to overflow -- confirming this check would have actually caught it."""
    value_box_cx, value_sz = 2002536, 2200
    label_box_cx, label_sz = 2002536, 950
    assert deck_filler._estimated_text_width_emu("3,000,000 sq ft", value_sz) > value_box_cx
    assert deck_filler._estimated_text_width_emu("over 3,000,000 sq ft", value_sz) > value_box_cx
    assert (deck_filler._estimated_text_width_emu("Of Office and Retail Space under Management", label_sz)
           > label_box_cx)


def test_check_stat_tile_fits_no_problems_for_a_normal_compacted_fill(monkeypatch):
    fill_data = deck_filler.build_fill_data(
        "In-Rel Properties",
        {"firm_oneliner": "x", "aum_tag": "AUM_ESTIMATED", "proxy_type": "units",
         "disclosed_figure": "over 3,000,000 sq ft",
         "disclosed_label": "of office and retail space under management"},
        {"profile_bullets": [], "fit_paragraphs": {}},
    )
    slot_map = deck_filler.load_slot_map()
    with zipfile.ZipFile(config.PITCH_MASTER_PPTX) as z:
        slide_xml = {slide_no: z.read(f"ppt/slides/slide{slide_no}.xml").decode("utf-8")
                    for slide_no in slot_map["slides"]}
    problems = deck_filler.check_stat_tile_fits("In-Rel Properties", fill_data, slide_xml, slot_map)
    assert problems == []


def test_check_stat_tile_fits_flags_an_uncompacted_overflow(monkeypatch):
    """Safety-net proof: if compaction were somehow bypassed (a future bug),
    this check independently catches the exact overflow the report found."""
    slot_map = deck_filler.load_slot_map()
    with zipfile.ZipFile(config.PITCH_MASTER_PPTX) as z:
        slide_xml = {slide_no: z.read(f"ppt/slides/slide{slide_no}.xml").decode("utf-8")
                    for slide_no in slot_map["slides"]}
    broken_fill_data = {"aum_value": "3,000,000 sq ft", "aum_label": "Gross Asset Value"}
    problems = deck_filler.check_stat_tile_fits("Broken Co", broken_fill_data, slide_xml, slot_map)
    assert problems
    assert any("aum_value" in p for p in problems), problems


def test_fill_deck_raises_when_a_stat_tile_would_overflow(tmp_path, monkeypatch):
    """End-to-end: fill_deck() itself refuses to ship a deck whose tile
    value doesn't fit -- not just a standalone check a caller could forget
    to run."""
    fill_data = deck_filler.build_fill_data(KINGBIRD, _kingbird_research(), _kingbird_deck_content())
    # Simulate a bug that bypassed compaction (build_fill_data itself always
    # compacts -- this directly exercises fill_deck()'s own safety net).
    fill_data["aum_value"] = "3,000,000 sq ft"
    with pytest.raises(deck_filler.DeckFillError, match="stat tile overflow"):
        deck_filler.fill_deck(KINGBIRD, fill_data, tmp_path / "out.pptx")


def test_fill_deck_succeeds_for_in_rel_properties_compacted_tile(tmp_path):
    """End-to-end: a REAL In-Rel-shaped fill (verbatim disclosed_figure with
    the "over" qualifier, compacted by build_fill_data itself) fills and
    renders without raising, and the compacted text actually lands in the
    output XML."""
    research = {"firm_oneliner": "A commercial real estate owner-operator.",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "units",
               "disclosed_figure": "over 3,000,000 sq ft",
               "disclosed_label": "of office and retail space under management",
               "founded_year": "1998"}
    fill_data = deck_filler.build_fill_data("In-Rel Properties", research,
                                            {"profile_bullets": [], "fit_paragraphs": {}})
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck("In-Rel Properties", fill_data, out)
    with zipfile.ZipFile(out) as z:
        xml = z.read("ppt/slides/slide3.xml").decode("utf-8")
    assert "<a:t>3M+ sq ft</a:t>" in xml
    assert "<a:t>Sq Ft Under Management</a:t>" in xml
    assert "3,000,000" not in xml
    assert "Office and Retail Space" not in xml


# ---------------------------------------------------------------------------
# FIX 36 -- Part A: whitelist the firm's own disclosed figure in the money
# scrub. Evidence: a live Kairos repro (research disclosed_figure="$254 M")
# had a fit paragraph citing "$254 M" verbatim dropped whole by the same
# guard that (correctly) drops an ESTIMATED, undisclosed figure.
# ---------------------------------------------------------------------------
_KAIROS_DISCLOSED_RESEARCH = {
    "firm_oneliner": "Commercial asset owner/operator and property manager.",
    "aum_tag": "AUM_ESTIMATED",
    "proxy_type": "non_floor",
    "disclosed_figure": "$254 M",
    "disclosed_label": "Capital Invested",
}


def test_money_scrub_whitelists_the_firms_own_disclosed_figure():
    deck_content = {
        "profile_bullets": ['Discloses "$254 M" in Capital Invested since founding.'],
        "fit_paragraphs": {
            "investor_reporting": 'With "$254 M" disclosed as Capital Invested, '
                                  "standardized reporting helps track owner distributions.",
        },
    }
    fill_data = deck_filler.build_fill_data("Kairos Real Estate Partners", _KAIROS_DISCLOSED_RESEARCH,
                                            deck_content)
    assert "254" in fill_data["profile_bullet_1"]
    assert "254" in fill_data["fit_paragraph_3"]  # investor_reporting is the 3rd fixed slot


def test_money_scrub_still_drops_a_different_dollar_figure_even_with_a_disclosed_figure_present():
    """The whitelist is ONE figure, not a weakening -- any OTHER money
    mention still drops exactly as before FIX 36, even for a firm that does
    have a genuinely disclosed figure elsewhere."""
    deck_content = {
        "profile_bullets": ["Manages an estimated $1.2 billion portfolio overall."],
        "fit_paragraphs": {
            "fund_structures": "Structures deals with roughly $600 million of leverage.",
        },
    }
    fill_data = deck_filler.build_fill_data("Kairos Real Estate Partners", _KAIROS_DISCLOSED_RESEARCH,
                                            deck_content)
    assert fill_data["profile_bullet_1"] == ""
    assert fill_data["fit_paragraph_2"] == ""


def test_money_scrub_whitelist_requires_the_item_to_have_a_recognized_claim():
    """A _BIG_MONEY_RE match fact_check can't classify at all (no adjacent
    unit word, no $ sign) must NOT be waved through just because an empty
    claim set is trivially a subset of anything -- conservative default:
    still drops."""
    deck_content = {
        "profile_bullets": ["Reports a bare 254M figure with no context at all."],
        "fit_paragraphs": {},
    }
    fill_data = deck_filler.build_fill_data("Kairos Real Estate Partners", _KAIROS_DISCLOSED_RESEARCH,
                                            deck_content)
    assert fill_data["profile_bullet_1"] == ""


# ---------------------------------------------------------------------------
# FIX 36 -- Part B: every drop/empty outcome logs its own line.
# ---------------------------------------------------------------------------
def test_deck_drop_and_empty_outcomes_are_logged_with_reason(capsys):
    research = {
        "firm_oneliner": "Same oneliner text.",
        "aum_tag": "AUM_ESTIMATED",
        "proxy_type": "non_floor",
        "disclosed_figure": "",
        "disclosed_label": "",
    }
    deck_content = {
        "profile_bullets": [
            "Manages an estimated $9.9 million fund overall.",
            "Operates in 42 states with no grounding at all.",
            "Same oneliner text.",
            "This raises the bar with a cutting-edge approach.",
        ],
        "fit_paragraphs": {"property_accounting": ""},
    }
    deck_filler.build_fill_data("Drop Logging Co", research, deck_content)
    out = capsys.readouterr().out
    assert "[DECK DROP] Drop Logging Co: profile_bullet reason=money_scrub -- dropped text:" in out
    assert "[DECK DROP] Drop Logging Co: profile_bullet reason=ungrounded:42 -- dropped text:" in out
    assert "[DECK DROP] Drop Logging Co: profile_bullet reason=oneliner_duplicate -- dropped text:" in out
    assert "[DECK DROP] Drop Logging Co: profile_bullet reason=marketing_language -- dropped text:" in out
    assert "[DECK EMPTY] Drop Logging Co: property_accounting returned empty by model." in out


def test_deck_drop_logs_leading_label_empty_when_stripping_the_label_leaves_nothing():
    research = {"firm_oneliner": "x"}
    deck_content = {"profile_bullets": [], "fit_paragraphs": {"fund_structures": "Fund Structures:"}}
    # capture prints without pytest's capsys fixture arg (keeps this test focused)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fill_data = deck_filler.build_fill_data("Label Only Co", research, deck_content)
    assert fill_data["fit_paragraph_2"] == ""
    assert "reason=leading_label_empty" in buf.getvalue()


# ---------------------------------------------------------------------------
# FIX 36 -- Part C: one bounded retry for blank fit keys.
# ---------------------------------------------------------------------------
def test_deck_retry_fires_once_and_fills_blank_fit_keys(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)
    calls = []

    class _Usage:
        total_tokens = 100

    def _fake_complete_json_retry(prompt, **kwargs):
        calls.append(prompt)
        return ({
            "fund_structures": "Uses a straightforward LP/GP structure across its markets.",
            "investor_reporting": "Provides regular owner updates across its regional portfolio.",
            "underwriting_acquisitions": "Applies consistent underwriting across its target markets.",
            "capital_markets": "Coordinates financing across its capital stack partners.",
        }, _Usage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)

    deck_content = {
        "profile_bullets": [],
        "fit_paragraphs": {"property_accounting": "Runs property-level books in-house for its portfolio."},
        "tokens_used": 500,
    }
    fill_data = deck_filler.build_fill_data("Retry Fill Co", _KAIROS_DISCLOSED_RESEARCH, deck_content)
    assert len(calls) == 1
    assert fill_data["fit_paragraph_1"] == "Runs property-level books in-house for its portfolio."
    assert fill_data["fit_paragraph_2"] != ""
    assert fill_data["fit_paragraph_3"] != ""
    assert fill_data["fit_paragraph_4"] != ""
    assert fill_data["fit_paragraph_5"] != ""
    out = capsys.readouterr().out
    assert "[DECK RETRY] Retry Fill Co: fund_structures filled on retry." in out


def test_deck_retry_never_fires_when_all_five_fit_keys_are_already_filled(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)
    calls = []

    def _fake_complete_json_retry(prompt, **kwargs):
        calls.append(prompt)
        return ({}, None)

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)

    deck_content = {
        "profile_bullets": [],
        "fit_paragraphs": {
            "property_accounting": "Runs property-level books for its portfolio.",
            "fund_structures": "Uses an LP/GP structure across its markets.",
            "investor_reporting": "Sends regular owner updates for its assets.",
            "underwriting_acquisitions": "Applies consistent underwriting for acquisitions.",
            "capital_markets": "Coordinates financing across its capital stack partners.",
        },
        "tokens_used": 500,
    }
    fill_data = deck_filler.build_fill_data("All Filled Co", {"firm_oneliner": "x", "aum_tag": "AUM_CONFIRMED"},
                                            deck_content)
    assert calls == []
    assert all(fill_data[f"fit_paragraph_{i}"] for i in range(1, 6))


def test_deck_retry_skips_when_token_budget_already_exhausted(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)
    calls = []

    def _fake_complete_json_retry(prompt, **kwargs):
        calls.append(prompt)
        return ({}, None)

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)

    deck_content = {
        "profile_bullets": [],
        "fit_paragraphs": {},
        "tokens_used": config.MAX_LLM_TOKENS_PER_FIRM,
    }
    research = {"firm_oneliner": "x", "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor"}
    fill_data = deck_filler.build_fill_data("Budget Exhausted Co", research, deck_content)
    assert calls == []
    assert all(fill_data[f"fit_paragraph_{i}"] == "" for i in range(1, 6))
    out = capsys.readouterr().out
    assert "[DECK RETRY] Budget Exhausted Co: property_accounting skipped -- token budget" in out


def test_deck_retry_output_still_goes_through_the_full_drop_pipeline(monkeypatch, capsys):
    """Part C's own instruction: retry output is re-run through the SAME
    drop checks (including Part A's whitelist) -- a retry that still cites
    an unrelated money figure drops again rather than shipping unchecked."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _Usage:
        total_tokens = 100

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"fund_structures": "Manages an estimated $1.2 billion structure overall."}, _Usage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)

    deck_content = {"profile_bullets": [], "fit_paragraphs": {}, "tokens_used": 500}
    fill_data = deck_filler.build_fill_data("Retry Still Drops Co", _KAIROS_DISCLOSED_RESEARCH, deck_content)
    assert fill_data["fit_paragraph_2"] == ""
    out = capsys.readouterr().out
    assert "[DECK DROP] Retry Still Drops Co: fund_structures reason=money_scrub" in out
    assert "[DECK RETRY] Retry Still Drops Co: fund_structures still blank after retry (dropped again)." in out


def test_deck_retry_skipped_entirely_in_test_mode(monkeypatch):
    """build_fill_data() must never attempt a real network call under
    TEST_MODE -- every one of this file's other blank-fit-key tests relies
    on this (they run with the session's default TEST_MODE=true and never
    monkeypatch models.complete_json_retry)."""
    assert config.TEST_MODE is True
    calls = []

    def _fake_complete_json_retry(prompt, **kwargs):
        calls.append(prompt)
        return ({}, None)

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)

    deck_filler.build_fill_data("Test Mode Co", {"firm_oneliner": "x", "aum_tag": "AUM_ESTIMATED",
                                                 "proxy_type": "non_floor"},
                               {"profile_bullets": [], "fit_paragraphs": {}})
    assert calls == []


# ---------------------------------------------------------------------------
# FIX 36 -- byte-identical confirmation companion: get_deck_content() now
# also returns tokens_used (Part C's own token-guard input).
# ---------------------------------------------------------------------------
def test_get_deck_content_real_mode_returns_tokens_used(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _Usage:
        total_tokens = 4321

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"profile_bullets": [], "fit_paragraphs": {}}, _Usage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)

    content = deck_filler.get_deck_content(KINGBIRD, {"firm_oneliner": "x"}, {})
    assert content["tokens_used"] == 4321


# ---------------------------------------------------------------------------
# FIX 37 Part A: _drop_marketing_language's regex, previously wired to
# profile bullets only, now also runs per fit-paragraph key inside the
# shared per-key resolver -- live Kairos ("a new approach to") and In-Rel
# ("best in class") fit paragraphs shipped exactly this spin.
# ---------------------------------------------------------------------------
def test_fit_paragraph_drops_marketing_language_same_regex_as_bullets(capsys):
    research = {"firm_oneliner": "x"}
    deck_content = {
        "profile_bullets": [],
        "fit_paragraphs": {
            "fund_structures": "Kairos publicly emphasizes a new approach to property management.",
        },
    }
    fill_data = deck_filler.build_fill_data("Marketing Spin Co", research, deck_content)
    assert fill_data["fit_paragraph_2"] == ""
    out = capsys.readouterr().out
    assert "[DECK DROP] Marketing Spin Co: fund_structures reason=marketing_language" in out


def test_fit_paragraph_marketing_language_drop_still_fills_via_retry(monkeypatch):
    """A blank from the NEW marketing_language reason feeds the EXISTING
    Part C retry exactly like any other blank reason -- no separate wiring
    needed, per the fix's own instruction."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _Usage:
        total_tokens = 100

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"fund_structures": "Uses an LP/GP structure across its target markets."}, _Usage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)

    deck_content = {
        "profile_bullets": [],
        "fit_paragraphs": {"fund_structures": "This is best in class asset management."},
        "tokens_used": 500,
    }
    fill_data = deck_filler.build_fill_data("Retry Spin Co", {"firm_oneliner": "x"}, deck_content)
    assert fill_data["fit_paragraph_2"] == "Uses an LP/GP structure across its target markets."


def test_fit_paragraph_marketing_language_check_does_not_flag_clean_prose():
    research = {"firm_oneliner": "x"}
    deck_content = {
        "profile_bullets": [],
        "fit_paragraphs": {
            "capital_markets": "Coordinates financing across its equity and debt partners.",
        },
    }
    fill_data = deck_filler.build_fill_data("Clean Prose Co", research, deck_content)
    assert fill_data["fit_paragraph_5"] == "Coordinates financing across its equity and debt partners."


# ---------------------------------------------------------------------------
# FIX 37 Part B: prompt-level note only -- no new mechanical check. Confirms
# the note text landed in both the first-pass and retry prompts and nowhere
# else changed the rule lists' meaning.
# ---------------------------------------------------------------------------
def test_content_prompt_tells_the_model_to_avoid_quoting_figures():
    assert "never inside quotation marks" in deck_filler._CONTENT_PROMPT
    assert "$254M in capital invested" in deck_filler._CONTENT_PROMPT


def test_fit_retry_prompt_also_tells_the_model_to_avoid_quoting_figures():
    assert "never inside quotation marks" in deck_filler._FIT_RETRY_PROMPT
