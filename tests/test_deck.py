"""QA Part 5 — deck fill & QA against §9's slot map and HARD RULE 14. Real
master PPTX is never mutated; corruption tests copy it to tmp_path first.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.reset_testmode import reset as reset_testmode
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
                    "fit_paragraphs": ["Para one.", "Para two.", "Para three.", "Para four.", "Para five."]}
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
                    "fit_paragraphs": ["Para A.", "Para B.", "Para C.", "Para D.", "Para E."]}
    fill_data = deck_filler.build_fill_data(firm, research, deck_content)
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck(firm, fill_data, out)

    slide3 = _slide_texts(out, 3)
    assert "A vertically integrated industrial REIT." in slide3
    assert "$750M" in slide3
    assert "Gross Asset Value" in slide3  # fixed label, still present
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
    deck_content = {"profile_bullets": ["b1"], "fit_paragraphs": ["p1", "p2", "p3", "p4", "p5"]}
    fill_data = deck_filler.build_fill_data(firm, research, deck_content)
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck(firm, fill_data, out)

    for slide_no in list(range(4, 12)) + [13]:
        with zipfile.ZipFile(config.PITCH_MASTER_PPTX) as zmaster, zipfile.ZipFile(out) as zout:
            master_xml = zmaster.read(f"ppt/slides/slide{slide_no}.xml")
            out_xml = zout.read(f"ppt/slides/slide{slide_no}.xml")
        assert master_xml == out_xml, f"slide{slide_no}.xml should be byte-identical to the master (fixed slide)"


# ---------------------------------------------------------------------------
# HARD RULE 14: AUM_ESTIMATED never shows as a hard stat
# ---------------------------------------------------------------------------
def test_aum_estimated_blanks_the_stat_card():
    research = {"firm_oneliner": "x", "aum_display": "$1.2B", "aum_tag": "AUM_ESTIMATED",
               "aum_asof": "as of 2026", "founded_year": "2010"}
    fill_data = deck_filler.build_fill_data("Estimated AUM Co", research, {"profile_bullets": [], "fit_paragraphs": []})
    assert fill_data["aum_value"] == ""
    assert fill_data["aum_label"] == ""
    assert fill_data["aum_asof"] == ""
    # founding year (a count/year, not a money figure) still passes through —
    # the guard is specifically about money figures, not all stats.
    assert fill_data["founded_year"] == "2010"


def test_aum_estimated_stat_card_blank_end_to_end_in_xml(tmp_path):
    research = {"firm_oneliner": "An estimated-AUM test firm.", "aum_display": "$1.2B",
               "aum_tag": "AUM_ESTIMATED", "aum_asof": "as of 2026", "founded_year": "2012",
               "capital_stack": "LP, GP"}
    deck_content = {"profile_bullets": ["A fact."], "fit_paragraphs": ["p1", "p2", "p3", "p4", "p5"]}
    fill_data = deck_filler.build_fill_data("Estimated Big Money LLC", research, deck_content)
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck("Estimated Big Money LLC", fill_data, out)
    slide3 = _slide_texts(out, 3)
    assert "$1.2B" not in slide3, "an ESTIMATED AUM figure must never render as a hard stat (HARD RULE 14)"
    assert "2012" in slide3  # founded year still renders — it's a count/year, not money


def test_aum_estimated_strips_money_mentions_from_bullets_and_fit_paragraphs():
    """QA finding (adversarial pass, §11.9/§11.2): the dedicated aum_value
    slot was correctly blanked for AUM_ESTIMATED, but the SAME dollar figure
    leaked straight through free-form profile_bullets/fit_paragraphs
    (exactly the 'money forms reaching deck stats under non-AUM labels'
    pattern §11.2 already names). A bullet/paragraph containing a money
    figure must be dropped whole (never partially edited) when AUM is
    estimated; clean entries must still survive and shift up to fill slots."""
    research = {"firm_oneliner": "x", "aum_display": "$1.2B", "aum_tag": "AUM_ESTIMATED"}
    deck_content = {
        "profile_bullets": [
            "Manages an estimated $1.2 billion portfolio across residential assets.",
            "Founded in 2015.",
            "Operates in 5 states.",
        ],
        "fit_paragraphs": [
            "With $1.2B in assets under management, this firm needs robust reporting.",
            "A clean paragraph with no dollar figure at all.",
            "p3", "p4", "p5",
        ],
    }
    fill_data = deck_filler.build_fill_data("Smuggled Money Co", research, deck_content)
    all_bullets = " ".join(fill_data[f"profile_bullet_{i}"] for i in range(1, 7))
    all_fit = " ".join(fill_data[f"fit_paragraph_{i}"] for i in range(1, 6))
    assert "1.2" not in all_bullets, f"a money figure leaked through a bullet: {all_bullets!r}"
    assert "1.2" not in all_fit, f"a money figure leaked through a fit paragraph: {all_fit!r}"
    assert fill_data["profile_bullet_1"] == "Founded in 2015."  # clean bullet shifted up
    assert fill_data["fit_paragraph_1"] == "A clean paragraph with no dollar figure at all."


def test_aum_confirmed_does_not_strip_money_mentions_from_bullets():
    """The strip is specific to ESTIMATED (unverified) figures — a CONFIRMED
    AUM is a verified fact and a bullet is free to state it."""
    research = {"firm_oneliner": "x", "aum_display": "$500M", "aum_tag": "AUM_CONFIRMED"}
    deck_content = {"profile_bullets": ["Manages a confirmed $500M portfolio."], "fit_paragraphs": []}
    fill_data = deck_filler.build_fill_data("Confirmed Money Co", research, deck_content)
    assert fill_data["profile_bullet_1"] == "Manages a confirmed $500M portfolio."


def test_aum_unverified_also_blanks_stat_card():
    """A qualified-on-manual-override firm could in principle still carry an
    UNVERIFIED tag from before the operator's /qualify — the guard must not
    depend on the firm having reached 'qualified' status, only on the tag."""
    research = {"firm_oneliner": "x", "aum_display": "", "aum_tag": "AUM_UNVERIFIED"}
    fill_data = deck_filler.build_fill_data("Unverified Co", research, {"profile_bullets": [], "fit_paragraphs": []})
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
                                            {"profile_bullets": [], "fit_paragraphs": []})
    assert fill_data["aum_value"] == "$4.4B"
    assert fill_data["aum_label"] == "Gross Asset Value"
    assert fill_data["aum_asof"] == "as of Dec 31, 2025"


def test_gav_proxy_estimated_does_not_strip_money_mentions_from_bullets():
    research = {"firm_oneliner": "x", "aum_display": "$4.4B", "aum_tag": "AUM_ESTIMATED", "proxy_type": "gav"}
    deck_content = {"profile_bullets": ["Manages $4.4 billion in gross asset value."], "fit_paragraphs": []}
    fill_data = deck_filler.build_fill_data("GAV Bullet Co", research, deck_content)
    assert fill_data["profile_bullet_1"] == "Manages $4.4 billion in gross asset value."


def test_gav_proxy_estimated_stat_card_renders_end_to_end_in_xml(tmp_path):
    """Uses a GAV figure deliberately different from the master's own literal
    "$4.4B" locator text, so the assertion proves the value came from
    research, not a no-op match against the unfilled master."""
    research = {"firm_oneliner": "A GAV-disclosing test firm.", "aum_display": "$3.4B",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "gav", "aum_asof": "as of Dec 31, 2025",
               "founded_year": "2012", "capital_stack": "LP, GP, Preferred Equity"}
    deck_content = {"profile_bullets": ["A fact."], "fit_paragraphs": ["p1", "p2", "p3", "p4", "p5"]}
    fill_data = deck_filler.build_fill_data("GAV Render Co", research, deck_content)
    out = tmp_path / "out.pptx"
    deck_filler.fill_deck("GAV Render Co", fill_data, out)
    slide3 = _slide_texts(out, 3)
    assert "$3.4B" in slide3
    assert "Gross Asset Value" in slide3


def test_non_gav_estimated_proxy_still_blanks_the_stat_card():
    """A floor/non_floor/units proxy has no directly-disclosed figure behind
    it -- only proxy_type == 'gav' is exempt from the blanking guard."""
    research = {"firm_oneliner": "x", "aum_display": "$1.7B", "aum_tag": "AUM_ESTIMATED", "proxy_type": "floor"}
    fill_data = deck_filler.build_fill_data("Floor Proxy Co", research, {"profile_bullets": [], "fit_paragraphs": []})
    assert fill_data["aum_value"] == ""
    assert fill_data["aum_label"] == ""


# ---------------------------------------------------------------------------
# Fix C (real-mode Kingbird run): capital_stack rendered as the degenerate,
# self-referential "across the capital stack" instead of the real disclosed
# tranche list. No real capital-stack value would ever contain the phrase
# "capital stack" describing itself.
# ---------------------------------------------------------------------------
def test_degenerate_capital_stack_value_is_blanked():
    research = {"firm_oneliner": "x", "aum_display": "$1.1B", "aum_tag": "AUM_CONFIRMED",
               "capital_stack": "across the capital stack"}
    fill_data = deck_filler.build_fill_data("Degenerate Stack Co", research, {"profile_bullets": [], "fit_paragraphs": []})
    assert fill_data["capital_stack"] == ""


def test_real_capital_stack_value_passes_through():
    research = {"firm_oneliner": "x", "aum_display": "$1.1B", "aum_tag": "AUM_CONFIRMED",
               "capital_stack": "LP, GP, Co-GP, Preferred Equity"}
    fill_data = deck_filler.build_fill_data("Real Stack Co", research, {"profile_bullets": [], "fit_paragraphs": []})
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
        "fit_paragraphs": [],
    }
    fill_data = deck_filler.build_fill_data("Drifted Numbers Co", research, deck_content)
    assert fill_data["profile_bullet_1"] == "A fiduciary for family offices and third-party LPs."
    assert "12 states" not in fill_data["profile_bullet_2"]


def test_grounded_numeric_claim_matching_research_survives():
    research = {"firm_oneliner": "x", "aum_display": "$1.1B", "aum_tag": "AUM_CONFIRMED",
               "geography": "US (17 states) and Puerto Rico"}
    deck_content = {"profile_bullets": ["Operates across 17 states and Puerto Rico."], "fit_paragraphs": []}
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
    deck_content = {"profile_bullets": [], "fit_paragraphs": []}
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
    deck_content = {"profile_bullets": [], "fit_paragraphs": []}
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
    fill_data = deck_filler.build_fill_data(firm, research, {"profile_bullets": [], "fit_paragraphs": []})

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
    fill_data = deck_filler.build_fill_data(firm, research, {"profile_bullets": [], "fit_paragraphs": []})
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
    deck_content = {"profile_bullets": ["Manages a portfolio of 21,000 multifamily units."], "fit_paragraphs": []}
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
                                            {"profile_bullets": long_bullets, "fit_paragraphs": []})
    bullet_keys = [f"profile_bullet_{i}" for i in range(1, 7)]
    assert "profile_bullet_7" not in fill_data  # only 6 slots exist in the data contract at all
    for k in bullet_keys:
        assert len(fill_data[k].split()) <= 19, f"{k} exceeds the ~18-word cap: {fill_data[k]!r}"  # +1 for trailing period tolerance


def test_overflow_fit_paragraphs_capped_at_5_slots_40_words():
    long_paragraphs = [f"This is fit paragraph number {i} " + ("word " * 60) for i in range(7)]
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
    truncate... to fit')."""
    long_name = "Meridian Bridge Capital Partners of the Southeastern United States"
    research = {"firm_oneliner": "x", "aum_display": "$1.4B", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data(long_name, research, {"profile_bullets": [], "fit_paragraphs": []})
    assert len(fill_data["firm_name_display"]) <= deck_filler._FIRM_NAME_DISPLAY_MAX_CHARS + 1  # +1 for the ellipsis char
    assert fill_data["firm_name_display"].endswith("…")

    out = tmp_path / "out.pptx"
    deck_filler.fill_deck(long_name, fill_data, out)
    slide1 = _slide_texts(out, 1)
    assert fill_data["firm_name_display"] in slide1
    assert long_name not in " ".join(slide1)  # the un-truncated name must never appear


def test_short_firm_name_never_truncated():
    fill_data = deck_filler.build_fill_data(KINGBIRD, {"firm_oneliner": "x", "aum_tag": "AUM_CONFIRMED"},
                                            {"profile_bullets": [], "fit_paragraphs": []})
    assert fill_data["firm_name_display"] == KINGBIRD
    assert not fill_data["firm_name_display"].endswith("…")


def test_overflow_firm_oneliner_capped_at_40_words():
    six_line_oneliner = " ".join(["word"] * 80)  # deliberately way over any reasonable single-line length
    research = {"firm_oneliner": six_line_oneliner, "aum_display": "$300M", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data("Long Oneliner Co", research, {"profile_bullets": [], "fit_paragraphs": []})
    assert len(fill_data["firm_oneliner"].split()) <= 41


def test_fewer_than_6_bullets_blanks_remaining_slots_not_placeholder_text():
    research = {"firm_oneliner": "x", "aum_display": "$300M", "aum_tag": "AUM_CONFIRMED"}
    fill_data = deck_filler.build_fill_data("Few Bullets Co", research,
                                            {"profile_bullets": ["Only one fact."], "fit_paragraphs": []})
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
        "fit_paragraphs": [
            "With three distinct asset classes under one roof, Meridian Bridge needs property "
            "accounting that can flex across very different operating models without adding headcount.",
            "A five-layer capital stack across industrial, multifamily, and storage deals means "
            "fund accounting has to track waterfalls correctly for every entity and every asset class.",
            "A confirmed $1.4B platform spanning three asset classes needs investor reporting "
            "that can roll up cleanly across very different property types for the same investor base.",
            "Continued acquisitions across three asset classes keep underwriting busy modeling "
            "very different deal types side by side against the existing multi-asset-class portfolio.",
            "Mezzanine debt alongside preferred equity on larger deals means capital markets work "
            "spans a wider set of instruments than a single-asset-class shop would ever need to manage.",
        ],
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
