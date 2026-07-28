"""Direct unit coverage for src/fact_check.py, shared by deck_filler.py and
email_drafter.py's numeric-fact-grounding checks.

FIX 17 (real-mode Kairos Real Estate Partners run): "Kairos has deployed
roughly $254M since 2013 across commercial properties" was flagged as
containing an ungrounded claim ("2013"), even though research["founded_year"]
== "2013". Investigated with printed evidence rather than assumed:
founded_year IS present in grounding_corpus()'s JSON dump as a raw
substring, but extract_numeric_claims(corpus) never recognized it as a
CLAIM at all (nothing in the JSON follows it with a recognized unit word).
The actual bug: the old proximity-based count regex had no concept of a
calendar year -- "2013" sitting within 2 filler words of "properties" (from
an unrelated "since <year>" clause) got misread as a claimed COUNT of
properties. Fixed by excluding a bare (no comma/decimal) 4-digit number in
the 1900-2099 range from count-claim extraction entirely -- it's a date,
not a count claim. FIX 35 later retired the whole proximity-based design
(see below) but kept this exclusion verbatim.
"""
from __future__ import annotations

import re

import pytest

from src import fact_check


# ---------------------------------------------------------------------------
# _looks_like_bare_calendar_year
# ---------------------------------------------------------------------------
def test_looks_like_bare_calendar_year_accepts_plausible_years():
    assert fact_check._looks_like_bare_calendar_year("2013")
    assert fact_check._looks_like_bare_calendar_year("1998")
    assert fact_check._looks_like_bare_calendar_year("2099")
    assert fact_check._looks_like_bare_calendar_year("1900")


def test_looks_like_bare_calendar_year_rejects_out_of_range():
    assert not fact_check._looks_like_bare_calendar_year("1899")
    assert not fact_check._looks_like_bare_calendar_year("2100")
    assert not fact_check._looks_like_bare_calendar_year("0254")


def test_looks_like_bare_calendar_year_rejects_non_4_digit():
    assert not fact_check._looks_like_bare_calendar_year("21000")
    assert not fact_check._looks_like_bare_calendar_year("254")


def test_looks_like_bare_calendar_year_rejects_comma_or_decimal():
    """A genuine count that large is conventionally comma-separated (e.g.
    "2,013 units") -- a comma or decimal point rules out "bare year"."""
    assert not fact_check._looks_like_bare_calendar_year("2,013")
    assert not fact_check._looks_like_bare_calendar_year("2013.5")


# ---------------------------------------------------------------------------
# extract_numeric_claims: the year-vs-count misclassification itself
# ---------------------------------------------------------------------------
def test_extract_numeric_claims_excludes_a_year_adjacent_to_a_unit_word():
    text = "Kairos has deployed roughly $254M since 2013 across commercial properties."
    claims = fact_check.extract_numeric_claims(text)
    assert claims == {"254M"}
    assert "2013" not in claims


def test_extract_numeric_claims_still_recognizes_a_real_count_near_a_unit_word():
    """The fix must not blind the extractor to genuine counts -- only a
    bare-year-SHAPED number (1900-2099, no comma) is excluded."""
    text = "Operates across 17 states and manages 21,000 multifamily units."
    claims = fact_check.extract_numeric_claims(text)
    assert "17" in claims
    assert "21000" in claims


def test_extract_numeric_claims_still_recognizes_a_comma_separated_count_in_year_range():
    """A count written with a comma (e.g. "2,013 units") is NOT excluded --
    only a bare, un-commaed 4-digit number is treated as a likely year."""
    text = "The portfolio includes 2,013 units across the Southeast."
    claims = fact_check.extract_numeric_claims(text)
    assert "2013" in claims


# ---------------------------------------------------------------------------
# End-to-end: the exact FIX 17 scenario, grounding a "since <founded_year>"
# sentence against the research/personalization corpus.
# ---------------------------------------------------------------------------
def test_unverifiable_claims_no_longer_flags_a_since_founded_year_sentence():
    research = {"firm_oneliner": "A boutique real estate manager.", "aum_display": "$254M",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": "2013"}
    personalization = {"thin_personalization": False}
    text = "Kairos has deployed roughly $254M since 2013 across commercial properties."

    corpus = fact_check.grounding_corpus(research, personalization)
    assert "2013" in corpus, "founded_year must actually be present in the corpus for this test to be meaningful"
    assert fact_check.unverifiable_claims(text, corpus) == set()


def test_unverifiable_claims_still_catches_a_genuinely_wrong_year_shaped_count():
    """The fix excludes YEARS from count-claim checking -- it must not
    become a blanket exemption for every 4-digit number. A DRIFTED unit
    count that happens to fall in 1900-2099 is a rare edge case this
    narrow fix knowingly accepts (see fact_check.py's own docstring on
    being deliberately narrow); this test documents that the exclusion is
    scoped to extraction, not to real, comma-written counts, which are
    unaffected either way."""
    research = {"geography": "Operates 21,000 units across the Southeast."}
    personalization = {}
    corpus = fact_check.grounding_corpus(research, personalization)
    text = "Operates 19,500 units across the Southeast."  # comma-written, drifted -- still checked
    assert fact_check.unverifiable_claims(text, corpus) == {"19500"}


# ---------------------------------------------------------------------------
# FIX 33 (real-mode American Real Estate Partners run): a digit run belongs
# to exactly one claim. "You closed a $309M fund..." tripped _COUNT_RE into
# ALSO reading "309" as a bare count of funds (proximity-only, the same
# over-permissive relationship FIX 17 found for calendar years) -- $309M
# was the gate's own verified figure, but the corpus's JSON phrasing didn't
# happen to trigger the same phantom, so unverifiable_claims saw a claim
# ("309") the corpus didn't have and fell back BOTH generated blocks over a
# number that was never actually wrong.
# ---------------------------------------------------------------------------

# Diagnose step: every string from this fix's own diagnosis, with the
# claim set actually produced by the (now-fixed) extractor.
_DIAGNOSE_CASES = [
    ("a $309M fund", {"309M"}),
    ("$309M (fund close)", {"309M"}),
    # FIX 35 widens this from FIX 33's own {"309M"} -- the old extractor
    # couldn't see the "12-15 projects" range or the bare "1,015,740 SF"
    # acquisition at all (two of the confirmed blind spots FIX 35 closes).
    ("You closed a $309M fund to finance roughly 12-15 PowerHouse data center "
     "projects while also running large industrial deals like a 1,015,740 SF "
     "acquisition in Middletown, Delaware.", {"309M", "12", "15", "1015740"}),
    ("$309M fund across 40 properties", {"40", "309M"}),
    ("3 funds", {"3"}),
    ("3M units", {"3"}),
    ("deployed roughly $254M since 2013 across commercial properties", {"254M"}),
    ("40+ entities", {"40"}),
    ("$1.9B portfolio", {"1.9B"}),
    ("15,000 units", {"15000"}),
    ("$2.5 billion in assets under management", {"2.5B"}),
]


@pytest.mark.parametrize("text,expected_claims", _DIAGNOSE_CASES)
def test_extract_numeric_claims_matches_the_fix33_diagnose_evidence(text, expected_claims):
    assert fact_check.extract_numeric_claims(text) == expected_claims


def test_unverifiable_claims_no_longer_flags_the_real_309m_block2_against_its_corpus():
    """End-to-end reproduction of the real American Real Estate Partners
    run: $309M appears in disclosed_figure/aum_display/recent_event, but
    Block 2 restates it in a sentence structurally different enough
    ("...$309M fund to finance...") that only IT, not the corpus's own
    JSON phrasing, used to trip the phantom-309 misread under FIX 33's
    OLDER extractor. FIX 35 widens what Block 2's own sentence extracts
    (adding the 12-15 project range and the 1,015,740 SF acquisition,
    previously invisible blind spots) -- this corpus is built to
    corroborate ALL FIVE numbers, not just $309M, since the whole point of
    FIX 35 is that a wider gate must not become a wider false-positive
    generator against a corpus that genuinely has the facts."""
    real_block2 = (
        "You closed a $309M fund to finance roughly 12-15 PowerHouse data "
        "center projects while also running large industrial deals like a "
        "1,015,740 SF acquisition in Middletown, Delaware."
    )
    research = {
        "disclosed_figure": "$309M",
        "disclosed_label": "Fund Close",
        "aum_display": "$309M (Fund Close)",
        "recent_event": "Fund III closed at $309M in Q2 2024, financing 12 to 15 "
                        "PowerHouse data center projects and a 1,015,740 sq ft "
                        "acquisition in Middletown, Delaware.",
        "aum_tag": "AUM_ESTIMATED",
        "proxy_type": "non_floor",
    }
    personalization = {"pain_point_angle": "x", "service_fit": "y"}
    corpus = fact_check.grounding_corpus(research, personalization)
    assert fact_check.extract_numeric_claims(corpus) == {"309M", "12", "15", "1015740"}, (
        "sanity check: the corpus must independently corroborate every one of "
        "the five numbers Block 2 states, or this test isn't reproducing a "
        "genuinely fully-grounded draft"
    )
    assert fact_check.unverifiable_claims(real_block2, corpus) == set()


# --- Must-not-regress: existing detection must survive the span-ownership fix
def test_extract_numeric_claims_still_flags_wrong_number_drift():
    """Regression guard: suppression must never hide a GENUINELY drifted
    number that has no dollar sign anywhere near it."""
    research = {"geography": "Operates across 17 states."}
    corpus = fact_check.grounding_corpus(research, {})
    text = "Operates across 12 states."
    assert fact_check.unverifiable_claims(text, corpus) == {"12"}


def test_extract_numeric_claims_still_excludes_calendar_year_fix17():
    """Regression guard: FIX 17's calendar-year exclusion must still hold
    after the span-ownership rewrite."""
    text = "Kairos has deployed roughly $254M since 2013 across commercial properties."
    assert fact_check.extract_numeric_claims(text) == {"254M"}


def test_extract_numeric_claims_still_extracts_a_count_with_no_dollar_sign_nearby():
    assert fact_check.extract_numeric_claims("40+ entities") == {"40"}


def test_extract_numeric_claims_extracts_both_dollar_and_a_separate_count():
    """The suppression must be scoped to the OVERLAPPING digit run only --
    a genuinely separate count elsewhere in the same text must survive."""
    assert fact_check.extract_numeric_claims("$309M fund across 40 properties") == {"309M", "40"}


def test_extract_numeric_claims_plain_count_unaffected():
    assert fact_check.extract_numeric_claims("3 funds") == {"3"}


def test_extract_numeric_claims_scale_letter_count_not_wrongly_suppressed():
    """Character-peeking (checking for a preceding "$" or a trailing scale
    letter) would wrongly suppress this -- "3M units" is a genuine count of
    3 million units with NO dollar sign anywhere near it. FIX 35's typed
    classification only types a number CURRENCY if it has an actual "$"
    prefix of its own; "3M units" has none, so it falls through to COUNT."""
    assert "3" in fact_check.extract_numeric_claims("3M units")


# --- Shape-level parametrized coverage, derived from the module's own
# _UNIT_WORD_ALTERNATION vocabulary string rather than a hardcoded parallel
# word list -- a future unit word added there is then covered
# automatically. FIX 35 exposes this vocabulary as a bare alternation
# string (not buried inside a larger compiled pattern), so deriving
# examples is a plain top-level "|"-split -- no more balanced-paren
# scanning of a compiled regex's .pattern needed.
def _unit_word_examples() -> list[str]:
    """One representative literal example per unit-word alternative,
    parsed directly out of fact_check._UNIT_WORD_ALTERNATION (still a
    depth-aware split, since the alternation contains its OWN nested
    groups, e.g. "propert(?:y|ies)") rather than hand-copying the word
    list a second time into the test file."""
    def _split_top_level(s: str, sep: str = "|") -> list[str]:
        parts, depth, current = [], 0, []
        for ch in s:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == sep and depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(ch)
        parts.append("".join(current))
        return parts

    examples = []
    for alt in _split_top_level(fact_check._UNIT_WORD_ALTERNATION):
        word = re.sub(r"\(\?:([^|)]+)\|[^)]+\)", r"\1", alt)  # e.g. "(?:y|ies)" -> "y"
        word = word.replace(r"\.?", "")           # optional literal dot -> drop it
        word = word.replace(r"\s*", " ")           # optional whitespace run -> one space
        word = word.replace("\\.", ".")            # remaining REQUIRED literal dot, e.g. "s\.f\."
        word = word.rstrip("?").strip()
        examples.append(word)
    return examples


UNIT_WORD_EXAMPLES = _unit_word_examples()


def test_unit_word_examples_derived_from_the_vocabulary_actually_extract():
    """Sanity check on the derivation itself, through the PUBLIC API (not
    an internal regex object) -- if a derived example doesn't actually
    produce a count claim, the parametrized tests below would be silently
    testing nothing."""
    assert len(UNIT_WORD_EXAMPLES) >= 20, UNIT_WORD_EXAMPLES  # FIX 35 expanded well past the original 9
    for word in UNIT_WORD_EXAMPLES:
        assert fact_check.extract_numeric_claims(f"42 {word}") == {"42"}, word


_DOLLAR_FORMS_AND_EXPECTED_CLAIM = [
    ("${n}M {unit}".replace("{n}", "309"), "309M"),
    ("${n}.9B {unit}".replace("{n}", "1"), "1.9B"),
    ("$2.5 billion in {unit}", "2.5B"),
]


@pytest.mark.parametrize("unit_word", UNIT_WORD_EXAMPLES)
@pytest.mark.parametrize("template,expected_claim", _DOLLAR_FORMS_AND_EXPECTED_CLAIM)
def test_extract_numeric_claims_suppresses_phantom_count_for_every_unit_word(
        template, expected_claim, unit_word):
    """FIX 33's own regression net, still green under FIX 35's typed
    extractor: a dollar figure directly adjacent to ANY unit word must
    never ALSO be read as a bare count of that unit -- parametrized over
    the vocabulary itself (see _unit_word_examples) crossed with three
    dollar-figure shapes, so a future unit word is covered automatically.
    Under FIX 35 this holds structurally (CURRENCY is typed before any
    unit-word check ever runs), not via a suppression pass."""
    text = template.format(unit=unit_word)
    assert fact_check.extract_numeric_claims(text) == {expected_claim}, text


# ---------------------------------------------------------------------------
# FIX 35 (real-mode American Real Estate Partners run) -- single-pass TYPED
# extraction (retires the whole FIX 9/17/33 defect class: a digit run's
# meaning decided by proximity to a word, not by the number's own
# attachments) and asymmetric grounding (retires FIX 33's own residual
# corpus-PHRASING dependency).
# ---------------------------------------------------------------------------

# --- Shape-level parametrized coverage: every unit word crossed with the
# four claim-bearing SHAPES this fix introduces or changes, so a future
# unit word is automatically exercised against all four.
@pytest.mark.parametrize("unit_word", UNIT_WORD_EXAMPLES)
def test_shape_dollar_form_types_currency_for_every_unit_word(unit_word):
    text = "$309M {unit}".format(unit=unit_word)
    assert fact_check.extract_numeric_claims(text) == {"309M"}, text


@pytest.mark.parametrize("unit_word", UNIT_WORD_EXAMPLES)
def test_shape_bare_count_extracts_for_every_unit_word(unit_word):
    text = "40 {unit}".format(unit=unit_word)
    assert fact_check.extract_numeric_claims(text) == {"40"}, text


@pytest.mark.parametrize("unit_word", UNIT_WORD_EXAMPLES)
def test_shape_since_year_never_emits_a_year_claim_for_any_unit_word(unit_word):
    text = "since 2013 across {unit}".format(unit=unit_word)
    assert fact_check.extract_numeric_claims(text) == set(), text


@pytest.mark.parametrize("unit_word", UNIT_WORD_EXAMPLES)
def test_shape_range_extracts_both_endpoints_for_every_unit_word(unit_word):
    text = "12-15 {unit}".format(unit=unit_word)
    assert fact_check.extract_numeric_claims(text) == {"12", "15"}, text


# --- RANGE type, specifically
def test_range_extracts_both_endpoints_hyphen():
    assert fact_check.extract_numeric_claims("12-15 projects") == {"12", "15"}


def test_range_extracts_both_endpoints_word_to():
    assert fact_check.extract_numeric_claims("12 to 15 projects") == {"12", "15"}


def test_range_extracts_both_endpoints_en_dash():
    assert fact_check.extract_numeric_claims("12–15 projects") == {"12", "15"}


def test_range_with_descriptive_words_between_endpoint_and_unit():
    """The real evidence: 3 filler words ("PowerHouse data center") between
    the range and its unit word -- wider than plain COUNT's 2-word budget,
    since real prose often inserts a short descriptive phrase here."""
    text = "12-15 PowerHouse data center projects"
    assert fact_check.extract_numeric_claims(text) == {"12", "15"}


def test_bare_range_with_no_unit_word_is_not_a_claim():
    """A bare '2024-2026' with no unit word is NOT a claim -- it reads as a
    date range or a version span, not a count of anything."""
    assert fact_check.extract_numeric_claims("2024-2026 strategic plan") == set()


def test_range_endpoints_that_look_like_years_are_excluded_like_any_bare_year():
    """FIX 17's exclusion applies to range endpoints too."""
    assert fact_check.extract_numeric_claims("2013-2015 acquisitions") == set()


# --- PERCENTAGE type, specifically
def test_percentage_is_typed_and_emits_no_claim():
    assert fact_check.extract_numeric_claims("grew 40% year over year") == set()


def test_percentage_does_not_block_a_separate_real_count_in_the_same_text():
    assert fact_check.extract_numeric_claims("grew 40% across 12 markets") == {"12"}


def test_percentage_word_form_also_typed():
    assert fact_check.extract_numeric_claims("occupancy at 95 percent") == set()


# --- Confirmed blind spots this fix closes
def test_bare_sf_no_period_no_sq_prefix_now_extracted():
    assert fact_check.extract_numeric_claims("1,015,740 sf") == {"1015740"}
    assert fact_check.extract_numeric_claims("1,015,740 SF") == {"1015740"}


def test_period_form_s_f_also_extracted():
    assert fact_check.extract_numeric_claims("1,015,740 s.f.") == {"1015740"}


@pytest.mark.parametrize("unit_word", ["projects", "buildings", "markets", "acquisitions",
                                       "assets", "investments", "loans", "transactions",
                                       "tenants", "leases", "offices", "floors", "beds",
                                       "keys", "doors"])
def test_each_newly_added_unit_word_extracts_a_count(unit_word):
    assert fact_check.extract_numeric_claims(f"12 {unit_word}") == {"12"}


# --- Diagnose-step strings not already covered above, asserted directly
def test_diagnose_grew_40_percent_across_12_markets():
    assert fact_check.extract_numeric_claims("grew 40% across 12 markets") == {"12"}


def test_diagnose_309_million_word_form_fund():
    assert fact_check.extract_numeric_claims("$309 million fund") == {"309M"}


def test_diagnose_1015740_sf_bare():
    assert fact_check.extract_numeric_claims("1,015,740 sf") == {"1015740"}


def test_diagnose_12_to_15_projects():
    assert fact_check.extract_numeric_claims("12 to 15 projects") == {"12", "15"}


# ---------------------------------------------------------------------------
# Part B -- asymmetric grounding
# ---------------------------------------------------------------------------
def test_currency_claim_grounded_by_dollar_form_in_corpus_despite_different_phrasing():
    """The exact FIX 33 case, now via typed classification + asymmetric
    grounding rather than span suppression: '$309M fund' in the block,
    '$309M (fund close)' in the corpus -- grounded."""
    assert fact_check.unverifiable_claims("$309M fund", "$309M (fund close)") == set()


def test_count_claim_grounded_by_a_standalone_digit_with_no_adjacent_unit_word():
    """FIX 35's core Part B case: the corpus is JSON, and a genuine count
    there often has NO adjacent unit word at all -- the key names it
    BEFORE the number ('"unit_count": "21,794"'). Grounding must depend on
    corpus CONTENT, not on the corpus re-deriving the identical typed
    claim via the same phrasing."""
    assert fact_check.unverifiable_claims(
        "21,794 units", '{"unit_count": "21,794"}') == set()


def test_count_claim_not_grounded_when_corpus_lacks_it_entirely():
    assert fact_check.unverifiable_claims(
        "40 properties", '{"firm_oneliner": "a real estate firm"}') == {"40"}


def test_a_dollar_figure_does_not_ground_an_unrelated_count_of_the_same_digits():
    """Asymmetric by design: a CURRENCY claim and a COUNT claim of the same
    digits are grounded differently. '309 properties' must NOT be
    considered grounded just because the corpus happens to have '$309M'
    somewhere -- a dollar figure is not a count."""
    assert fact_check.unverifiable_claims(
        "309 properties", '{"disclosed_figure": "$309M"}') == {"309"}


def test_a_bare_count_does_not_ground_a_currency_claim_of_the_same_digits():
    """The reverse asymmetry: '$309M fund' must NOT be considered grounded
    by a corpus that only has a bare '309' with no dollar sign."""
    assert fact_check.unverifiable_claims(
        "$309M fund", '{"note": "reference number 309"}') == {"309M"}


def test_url_digit_in_corpus_does_not_ground_an_unrelated_count():
    """Anti-weakening guard: a source URL's path/query digits must never
    ground a claim -- a listing ID or a date segment in a link is not a
    verified fact."""
    assert fact_check.unverifiable_claims(
        "3000 properties",
        '{"source": "https://example.com/listing/3000/detail"}') == {"3000"}


def test_url_digit_does_not_block_grounding_when_the_same_digit_also_appears_outside_a_url():
    assert fact_check.unverifiable_claims(
        "3000 properties",
        '{"unit_count": "3000", "source": "https://example.com/listing/3000/detail"}') == set()


def test_url_digit_does_not_ground_a_currency_claim_either():
    assert fact_check.unverifiable_claims(
        "$2024M fund",
        '{"source": "https://example.com/deals/2024/announcement"}') == {"2024M"}


def test_comma_grouped_corpus_number_grounds_a_comma_free_claim_and_vice_versa():
    assert fact_check.unverifiable_claims("21794 units", '{"unit_count": "21,794"}') == set()
    assert fact_check.unverifiable_claims("21,794 units", '{"unit_count": "21794"}') == set()


# ---------------------------------------------------------------------------
# Consumers (find_redundant_claims / find_missing_required_facts /
# find_missing_temporal_framing all call extract_numeric_claims) -- verify
# behavior, per FIX 35's explicit instruction not to edit email_drafter.py.
# ---------------------------------------------------------------------------
def test_consumer_shape_disclosed_figure_dollar():
    assert fact_check.extract_numeric_claims("$309M") == {"309M"}


def test_consumer_shape_disclosed_figure_units_proxy():
    assert fact_check.extract_numeric_claims("21,794 units") == {"21794"}


def test_consumer_shape_disclosed_figure_sqft_proxy():
    assert fact_check.extract_numeric_claims("3,000,000 sq ft") == {"3000000"}


# ---------------------------------------------------------------------------
# FIX 37 Part C (real-mode American Real Estate Partners run): a retried
# "12-15" range endpoint failed ungrounded:15 even though an identical
# first-pass range passed. DIAGNOSED before fixing -- isolated hyphen-vs-
# en-dash extraction/grounding was symmetric (no asymmetry there at all);
# the actual cause was json.dumps()'s default ensure_ascii=True escaping
# the corpus's en dash into a literal "–" sequence whose trailing hex
# digits ("2013") sit with zero separator before the real "15", so the
# corpus tokenizer swallowed both into one bogus "201315" token and "15"
# never appeared standalone. Fixed via ensure_ascii=False in
# grounding_corpus() itself -- not a dash-normalization patch, since there
# was no dash asymmetry to normalize.
# ---------------------------------------------------------------------------
def test_grounding_corpus_does_not_escape_non_ascii_characters():
    """The bug in one line: json.dumps's default ensure_ascii=True would
    turn this en dash into a literal 6-char "\\u2013" escape sequence.
    grounding_corpus() must emit the real character instead."""
    corpus = fact_check.grounding_corpus({"hook": "a 12–15 project pipeline"})
    assert "–" in corpus
    assert "\\u2013" not in corpus


def test_en_dash_range_endpoint_grounds_when_corpus_also_uses_an_en_dash():
    """The exact failure shape: a range's SECOND endpoint, immediately
    followed (with no space) by the JSON string's closing quote once
    json-encoded, used to merge with an escaped en dash's hex digits."""
    research = {"hook_1": "AREP has a newly closed fund earmarked to support "
                          "12–15 PowerHouse Data Centers projects across jurisdictions."}
    corpus = fact_check.grounding_corpus(research)
    claim_text = "Pipeline to support 12–15 PowerHouse Data Centers projects"
    assert fact_check.unverifiable_claims(claim_text, corpus) == set()


def test_other_non_ascii_characters_adjacent_to_digits_do_not_corrupt_corpus_tokenization():
    """The fix is general (fixed at the JSON-encoding source), not an
    en-dash special case -- any non-ASCII character sitting next to a
    digit run in a research/personalization value must not corrupt
    tokenization of a real, separate, later number."""
    corpus = fact_check.grounding_corpus({"hook": "a “best-in-class” 17 state footprint"})
    assert fact_check.unverifiable_claims("a 17 state footprint", corpus) == set()
