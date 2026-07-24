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
    find_grounding_violations,
    find_redundant_claims,
    find_violations,
    reload_template,
)

KINGBIRD = "Kingbird Investment Management"


@pytest.fixture(autouse=True)
def _fresh_template_cache():
    reload_template()
    yield
    reload_template()


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
    past 230 total, trim Block 4 first, then Block 2.'"""
    block2 = "One clean sentence hook about this firm specifically."
    block4 = "First pain sentence here. Second pain sentence follows. Third trailing sentence to drop."
    # Budget tight enough that trimming block4 alone should suffice.
    fixed_word_count = config.EMAIL_MAX_WORDS - len(block2.split()) - len(block4.split()) + 5
    new_block2, new_block4 = _trim_to_budget(block2, block4, fixed_word_count)
    assert new_block2 == block2, "block2 should be untouched while trimming block4 alone suffices"
    assert new_block4 != block4
    assert len(new_block4.split()) < len(block4.split())


# ---------------------------------------------------------------------------
# Word count sanity (documents the known template-arithmetic tension from
# HANDOVER.md rather than asserting a number the template's own math doesn't
# actually support)
# ---------------------------------------------------------------------------
def test_word_count_is_reported_and_never_silently_wrong():
    result = draft_email(KINGBIRD, "Lei", "lei@example.com", _kingbird_research(), _kingbird_personalization())
    assert result["word_count"] == len(result["body"].split())
