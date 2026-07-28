"""QA Part 4 — email generation quality gates against
templates/referral_email.md's rules. No network calls; TEST_MODE fixtures for
the happy path, direct calls to find_violations()/_trim_to_budget() for the
adversarial cases, and a monkeypatched "real-mode LLM" for the one case that
needs to prove the mechanical guard actually intercepts a bad model output
(not just a hand-picked fixture that happens to be clean).

QA FINDING (fixed): HARD RULE 8 (referral-claim discipline — "must NEVER
fabricate specifics ('Manpreet mentioned your Q3 raise')") existed only as a
prompt instruction to the LLM, with no mechanical backstop — unlike every
other anti-slop rule, which has a regex check in find_violations(). Added
_MANPREET_ATTRIBUTION_RE there; see src/email_drafter.py.
"""
from __future__ import annotations

import pytest

from src import config, testmode
from src.email_drafter import (
    FIXED_SUBJECT,
    _THIN_FALLBACK_BLOCK_4,
    _fixed_blocks,
    _generate_blocks,
    _trim_to_budget,
    draft_email,
    fallback_counts,
    find_grounding_violations,
    find_missing_block4_argument,
    find_missing_required_facts,
    find_missing_temporal_framing,
    find_redundant_claims,
    find_violations,
    print_fallback_summary,
    reload_template,
    reset_fallback_counts,
)

KINGBIRD = "Kingbird Investment Management"


@pytest.fixture(autouse=True)
def _fresh_template_cache():
    reload_template()
    yield
    reload_template()


@pytest.fixture(autouse=True)
def _fresh_fallback_counts():
    reset_fallback_counts()
    yield
    reset_fallback_counts()


def _kingbird_research():
    return testmode.firm_research_fixture(KINGBIRD)["research"]


def _kingbird_personalization():
    return testmode.personalization_fixture(KINGBIRD)


# ---------------------------------------------------------------------------
# Fixed blocks reproduced verbatim, never mutated
# ---------------------------------------------------------------------------
def test_fixed_blocks_appear_verbatim_in_the_assembled_email():
    fixed = _fixed_blocks()
    result = draft_email(KINGBIRD, "Lei", "lei@example.com", _kingbird_research(),
                        _kingbird_personalization())
    body = result["body"]
    for block_num in (1, 3, 5, 6, 7):
        text = fixed[block_num].replace("{{poc_first_name}}", "Lei")
        assert text in body, f"Block {block_num} not found verbatim in assembled email"


def test_fixed_blocks_unaffected_by_firm_or_research_content():
    """Fixed blocks must be IDENTICAL across two completely different firms —
    they are template content, never personalized."""
    r1 = draft_email(KINGBIRD, "Lei", "lei@example.com", _kingbird_research(), _kingbird_personalization())
    r2 = draft_email("Totally Different Firm LLC", "Alex", "alex@example.com",
                     {"firm_oneliner": "A totally different firm."},
                     {"thin_personalization": True})
    fixed = _fixed_blocks()
    for block_num in (3, 5, 6):  # blocks without a {{poc_first_name}} substitution
        assert fixed[block_num] in r1["body"]
        assert fixed[block_num] in r2["body"]


def test_subject_is_always_the_fixed_string_regardless_of_input():
    assert FIXED_SUBJECT == "Intro via Manpreet Singh (Singh Capital)"
    for firm, research, pers in [
        (KINGBIRD, _kingbird_research(), _kingbird_personalization()),
        ("Some Other Firm", {"firm_oneliner": "x"}, {"thin_personalization": True}),
    ]:
        result = draft_email(firm, "Alex", "alex@example.com", research, pers)
        assert result["subject"] == FIXED_SUBJECT


# ---------------------------------------------------------------------------
# poc_first_name substitution
# ---------------------------------------------------------------------------
def test_first_name_substitution_one_word_name():
    result = draft_email(KINGBIRD, "Lei", "lei@example.com", _kingbird_research(), _kingbird_personalization())
    assert "Hi Lei," in result["body"]


def test_first_name_substitution_accented_name():
    result = draft_email(KINGBIRD, "José", "jose@example.com", _kingbird_research(), _kingbird_personalization())
    assert "Hi José," in result["body"]
    assert "{{poc_first_name}}" not in result["body"]


def test_missing_first_name_falls_back_to_there():
    result = draft_email(KINGBIRD, "", "x@example.com", _kingbird_research(), _kingbird_personalization())
    assert "Hi there," in result["body"]


# ---------------------------------------------------------------------------
# Anti-AI-slop guard — every mechanically checkable rule
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected_violation", [
    ("You run 15,000 units — a lot of moving parts.", "em_dash"),
    ("You run 15,000 units--a lot of moving parts.", "em_dash"),
    ("Your controller handles this; it costs time.", "semicolon"),
    ("You run “fifteen thousand” units across Texas.", "curly_quotes"),
    ("You run 'fifteen thousand' units across Texas?", "question_mark"),
    ("I noticed your recent fund close.", "banned_phrase:i noticed"),
    ("This is an exciting opportunity for your firm.", "banned_phrase:exciting opportunity"),
    ("We can leverage our team to help.", "banned_word:leverage"),
    ("Our robust platform helps firms scale.", "banned_word:robust"),
    ("This is a key consideration for you.", "banned_word:key"),
])
def test_find_violations_catches_each_rule(text, expected_violation):
    violations = find_violations(text)
    assert expected_violation in violations, f"expected {expected_violation!r} in {violations!r} for {text!r}"


@pytest.mark.parametrize("text", [
    "Manpreet mentioned your Q3 raise, which is why I looked you up.",
    "Manpreet speaks highly of your work with the team.",
    "As Manpreet told me, your fund just closed a big round.",
    "Manpreet said you run a tight operation.",
    "Manpreet shared that you're expanding into new markets.",
])
def test_find_violations_catches_fabricated_manpreet_attribution(text):
    """HARD RULE 8: Block 2/4 must never fabricate something attributed TO
    Manpreet (only fixed Block 1 states the real, factual referral)."""
    assert "fabricated_manpreet_attribution" in find_violations(text)


@pytest.mark.parametrize("text", [
    "Singh Capital and Kingbird sit in the same corner of the market, so Manpreet knows your model well.",
    "You're part of Manpreet's network, which is how this came across my desk.",
    "You run roughly 15,000 multifamily units across Texas and the Southeast.",
])
def test_find_violations_allows_legitimate_manpreet_context(text):
    """Referencing the connection as CONTEXT is explicitly fine per
    referral_email.md rule 4 — only fabricated attributions are banned."""
    assert "fabricated_manpreet_attribution" not in find_violations(text)


def test_clean_text_has_no_violations():
    good = ("You run roughly 15,000 workforce and market-rate multifamily units "
           "across Texas and the Southeast, with LP, GP, and co-GP capital on every deal.")
    assert find_violations(good) == []


def test_real_mode_llm_output_with_violation_falls_back_instead_of_shipping(monkeypatch):
    """Proves find_violations() actually gates the real-mode LLM generation
    path (not just that the function detects bad text in isolation): a
    monkeypatched 'LLM' that returns a fabricated Manpreet quote must be
    rejected, falling back to the safe firm_oneliner / generic pain
    paragraph rather than shipping the violation."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"block_2": "Manpreet mentioned your Q3 raise, which caught my attention.",
                "block_4": "That inertia costs you every close."}, _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    research = _kingbird_research()
    block2, block4, used_fallback = _generate_blocks(KINGBIRD, research, {"thin_personalization": False})
    assert used_fallback is True
    assert "Manpreet" not in block2
    assert block2 == research["firm_oneliner"]
    assert block4 == _THIN_FALLBACK_BLOCK_4


# ---------------------------------------------------------------------------
# Fix B (real-mode Kingbird run): research had 17 states / $4.3B; Block 2 said
# "12 states / $4.3B" — a number in a generated block that doesn't trace to
# the research/personalization object is an extraction-drift or hallucination
# risk and must not ship.
# ---------------------------------------------------------------------------
def test_find_grounding_violations_catches_ungrounded_dollar_figure():
    corpus = '{"aum_display": "$4.4B", "geography": "17 states"}'
    violations = find_grounding_violations("You manage roughly $5.2B across the Southeast.", corpus)
    assert any("5.2B" in v for v in violations), violations


def test_find_grounding_violations_allows_figure_present_in_corpus():
    corpus = '{"aum_display": "$4.4B", "geography": "17 states"}'
    violations = find_grounding_violations("You manage roughly $4.4B across 17 states.", corpus)
    assert violations == []


def test_find_grounding_violations_ignores_text_with_no_numbers():
    assert find_grounding_violations("A clean sentence with no figures at all.", "") == []


def test_find_grounding_violations_catches_a_number_copied_from_the_template_example(monkeypatch):
    """QA finding (this fix's own verification run): referral_email.md's
    Block 4 'Good example' text includes a specific count ("40+ entities") as
    a style sample. A real LLM call echoed that exact number into a live
    Kingbird draft where it had no basis in the research object at all."""
    corpus = '{"aum_display": "$4.4B", "geography": "US secondary markets, 12 states"}'
    text = ("When your controller is stuck between overpaying a provider who doesn't "
           "understand your waterfalls, or burning nights on property-level books "
           "across 40+ entities, the inertia costs you every quarter.")
    violations = find_grounding_violations(text, corpus)
    assert any("40" in v for v in violations), violations


def test_real_mode_llm_output_with_ungrounded_number_falls_back_instead_of_shipping(monkeypatch):
    """Proves find_grounding_violations() actually gates the real-mode LLM
    generation path: a monkeypatched 'LLM' that drifts research's $4.4B/17
    states into $4.3B/12 states must be rejected, not shipped."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"block_2": "You manage roughly $4.3B in GAV across 12 states and Puerto Rico.",
                "block_4": "That inertia costs you every close, every single month."}, _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    research = {"firm_oneliner": "A multifamily investor.", "aum_display": "$4.4B",
               "geography": "17 states and Puerto Rico"}
    block2, block4, used_fallback = _generate_blocks(KINGBIRD, research, {"thin_personalization": False})
    assert used_fallback is True
    assert block2 == research["firm_oneliner"]
    assert block4 == _THIN_FALLBACK_BLOCK_4


# ---------------------------------------------------------------------------
# Fix D (real-mode Kingbird run): Block 2 and Block 4 both said "roughly
# 21,000 units" — Block 4 must add a NEW fact, not restate one Block 2
# already used.
# ---------------------------------------------------------------------------
def test_find_redundant_claims_catches_repeated_unit_count():
    block2 = "You manage roughly 21,000 multifamily units across 17 states."
    block4 = "Managing 21,000 units for third-party LPs means onboarding friction is expensive."
    violations = find_redundant_claims(block2, block4)
    assert any("21000" in v for v in violations), violations


def test_find_redundant_claims_allows_distinct_numbers():
    block2 = "You manage roughly 21,000 multifamily units across 17 states."
    block4 = "With 40+ entities in the capital stack, your controller is stretched thin at close."
    assert find_redundant_claims(block2, block4) == []


def test_real_mode_llm_output_with_redundant_claim_falls_back_instead_of_shipping(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"block_2": "You manage roughly 21,000 multifamily units across 17 states.",
                "block_4": "Managing 21,000 units for third-party LPs means onboarding is expensive."},
               _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    research = {"firm_oneliner": "A multifamily investor.", "geography": "17 states",
               "unit_count_note": "21,000 multifamily units"}
    block2, block4, used_fallback = _generate_blocks(KINGBIRD, research, {"thin_personalization": False})
    assert used_fallback is True
    assert block4 == _THIN_FALLBACK_BLOCK_4


# ---------------------------------------------------------------------------
# FIX 10 (real-mode Kairos Real Estate Partners run): the gate's own verified
# AUM must actually appear somewhere in the generated email, not just be
# available as context the model was free to skip. A real run produced a
# complete, non-violating email with NO dollar figure at all despite
# research.aum_display holding a gate-verified $254M -- find_grounding_
# violations only catches a WRONG number already in the text, not a MISSING
# one.
# ---------------------------------------------------------------------------
def test_find_missing_required_facts_catches_an_omitted_aum_figure():
    research = {"aum_display": "$254M"}
    violations = find_missing_required_facts("This email never mentions a dollar figure at all.", research)
    assert any("254M" in v for v in violations), violations


def test_find_missing_required_facts_allows_the_figure_when_present():
    research = {"aum_display": "$254M"}
    violations = find_missing_required_facts("You've deployed roughly $254M across your portfolio.", research)
    assert violations == []


def test_find_missing_required_facts_no_op_when_research_has_no_aum_display():
    assert find_missing_required_facts("No figure needed here.", {"aum_display": ""}) == []
    assert find_missing_required_facts("No figure needed here.", {}) == []


# FIX 22 (real-mode In-Rel Properties run): disclosed_figure -- verbatim,
# no arithmetic -- is now the PRIMARY required-fact source; aum_display is
# only a fallback for un-migrated research dicts.
def test_find_missing_required_facts_uses_disclosed_figure_when_present():
    research = {"disclosed_figure": "3,000,000 sq ft", "aum_display": "3,000,000 sq ft (of office and retail space under management)"}
    violations = find_missing_required_facts("This email never mentions the square footage at all.", research)
    assert any("3000000" in v or "3,000,000" in v for v in violations), violations


def test_find_missing_required_facts_disclosed_figure_present_in_text_is_clean():
    research = {"disclosed_figure": "3,000,000 sq ft", "aum_display": "3,000,000 sq ft (of office and retail space under management)"}
    violations = find_missing_required_facts(
        "You manage roughly 3,000,000 sq ft of office and retail space.", research)
    assert violations == []


def test_find_missing_required_facts_falls_back_to_aum_display_when_disclosed_figure_absent():
    """Backward-compat for un-migrated research dicts (e.g. testmode.py
    fixtures) that only set aum_display, not the newer disclosed_figure."""
    research = {"aum_display": "$254M"}
    violations = find_missing_required_facts("This email never mentions a dollar figure at all.", research)
    assert any("254M" in v for v in violations), violations


def test_real_mode_llm_output_omitting_aum_falls_back_instead_of_shipping(monkeypatch):
    """The exact observed defect: a well-formed, non-violating email that
    simply never states the gate-verified figure."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"block_2": "Your firm came up as a strong fit for what we do at X Analyst.",
                "block_4": "Onboarding friction at every close is costly for a firm your size."},
               _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    research = {"firm_oneliner": "A boutique real estate manager.", "aum_display": "$254M",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor"}
    block2, block4, used_fallback = _generate_blocks("Kairos Real Estate Partners, LLC", research,
                                                      {"thin_personalization": False})
    assert used_fallback is True
    assert block4 == _THIN_FALLBACK_BLOCK_4


def test_real_mode_llm_output_including_aum_ships_normally(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"block_2": "You've deployed roughly $254M across your acquisitions to date.",
                "block_4": "Most owners in your position are either paying an outsourced "
                          "accountant who barely knows their properties or asking their "
                          "controller to grind through it after hours. That kind of inertia "
                          "adds up, and you feel it every close."},
               _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    research = {"firm_oneliner": "A boutique real estate manager.", "aum_display": "$254M",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor"}
    block2, block4, used_fallback = _generate_blocks("Kairos Real Estate Partners, LLC", research,
                                                      {"thin_personalization": False})
    assert used_fallback is False
    assert "254M" in block2


# ---------------------------------------------------------------------------
# FIX 16 (real-mode Kairos Real Estate Partners run): an invested/deployed-
# capital proxy is a CUMULATIVE figure, not a snapshot of current holdings --
# "roughly $254M deployed since 2013" is correct; "$254M across the
# portfolio" wrongly implies a current asset base. Prompt-level guidance
# alone proved unreliable (a live check found the model write "$254M in
# invested capital across..." despite the template instruction), so this is
# enforced mechanically: founded_year's own digits must appear whenever this
# proxy shape is in play.
# ---------------------------------------------------------------------------
def test_find_missing_temporal_framing_catches_across_the_portfolio_phrasing():
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": "2013"}
    violations = find_missing_temporal_framing(
        "Kairos runs $254M in invested capital across commercial properties.", research)
    assert any("2013" in v for v in violations), violations


def test_find_missing_temporal_framing_allows_since_founded_year_phrasing():
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": "2013"}
    violations = find_missing_temporal_framing(
        "Kairos has deployed roughly $254M since 2013 across commercial properties.", research)
    assert violations == []


def test_find_missing_temporal_framing_no_op_for_gav_proxy():
    """A GAV proxy IS a disclosed current figure -- no temporal-framing
    requirement applies (only the invested-capital proxy shape needs it)."""
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "gav",
               "proxy_basis": "gross asset value", "founded_year": "2013"}
    assert find_missing_temporal_framing("You run $4.4B in GAV across your portfolio.", research) == []


def test_find_missing_temporal_framing_no_op_for_confirmed_aum():
    research = {"aum_tag": "AUM_CONFIRMED", "founded_year": "2013"}
    assert find_missing_temporal_framing("You manage $450M in AUM.", research) == []


def test_find_missing_temporal_framing_no_op_without_founded_year():
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": ""}
    assert find_missing_temporal_framing("Kairos runs $254M in invested capital across properties.", research) == []


def test_find_missing_temporal_framing_no_op_for_portfolio_value_proxy():
    """A portfolio-value/fund-size/unit-count proxy IS a snapshot-style
    figure -- only the invested-capital proxy shape needs "since <year>"."""
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "portfolio value, proxy", "founded_year": "2013"}
    assert find_missing_temporal_framing("With an estimated $900M portfolio value.", research) == []


# ---------------------------------------------------------------------------
# FIX 32A (real-mode Kairos Real Estate Partners run, FIX 31's own VERIFY):
# a real extraction returned founded_year as a bare JSON int (2013, not
# "2013"), crashing find_missing_temporal_framing with
# AttributeError: 'int' object has no attribute 'strip'. The extraction
# prompt's own schema hint ("e.g. 2008 or empty string") invites this, so
# it's stochastic and will recur -- must never raise regardless of type.
# ---------------------------------------------------------------------------
def test_find_missing_temporal_framing_handles_founded_year_as_int():
    """The exact production crash: founded_year=2013 (int), not "2013"."""
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": 2013}
    violations = find_missing_temporal_framing(
        "Kairos runs $254M in invested capital across commercial properties.", research)
    assert any("2013" in v for v in violations), violations


def test_find_missing_temporal_framing_handles_founded_year_as_int_when_present_in_text():
    """An int founded_year must still be recognized as satisfied when its
    digits already appear in the text (the "since <year>" case), not just
    when it's missing."""
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": 2013}
    violations = find_missing_temporal_framing(
        "Kairos has deployed roughly $254M since 2013 across commercial properties.", research)
    assert violations == []


def test_find_missing_temporal_framing_handles_founded_year_as_str():
    """Regression guard: the normal, expected string shape must keep working
    exactly as before."""
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": "2013"}
    violations = find_missing_temporal_framing(
        "Kairos runs $254M in invested capital across commercial properties.", research)
    assert any("2013" in v for v in violations), violations


def test_find_missing_temporal_framing_handles_founded_year_as_none():
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": None}
    assert find_missing_temporal_framing(
        "Kairos runs $254M in invested capital across commercial properties.", research) == []


def test_find_missing_temporal_framing_handles_founded_year_absent_entirely():
    research = {"aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy"}
    assert find_missing_temporal_framing(
        "Kairos runs $254M in invested capital across commercial properties.", research) == []


def test_real_mode_llm_output_wrong_framing_for_invested_capital_falls_back(monkeypatch):
    """The exact observed defect: a well-formed email that includes the AUM
    figure but frames it as a current asset base ("across the portfolio")
    instead of the cumulative-since-founding figure it actually is."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"block_2": "Kairos runs $254M in invested capital across commercial properties.",
                "block_4": "Onboarding friction at every close is costly for a firm your size."},
               _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    research = {"firm_oneliner": "A boutique real estate manager.", "aum_display": "$254M",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": "2013"}
    block2, block4, used_fallback = _generate_blocks("Kairos Real Estate Partners, LLC", research,
                                                      {"thin_personalization": False})
    assert used_fallback is True
    assert block4 == _THIN_FALLBACK_BLOCK_4


def test_real_mode_llm_output_correct_since_framing_ships_normally(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake_complete_json_retry(prompt, **kwargs):
        # FIX 17: this exact phrasing ("...since 2013 across ... properties")
        # previously tripped a real fact_check.py bug -- _COUNT_RE misread the
        # bare YEAR "2013" as a count-of-properties claim purely because it
        # sat within 2 filler words of "properties", then flagged it
        # "ungrounded" (extract_numeric_claims never recognized founded_year's
        # own "2013" in the corpus as a claim either, so there was nothing to
        # match against). Fixed at the source in fact_check.py
        # (_looks_like_bare_calendar_year excludes a bare 1900-2099 number
        # from count-claim extraction entirely) rather than reworded away --
        # this is the ORIGINAL wording, restored.
        return ({"block_2": "Kairos has deployed roughly $254M since 2013 across commercial properties.",
                "block_4": "Most owners in your position are either paying an outsourced "
                          "accountant who barely knows their properties or asking their "
                          "controller to grind through it after hours. That kind of inertia "
                          "adds up, and you feel it every close."},
               _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    research = {"firm_oneliner": "A boutique real estate manager.", "aum_display": "$254M",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": "2013"}
    block2, block4, used_fallback = _generate_blocks("Kairos Real Estate Partners, LLC", research,
                                                      {"thin_personalization": False})
    assert used_fallback is False
    assert "2013" in block2


# ---------------------------------------------------------------------------
# FIX 30 (real-mode Kairos Real Estate Partners run, FIX 29's own
# verification): referral_email.md's Block 4 spec requires two elements --
# the "overpaying a provider OR burning your controller's nights" core
# insight, and a closing beat near "that inertia has a price, and you're
# paying it every close." Neither was ever mechanically checked -- every
# check above covers punctuation/style, grounding, redundancy, or figures,
# not argument structure. The one successfully-generated (non-fallback)
# Block 4 from FIX 29's own verification shipped on-topic and PropTech-free
# but had NEITHER element, since both were only ever guaranteed when
# generation FAILED (the fixed fallback paragraph has both).
# ---------------------------------------------------------------------------
def test_find_missing_block4_argument_passes_the_fixed_fallback_paragraph():
    """The canonical correct Block 4 -- referral_email.md's own reference
    text -- must never itself be flagged."""
    assert find_missing_block4_argument(_THIN_FALLBACK_BLOCK_4) == []


def test_find_missing_block4_argument_catches_the_real_kairos_regression():
    """The exact real generated Block 4 from FIX 29's own verification run
    (Kairos Real Estate Partners, 260/260 words, no other violations) --
    on-topic, PropTech-free, but missing BOTH required elements. Pasted
    verbatim, not paraphrased."""
    real_kairos_block4 = (
        "With in-house accounting plus property management across "
        "multi-asset office and other commercial holdings, your controller "
        "is likely juggling month-end close, bank and CAM reconciliations, "
        "lender covenant packs, and owner reports across multiple entities."
    )
    violations = find_missing_block4_argument(real_kairos_block4)
    assert "missing_two_bad_options" in violations
    assert "missing_inertia_closing" in violations


def test_find_missing_block4_argument_allows_a_good_paraphrase_one():
    """Checks meaning via keyword co-occurrence, not exact wording --
    referral_email.md itself says "something close to". Different sentence
    structure and vocabulary than the fallback paragraph, both elements
    present."""
    paraphrase = (
        "Across a dozen entities and a tight month-end calendar, you're "
        "either handing the books to an outsourced shop that barely knows "
        "your chart of accounts or watching your controller grind through "
        "long hours to close them internally. That gap doesn't close "
        "itself, and you keep paying it every close."
    )
    assert find_missing_block4_argument(paraphrase) == []


def test_find_missing_block4_argument_allows_a_good_paraphrase_two():
    """A second, independently-worded paraphrase -- proves the first pass
    wasn't a coincidence of matching the fallback's own vocabulary."""
    paraphrase = (
        "With multiple funds and a heavy reporting load, the trade-off is "
        "stark: an expensive outside retainer that never touches your "
        "actual ledgers, versus your internal team burning nights to get "
        "owner reports out the door. Neither is free, and that price shows "
        "up every close."
    )
    assert find_missing_block4_argument(paraphrase) == []


def test_find_missing_block4_argument_recognizes_outsourcers_noun_form_when_in_first_sentence():
    """The outsourc\\w* broadening (recognizing the noun "outsourcers", not
    just the adjective "outsourced") must still work now that FIX 32B
    scopes the two-bad-options check to the first sentence -- as long as
    the word actually appears there, where the rule now requires it."""
    text = ("Your controller is either buried in month-end close or paying "
           "expensive outsourcers who barely know your ledgers. "
           "That inertia costs you every close.")
    assert find_missing_block4_argument(text) == []


def test_find_missing_block4_argument_now_catches_the_disjunction_in_sentence_2_only_case():
    """FIX 32B's own real-world regression case, pasted verbatim from the
    live run that originally motivated the outsourc\\w* broadening (see the
    test above): this text's disjunction sits in the SECOND sentence, not
    the first, so trimming (which drops trailing sentences first) could
    strip the whole two-bad-options argument while leaving this text's
    pre-trim check passing -- exactly the failure FIX 31's VERIFY caught
    live. Under FIX 32B's first-sentence scoping this now correctly FAILS
    -- it previously passed (the "outsourcers" word-form fix on its own
    was correct; the gap was WHERE the disjunction sits, not whether the
    word was recognized)."""
    real_generated_block4 = (
        "Running operator-led repositionings across office, industrial/flex, "
        "and medical office means many entity-level ownerships and "
        "transaction closes. That either lands heavy deal reporting on your "
        "controller at month-end, or pushes you into high-fee outsourcers. "
        "Either way, that inertia costs you every close."
    )
    assert find_missing_block4_argument(real_generated_block4) == ["missing_two_bad_options"]


def test_find_missing_block4_argument_recognizes_fund_admin_wording():
    """Regression guard: this fix's own real-mode VERIFY found a second,
    independent word-list gap -- a genuinely well-formed Grubb Properties
    Block 4 used "paying an admin who has never opened your ledgers"
    instead of "provider"/"outsourced," and was wrongly rejected. Notable
    because "fund admin" is the EXACT vocabulary referral_email.md's own
    Block 4 "Good example" text uses (see the next test) -- this word list
    had never actually been checked against the template's own reference
    example before this fix's VERIFY caught it."""
    real_generated_block4 = (
        "With a vertically integrated developer/operator model and "
        "multiple fund structures, your controller is either paying an "
        "admin who has never opened your ledgers, or closing "
        "property-level books at night to meet lender and owner reports. "
        "Neither is efficient. That inertia has a price, and you are "
        "paying it every close."
    )
    assert find_missing_block4_argument(real_generated_block4) == []


def test_find_missing_block4_argument_passes_the_templates_own_good_example():
    """referral_email.md's Block 4 "Good example" text (the reference the
    LLM prompt itself points to as correct) must pass this check -- if it
    didn't, the check would be rejecting the very style it's meant to
    encourage."""
    template_good_example = (
        "With LP, GP, and co-GP structures across 40+ entities, your "
        "controller is either paying a fund admin who does not know your "
        "waterfalls, or doing it in-house at 11pm. Neither works. That "
        "inertia costs you every quarter-end."
    )
    assert find_missing_block4_argument(template_good_example) == []


def test_find_missing_block4_argument_passes_the_templates_other_good_example():
    """referral_email.md's SECOND Block 4 "Good example" text closes with
    "Both cost more than they should" -- not "inertia"/"that price"/"every
    close" -- and failed this check before \\bcost\\w*\\b was added to the
    inaction-cost signal list, the same real-mode VERIFY finding as the
    "admin" case above: the check must not reject the template's own
    reference examples."""
    template_good_example_two = (
        "At 15,000 units across six states, your property accounting is "
        "either running through an outsourcer who has never opened your "
        "Yardi instance, or it is landing on your controller every "
        "month-end. Both cost more than they should."
    )
    assert find_missing_block4_argument(template_good_example_two) == []


def test_find_missing_block4_argument_catches_missing_two_bad_options_only():
    """A closing beat but no dual-option framing at all -- only the first
    violation should fire, proving the two checks are independent."""
    text = ("Your controller handles month-end close across several "
           "entities. That inertia has a price, and you're paying it "
           "every close.")
    assert find_missing_block4_argument(text) == ["missing_two_bad_options"]


def test_find_missing_block4_argument_catches_missing_closing_only():
    """The dual-option framing but no cost-of-inaction closing beat -- only
    the second violation should fire."""
    text = ("You're either overpaying an outsourced provider or burning "
           "your controller's nights on manual reconciliations across a "
           "dozen entities.")
    assert find_missing_block4_argument(text) == ["missing_inertia_closing"]


def test_real_mode_llm_output_missing_block4_argument_falls_back(monkeypatch):
    """Proves the new check actually gates the real-mode LLM generation
    path -- the exact real Kairos regression, reproduced through
    _generate_blocks end to end, must fall back rather than ship."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"block_2": "You run asset, property, and accounting functions across the Mid-Atlantic.",
                "block_4": "With in-house accounting plus property management across "
                          "multi-asset office and other commercial holdings, your "
                          "controller is likely juggling month-end close, bank and CAM "
                          "reconciliations, lender covenant packs, and owner reports "
                          "across multiple entities."},
               _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    research = {"firm_oneliner": "A boutique real estate manager."}
    block2, block4, used_fallback = _generate_blocks("Kairos Real Estate Partners, LLC", research,
                                                      {"thin_personalization": False})
    assert used_fallback is True
    assert block4 == _THIN_FALLBACK_BLOCK_4


# ---------------------------------------------------------------------------
# FIX 32B: scope find_missing_block4_argument's two-bad-options check to the
# FIRST sentence only, closing the exact gap FIX 31's own VERIFY found --
# the check ran pre-trim against the WHOLE block, so a model that placed the
# disjunction in a LATER sentence passed the check, then had that sentence
# trimmed away, shipping an argument-free Block 4 undetected (FIX 31
# deliberately removed the post-trim re-check, so nothing else catches this).
# ---------------------------------------------------------------------------
def test_find_missing_block4_argument_fails_the_fix31_disjunction_in_sentence_2_reproduction():
    """The exact controlled reproduction FIX 31 documented: a two-sentence
    Block 4 where the disjunction lives ONLY in sentence 2 used to pass
    this check cleanly (proving the pre-trim check alone can't guarantee
    the shipped text keeps its argument) -- it must now fail, since the
    argument doesn't lead."""
    disjunction_in_sentence_2_only = (
        "With that expansion and LP/GP capital structures, your controller is "
        "likely juggling month-end close, reconciliations, and lender and "
        "owner reporting across multiple entities. "
        "That either lands squarely on an expensive outsourced provider who "
        "barely knows your ledgers, or costs your controller nights and "
        "weekends every single month, and that inertia adds up every close."
    )
    assert find_missing_block4_argument(disjunction_in_sentence_2_only) == ["missing_two_bad_options"]


def test_find_missing_block4_argument_fails_both_real_shipped_strings_from_fix31_verify():
    """The two real, live-shipped Block 4s from FIX 31's own VERIFY that
    ended up argument-free after trimming -- both must now fail on
    missing_two_bad_options (this is what should have triggered a fallback
    at generation time, before trimming ever ran)."""
    grubb_shipped = (
        "With multiple Link Apartments developments and gateway projects, "
        "your controller is likely juggling many property entities and "
        "lender covenants each month-end."
    )
    inrel_shipped = (
        "With that expansion and LP/GP capital structures, your controller "
        "is likely juggling month-end close, reconciliations, and lender "
        "and owner reporting across multiple entities."
    )
    assert find_missing_block4_argument(grubb_shipped) == ["missing_two_bad_options", "missing_inertia_closing"]
    assert find_missing_block4_argument(inrel_shipped) == ["missing_two_bad_options", "missing_inertia_closing"]


def test_find_missing_block4_argument_passes_both_templates_good_examples():
    """referral_email.md's own two Block 4 "Good example" texts both
    already lead with the two-bad-options structure in sentence one --
    FIX 32B's stricter scoping must not reject the template's own
    reference examples."""
    good_example_1 = (
        "With LP, GP, and co-GP structures across 40+ entities, your "
        "controller is either paying a fund admin who does not know your "
        "waterfalls, or doing it in-house at 11pm. Neither works. That "
        "inertia costs you every quarter-end."
    )
    good_example_2 = (
        "At 15,000 units across six states, your property accounting is "
        "either running through an outsourcer who has never opened your "
        "Yardi instance, or it is landing on your controller every "
        "month-end. Both cost more than they should."
    )
    assert find_missing_block4_argument(good_example_1) == []
    assert find_missing_block4_argument(good_example_2) == []


def test_trimming_the_closing_sentence_away_still_ships_the_generated_text(monkeypatch):
    """FIX 31 (replaces FIX 30's post-trim re-check, which caused a 100%
    fallback rate -- config.EMAIL_MAX_WORDS=260 minus the ~204-word fixed
    blocks leaves only 56 words for Block 2 AND Block 4 combined, well
    under referral_email.md's own 30-50-word target for Block 4 alone, so
    _trim_to_budget fired on essentially every real run and the post-trim
    re-check then correctly-but-uselessly rejected the result every time).
    Pre-FIX-29 behavior (reviewed and approved by the operator) shipped a
    trimmed, single-sentence Block 4 that never had the closing beat either
    -- the two-bad-options structure lives in the FIRST sentence, which
    trimming preserves, so a trimmed generated Block 4 is acceptable output
    and always was. draft_email() must trim ONCE and ship the trimmed
    result -- no re-check, no second fallback path."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    # Long enough, combined with the real fixed-block word count (~204), to
    # force _trim_to_budget to cut the closing sentence to fit 260 words.
    long_block4 = (
        "With active repositionings and recapitalizations across "
        "multi-building office and mixed commercial assets spanning "
        "several distinct legal entities and lender relationships, your "
        "controller is either carrying transaction-driven reporting at "
        "month-end across every one of those entities or you are paying "
        "an outsourcer who has to relearn your entity and lender "
        "structure at every single close cycle. "
        "That inertia has a price, and you're paying it every close."
    )

    def _fake_complete_json_retry(prompt, **kwargs):
        return ({"block_2": "You report $254M in Capital Invested since 2013.",
                "block_4": long_block4}, _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    research = {"firm_oneliner": "A boutique real estate manager.", "aum_display": "$254M"}
    result = draft_email("Kairos Real Estate Partners, LLC", "Stephen", "s@example.com",
                        research, {"thin_personalization": False})
    # Sanity check: the pre-trim text really did pass on its own, and
    # trimming really did remove the closing sentence -- proves this test
    # exercises the same trim interaction FIX 30 found, just asserting the
    # opposite (now correct) outcome.
    assert find_missing_block4_argument(long_block4) == []
    assert find_missing_block4_argument(result["block_4"]) == ["missing_inertia_closing"]
    assert result["thin_personalization"] is False
    assert result["block_4"] != _THIN_FALLBACK_BLOCK_4
    assert "either carrying transaction-driven reporting" in result["block_4"]


# ---------------------------------------------------------------------------
# FIX 34 (real-mode American Real Estate Partners run): a single validator
# false positive on EITHER block used to discard BOTH via _fallback_blocks,
# with no retry -- FIX 33's own phantom-309 grounding false positive on
# Block 2 threw away a genuinely good Block 4 as collateral. Now: per-block
# fallback, with exactly one retry (re-prompted with the specific violation)
# before either block gives up.
# ---------------------------------------------------------------------------
_CLEAN_TWO_BAD_OPTIONS_BLOCK4 = (
    "You're stuck between two bad options: paying an outsourced provider "
    "who has never opened your books, or landing every reconciliation on "
    "your controller at month-end. That inertia has a price, and you're "
    "paying it every close."
)


def test_generate_blocks_block2_violation_retries_then_falls_back_alone_block4_ships(monkeypatch):
    """FIX 34 VERIFY (a): Block 2 violates (fabricated Manpreet
    attribution), Block 4 is clean. Block 4 must ship UNTOUCHED -- never
    regenerated, never discarded -- while Block 2 gets exactly one retry
    and, since the mock repeats the same bad text, falls back alone."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    bad_block2 = "Manpreet mentioned your Q3 raise, which caught my attention."

    class _FakeUsage:
        total_tokens = 300
        cost_usd = 0.0

    calls = []

    def _fake(prompt, **kwargs):
        calls.append(prompt)
        return ({"block_2": bad_block2, "block_4": _CLEAN_TWO_BAD_OPTIONS_BLOCK4}, _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake)

    research = {"firm_oneliner": "A boutique real estate manager."}
    block2, block4, used_thin = _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    assert len(calls) == 2, "exactly one retry, never more"
    retry_prompt = calls[1]
    assert '"block_2"' in retry_prompt and '"block_4"' not in retry_prompt, (
        "the retry must ask for ONLY the violating block -- the clean block's "
        "key must not even appear in the requested JSON shape"
    )
    assert block4 == _CLEAN_TWO_BAD_OPTIONS_BLOCK4, "the clean block must ship untouched"
    assert block2 == research["firm_oneliner"], "block2 alone falls back to its own oneliner"
    assert used_thin is False, "block4 is NOT the fixed fallback text -- only block2 fell back"
    assert fallback_counts().get("fabricated_manpreet_attribution") == 1


def test_generate_blocks_retry_fixes_the_flagged_block_both_ship(monkeypatch):
    """FIX 34 VERIFY (b): Block 4 violates on the first pass; the retry
    (a distinct, corrected response) fixes it. Both blocks ship -- Block 2
    untouched throughout, Block 4 as the RETRIED text, not the fallback."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    good_block2 = "You report $254M in Capital Invested since 2013."
    bad_block4 = "Your controller handles month-end close across several entities."

    class _FakeUsage:
        total_tokens = 300
        cost_usd = 0.0

    calls = []

    def _fake(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return ({"block_2": good_block2, "block_4": bad_block4}, _FakeUsage())
        return ({"block_4": _CLEAN_TWO_BAD_OPTIONS_BLOCK4}, _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake)

    research = {"firm_oneliner": "x", "aum_display": "$254M"}
    block2, block4, used_thin = _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    assert len(calls) == 2
    retry_prompt = calls[1]
    assert '"block_4"' in retry_prompt and '"block_2"' not in retry_prompt
    assert block2 == good_block2
    assert block4 == _CLEAN_TWO_BAD_OPTIONS_BLOCK4
    assert used_thin is False, "block4 is genuinely generated (via retry), not the fixed fallback"
    assert fallback_counts() == {}, "a fix on retry is not a fallback event"


def test_generate_blocks_token_guard_skips_retry_falls_back_per_block(monkeypatch):
    """FIX 34 VERIFY (c): the first pass already reports token usage at or
    over config.MAX_LLM_TOKENS_PER_FIRM -- no retry may be attempted; the
    violating block falls back immediately, the clean block still ships."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    clean_block2 = "A clean hook about this firm."
    bad_block4 = "Your controller handles month-end close across several entities."

    class _FakeUsageOverBudget:
        total_tokens = config.MAX_LLM_TOKENS_PER_FIRM
        cost_usd = 0.0

    calls = []

    def _fake(prompt, **kwargs):
        calls.append(prompt)
        return ({"block_2": clean_block2, "block_4": bad_block4}, _FakeUsageOverBudget())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake)

    research = {"firm_oneliner": "A boutique real estate manager."}
    block2, block4, used_thin = _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    assert len(calls) == 1, "the token guard must skip the retry call entirely"
    assert block2 == clean_block2, "the clean block still ships even when retry is skipped"
    assert block4 == _THIN_FALLBACK_BLOCK_4
    assert used_thin is True
    assert fallback_counts().get("missing_two_bad_options") == 1
    assert fallback_counts().get("missing_inertia_closing") == 1


def test_generate_blocks_both_blocks_violate_and_retry_fails_both_fall_back(monkeypatch):
    """Regression guard for the ORIGINAL joint-fallback shape: when BOTH
    blocks violate and the retry (same broken response) doesn't fix
    either, both fall back together -- the pre-FIX-34 outcome for this
    specific case must be preserved."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    bad_block2 = "Manpreet mentioned your Q3 raise, which caught my attention."
    bad_block4 = "Your controller handles month-end close across several entities."

    class _FakeUsage:
        total_tokens = 300
        cost_usd = 0.0

    calls = []

    def _fake(prompt, **kwargs):
        calls.append(prompt)
        return ({"block_2": bad_block2, "block_4": bad_block4}, _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake)

    research = {"firm_oneliner": "A boutique real estate manager."}
    block2, block4, used_thin = _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    assert len(calls) == 2
    retry_prompt = calls[1]
    assert '"block_2"' in retry_prompt and '"block_4"' in retry_prompt, (
        "both blocks violated, so the retry must ask for both"
    )
    assert block2 == research["firm_oneliner"]
    assert block4 == _THIN_FALLBACK_BLOCK_4
    assert used_thin is True


def test_generate_blocks_logs_stage_on_both_first_pass_and_post_retry(monkeypatch, capsys):
    """FIX 34's logging requirement: the existing [EMAIL FALLBACK] log line
    must now also name the stage (first-pass / post-retry)."""
    monkeypatch.setattr(config, "TEST_MODE", False)

    bad_block4 = "Your controller handles month-end close across several entities."

    class _FakeUsage:
        total_tokens = 300
        cost_usd = 0.0

    def _fake(prompt, **kwargs):
        return ({"block_2": "A clean hook.", "block_4": bad_block4}, _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake)

    research = {"firm_oneliner": "x"}
    _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    out = capsys.readouterr().out
    assert "[first-pass]" in out
    assert "[post-retry]" in out
    assert "[per-block]" in out


def test_fallback_counts_and_print_summary_reflect_actual_fallback_events(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 300
        cost_usd = 0.0

    def _fake(prompt, **kwargs):
        return ({"block_2": "Manpreet mentioned your Q3 raise.",
                "block_4": "Your controller handles month-end close across several entities."},
               _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake)

    assert fallback_counts() == {}
    _generate_blocks("Some Firm LLC", {"firm_oneliner": "x"}, {"thin_personalization": False})
    counts = fallback_counts()
    assert counts.get("fabricated_manpreet_attribution") == 1
    assert counts.get("missing_two_bad_options") == 1
    assert counts.get("missing_inertia_closing") == 1

    capsys.readouterr()
    print_fallback_summary()
    summary_out = capsys.readouterr().out
    assert "[EMAIL FALLBACK SUMMARY]" in summary_out
    assert "fabricated_manpreet_attribution=1" in summary_out


def test_reset_fallback_counts_clears_the_aggregate(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    class _FakeUsage:
        total_tokens = 300
        cost_usd = 0.0

    def _fake(prompt, **kwargs):
        return ({"block_2": "Manpreet mentioned your Q3 raise.", "block_4": "Clean enough."}, _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake)

    _generate_blocks("Some Firm LLC", {"firm_oneliner": "x"}, {"thin_personalization": False})
    assert fallback_counts() != {}
    reset_fallback_counts()
    assert fallback_counts() == {}


# ---------------------------------------------------------------------------
# FIX 15/16 follow-up (real-mode Kairos run): a live diagnostic (3 runs
# against the Kairos research object) showed 2 of 3 escalating to DeepSeek
# after gpt-5-mini hit the full 2048-token reasoning ceiling twice each
# (finish_reason='length', reasoning_tokens=2048, response_chars=0); the one
# run that succeeded on attempt 1 still spent 1728 of that budget on
# reasoning. reasoning_effort="minimal" applied on that basis; re-measured
# 3x afterward: reasoning_tokens=0 and finish_reason='stop' on attempt 1 in
# all 3, zero escalations.
# ---------------------------------------------------------------------------
def test_generate_blocks_real_mode_passes_reasoning_effort_minimal(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)
    captured = {}

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake_complete_json_retry(prompt, **kwargs):
        captured.update(kwargs)
        return ({"block_2": "A clean hook.", "block_4": "A clean pain point."}, _FakeUsage())

    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry", _fake_complete_json_retry)

    _generate_blocks(KINGBIRD, {"firm_oneliner": "x"}, {"thin_personalization": False})
    assert captured.get("reasoning_effort") == "minimal"


# ---------------------------------------------------------------------------
# A 1-in-3 fallback rate shipping generic copy with no logged reason is the
# same silent-degradation pattern found elsewhere this session -- when
# _generate_blocks falls back, it must print WHICH check fired (grounding,
# redundancy, missing required fact, temporal framing, or style) and the
# actual offending text, not just an opaque violations list.
# ---------------------------------------------------------------------------
def _fake_complete_json_retry_returning(block2: str, block4: str):
    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.0

    def _fake(prompt, **kwargs):
        return ({"block_2": block2, "block_4": block4}, _FakeUsage())
    return _fake


def test_fallback_log_names_grounding_category_and_offending_text(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)
    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry",
                        _fake_complete_json_retry_returning(
                            "You manage $9.9B across your portfolio.", "A clean pain point sentence."))

    research = {"firm_oneliner": "A boutique real estate manager.", "aum_display": ""}
    _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    out = capsys.readouterr().out
    assert "[EMAIL FALLBACK]" in out
    assert "grounding" in out
    assert "$9.9B" in out or "9.9B" in out
    assert "You manage $9.9B across your portfolio." in out


def test_fallback_log_names_redundancy_category_and_offending_text(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)
    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry",
                        _fake_complete_json_retry_returning(
                            "You manage roughly 21,000 multifamily units across 17 states.",
                            "Managing 21,000 units for third-party LPs means onboarding friction is expensive."))

    research = {"firm_oneliner": "x", "geography": "17 states", "unit_count_note": "21,000 multifamily units"}
    _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    out = capsys.readouterr().out
    assert "[EMAIL FALLBACK]" in out
    assert "redundancy" in out
    assert "21000" in out
    assert "block2=" in out and "block4=" in out


def test_fallback_log_names_missing_required_fact_category_and_offending_text(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)
    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry",
                        _fake_complete_json_retry_returning(
                            "Your firm came up as a strong fit for what we do at X Analyst.",
                            "Onboarding friction at every close is costly for a firm your size."))

    research = {"firm_oneliner": "A boutique real estate manager.", "aum_display": "$254M",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor"}
    _generate_blocks("Kairos Real Estate Partners, LLC", research, {"thin_personalization": False})

    out = capsys.readouterr().out
    assert "[EMAIL FALLBACK]" in out
    assert "missing_required_fact" in out
    assert "254M" in out
    assert "Your firm came up as a strong fit for what we do at X Analyst." in out


def test_fallback_log_names_temporal_framing_category_and_offending_text(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)
    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry",
                        _fake_complete_json_retry_returning(
                            "Kairos runs $254M in invested capital across commercial properties.",
                            "Onboarding friction at every close is costly for a firm your size."))

    research = {"firm_oneliner": "A boutique real estate manager.", "aum_display": "$254M",
               "aum_tag": "AUM_ESTIMATED", "proxy_type": "non_floor",
               "proxy_basis": "cumulative invested capital, proxy", "founded_year": "2013"}
    _generate_blocks("Kairos Real Estate Partners, LLC", research, {"thin_personalization": False})

    out = capsys.readouterr().out
    assert "[EMAIL FALLBACK]" in out
    assert "temporal_framing" in out
    assert "2013" in out
    assert "Kairos runs $254M in invested capital across commercial properties." in out


def test_fallback_log_names_style_category_and_offending_text(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)
    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry",
                        _fake_complete_json_retry_returning(
                            "You run a great business — truly best-in-class.", "A clean pain point sentence."))

    research = {"firm_oneliner": "A boutique real estate manager."}
    _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    out = capsys.readouterr().out
    assert "[EMAIL FALLBACK]" in out
    assert "style" in out
    assert "You run a great business — truly best-in-class." in out


def test_fallback_log_names_block4_argument_category_and_offending_text(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)
    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry",
                        _fake_complete_json_retry_returning(
                            "A clean hook about this firm.",
                            "With in-house accounting plus property management across "
                            "multi-asset office and other commercial holdings, your "
                            "controller is likely juggling month-end close, bank and CAM "
                            "reconciliations, lender covenant packs, and owner reports "
                            "across multiple entities."))

    research = {"firm_oneliner": "A boutique real estate manager."}
    _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    out = capsys.readouterr().out
    assert "[EMAIL FALLBACK]" in out
    assert "block4_argument" in out
    assert "missing_two_bad_options" in out
    assert "missing_inertia_closing" in out


def test_no_fallback_log_when_blocks_are_clean(monkeypatch, capsys):
    monkeypatch.setattr(config, "TEST_MODE", False)
    import lib_snapshot.models as real_models
    monkeypatch.setattr(real_models, "complete_json_retry",
                        _fake_complete_json_retry_returning(
                            "A clean hook about this firm.",
                            "Most firms in this spot either overpay an outsourced provider "
                            "that never opens their books or push it onto an "
                            "already-stretched controller. That inertia costs more than "
                            "people think, every close."))

    research = {"firm_oneliner": "A boutique real estate manager."}
    _generate_blocks("Some Firm LLC", research, {"thin_personalization": False})

    out = capsys.readouterr().out
    assert "[EMAIL FALLBACK]" not in out


# ---------------------------------------------------------------------------
# thin_personalization fallback
# ---------------------------------------------------------------------------
def test_thin_personalization_uses_generic_block4_verbatim():
    research = {"firm_oneliner": "A boutique real estate manager focused on industrial assets."}
    result = draft_email("Thin Research Co", "Alex", "alex@example.com", research,
                         {"thin_personalization": True})
    assert result["thin_personalization"] is True
    assert result["block_4"] == _THIN_FALLBACK_BLOCK_4
    assert result["block_2"] == research["firm_oneliner"]


def test_thin_personalization_with_no_oneliner_still_produces_something_safe():
    result = draft_email("No Research Co", "Alex", "alex@example.com", {}, {"thin_personalization": True})
    assert result["block_2"]  # never empty
    assert result["block_4"] == _THIN_FALLBACK_BLOCK_4


# ---------------------------------------------------------------------------
# Length guard: trims by whole sentences, never mid-sentence
# ---------------------------------------------------------------------------
def test_trim_ends_on_a_clean_sentence_boundary_not_a_fragment():
    block2 = "You run roughly 15,000 multifamily units across Texas and the Southeast."
    block4 = (
        "With LP, GP, and co-GP capital on every deal across a dispersed multifamily "
        "portfolio, your controller is either paying a fund admin who does not know "
        "your waterfalls, or building it in house at month end. "
        "That inertia costs you every close. "
        "It shows up every quarter in ways that are hard to staff around."
    )
    fixed_word_count = 300  # force trimming even though block2/4 alone are short
    new_block2, new_block4 = _trim_to_budget(block2, block4, fixed_word_count)
    for text in (new_block2, new_block4):
        assert text, "trim must never produce an empty block"
        assert text.strip()[-1] in ".!?", f"trimmed text ends mid-sentence: {text!r}"
        # no dangling fragment like "...with LP." from a mid-sentence word cut
        assert not text.rstrip(".!?").endswith((" with", " and", " a", " the", " your")), (
            f"trimmed text looks like a truncated fragment: {text!r}"
        )


def test_trim_never_used_when_already_under_budget():
    block2, block4 = "Short hook.", "Short pain point sentence here."
    out2, out4 = _trim_to_budget(block2, block4, fixed_word_count=50)
    assert out2 == block2
    assert out4 == block4


def test_trim_drops_block4_before_block2():
    """referral_email.md's generation rules: 'If the generated blocks push
    past 260 total, trim Block 4 first, then Block 2.'"""
    block2 = "One clean sentence hook about this firm specifically."
    block4 = "First pain sentence here. Second pain sentence follows. Third trailing sentence to drop."
    # Budget tight enough that trimming block4 alone should suffice.
    fixed_word_count = config.EMAIL_MAX_WORDS - len(block2.split()) - len(block4.split()) + 5
    new_block2, new_block4 = _trim_to_budget(block2, block4, fixed_word_count)
    assert new_block2 == block2, "block2 should be untouched while trimming block4 alone suffices"
    assert new_block4 != block4
    assert len(new_block4.split()) < len(block4.split())


# ---------------------------------------------------------------------------
# Word count sanity. FIX 10 follow-up (real-mode Kairos Real Estate Partners
# run, the first real-mode firm in this engagement to ever reach Phase 5a):
# EMAIL_MAX_WORDS was 230, but the fixed blocks alone total 204 words once
# assembled (not the ~160 HANDOVER.md's template estimate assumed) and
# Block 2/4's own minimum ranges (15-30 + 30-50) pushed the floor to 249 --
# already over budget before generation even started. Raised to 260 to
# match reality (HANDOVER.md's own prior recommendation) instead of shipping
# every real email over a cap it structurally could never meet. This test
# still doesn't assert word_count <= EMAIL_MAX_WORDS -- the upper end of
# Block 2/4's own stated ranges (204+30+50=284) can still exceed 260, and
# _trim_to_budget's floor-of-one-sentence-per-block guard means trimming
# still can't always close that gap -- only that word_count is reported
# accurately, never silently wrong.
# ---------------------------------------------------------------------------
def test_word_count_is_reported_and_never_silently_wrong():
    result = draft_email(KINGBIRD, "Lei", "lei@example.com", _kingbird_research(), _kingbird_personalization())
    assert result["word_count"] == len(result["body"].split())
