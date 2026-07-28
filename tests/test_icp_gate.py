"""QA Part 3 — ICP gate & AUM proxy logic, tested against
prompts/icp_scoring_v2_2.md exactly, with synthetic (no-network) inputs.

QA FINDING (fixed, not just tested around): src/icp_gate.py used to delegate
the ENTIRE gate decision — qualify/disqualify/park, exact threshold
comparisons, the asymmetric below-$200M table — to a single LLM call's own
judgment. icp_scoring_v2_2.md itself says the floor-proxy rule "is a
deterministic rule — do not second-guess floor statements", and the
asymmetric gate table is written as an exact decision table, not something to
ask an LLM to weigh holistically. Extracted `apply_gate()`: a pure Python
function that takes already-extracted facts (firm type ok, region ok, AUM
value, AUM tag, proxy type/basis) and applies the gate deterministically. The
LLM's real-mode job (src/icp_gate.py:_extract) is now EXTRACTION ONLY. These
tests exercise `apply_gate` directly — no network, no LLM, no fixtures.
"""
from __future__ import annotations

import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import config, icp_gate
from src.icp_gate import InfrastructureError, apply_gate

AUM_MIN = config.AUM_MIN_USD    # 200_000_000
AUM_MAX = config.AUM_MAX_USD    # 2_000_000_000


@pytest.fixture(autouse=True)
def _clean_wc_state():
    reset_testmode(verbose=False)
    yield


# ---------------------------------------------------------------------------
# Condition 2 (AUM): confirmed figures
# ---------------------------------------------------------------------------
def test_confirmed_aum_in_band_qualifies():
    result, reason = apply_gate(True, True, 450_000_000, "AUM_CONFIRMED", source_note="firm website")
    assert result == "qualified"
    assert "$450M" in reason and "confirmed" in reason


def test_confirmed_aum_below_200m_disqualifies():
    result, reason = apply_gate(True, True, 150_000_000, "AUM_CONFIRMED")
    assert result == "disqualified"
    assert "$150M" in reason


def test_confirmed_aum_above_2b_disqualifies():
    result, reason = apply_gate(True, True, 5_000_000_000, "AUM_CONFIRMED")
    assert result == "disqualified"
    assert "$5B" in reason


def test_confirmed_aum_exactly_at_floor_boundary_qualifies():
    """Exact $200M boundary — icp_scoring_v2_2.md: 'Must be BETWEEN $200M and
    $2B', i.e. inclusive. A boundary an LLM could round either way; the
    deterministic function must not waver."""
    result, _ = apply_gate(True, True, AUM_MIN, "AUM_CONFIRMED")
    assert result == "qualified"


def test_confirmed_aum_exactly_at_ceiling_boundary_qualifies():
    result, _ = apply_gate(True, True, AUM_MAX, "AUM_CONFIRMED")
    assert result == "qualified"


def test_confirmed_aum_one_dollar_over_ceiling_disqualifies():
    result, _ = apply_gate(True, True, AUM_MAX + 1, "AUM_CONFIRMED")
    assert result == "disqualified"


# ---------------------------------------------------------------------------
# Floor-proxy LANGUAGE ("over $2B", "north of $500M")
# ---------------------------------------------------------------------------
def test_floor_language_over_2b_disqualifies():
    """'over $2B' -> treat the floor as the AUM; floor alone exceeds ceiling -> DISQUALIFY."""
    result, reason = apply_gate(True, True, 2_500_000_000, "AUM_ESTIMATED", proxy_type="floor",
                               proxy_basis="floor-proxy language (over $2B)")
    assert result == "disqualified"
    assert "exceeds ceiling" in reason


def test_floor_language_within_band_tags_estimated_and_qualifies():
    """'north of $500M' falls within $200M-$2B -> tag AUM_ESTIMATED, QUALIFY."""
    result, reason = apply_gate(True, True, 600_000_000, "AUM_ESTIMATED", proxy_type="floor",
                               proxy_basis="floor-proxy language (north of $500M)")
    assert result == "qualified"
    assert "QUALIFIED" in reason and "$600M" in reason


def test_floor_language_below_200m_parks_not_disqualifies():
    """Floor alone below $200M -> asymmetric gate: PARK for manual review,
    not disqualify (the true AUM could be higher than a floor statement)."""
    result, reason = apply_gate(True, True, 150_000_000, "AUM_ESTIMATED", proxy_type="floor",
                               proxy_basis="floor-proxy language (at least $150M)")
    assert result == "parked"
    assert "below floor" in reason


# ---------------------------------------------------------------------------
# Proxy taxonomy a-d (icp_scoring_v2_2.md "AUM proxy rules")
# ---------------------------------------------------------------------------
def test_proxy_a_regulatory_aum_form_adv_is_confirmed_not_estimated():
    """Proxy rule (a): regulatory AUM (Form ADV) -> AUM_CONFIRMED, not estimated."""
    result, reason = apply_gate(True, True, 900_000_000, "AUM_CONFIRMED",
                               source_note="regulatory AUM (Form ADV)")
    assert result == "qualified"
    assert "confirmed" in reason


def test_proxy_b_invested_capital_non_floor_type_in_band_qualifies():
    """Proxy rule (b): '$800M invested' -> NON-FLOOR estimate (FIX 8: a
    specific, dated cumulative figure is not itself phrased as a lower
    bound the way floor-proxy LANGUAGE is -- reclassified from "floor" to
    "non_floor" in the extraction prompt; apply_gate's own floor/non_floor
    branching is unchanged, only the proxy_type it's called with differs).
    In-band -> qualify, and the proxy basis string appears in the reason
    (auditability)."""
    result, reason = apply_gate(True, True, 800_000_000, "AUM_ESTIMATED", proxy_type="non_floor",
                               proxy_basis="cumulative invested capital, proxy")
    assert result == "qualified"
    assert "invested capital" in reason


def test_proxy_b_invested_capital_below_200m_disqualifies_not_parks():
    """FIX 8 (real-mode Kairos run): this used to PARK under the old
    floor-classification (the exact "Seyon Group" example from §10a) --
    invested capital is now classified non_floor, so below-$200M
    DISQUALIFIES, same as portfolio value/fund size (proxy rules c/d).
    apply_gate's branching itself is unchanged; only the proxy_type an
    invested-capital figure now arrives tagged with differs."""
    result, reason = apply_gate(True, True, 150_000_000, "AUM_ESTIMATED", proxy_type="non_floor",
                               proxy_basis="cumulative invested capital, proxy")
    assert result == "disqualified"
    assert "below floor" in reason


def test_proxy_c_portfolio_value_is_non_floor_disqualifies_below_200m():
    """Proxy rule (c): 'portfolio valued at $X' -> non-floor, closer to
    actual value -> DISQUALIFY (not park) when below $200M."""
    result, reason = apply_gate(True, True, 150_000_000, "AUM_ESTIMATED", proxy_type="non_floor",
                               proxy_basis="portfolio value, proxy")
    assert result == "disqualified"
    assert "portfolio value" in reason


def test_proxy_d_fund_size_is_non_floor_disqualifies_below_200m():
    """Proxy rule (d): fund size announcements -> non-floor -> DISQUALIFY below $200M."""
    result, reason = apply_gate(True, True, 150_000_000, "AUM_ESTIMATED", proxy_type="non_floor",
                               proxy_basis="fund size, proxy")
    assert result == "disqualified"
    assert "fund size" in reason


def test_proxy_d_fund_size_above_2b_disqualifies_regardless_of_source():
    """'ESTIMATED value above $2B -> DISQUALIFY. The ceiling applies
    regardless of source.' — matches icp_scoring_v2_2.md's own example
    exactly: 'DISQUALIFIED (est. $8B — fund size proxy, exceeds ceiling)'."""
    result, reason = apply_gate(True, True, 8_000_000_000, "AUM_ESTIMATED", proxy_type="non_floor",
                               proxy_basis="fund size proxy")
    assert result == "disqualified"
    assert "exceeds ceiling" in reason


# ---------------------------------------------------------------------------
# Proxy rule e (GAV) — QA finding, real-mode dry run: Kingbird Investment
# Management publicly discloses ~$4.4B GAV, but GAV is a GROSS figure (it
# includes debt/leverage) and must never be gated against the $2B ceiling
# as-is. Converted to an estimated equity/AUM proxy via a 60% leverage
# haircut (icp_scoring_v2_2.md proxy rule e / GAV_LEVERAGE_HAIRCUT) BEFORE
# any threshold comparison.
# ---------------------------------------------------------------------------
def test_proxy_e_gav_1b_converts_to_400m_qualifies():
    result, reason = apply_gate(True, True, 1_000_000_000, "AUM_ESTIMATED", proxy_type="gav")
    assert result == "qualified"
    assert "$400M" in reason
    assert "GAV" in reason


def test_proxy_e_gav_4_4b_kingbird_converts_to_1_76b_qualifies():
    """The exact real-mode dry-run scenario this fix addresses: Kingbird's
    disclosed ~$4.4B GAV must convert to ~$1.76B estimated equity and
    QUALIFY, not park as 'AUM not found'."""
    result, reason = apply_gate(True, True, 4_400_000_000, "AUM_ESTIMATED", proxy_type="gav")
    assert result == "qualified"
    assert "$1.8B" in reason  # _fmt_usd rounds 1.76B to one decimal
    assert "GAV-derived equity proxy" in reason
    assert "60% LTV" in reason


def test_proxy_e_gav_6b_converts_to_2_4b_disqualifies():
    result, reason = apply_gate(True, True, 6_000_000_000, "AUM_ESTIMATED", proxy_type="gav")
    assert result == "disqualified"
    assert "$2.4B" in reason
    assert "exceeds ceiling" in reason


def test_proxy_e_raw_gav_never_compared_to_ceiling_directly():
    """The whole point of the haircut: a raw GAV figure ABOVE the $2B
    ceiling ($3.5B here) must still QUALIFY once converted (~$1.4B, within
    band) — proving the RAW number was never itself gated against the
    ceiling. If it had been, this would wrongly disqualify."""
    result, reason = apply_gate(True, True, 3_500_000_000, "AUM_ESTIMATED", proxy_type="gav")
    assert result == "qualified", (
        "raw GAV of $3.5B exceeds the $2B ceiling, but the CONVERTED ~$1.4B "
        "equity estimate is within band -- must qualify, not disqualify"
    )
    assert "$1.4B" in reason


def test_proxy_e_gav_below_floor_after_conversion_disqualifies():
    """A small GAV that converts to well under $200M is treated like any
    other non-floor AUM_ESTIMATED figure below the floor: disqualify (GAV is
    a real, current value once converted -- not a floor-type proxy)."""
    result, reason = apply_gate(True, True, 300_000_000, "AUM_ESTIMATED", proxy_type="gav")
    assert result == "disqualified"
    assert "$120M" in reason  # 300M * 0.4


def test_proxy_e_caller_supplied_basis_overrides_default():
    result, reason = apply_gate(True, True, 1_000_000_000, "AUM_ESTIMATED", proxy_type="gav",
                               proxy_basis="GAV per 2025 investor letter")
    assert "GAV per 2025 investor letter" in reason


# ---------------------------------------------------------------------------
# Tertiary proxy: unit-count-only (icp_scoring_v2_2.md "Tertiary proxy"
# section) — last resort when NO dollar figure of any kind was found, only
# a disclosed unit count. Same leverage haircut applied to a conservative
# per-unit gross estimate.
# ---------------------------------------------------------------------------
def test_tertiary_unit_count_only_proxy_estimates_and_qualifies():
    # 21,794 units (Kingbird's real disclosed unit count) x $120,000/unit
    # gross x 40% (1 - 60% haircut) = ~$1.045B -> qualifies.
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               unit_count=21_794)
    assert result == "qualified"
    assert "21,794 units" in reason
    assert "60% leverage haircut" in reason


def test_tertiary_unit_count_proxy_small_count_parks_below_floor_not_disqualifies():
    """FIX 21 (real-mode In-Rel Properties run): this test used to assert
    'disqualified', encoding the exact behavior being corrected -- a
    units/sqft-derived estimate is MANUFACTURED by this codebase from a
    hardcoded rate, never disclosed by the firm, so below the floor it
    must PARK for manual verification (same branch/reasoning as the
    floor-proxy case), not permanently delete the firm on a number the
    system computed."""
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               unit_count=500)
    # 500 * 120,000 * 0.4 = $24M -- below the floor, but a MANUFACTURED
    # estimate, not a disclosed figure -> park, not disqualify.
    assert result == "parked"
    assert "derived, not disclosed" in reason
    assert "verify manually" in reason


def test_tertiary_unit_count_proxy_caller_basis_overrides_default():
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               unit_count=21_794, proxy_basis="unit count per press release")
    assert "unit count per press release" in reason


def test_units_proxy_without_unit_count_falls_through_to_park():
    """proxy_type='units' with no unit_count at all (defensive: an
    inconsistent LLM extraction) must still PARK, not crash or silently
    qualify -- same discipline as the null-AUM defensive test below."""
    result, _ = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units")
    assert result == "parked"


# ---------------------------------------------------------------------------
# FIX 20 (real-mode In-Rel Properties run): "owns and manages over three
# million square feet" was previously written straight into aum_value_usd as
# if it were a dollar figure ($3,000,000) -- disqualifying a firm plausibly
# well inside the $200M-$2B band on a fabricated number. Square footage is a
# SEPARATE quantity from a residential unit count -- it converts at its own
# per-square-foot rate (PER_SQFT_GROSS_ESTIMATE_USD), never
# PER_UNIT_GROSS_ESTIMATE_USD (that rate is calibrated for a per-APARTMENT-
# UNIT gross value; applying it to a square-footage count would produce a
# wildly wrong, absurdly large estimate).
# ---------------------------------------------------------------------------
def test_tertiary_sqft_only_proxy_estimates_and_qualifies():
    # In-Rel Properties' real disclosed figure: 3,000,000 sq ft x $200/sq ft
    # gross x 40% (1 - 60% haircut) = $240M -> qualifies, comfortably inside
    # the $200M-$2B band, per the incident report's own expectation.
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               sqft_count=3_000_000)
    assert result == "qualified"
    assert "3,000,000 sq ft" in reason
    assert "60% leverage haircut" in reason
    assert "$3M" not in reason  # never the raw count misread as a dollar figure


def test_tertiary_sqft_proxy_never_uses_the_per_unit_rate():
    """The exact confusion this fix closes: applying PER_UNIT_GROSS_ESTIMATE_USD
    ($120,000) to a square-footage count instead of PER_SQFT_GROSS_ESTIMATE_USD
    ($200) would produce a wildly, absurdly large estimate (3,000,000 x
    $120,000 x 0.4 = $144B) that trivially "qualifies" for the wrong reason.
    Confirms the actual conversion used the per-sq-ft rate, not the per-unit
    one, by checking the qualifying figure lands in a sane range."""
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               sqft_count=3_000_000)
    assert result == "qualified"
    assert "$240M" in reason  # the correct, per-sq-ft-rate estimate
    assert "$144B" not in reason  # what the per-unit rate would have wrongly produced


def test_tertiary_sqft_proxy_small_count_parks_below_floor_not_disqualifies():
    """FIX 21 (real-mode In-Rel Properties run): a units/sqft-derived
    estimate is MANUFACTURED by this codebase from a hardcoded rate, never
    disclosed by the firm -- below the floor it must PARK for manual
    verification, not disqualify. The exact arithmetic named in the
    incident report: PER_SQFT_GROSS_ESTIMATE_USD=200 with a 60% haircut is
    $80/sq ft net, so the $200M floor sits at 2.5M sq ft -- a 2M sq ft firm
    computes to $160M and, before this fix, would have been disqualified
    outright on a number this codebase invented."""
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               sqft_count=2_000_000)
    # 2,000,000 x 200 x 0.4 = $160M -- below the floor, but MANUFACTURED,
    # not disclosed -> park, not disqualify.
    assert result == "parked"
    assert "$160M" in reason
    assert "2,000,000 sq ft" in reason
    assert "derived, not disclosed" in reason
    assert "verify manually" in reason


def test_tertiary_sqft_proxy_moderately_above_ceiling_parks_not_disqualifies():
    """FIX 21: above the ceiling, a units/sqft-derived estimate PARKS
    (needs manual verification) unless it clears the ceiling by a wide
    margin -- an estimate merely somewhat over $2B could easily be a rate-
    assumption artifact, not genuine oversized AUM."""
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               sqft_count=30_000_000)
    # 30,000,000 x 200 x 0.4 = $2.4B -- over the $2B ceiling, but well under
    # the 2x-ceiling ($4B) wide-margin threshold -> park, not disqualify.
    assert result == "parked"
    assert "derived, not disclosed" in reason


def test_tertiary_sqft_proxy_wide_margin_above_ceiling_disqualifies():
    """Only a units/sqft-derived estimate that clears the ceiling by a WIDE
    margin (2x, DERIVED_PROXY_CEILING_MULTIPLIER) disqualifies -- at that
    point no plausible rate error would bring it back under the ceiling."""
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               sqft_count=60_000_000)
    # 60,000,000 x 200 x 0.4 = $4.8B -- well over 2x the $2B ceiling -> disqualify.
    assert result == "disqualified"
    assert "wide margin" in reason


def test_tertiary_sqft_proxy_caller_basis_overrides_default():
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               sqft_count=3_000_000, proxy_basis="square footage per Crunchbase")
    assert "square footage per Crunchbase" in reason


def test_unit_count_takes_priority_when_both_unit_count_and_sqft_count_present():
    """Rare (a disclosure carrying both a unit count and a square-footage
    figure): unit_count is the pre-existing, more-specific residential
    path and takes priority if both are somehow present."""
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               unit_count=21_794, sqft_count=3_000_000)
    assert "21,794 units" in reason
    assert "sq ft" not in reason


def test_research_and_gate_threads_sqft_count_from_extraction_to_apply_gate(monkeypatch):
    """End-to-end wiring test (mocked extraction, matching a CORRECTLY-
    behaving model per the fixed schema): a research snippet whose only
    size signal is "over three million square feet" and no dollar figure
    anywhere must reach apply_gate() as proxy_type='units'/sqft_count, and
    must NEVER produce aum_value_usd == 3_000_000 (dollars) or a
    disqualification on that fabricated figure."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "search",
                        lambda query, max_results=6, topic="general", **_kw: [
                            _fake_result("https://www.crunchbase.com/organization/in-rel-properties",
                                        "In-Rel Properties owns and manages over three million "
                                        "square feet of office and retail space.")
                        ])
    _patch_extract_dependencies(monkeypatch)

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_ESTIMATED",
                "aum_value_usd": None, "proxy_type": "units", "proxy_basis": "",
                "unit_count": None, "sqft_count": 3_000_000, "sources": []},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("In-Rel Properties", progress=lambda *_a: None)
    assert result.icp_result == "qualified"
    assert result.research.get("aum_value_usd") != 3_000_000
    assert "$3M" not in result.icp_reason


# ---------------------------------------------------------------------------
# FIX 21 (real-mode In-Rel Properties run): _tertiary_proxy_conversion() is
# the shared helper apply_gate() and research_and_gate() both call, so the
# gate decision and the persisted display figure can never drift apart.
# ---------------------------------------------------------------------------
def test_tertiary_proxy_conversion_sqft():
    value, detail = icp_gate._tertiary_proxy_conversion(None, 3_000_000)
    assert value == 3_000_000 * icp_gate.PER_SQFT_GROSS_ESTIMATE_USD * (1 - icp_gate.GAV_LEVERAGE_HAIRCUT)
    assert "3,000,000 sq ft" in detail
    assert "60% leverage haircut" in detail


def test_tertiary_proxy_conversion_unit_count():
    value, detail = icp_gate._tertiary_proxy_conversion(21_794, None)
    assert value == 21_794 * icp_gate.PER_UNIT_GROSS_ESTIMATE_USD * (1 - icp_gate.GAV_LEVERAGE_HAIRCUT)
    assert "21,794 units" in detail


def test_tertiary_proxy_conversion_neither_present():
    assert icp_gate._tertiary_proxy_conversion(None, None) == (None, "")


# ---------------------------------------------------------------------------
# FIX 22 (real-mode In-Rel Properties run): aum_value_usd was doing two
# incompatible jobs -- driving the band comparison AND being the source of
# what a prospect reads. disclosed_figure/disclosed_label are now the ONLY
# fields deck_filler.py/email_drafter.py read; they carry EXACTLY what the
# firm published, verbatim, no arithmetic -- never a haircut or derived
# value (that's apply_gate()'s internal gate_value_usd, which never reaches
# this dict at all). For the units/sqft tertiary proxy specifically, the
# tile shows the RAW QUANTITY ("3,000,000 sq ft"), never a derived dollar
# estimate -- reversing FIX 21's "~$240M est." display, which this fix
# explicitly supersedes as the wrong choice.
# ---------------------------------------------------------------------------
def test_disclosed_figure_for_sqft_proxy_is_the_raw_quantity_not_a_dollar_estimate(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "search",
                        lambda query, max_results=6, topic="general", **_kw: [
                            _fake_result("https://www.crunchbase.com/organization/in-rel-properties",
                                        "In-Rel Properties owns and manages over three million "
                                        "square feet of office and retail space.")
                        ])
    _patch_extract_dependencies(monkeypatch)

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_ESTIMATED",
                "aum_value_usd": None, "proxy_type": "units", "proxy_basis": "",
                "disclosed_label": "of office and retail space under management",
                "quantity_qualifier": "over",
                "unit_count": None, "sqft_count": 3_000_000, "sources": []},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("In-Rel Properties", progress=lambda *_a: None)
    assert result.icp_result == "qualified"
    # FIX 24: the source said "OVER three million square feet" -- a floor,
    # not an exact figure. The qualifier must survive into disclosed_figure.
    assert result.research["disclosed_figure"] == "over 3,000,000 sq ft"
    assert result.research["disclosed_label"] == "of office and retail space under management"
    assert "$240M" not in result.research["disclosed_figure"]
    assert "est." not in result.research["disclosed_figure"]
    # aum_display (operator-facing views only) reflects the same verbatim pair
    assert result.research["aum_display"] == "over 3,000,000 sq ft (of office and retail space under management)"


# ---------------------------------------------------------------------------
# FIX 24 (real-mode In-Rel Properties run): a bare "3,000,000 sq ft" implies
# an exactness the firm never claimed when the source actually said "over
# three million square feet". quantity_qualifier preserves the firm's own
# claimed precision in disclosed_figure.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("qualifier,expected_prefix", [
    ("over", "over "),
    ("more_than", "more than "),
    ("more than", "more than "),
    ("at_least", "at least "),
    ("north_of", "north of "),
    ("approximately", "approximately "),
    ("about", "about "),
    ("roughly", "roughly "),
    ("~", "approximately "),
    ("", ""),
    (None, ""),
    ("some unrecognized phrase", ""),
])
def test_normalize_quantity_qualifier_maps_to_canonical_prefix(qualifier, expected_prefix):
    assert icp_gate._normalize_quantity_qualifier(qualifier) == expected_prefix


def test_disclosed_figure_and_label_prepends_qualifier_for_sqft():
    parsed = {"proxy_type": "units", "sqft_count": 3_000_000, "unit_count": None,
             "quantity_qualifier": "over", "disclosed_label": "of office and retail space"}
    figure, label = icp_gate._disclosed_figure_and_label(parsed)
    assert figure == "over 3,000,000 sq ft"
    assert label == "of office and retail space"


def test_disclosed_figure_and_label_prepends_qualifier_for_units():
    parsed = {"proxy_type": "units", "unit_count": 21_794, "sqft_count": None,
             "quantity_qualifier": "approximately", "disclosed_label": "units across the Southeast"}
    figure, label = icp_gate._disclosed_figure_and_label(parsed)
    assert figure == "approximately 21,794 units"


def test_disclosed_figure_and_label_no_qualifier_leaves_figure_unqualified():
    """A source that states the quantity as a plain, unqualified number must
    NOT gain an invented qualifier -- empty quantity_qualifier means empty
    prefix, not a default "approximately"."""
    parsed = {"proxy_type": "units", "sqft_count": 3_000_000, "unit_count": None,
             "quantity_qualifier": "", "disclosed_label": "square feet under management"}
    figure, _ = icp_gate._disclosed_figure_and_label(parsed)
    assert figure == "3,000,000 sq ft"


def test_disclosed_figure_and_label_missing_qualifier_key_defaults_to_unqualified():
    """Backward compatibility: a parsed dict that predates this field (no
    quantity_qualifier key at all) must behave exactly as before."""
    parsed = {"proxy_type": "units", "sqft_count": 3_000_000, "unit_count": None,
             "disclosed_label": "square feet under management"}
    figure, _ = icp_gate._disclosed_figure_and_label(parsed)
    assert figure == "3,000,000 sq ft"


def test_disclosed_label_falls_back_to_generic_honest_description_when_model_leaves_it_empty(monkeypatch):
    """The model's disclosed_label is best-effort (genuine source-grounded
    context) -- if it comes back empty, fall back to a GENERIC but honest
    description (never fabricated specifics), keyed to which quantity was
    actually used."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "search",
                        lambda query, max_results=6, topic="general", **_kw: [
                            _fake_result("https://example.com", "owns over three million square feet")
                        ])
    _patch_extract_dependencies(monkeypatch)

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_ESTIMATED",
                "aum_value_usd": None, "proxy_type": "units", "proxy_basis": "",
                "disclosed_label": "", "unit_count": None, "sqft_count": 3_000_000, "sources": []},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("Some Sqft Firm LLC", progress=lambda *_a: None)
    assert result.research["disclosed_figure"] == "3,000,000 sq ft"
    assert result.research["disclosed_label"] == "square feet under management"


def test_disclosed_figure_for_non_units_proxy_comes_from_the_models_own_extraction(monkeypatch):
    """A GAV/portfolio-value/fund-size/invested-capital proxy has no clean
    structured quantity to derive a figure from programmatically -- the
    model's own disclosed_figure/disclosed_label extraction is used
    directly (there's no equivalent to unit_count/sqft_count for these)."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "search",
                        lambda query, max_results=6, topic="general", **_kw: [
                            _fake_result("https://example.com", "$800M invested since 2015")
                        ])
    _patch_extract_dependencies(monkeypatch)

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_ESTIMATED",
                "aum_value_usd": 800_000_000, "proxy_type": "non_floor",
                "proxy_basis": "cumulative invested capital, proxy",
                "disclosed_figure": "$800M", "disclosed_label": "capital invested",
                "sources": []},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("Some Invested Capital Firm LLC", progress=lambda *_a: None)
    assert result.research["disclosed_figure"] == "$800M"
    assert result.research["disclosed_label"] == "capital invested"
    assert result.research["aum_display"] == "$800M (capital invested)"
    # proxy_basis is untouched -- no longer the source of the display label
    assert result.research["proxy_basis"] == "cumulative invested capital, proxy"


def test_qualified_firm_with_empty_disclosed_figure_prints_a_bug_warning(monkeypatch, capsys):
    """"There is always something disclosed to show... if it would be
    blank, that is a bug — report it rather than shipping a blank." A
    qualifying firm whose extraction somehow produced no disclosed_figure
    at all (an inconsistent/buggy extraction) must be visibly flagged, not
    silently shipped with a blank tile."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "search",
                        lambda query, max_results=6, topic="general", **_kw: [
                            _fake_result("https://example.com", "$800M invested since 2015")
                        ])
    _patch_extract_dependencies(monkeypatch)

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        # Inconsistent extraction: qualifies, but never supplied disclosed_figure.
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_ESTIMATED",
                "aum_value_usd": 800_000_000, "proxy_type": "non_floor",
                "proxy_basis": "cumulative invested capital, proxy",
                "disclosed_figure": "", "disclosed_label": "", "sources": []},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("Buggy Extraction Firm LLC", progress=lambda *_a: None)
    assert result.icp_result == "qualified"
    assert result.research["disclosed_figure"] == ""
    out = capsys.readouterr().out
    assert "[DISCLOSED FIGURE WARNING]" in out
    assert "bug" in out.lower()


def test_no_dollar_figure_at_all_value_null_parks():
    """'If neither a GAV/dollar figure nor a unit/sq-ft count exists after
    exhaustive search: AUM_UNVERIFIED, PARK' (icp_scoring_v2_2.md). With
    aum_value_usd=None and no unit_count either, the gate must PARK
    regardless of aum_tag."""
    result, reason = apply_gate(True, True, None, "AUM_UNVERIFIED")
    assert result == "parked"
    assert "not found" in reason


def test_null_aum_value_parks_even_if_tag_is_estimated_by_mistake():
    """Defensive: if aum_value_usd is somehow None but aum_tag claims
    AUM_ESTIMATED (an inconsistent LLM extraction), the gate must still PARK
    rather than crash or silently qualify — value_usd null is the actual
    signal, not the tag string."""
    result, _ = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="floor")
    assert result == "parked"


# ---------------------------------------------------------------------------
# Conditions 1 (firm type) and 3 (region) — checked before AUM
# ---------------------------------------------------------------------------
def test_wrong_firm_type_disqualifies_regardless_of_aum():
    result, reason = apply_gate(False, True, 500_000_000, "AUM_CONFIRMED")
    assert result == "disqualified"
    assert "firm type" in reason.lower()


def test_outside_north_america_disqualifies_regardless_of_aum():
    result, reason = apply_gate(True, False, 500_000_000, "AUM_CONFIRMED")
    assert result == "disqualified"
    assert "north america" in reason.lower()


def test_firm_type_checked_before_aum_even_when_aum_unverified():
    """Wrong firm type disqualifies even with no AUM data at all — Condition
    1 short-circuits before Condition 2 is ever evaluated."""
    result, reason = apply_gate(False, True, None, "AUM_UNVERIFIED")
    assert result == "disqualified"
    assert "firm type" in reason.lower()


# ---------------------------------------------------------------------------
# Every result string carries the auditable proxy basis (icp_scoring_v2_2.md
# "icp_reason: one short line that ALWAYS includes the proxy basis when
# applicable")
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("aum_value,aum_tag,proxy_type,proxy_basis", [
    (800_000_000, "AUM_ESTIMATED", "non_floor", "invested capital proxy"),
    # FIX 8 (real-mode Kairos run): invested capital is a specific, dated
    # figure, not floor-proxy LANGUAGE -- reclassified non_floor, so
    # below-$200M disqualifies rather than parks (see
    # test_proxy_b_invested_capital_below_200m_disqualifies_not_parks).
    (150_000_000, "AUM_ESTIMATED", "non_floor", "invested capital proxy"),
    (150_000_000, "AUM_ESTIMATED", "non_floor", "portfolio value proxy"),
    (8_000_000_000, "AUM_ESTIMATED", "non_floor", "fund size proxy"),
])
def test_every_estimated_reason_carries_proxy_basis(aum_value, aum_tag, proxy_type, proxy_basis):
    _result, reason = apply_gate(True, True, aum_value, aum_tag, proxy_type, proxy_basis)
    assert proxy_basis.split(",")[0].split(" proxy")[0].strip() in reason or proxy_basis in reason, (
        f"proxy basis {proxy_basis!r} not auditable in reason: {reason!r}"
    )


# ---------------------------------------------------------------------------
# Infrastructure vs. research parks (HARD RULE 15 / §11.1): a quota/rate-limit
# failure must NEVER silently become a bogus "AUM unverified" park.
# ---------------------------------------------------------------------------
def test_infrastructure_error_is_not_a_research_park():
    """InfrastructureError is a distinct exception type from a normal park —
    callers (src/pipeline.py) must catch it separately and tag
    park_source='infrastructure', never confusing a dead Tavily/LLM/Apollo
    with a genuine 'couldn't verify this firm's AUM' outcome."""
    err = InfrastructureError("Tavily", "HTTP 432 quota exceeded")
    assert err.service == "Tavily"
    assert "432" in err.message
    # It must be a RuntimeError subclass (so it doesn't silently vanish into
    # a broad `except Exception` that treats it like any other park) — and
    # a caller checking isinstance(e, InfrastructureError) is how pipeline.py
    # actually distinguishes it; confirm that contract here.
    assert isinstance(err, RuntimeError)


def test_pipeline_tags_infra_failure_as_infrastructure_not_research(monkeypatch):
    """End-to-end of the distinction through src/pipeline.py: when
    icp_gate.research_and_gate raises InfrastructureError, the firm row must
    be parked with park_source='infrastructure' (auto-re-researched on
    resume), never park_source='research' (a genuine, settled verification
    failure) or aum_unverified."""
    from src import dbc, pipeline

    def _boom(firm_name, progress=None):
        raise InfrastructureError("Tavily", "HTTP 432 quota exceeded")

    # pipeline.py does `from . import icp_gate` then calls icp_gate.research_and_gate
    # at call time — patching the attribute on the module object it references
    # (pipeline.icp_gate IS src.icp_gate) is what actually intercepts the call.
    monkeypatch.setattr(pipeline.icp_gate, "research_and_gate", _boom)

    run = dbc.create_wc_run("single_firm", query_firm="Infra Failure Test Co")
    firm = dbc.create_wc_firm(run["id"], "Infra Failure Test Co")
    breaker = pipeline.CircuitBreaker()

    result = pipeline.process_referral_firm(firm["id"], breaker)
    assert result["outcome"] == "parked"
    assert result["park_source"] == "infrastructure"

    row = dbc.get_wc_firm(firm["id"])
    assert row["park_source"] == "infrastructure"
    assert row["park_reason"] != "aum_unverified"


# ---------------------------------------------------------------------------
# Research depth/breadth (QA finding, real-mode dry run): a real run against
# Kingbird Investment Management parked with "AUM not found" after only 4
# searches, all against kingbird.com/its LinkedIn company page, despite
# Kingbird publicly disclosing ~$4.4B GAV / 21,794 units / 1.8M sq ft
# elsewhere online. _run_research must escalate to wider, third-party-
# leaning angles when the core pass finds no size signal, rather than
# parking with search budget still unused. Tests exercise _run_research
# directly (real-mode-path internals, TEST_MODE bypassed) with
# search_client.search mocked — no real network.
# ---------------------------------------------------------------------------
def _fake_result(url: str, content: str, title: str = "") -> dict:
    return {"title": title, "url": url, "content": content, "score": 0.5}


def test_escalation_runs_when_core_pass_finds_no_size_signal(monkeypatch):
    from lib_snapshot.researcher import search_client

    calls: list[str] = []

    def _fake_search(query, max_results=6, topic="general", **_kw):
        calls.append(query)
        if len(calls) <= len(icp_gate._CORE_ANGLES):
            # Core angles: generic firm-site content, no size signal at all.
            return [_fake_result("https://examplefirm.com/about",
                                 "ExampleFirm is a real estate investment manager.")]
        # Escalation angle: a THIRD-PARTY source discloses the actual GAV.
        return [_fake_result("https://re-industry-news.example/examplefirm-gav",
                             "ExampleFirm reports a gross asset value of $4.4 billion "
                             "across its multifamily portfolio.")]

    monkeypatch.setattr(search_client, "search", _fake_search)
    results, total_calls = icp_gate._run_research("ExampleFirm", progress=lambda *_a: None)

    assert total_calls > len(icp_gate._CORE_ANGLES), (
        "must escalate beyond the core angles when no size signal was found in them"
    )
    assert icp_gate._has_size_signal(results), (
        "the escalation-found GAV signal must actually be present in the aggregated results"
    )
    assert any("re-industry-news.example" in r.get("url", "") for r in results), (
        "escalation must genuinely reach third-party content, not just repeat firm-site queries"
    )


def test_no_escalation_when_core_pass_already_finds_a_signal(monkeypatch):
    from lib_snapshot.researcher import search_client

    def _fake_search(query, max_results=6, topic="general", **_kw):
        return [_fake_result("https://examplefirm.com/about",
                             "ExampleFirm manages $1.1 billion in assets under management.")]

    monkeypatch.setattr(search_client, "search", _fake_search)
    _results, total_calls = icp_gate._run_research("ExampleFirm", progress=lambda *_a: None)

    assert total_calls == len(icp_gate._CORE_ANGLES), (
        "a size signal already in the core pass must skip escalation entirely -- no wasted search budget"
    )


def test_escalation_stops_as_soon_as_a_signal_is_found(monkeypatch):
    from lib_snapshot.researcher import search_client

    calls: list[str] = []

    def _fake_search(query, max_results=6, topic="general", **_kw):
        calls.append(query)
        if len(calls) <= len(icp_gate._CORE_ANGLES):
            return [_fake_result("https://examplefirm.com/about", "No numbers here at all.")]
        # The FIRST escalation angle already finds a signal.
        return [_fake_result("https://news.example/examplefirm", "ExampleFirm's GAV stands at $2 billion.")]

    monkeypatch.setattr(search_client, "search", _fake_search)
    _results, total_calls = icp_gate._run_research("ExampleFirm", progress=lambda *_a: None)

    assert total_calls == len(icp_gate._CORE_ANGLES) + 1, (
        "escalation must stop at the FIRST angle that finds a signal, not burn through all of them"
    )


def test_escalation_respects_the_12_search_budget_cap(monkeypatch):
    from lib_snapshot.researcher import search_client

    def _fake_search(query, max_results=6, topic="general", **_kw):
        return [_fake_result("https://examplefirm.com/about", "Nothing findable, ever.")]

    monkeypatch.setattr(search_client, "search", _fake_search)
    _results, total_calls = icp_gate._run_research("ExampleFirm", progress=lambda *_a: None)

    assert total_calls <= config.MAX_SEARCH_CALLS_PER_FIRM, (
        "HARD RULE 9: never exceed the 12-search-per-firm budget, even while escalating"
    )


def test_extract_prioritizes_signal_bearing_results_so_escalation_finding_survives_truncation(monkeypatch):
    """The bug this fix would otherwise silently reintroduce: _extract only
    shows the LLM a bounded number of results. An escalation-angle finding
    is APPENDED after the (larger) core-angle result set — without
    prioritizing signal-bearing results first, it could be truncated out
    before the LLM ever sees it, silently defeating the whole escalation fix."""
    captured_prompts: list[str] = []

    class _FakeUsage:
        total_tokens = 100
        cost_usd = 0.001

    def _fake_complete_json_retry(prompt, **_kw):
        captured_prompts.append(prompt)
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_ESTIMATED",
                "aum_value_usd": 4_400_000_000, "proxy_type": "gav",
                "proxy_basis": "", "sources": []}, _FakeUsage())

    from lib_snapshot import models
    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)
    # This test is about signal-bearing-result prioritization, not FIX 1/2's
    # full-text/ADV retrieval -- mock those out so it stays a pure, offline
    # unit test (30 examplefirm.com hits would otherwise be inferred as the
    # "own domain" and trigger a real search_client.extract() call; every
    # real-mode _extract() call also now does a real SEC IAPD lookup).
    monkeypatch.setattr(search_client, "extract", lambda urls: {})
    # FIX 28 adversarial review finding: _extract() now calls
    # _lookup_sec_adv_hits()/_classify_sec_adv_hits() directly (not
    # _lookup_sec_adv, which stays a thin wrapper around them), so mocking
    # only _lookup_sec_adv left this test making a REAL, live SEC IAPD
    # network call on every run -- exactly the "stays a pure, offline unit
    # test" guarantee this comment already claimed. Mock at the level
    # _extract() actually calls (matching _patch_extract_dependencies's own
    # pattern for every other _extract()-calling test in this file).
    monkeypatch.setattr(icp_gate, "_lookup_sec_adv_hits", lambda firm_name: [])
    monkeypatch.setattr(icp_gate, "_classify_sec_adv_hits", lambda firm_name, hits: (None, [], []))
    monkeypatch.setattr(icp_gate, "_lookup_sec_adv", lambda firm_name: (None, []))

    # 30 generic no-signal results (more than the 24-result display cap),
    # followed by ONE signal-bearing result at the very end -- simulating an
    # escalation-angle finding appended after a full core-angle batch.
    padding = [{"title": "", "url": f"https://examplefirm.com/page{i}",
               "content": "Generic firm page content with no size figures."}
              for i in range(30)]
    escalation_hit = {"title": "GAV disclosure", "url": "https://news.example/examplefirm-gav",
                      "content": "ExampleFirm discloses a gross asset value of $4.4 billion."}

    icp_gate._extract("ExampleFirm", padding + [escalation_hit])

    assert len(captured_prompts) == 1
    assert "examplefirm-gav" in captured_prompts[0], (
        "the signal-bearing escalation result must survive into the LLM prompt, "
        "not be truncated out by the display cap"
    )


# ---------------------------------------------------------------------------
# Retrieval-layer fixes (Kairos Real Estate Partners real-mode review):
# FIX 1 full-document retrieval, FIX 2 deterministic SEC ADV lookup, FIX 3
# source-cap ordering, FIX 4 research payload cache/replay. Gate thresholds
# and the qualify/park decision (apply_gate) are untouched by any of this.
# ---------------------------------------------------------------------------
class _FakeUsage:
    total_tokens = 100
    cost_usd = 0.001


def _fake_complete_json_retry_factory(captured_prompts):
    def _fake(prompt, **_kw):
        captured_prompts.append(prompt)
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_UNVERIFIED",
                "aum_value_usd": None, "proxy_type": "", "proxy_basis": "", "sources": []},
               _FakeUsage())
    return _fake


# --- _infer_own_domain ------------------------------------------------------
def test_infer_own_domain_picks_the_most_frequent_non_third_party_domain():
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(6)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(4)]
        + [_fake_result("https://www.linkedin.com/company/kairos", "x") for _ in range(5)]
        + [_fake_result("https://kimc.com/home", "x")]
    )
    assert icp_gate._infer_own_domain(results) == "kairosrep.com"


def test_infer_own_domain_requires_at_least_two_hits():
    results = [_fake_result("https://onlyone.example/page", "x")]
    assert icp_gate._infer_own_domain(results) is None


def test_infer_own_domain_never_picks_a_known_third_party_domain():
    results = [_fake_result("https://www.linkedin.com/company/foo", "x") for _ in range(10)]
    assert icp_gate._infer_own_domain(results) is None


def test_infer_own_domain_does_not_conflate_two_different_similarly_named_firms():
    """The exact confusion a real run surfaced: 'Kairos Real Estate Partners'
    (kairosrep.com) and 'Kairos Investment Management Company' (kimc.com)
    are different real companies. kimc.com must not be picked as the
    target's own domain just because both share the word 'kairos'."""
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(4)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(3)]
        + [_fake_result("https://kimc.com/home", "x")]
        + [_fake_result("https://kimc.com/strategies", "x")]
    )
    # Tied at 2 vs 2 would be ambiguous by count alone in a smaller example,
    # but with kairosrep.com clearly dominant (7 vs 2) it must win.
    assert icp_gate._infer_own_domain(results) == "kairosrep.com"


def test_infer_own_domain_breaks_a_distinct_page_tie_using_raw_search_repetition():
    """Regression for a REAL failure this fix's own live verification hit:
    at the DEDUPLICATED-URL level, kairosrep.com and kimc.com can genuinely
    tie (3 distinct pages each) -- resolved incorrectly, by insertion order,
    in kimc.com's favor. _infer_own_domain must be called on the
    UN-deduplicated results (what _extract() actually passes), which breaks
    the tie correctly by counting how many search angles resurfaced each
    domain (the real firm's own site keeps getting rediscovered by
    differently-worded angles), not just how many distinct pages exist."""
    # kairosrep.com: 3 distinct pages, resurfaced across multiple angles (6 hits)
    kairosrep_hits = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(2)]
        + [_fake_result("https://kairosrep.com/factsheet.pdf", "x")]
    )
    # kimc.com: 3 distinct pages, each found by exactly one angle
    kimc_hits = [
        _fake_result("https://kimc.com/associate-asset-management", "x"),
        _fake_result("https://kimc.com", "x"),
        _fake_result("https://kimc.com/home-2222", "x"),
    ]
    assert icp_gate._infer_own_domain(kairosrep_hits + kimc_hits) == "kairosrep.com"


# --- FIX 28 sub-change A: _domain_name_similarity ----------------------------
def test_domain_name_similarity_full_token_containment():
    """'kingbirdinvestments' contains the firm's own meaningful token
    'kingbird', with the leftover ('investments') explained by the firm's
    own (otherwise-generic) 'investment' token."""
    score = icp_gate._domain_name_similarity("kingbirdinvestments.com", "Kingbird Investment Management")
    assert score >= icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD


def test_domain_name_similarity_initialism_match():
    """The exact real-mode evidence: 'kairosrep' = Kairos (spelled out) +
    R/E/P (initials of 'Real Estate Partners')."""
    score = icp_gate._domain_name_similarity("kairosrep.com", "Kairos Real Estate Partners")
    assert score >= icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD


def test_domain_name_similarity_survives_an_llc_suffix_on_the_firm_name():
    """QA finding while implementing this fix: appending ', LLC' to the
    firm name (some call sites do, some don't) must not change the
    answer -- 'LLC' is a pure corporate-structure word, never part of a
    real initialism/domain derivation."""
    score = icp_gate._domain_name_similarity("kairosrep.com", "Kairos Real Estate Partners, LLC")
    assert score >= icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD


def test_domain_name_similarity_hyphen_normalization():
    score = icp_gate._domain_name_similarity("in-rel.com", "In-Rel Properties")
    assert score >= icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD


def test_domain_name_similarity_rejects_a_confuser_sharing_only_the_brand_word():
    """Regression guard: kairoscm.com/kairosgroupllc.com are the EXISTING
    _identify_other_entity_domains confuser examples -- they contain the
    firm's brand word "kairos" too, but the leftover ("cm"/"groupllc")
    explains nothing in the firm's own name. Must NOT score as a strong
    match (that would wrongly protect a genuinely different entity's
    domain from being dropped -- sub-change B)."""
    for domain in ("kairoscm.com", "kairosgroupllc.com"):
        score = icp_gate._domain_name_similarity(domain, "Kairos Real Estate Partners, LLC")
        assert score < icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD, f"{domain} scored {score}"


def test_domain_name_similarity_low_for_unrelated_domains():
    """The VM run's own noise: kimc.com (the wrong entity), merriam-
    webster.com ("kairos" is an English dictionary word), youtube.com --
    none are name-derived from the target at all."""
    for domain in ("kimc.com", "merriam-webster.com", "youtube.com"):
        score = icp_gate._domain_name_similarity(domain, "Kairos Real Estate Partners")
        assert score < icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD, f"{domain} scored {score}"


def test_domain_name_similarity_returns_zero_for_empty_input():
    assert icp_gate._domain_name_similarity("", "Kairos Real Estate Partners") == 0.0
    assert icp_gate._domain_name_similarity("kairosrep.com", "") == 0.0


# --- Adversarial review follow-up: severe false positives found and fixed --
def test_domain_name_similarity_rejects_ampersand_short_segment_collisions():
    """Adversarial review finding (severe, live-reproduced): src/matching/
    normalize.py expands "&" to " and " before tokenizing, so "H&R" became
    bare single-character tokens ["h","r"] -- trivially "contained in"
    almost any domain of nontrivial length. harborview.com/harmony.com/
    hershey.com all scored a false 100.0 against "H&R Real Estate
    Investment Trust" before this fix. The firm's genuine own domain
    (hrreit.com, matching via the full-initials-of-every-token mechanism,
    unaffected by this fix) must still score strong."""
    firm = "H&R Real Estate Investment Trust"
    for domain in ("harborview.com", "harmony.com", "hershey.com", "worldhistory.com",
                  "chartered.com", "birthday.com"):
        score = icp_gate._domain_name_similarity(domain, firm)
        assert score < icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD, f"{domain} scored {score}"
    assert icp_gate._domain_name_similarity("hrreit.com", firm) >= icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD


def test_domain_name_similarity_rejects_coincidental_substring_of_a_generic_word():
    """Adversarial review finding (severe, live-reproduced): the original
    leftover check accepted ANY substring overlap in either direction, so a
    leftover coincidentally matching part of a generic suffix word (not the
    whole word) scored a false 100 -- "pit" inside "capital" (kairospit.com),
    "art"/"ers" inside "partners" (kairosart.com/kairosers.com) -- for an
    ORDINARY firm name with no unusual punctuation at all."""
    firm = "Kairos Capital Partners"
    for domain in ("kairospit.com", "kairosart.com", "kairosers.com", "kairospita.com"):
        score = icp_gate._domain_name_similarity(domain, firm)
        assert score < icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD, f"{domain} scored {score}"


def test_domain_name_similarity_rejects_a_single_letter_leftover_matching_any_word():
    """Adversarial review finding: a single-character LEFTOVER (after
    removing the anchor) was treated as "explained" whenever that one
    letter appeared anywhere inside any remaining token -- true for most
    common letters. Reproduced against the function's own docstring
    example, "Kingbird Investment Management": 'm' is in "management",
    'i' is in "investment", so kingbirdm.com/kingbirdi.com both falsely
    scored 100 pre-fix."""
    firm = "Kingbird Investment Management"
    for domain in ("kingbirdm.com", "kingbirdi.com"):
        score = icp_gate._domain_name_similarity(domain, firm)
        assert score < icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD, f"{domain} scored {score}"
    # kingbirdx.com ('x' in neither word) was always correctly weak -- a
    # sanity check that this fix didn't just blanket-disable the mechanism.
    assert icp_gate._domain_name_similarity("kingbirdx.com", firm) < icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD


def test_domain_name_similarity_pure_corporate_suffix_generalizes_beyond_llc():
    """Adversarial review finding: the ONLY regression test for
    _PURE_CORPORATE_SUFFIX's exclusion used the literal word 'LLC' --
    every other member (lp, inc, corp, corporation, co, ltd, limited, the,
    company) was unexercised, so a narrow 'llc'-only special case could
    have passed unnoticed. Directly exercises the other members."""
    domain = "kairosrep.com"
    for suffix in (", L.P.", ", Inc.", ", Incorporated", ", Corp.", ", Corporation",
                  ", Co.", ", Ltd.", ", Limited", " The", ", Company"):
        firm = f"Kairos Real Estate Partners{suffix}"
        score = icp_gate._domain_name_similarity(domain, firm)
        assert score >= icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD, f"{firm!r} scored {score}"


# --- FIX 28 sub-change A/C: _resolve_own_domain / _infer_own_domain ---------
def test_infer_own_domain_regression_vm_payload_distribution(monkeypatch):
    """THE regression test: the exact domain hit-counts from the deployed-
    VM run that produced the wrong answer (kimc.com 5, kairosrep.com 3,
    merriam-webster.com 5, youtube.com 4) -- do not paraphrase these
    counts, they are the real evidence. Before this fix, own_domain
    resolved to kimc.com (pure hit-count plurality); it must now resolve
    to kairosrep.com, the name-derived match, regardless of its lower
    count."""
    results = (
        [_fake_result(f"https://kimc.com/page-{i}", "x") for i in range(5)]
        + [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(3)]
        + [_fake_result(f"https://www.merriam-webster.com/dictionary/kairos-{i}", "x") for i in range(5)]
        + [_fake_result(f"https://www.youtube.com/watch?v=fake{i}", "x") for i in range(4)]
    )
    assert icp_gate._infer_own_domain(results, "Kairos Real Estate Partners") == "kairosrep.com"


def test_infer_own_domain_without_firm_name_keeps_old_hit_count_behavior():
    """Backward compatibility: existing callers/tests that never pass a
    firm_name (name-similarity unavailable) keep the exact prior
    hit-count-plurality behavior -- confirmed here using the SAME VM
    distribution, which would (correctly, for THIS call shape) still
    resolve to the hit-count winner when no name signal is available at
    all to override it."""
    results = (
        [_fake_result(f"https://kimc.com/page-{i}", "x") for i in range(5)]
        + [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(3)]
    )
    assert icp_gate._infer_own_domain(results) == "kimc.com"


def test_resolve_own_domain_parks_when_two_strong_matches_are_comparable():
    """Sub-change C: two domains BOTH scoring as strong name matches (a
    genuine same-brand-word tie between two real-looking candidates) must
    park, not silently pick one."""
    results = (
        [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(3)]
        + [_fake_result(f"https://kairospartners.com/page-{i}", "x") for i in range(3)]
    )
    resolution = icp_gate._resolve_own_domain(results, "Kairos Real Estate Partners")
    assert resolution.ambiguous is True
    assert resolution.domain is None
    assert set(resolution.candidates) == {"kairosrep.com", "kairospartners.com"}


def test_resolve_own_domain_parks_when_hitcount_and_moderate_name_signal_disagree():
    """Sub-change C's second trigger: no domain clears the STRONG
    threshold, but a domain with a real (moderate) name signal disagrees
    with the hit-count winner, and the hit-count winner isn't
    overwhelmingly ahead -- neither signal dominates, so park.
    kairoscm.com scores 70 (moderate, below the 85 strong threshold) for
    this firm name -- verified directly before writing this test."""
    firm = "Kairos Real Estate Partners"
    assert icp_gate._OWN_DOMAIN_MODERATE_NAME_MATCH <= icp_gate._domain_name_similarity(
        "kairoscm.com", firm) < icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD
    results = (
        [_fake_result(f"https://kimc.com/page-{i}", "x") for i in range(3)]
        + [_fake_result(f"https://kairoscm.com/page-{i}", "x") for i in range(2)]
    )
    resolution = icp_gate._resolve_own_domain(results, firm)
    assert resolution.ambiguous is True
    assert resolution.domain is None


def test_resolve_own_domain_does_not_park_when_hitcount_winner_dominates():
    """The disagreement trigger must not fire just because SOME other
    domain has a nonzero name score -- if the hit-count winner is
    overwhelmingly ahead, that's not a real conflict."""
    firm = "Kairos Real Estate Partners"
    results = (
        [_fake_result(f"https://kimc.com/page-{i}", "x") for i in range(10)]
        + [_fake_result("https://kairoscm.com/page-0", "x"), _fake_result("https://kairoscm.com/page-1", "x")]
    )
    resolution = icp_gate._resolve_own_domain(results, firm)
    assert resolution.ambiguous is False


def test_resolve_own_domain_dominance_override_wins_outright_with_unequal_strong_counts():
    """Adversarial review finding (test-coverage gap, high severity): every
    existing "comparable strong matches" test used EQUAL hit counts (3 vs
    3), so the 2x hit-count-dominance override added mid-implementation
    (after the live Atlas Real Estate run surfaced this exact need) was
    completely unexercised -- deleting it outright would not have failed
    any existing test. This directly reproduces the real Atlas shape with
    a SYNTHETIC pair of domains that BOTH score a clean 100 (not just
    "comparable"), at meaningfully unequal hit counts, and confirms the
    dominant one wins outright rather than parking."""
    firm = "Kairos Real Estate Partners"
    assert icp_gate._domain_name_similarity("kairosrep.com", firm) == 100.0
    assert icp_gate._domain_name_similarity("kairospartners.com", firm) == 100.0
    results = (
        [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(6)]
        + [_fake_result(f"https://kairospartners.com/page-{i}", "x") for i in range(2)]
    )
    resolution = icp_gate._resolve_own_domain(results, firm)
    assert resolution.ambiguous is False
    assert resolution.domain == "kairosrep.com"


# --- FIX 28 sub-change B: strong name matches are never droppable -----------
def test_identify_other_entity_domains_never_drops_a_strong_name_match():
    """The exact real-mode bug: own_domain resolved (wrongly, pre-fix) to
    kimc.com, and _identify_other_entity_domains then dropped the
    TARGET's own site, kairosrep.com, because it shares the brand token
    "kairos" with the wrong anchor. A domain scoring a strong name match
    must never be dropped, regardless of which domain is passed as the
    (possibly wrong) own_domain anchor."""
    results = (
        [_fake_result(f"https://kimc.com/page-{i}", "x") for i in range(5)]
        + [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(3)]
    )
    other = icp_gate._identify_other_entity_domains(results, "kimc.com", "Kairos Real Estate Partners")
    assert "kairosrep.com" not in other


def test_identify_other_entity_domains_still_drops_a_real_confuser_under_the_correct_anchor():
    """Regression guard: sub-change B's protection must not blanket-
    disable the EXISTING brand-token-sharing detection for genuinely
    different entities -- kairoscm.com (a real confuser example, see
    _identify_other_entity_domains's own docstring) must still be
    droppable even under the CORRECT own_domain anchor."""
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(2)]
        + [_fake_result("https://kairoscm.com", "x")]
    )
    other = icp_gate._identify_other_entity_domains(results, "kairosrep.com",
                                                     "Kairos Real Estate Partners, LLC")
    assert "kairoscm.com" in other


def test_identify_other_entity_domains_still_drops_a_low_hit_count_strong_name_match():
    """Adversarial review finding (severe, live-reproduced): the strong-
    name-match protection applied the >=85 threshold with NO reference to
    hit count at all -- a genuinely different company's own site
    (kairosestates.com, a single incidental bare-root hit, scoring a full
    match via the coincidental-generic-word-pluralization residual
    _domain_name_similarity's own docstring documents and accepts as an
    inherent trade-off) was shielded from exclusion even while own_domain
    resolved correctly and confidently with 8x the corroborating hits.
    Requires the candidate's own hit count to be at least half of
    own_domain's before granting the exemption."""
    results = (
        [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(8)]
        + [_fake_result("https://kairosestates.com", "$3.1B in assets under management")]
    )
    assert icp_gate._domain_name_similarity(
        "kairosestates.com", "Kairos Real Estate Partners") >= icp_gate._OWN_DOMAIN_NAME_MATCH_THRESHOLD, (
        "sanity check: this domain must still score a strong match for the test to be meaningful"
    )
    other = icp_gate._identify_other_entity_domains(results, "kairosrep.com", "Kairos Real Estate Partners")
    assert "kairosestates.com" in other


def test_identify_other_entity_domains_protects_a_strong_match_with_comparable_hits():
    """Regression guard for the fix above: the hit-count corroboration
    requirement must not become a blanket re-disabling of sub-change B --
    a strong name match with REAL, comparable search-frequency support
    (not just an incidental single mention) must still be protected."""
    results = (
        [_fake_result(f"https://kimc.com/page-{i}", "x") for i in range(5)]
        + [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(3)]
    )
    other = icp_gate._identify_other_entity_domains(results, "kimc.com", "Kairos Real Estate Partners")
    assert "kairosrep.com" not in other


# --- FIX 28 sub-change D: SEC ADV rejection similarity signal ---------------
def test_classify_sec_adv_hits_flags_a_suspiciously_close_rejected_name(monkeypatch):
    payload = {"hits": {"hits": [{"_source": {
        "firm_source_id": "555555", "firm_name": "KAIROS REAL ESTATE PARTNER",  # one word off
        "firm_other_names": [], "firm_ia_scope": "ACTIVE", "firm_ia_address_details": "PA",
    }}]}}
    match, rejected_crds, rejected_details = icp_gate._classify_sec_adv_hits(
        "Kairos Real Estate Partners", payload["hits"]["hits"])
    assert match is None
    assert rejected_crds == ["555555"]
    assert rejected_details[0]["similarity_to_target"] >= icp_gate._SEC_ADV_SUSPICIOUS_REJECTION_THRESHOLD


def test_classify_sec_adv_hits_does_not_flag_the_real_confirmed_different_entity(monkeypatch):
    """Regression guard: the REAL live evidence for this fix (CRD 161321,
    registered as KAIROS/KIMC/"Redwood-Kairos Real Estate Partners") is a
    genuinely different, confirmed-different entity -- verified via a live
    SEC ADV query during this fix's own diagnosis (similarity 87.1,
    comfortably below the suspicious threshold). Must NOT be flagged as
    suspicious; the original bug report's claim that this CRD was the
    target's own filing did not hold up under direct verification."""
    match, rejected_crds, rejected_details = icp_gate._classify_sec_adv_hits(
        "Kairos Real Estate Partners, LLC", _KAIROS_LIKE_SEC_RESPONSE["hits"]["hits"])
    assert match is None
    assert rejected_crds == ["161321"]
    assert rejected_details[0]["similarity_to_target"] < icp_gate._SEC_ADV_SUSPICIOUS_REJECTION_THRESHOLD


def test_lookup_sec_adv_unchanged_behavior_after_the_hits_classify_refactor(monkeypatch):
    """_lookup_sec_adv's own observable 2-tuple contract must be byte-for-
    byte unchanged after being refactored into a thin wrapper around
    _lookup_sec_adv_hits()/_classify_sec_adv_hits() (FIX 28)."""
    monkeypatch.setattr(icp_gate.requests, "get",
                        lambda *a, **kw: _FakeSecResponse(200, _KAIROS_LIKE_SEC_RESPONSE))
    match, rejected_crds = icp_gate._lookup_sec_adv("Kairos Real Estate Partners, LLC")
    assert match is None
    assert rejected_crds == ["161321"]


def test_classify_sec_adv_hits_similarity_restricted_to_brand_sharing_names():
    """Adversarial review finding: similarity_to_target originally took the
    MAX across a hit's firm_name plus ALL firm_other_names, not restricted
    to whichever name actually shares a brand token with the target (the
    reason the hit was flagged "plausibly confusable" in the first place).
    An other_name sharing zero brand tokens with the target could
    otherwise drive a "suspiciously close" signal disconnected from why
    the hit was rejected. Confirmed live: a firm name built entirely from
    generic words could cross the 95 threshold via an unrelated
    other_name while the actual brand-token-sharing name scored much
    lower -- restricting the search to brand-sharing names only closes
    this and also fixes which name is shown as the ambiguity candidate."""
    payload = {"hits": {"hits": [{"_source": {
        "firm_source_id": "777777", "firm_name": "Kairos Something Else Entirely",
        "firm_other_names": ["Totally Unrelated Real Estate Partners LLC"],
    }}]}}
    match, rejected_crds, rejected_details = icp_gate._classify_sec_adv_hits(
        "Kairos Real Estate Partners", payload["hits"]["hits"])
    assert match is None
    assert rejected_crds == ["777777"]
    # Only "Kairos Something Else Entirely" shares the brand token "kairos" --
    # the unrelated other_name must never be considered, even though it is
    # a much closer textual match to the target's generic words.
    assert rejected_details[0]["best_name"] == "Kairos Something Else Entirely"


# --- FIX 28 sub-change C/D: ambiguity park reaches _extract() end-to-end ----
def test_extract_parks_on_a_suspicious_adv_rejection_even_with_a_resolved_own_domain(monkeypatch):
    """Adversarial review finding (high severity, live-reproduced): the
    original "suspicious_adv_rejection AND NOT own_domain" gate meant a
    CONFIDENTLY-but-WRONGLY-resolved own_domain (a domain that scores a
    full, syntactic match yet isn't actually the target's site) completely
    silenced a simultaneous near-exact SEC ADV rejection. The two signals
    are orthogonal and neither validates the other -- a suspicious
    rejection must park regardless of how confident own_domain looks."""
    llm_calls = []

    def _fail_if_called(*a, **kw):
        llm_calls.append(1)
        raise AssertionError("the LLM must never be called when sub-change D flags a suspicious rejection")

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fail_if_called)

    # A clean, single, non-ambiguous own_domain resolution (no rival domain
    # at all) -- the exact "confidently-but-possibly-wrong" shape the
    # finding describes.
    results = [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(5)]
    rejected_adv_details = [{
        "crd": "555555", "names": ["Kairos Real Estate Partner"],
        "best_name": "Kairos Real Estate Partner",
        "similarity_to_target": 97.9,  # well above _SEC_ADV_SUSPICIOUS_REJECTION_THRESHOLD
    }]
    _patch_extract_dependencies(monkeypatch, rejected_crds=["555555"],
                                rejected_adv_details=rejected_adv_details)

    parsed, tokens, cost, own_domain, rejected_crds, ambiguous_candidates = icp_gate._extract(
        "Kairos Real Estate Partners", results)
    assert not llm_calls
    assert own_domain is None
    assert "Kairos Real Estate Partner" in ambiguous_candidates


def test_research_and_gate_parks_on_suspicious_adv_rejection(monkeypatch, tmp_path):
    """End-to-end: sub-change D's signal must reach research_and_gate()'s
    actual park decision, not just _classify_sec_adv_hits's own isolated
    return value. Before this test, _patch_extract_dependencies's
    rejected_adv_details parameter was never invoked with a non-None value
    anywhere in the file -- a regression inverting the gate, using the
    wrong dict key, or dropping the OR entirely would have passed every
    other test."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client

    def _fake_search(query, max_results=6, topic="general", **_kw):
        return [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(5)]

    monkeypatch.setattr(search_client, "search", _fake_search)

    llm_calls = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", lambda *a, **kw: llm_calls.append(1))

    rejected_adv_details = [{
        "crd": "555555", "names": ["Kairos Real Estate Partner"],
        "best_name": "Kairos Real Estate Partner", "similarity_to_target": 97.9,
    }]
    monkeypatch.setattr(icp_gate, "_lookup_sec_adv_hits", lambda firm_name: [])
    monkeypatch.setattr(icp_gate, "_classify_sec_adv_hits",
                        lambda firm_name, hits: (None, ["555555"], rejected_adv_details))

    result = icp_gate.research_and_gate("Kairos Real Estate Partners", progress=lambda *_a: None)
    assert not llm_calls
    assert result.icp_result == "parked"
    assert result.park_reason == "firm_identity_ambiguous"
    assert result.park_source == "match"
    assert "Kairos Real Estate Partner" in result.research.get("ambiguous_candidate_domains", [])


# --- _is_high_value_source ---------------------------------------------------
def test_is_high_value_source_own_domain():
    assert icp_gate._is_high_value_source("https://kairosrep.com/team", "kairosrep.com") is True


def test_is_high_value_source_any_pdf_regardless_of_domain():
    assert icp_gate._is_high_value_source(
        "https://some-random-cdn.example/factsheet.pdf", None) is True


def test_is_high_value_source_sec_adv():
    assert icp_gate._is_high_value_source(
        "https://adviserinfo.sec.gov/firm/summary/161321", None) is True


def test_is_high_value_source_rejects_aggregators():
    assert icp_gate._is_high_value_source(
        "https://rocketreach.co/some-firm-profile", "kairosrep.com") is False


def test_is_high_value_source_rejects_unrelated_domain():
    assert icp_gate._is_high_value_source("https://kimc.com/home", "kairosrep.com") is False


# --- _normalize_firm_name_for_match / _lookup_sec_adv ------------------------
def test_normalize_firm_name_strips_corporate_suffixes_and_punctuation():
    assert (icp_gate._normalize_firm_name_for_match("Kingbird Investment Management, LLC")
           == icp_gate._normalize_firm_name_for_match("KINGBIRD INVESTMENT MANAGEMENT"))


def test_normalize_firm_name_does_not_conflate_kairos_variants():
    """Regression guard for the exact false-positive risk a real SEC IAPD
    query surfaced: 'Kairos Real Estate Partners' must NOT normalize the
    same as 'KAIROS' or 'Redwood-Kairos Real Estate Partners' -- distinct,
    real, differently-named firms in IAPD's own records."""
    q = icp_gate._normalize_firm_name_for_match("Kairos Real Estate Partners, LLC")
    assert q != icp_gate._normalize_firm_name_for_match("KAIROS")
    assert q != icp_gate._normalize_firm_name_for_match("REDWOOD-KAIROS REAL ESTATE PARTNERS")
    assert q != icp_gate._normalize_firm_name_for_match("KAIROS INVESTMENT MANAGEMENT COMPANY, LLC")


class _FakeSecResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


_KAIROS_LIKE_SEC_RESPONSE = {
    "hits": {"hits": [{"_source": {
        "firm_source_id": "161321", "firm_name": "KAIROS",
        "firm_other_names": ["KAIROS", "REDWOOD-KAIROS REAL ESTATE PARTNERS",
                             "REDWOOD-KAIROS REAL ESTATE", "KIMC",
                             "KAIROS INVESTMENT MANAGEMENT COMPANY, LLC"],
        "firm_ia_scope": "ACTIVE", "firm_ia_address_details": "Irvine, CA",
    }}]}
}


def test_lookup_sec_adv_rejects_the_real_confirmed_false_positive(monkeypatch):
    """The exact live scenario this fix was built against: a fuzzy IAPD
    search for 'Kairos Real Estate Partners' returns ONLY a firm registered
    as KAIROS/KIMC -- a different, real company. Must return no match, not
    a false positive -- and that CRD must come back as a rejected_crd (FIX
    5 uses this to also drop that entity's documents elsewhere in the
    prompt)."""
    monkeypatch.setattr(icp_gate.requests, "get",
                        lambda *a, **kw: _FakeSecResponse(200, _KAIROS_LIKE_SEC_RESPONSE))
    match, rejected_crds = icp_gate._lookup_sec_adv("Kairos Real Estate Partners, LLC")
    assert match is None
    assert rejected_crds == ["161321"]


def test_lookup_sec_adv_accepts_an_exact_normalized_match(monkeypatch):
    payload = {"hits": {"hits": [{"_source": {
        "firm_source_id": "999999", "firm_name": "KINGBIRD INVESTMENT MANAGEMENT",
        "firm_other_names": [], "firm_ia_scope": "ACTIVE", "firm_ia_address_details": "TX",
    }}]}}
    monkeypatch.setattr(icp_gate.requests, "get", lambda *a, **kw: _FakeSecResponse(200, payload))
    match, rejected_crds = icp_gate._lookup_sec_adv("Kingbird Investment Management, LLC")
    assert match is not None
    assert match["crd"] == "999999"
    assert match["summary_url"] == "https://adviserinfo.sec.gov/firm/summary/999999"
    assert rejected_crds == []


def test_lookup_sec_adv_returns_none_on_non_200(monkeypatch):
    monkeypatch.setattr(icp_gate.requests, "get", lambda *a, **kw: _FakeSecResponse(500, {}))
    assert icp_gate._lookup_sec_adv("Any Firm LLC") == (None, [])


def test_lookup_sec_adv_returns_none_on_request_exception(monkeypatch):
    def _raise(*a, **kw):
        raise icp_gate.requests.RequestException("timeout")
    monkeypatch.setattr(icp_gate.requests, "get", _raise)
    assert icp_gate._lookup_sec_adv("Any Firm LLC") == (None, [])


def test_lookup_sec_adv_returns_none_on_bad_json(monkeypatch):
    class _BadJson:
        status_code = 200

        def json(self):
            raise ValueError("not json")
    monkeypatch.setattr(icp_gate.requests, "get", lambda *a, **kw: _BadJson())
    assert icp_gate._lookup_sec_adv("Any Firm LLC") == (None, [])


def test_format_adv_result_no_match_is_explicit_not_silent():
    url, text = icp_gate._format_adv_result("Kairos Real Estate Partners, LLC", None)
    assert "No SEC Investment Adviser" in text
    assert "Kairos Real Estate Partners, LLC" in text


def test_format_adv_result_with_match_includes_crd_and_names():
    match = {"crd": "999999", "matched_name": "KINGBIRD INVESTMENT MANAGEMENT",
            "all_names": ["KINGBIRD INVESTMENT MANAGEMENT"], "scope": "ACTIVE",
            "address": "TX", "summary_url": "https://adviserinfo.sec.gov/firm/summary/999999"}
    url, text = icp_gate._format_adv_result("Kingbird Investment Management", match)
    assert url == "https://adviserinfo.sec.gov/firm/summary/999999"
    assert "999999" in text
    assert "KINGBIRD INVESTMENT MANAGEMENT" in text


# --- _fetch_full_text_for_high_value_sources ---------------------------------
def test_fetch_full_text_only_calls_extract_with_high_value_urls(monkeypatch):
    from lib_snapshot.researcher import search_client

    captured_urls = []

    def _fake_extract(urls):
        captured_urls.extend(urls)
        return {u: f"FULL TEXT for {u}" for u in urls}

    monkeypatch.setattr(search_client, "extract", _fake_extract)
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(2)]
        + [_fake_result("https://rocketreach.co/kairos-profile", "x") for _ in range(4)]
        + [_fake_result("https://some.example/report.pdf", "x")]
    )
    out = icp_gate._fetch_full_text_for_high_value_sources(results, "kairosrep.com")
    assert set(captured_urls) == {"https://kairosrep.com", "https://kairosrep.com/team",
                                  "https://some.example/report.pdf"}
    assert "rocketreach.co" not in " ".join(captured_urls)
    assert out["https://kairosrep.com"] == "FULL TEXT for https://kairosrep.com"


def test_fetch_full_text_skips_extract_call_entirely_when_no_high_value_urls(monkeypatch):
    from lib_snapshot.researcher import search_client

    def _fail_if_called(urls):
        raise AssertionError("extract() must not be called with zero high-value URLs")

    monkeypatch.setattr(search_client, "extract", _fail_if_called)
    results = [_fake_result("https://rocketreach.co/x", "x")]
    assert icp_gate._fetch_full_text_for_high_value_sources(results, None) == {}


# --- _extract() end-to-end wiring of FIX 1/2/3 -------------------------------
def _patch_extract_dependencies(monkeypatch, extract_map=None, adv_match=None, rejected_crds=None,
                                rejected_adv_details=None):
    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "extract", lambda urls: extract_map or {})
    # FIX 28: _extract() now calls _lookup_sec_adv_hits()/_classify_sec_adv_hits()
    # directly (not _lookup_sec_adv itself, which stays a thin wrapper around
    # them -- see its own docstring) so sub-change D's rejected-hit similarity
    # signal is available without a second live query. Mock at that level.
    monkeypatch.setattr(icp_gate, "_lookup_sec_adv_hits", lambda firm_name: [])
    monkeypatch.setattr(icp_gate, "_classify_sec_adv_hits",
                        lambda firm_name, hits: (adv_match, rejected_crds or [], rejected_adv_details or []))
    monkeypatch.setattr(icp_gate, "_lookup_sec_adv",
                        lambda firm_name: (adv_match, rejected_crds or []))


def test_extract_uses_full_text_for_own_domain_not_truncated_snippet(monkeypatch):
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))

    full_text = "X" * 800 + " $254 M Capital Invested " + "Y" * 200  # > 500-char truncation point
    _patch_extract_dependencies(monkeypatch, extract_map={"https://kairosrep.com": full_text})

    results = (
        [_fake_result("https://kairosrep.com", "short snippet, truncated version")
        for _ in range(2)]
        + [_fake_result("https://kairosrep.com/team", "team bios, no figures")]
    )
    icp_gate._extract("Kairos Real Estate Partners, LLC", results)
    assert len(captured) == 1
    assert "$254 M Capital Invested" in captured[0], (
        "own-domain source must use the FULL extract() text, not the "
        "500-char-truncated search snippet"
    )


def test_extract_still_truncates_third_party_snippets_at_500_chars(monkeypatch):
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch)

    long_third_party_content = "Z" * 600 + " SHOULD BE CUT"
    results = [_fake_result("https://rocketreach.co/some-firm", long_third_party_content)]
    icp_gate._extract("Some Firm LLC", results)
    assert "SHOULD BE CUT" not in captured[0], (
        "third-party (non-high-value) sources must keep the existing "
        "500-char truncation, unchanged"
    )


# --- FIX 27 sub-change A: full text only wins when it's actually BETTER ------
def test_extract_keeps_content_when_full_text_is_shorter_than_content(monkeypatch):
    """Real Atlas run: Tavily's /extract call returned a degraded page shell
    for an own-domain URL -- shorter than the search-result `content` already
    in hand. Must NOT substitute a worse, shorter fetch just because a full-
    text fetch happened."""
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))

    content = "SNIPPET_MARKER discloses $500 million in assets under management, plenty of detail here."
    shell = "login | contact us"  # shorter than content -- a degraded /extract fetch
    _patch_extract_dependencies(monkeypatch, extract_map={"https://ownsite.example": shell})

    results = [_fake_result("https://ownsite.example", content) for _ in range(2)]
    icp_gate._extract("Own Site Firm LLC", results)
    assert "SNIPPET_MARKER" in captured[0], "a shorter full-text fetch must not replace content"
    assert "login | contact us" not in captured[0]


def test_extract_keeps_content_when_full_text_loses_a_signal_content_had(monkeypatch):
    """Real Atlas run's OTHER mechanism: the fetched full text was LONGER
    than content, but a naive "longer always wins" rule would still pick a
    shell page that happens to be padded with irrelevant text and has lost
    the dollar/size signal content already had."""
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))

    content = "SIGNAL_MARKER reports $500 million in assets under management."
    # Longer than content, but no currency/size signal anywhere -- a shell.
    shell = "resident login | owner login | contact us | " * 5
    assert len(shell) > len(content)
    _patch_extract_dependencies(monkeypatch, extract_map={"https://ownsite.example": shell})

    results = [_fake_result("https://ownsite.example", content) for _ in range(2)]
    icp_gate._extract("Own Site Firm LLC", results)
    assert "SIGNAL_MARKER" in captured[0], (
        "a longer full-text fetch that LOSES content's own currency signal must not win"
    )
    assert "resident login" not in captured[0]


def test_extract_uses_longer_full_text_when_content_had_no_signal_either(monkeypatch):
    """Sub-change A must not become so conservative it never substitutes:
    when content has NO signal to lose, a longer full text still wins even
    if the full text also has no signal (matches the real Atlas root-URL
    case: https://realatlas.com's own content had no currency match, and
    neither did its full text, but the full text was still preferred for
    being genuinely more complete)."""
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))

    content = "short snippet, no figures"
    full_text = "FULL_TEXT_MARKER much longer page content, still no dollar figures here at all"
    assert len(full_text) > len(content)
    _patch_extract_dependencies(monkeypatch, extract_map={"https://ownsite.example": full_text})

    results = [_fake_result("https://ownsite.example", content) for _ in range(2)]
    icp_gate._extract("Own Site Firm LLC", results)
    assert "FULL_TEXT_MARKER" in captured[0]


def test_extract_keeps_content_when_full_text_only_has_an_unrelated_units_signal(monkeypatch):
    """Adversarial review finding: a plain 'does full_text have ANY
    _SIZE_SIGNAL_RE match' boolean check has a blind spot -- a degraded
    shell containing only an incidental, unrelated units/sq-ft mention (nav
    chrome, e.g. real-estate listing copy) satisfied that check and
    replaced a `content` that had the REAL currency disclosure. Ranked
    signal comparison (currency > units > none) must not let a units-only
    full_text outrank a currency-bearing content."""
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))

    content = "CURRENCY_MARKER discloses $2.5 billion in assets under management."
    # Longer than content, but its only "signal" is an unrelated sq-ft
    # mention -- nav/listing chrome, not an AUM disclosure.
    shell = "Resident Login | Owner Login | Browse 1,200 sq ft office suites nationwide. " * 3
    assert len(shell) > len(content)
    _patch_extract_dependencies(monkeypatch, extract_map={"https://ownsite.example": shell})

    results = [_fake_result("https://ownsite.example", content) for _ in range(2)]
    icp_gate._extract("Own Site Firm LLC", results)
    assert "CURRENCY_MARKER" in captured[0], (
        "a units-only full-text shell must not outrank content's real currency disclosure"
    )
    assert "1,200 sq ft" not in captured[0]


def test_extract_dedup_keeps_signal_bearing_copy_over_longer_boilerplate(monkeypatch):
    """Adversarial review finding: 'longest content wins' alone is blind to
    WHICH copy carries the disclosure -- a longer, pure-boilerplate
    duplicate could discard a SHORTER duplicate that has the real dollar
    signal, for a third-party URL with no full-text-fetch recovery path.
    Signal rank must be checked before length."""
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch)

    signal_bearing = "ExampleFirm discloses assets under management of $750 million."
    boilerplate = ("Cookie settings | Privacy Policy | Terms of Service | Sign up for our "
                  "newsletter | Follow us on social media | Contact support")
    assert len(boilerplate) > len(signal_bearing)
    results = [
        _fake_result("https://thirdparty.example/firm", signal_bearing),
        _fake_result("https://thirdparty.example/firm", boilerplate),  # longer, but no signal
    ]
    icp_gate._extract("Some Firm LLC", results)
    assert "$750 million" in captured[0], (
        "a shorter signal-bearing duplicate must survive dedup over a longer signal-less one"
    )


# --- FIX 27 sub-change B: _windowed_slice -------------------------------------
def test_windowed_slice_returns_short_text_unchanged():
    text = "short text under the width"
    assert icp_gate._windowed_slice(text, 500) == text


def test_windowed_slice_falls_back_to_head_slice_when_no_match():
    text = "X" * 600  # no currency/size signal anywhere
    assert icp_gate._windowed_slice(text, 500) == text[:500]


def test_windowed_slice_centers_on_first_match_past_the_head_slice_cutoff():
    """The exact real-mode Atlas bug: a 915-char body whose head-slice(500)
    lands entirely inside nav-menu preamble, one character before the real
    disclosure."""
    nav_preamble = "## View All Consumer Products & Retail. "  # ~42 chars
    filler = "X" * 460  # pushes the disclosure well past char 500
    disclosure = "DENVER -- Atlas Real Estate reached $2.5 billion in assets under management today."
    text = nav_preamble + filler + disclosure + ("Y" * 200)
    assert "$2.5 billion" not in text[:500], "sanity check: head-slice must NOT already contain it"

    windowed = icp_gate._windowed_slice(text, 500)
    assert "$2.5 billion" in windowed
    assert "under management" in windowed
    assert len(windowed) == 500


def test_windowed_slice_reclamps_when_match_sits_near_the_very_end():
    """A match near the END of a long text must still yield a FULL-width
    window (re-clamped backward), not a short/truncated one."""
    text = "X" * 2000 + " $4.4 billion GAV disclosed right at the end"
    windowed = icp_gate._windowed_slice(text, 500)
    assert len(windowed) == 500
    assert "$4.4 billion GAV" in windowed


def test_windowed_slice_skips_an_earlier_unrelated_units_match():
    """Adversarial review finding: centering on the literal FIRST
    _SIZE_SIGNAL_RE match is itself defeatable by an earlier, irrelevant
    units/sq-ft mention (nav chrome) sitting ahead of the real disclosure --
    collapsing the window back to a plain head-slice. Must prefer a
    currency-class match wherever it sits over an earlier units-class one."""
    red_herring = "Browse 1,200 sq ft office suites nationwide. "  # units-class, near the start
    filler = "X" * 460
    disclosure = "DENVER -- Atlas Real Estate reached $2.5 billion in assets under management today."
    text = red_herring + filler + disclosure + ("Y" * 200)

    windowed = icp_gate._windowed_slice(text, 500)
    assert "$2.5 billion" in windowed, (
        "an earlier units-class red herring must not defeat windowing on the real currency disclosure"
    )
    assert len(windowed) == 500


# --- FIX 27 sub-change D: _entity_prefix + end-to-end wiring -----------------
def test_entity_prefix_extracts_name_and_location():
    text = ("Waterfall Asset Management Closes Atlas Fund at $485M – New York, "
           "New York – April 11, 2023 – Waterfall Asset Management, a global "
           "alternative investment adviser, announced the closing.")
    assert icp_gate._entity_prefix(text) == "[Waterfall Asset Management, New York, New York] "


def test_entity_prefix_handles_hyphenated_firm_name():
    """QA finding (real-mode In-Rel Properties run): the character class
    originally excluded "-", truncating "In-Rel Properties" to "Rel
    Properties" -- a wrong name that could make the model see a genuine
    same-target snippet as a near-miss "different company"."""
    text = "In-Rel Properties is a real estate investment and management firm."
    assert icp_gate._entity_prefix(text) == "[In-Rel Properties] "


def test_entity_prefix_name_only_when_no_location_extractable():
    text = "Atlas Capital Group has closed a value-add real estate fund at $405 million."
    assert icp_gate._entity_prefix(text) == "[Atlas Capital Group] "


def test_entity_prefix_returns_empty_when_neither_extractable():
    text = "Nearly all apartments in the building are micro-studio units."
    assert icp_gate._entity_prefix(text) == ""


def test_entity_prefix_matches_a_bare_real_estate_suffix():
    """Adversarial review finding: the suffix whitelist omitted "Estate", so
    a TARGET whose own name is bare "X Real Estate" (this pipeline's own
    target vertical -- an extremely common naming pattern) could never get
    its own name tagged on a third-party source, while a confuser ending in
    an already-whitelisted word (e.g. "X Real Estate Partners") got the
    full tag -- backwards from the intended effect. Reproduced live: the
    real Atlas Real Estate run's own genuine disclosure (re-hosted on
    prnewswire.com) tagged only "[Denver]" (location-only, no name)."""
    text = "Atlas Real Estate, a Denver-based leader in the single-family rental sector."
    assert icp_gate._entity_prefix(text) == "[Atlas Real Estate, Denver] "


def test_entity_prefix_suppresses_tag_when_target_itself_is_also_named():
    """Adversarial review finding: a co-mention snippet naming BOTH the
    target and another company (e.g. two firms both bidding on the same
    deal) would tag with only whichever name the regex matched FIRST -- if
    that's the other company, the model could wrongly discard a snippet
    that's ALSO genuinely about the target. When the target's own exact
    name is found anywhere in the text, the tag must be suppressed."""
    text = ("Blackstone Real Estate Partners and Atlas Real Estate Partners both bid "
           "on the downtown Denver tower this month.")
    assert icp_gate._entity_prefix(text, firm_name="Atlas Real Estate Partners") == ""


def test_entity_prefix_still_tags_a_same_brand_word_confuser():
    """Regression guard for the fix above: suppression must be EXACT-NAME
    based, not brand-token based -- a confuser sharing just the word
    "Atlas" with target "Atlas Real Estate" (e.g. "Atlas Real Estate
    Partners", a genuinely different company per the real bug report) must
    NOT have its tag suppressed just because it shares that one word. Only
    the target's own FULL name appearing verbatim should suppress the tag."""
    text = "Atlas Real Estate Partners is a private, New York-based real estate investment firm."
    assert icp_gate._entity_prefix(text, firm_name="Atlas Real Estate") == (
        "[Atlas Real Estate Partners, New York] "
    )


def test_extract_prefixes_third_party_snippet_with_entity_tag(monkeypatch):
    """End-to-end: a third-party (non-own-domain) snippet describing a
    differently-located company gets tagged, so the extraction prompt's own
    disambiguation instruction has something concrete to act on."""
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch)

    other_company_content = (
        "Widgets Capital Partners is a private, Chicago-based real estate "
        "investment firm with over $900 million in assets under management."
    )
    results = [_fake_result("https://thirdpartydirectory.example/widgets", other_company_content)]
    icp_gate._extract("Some Target Firm LLC", results)
    assert "[Widgets Capital Partners, Chicago] " in captured[0]


def test_extract_does_not_prefix_own_domain_sources(monkeypatch):
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch)

    content = "Own Target Firm LLC is a Chicago-based real estate investment firm."
    results = [_fake_result("https://targetfirm.example", content) for _ in range(2)]
    icp_gate._extract("Own Target Firm LLC", results)
    assert "[Own Target Firm LLC" not in captured[0], (
        "own-domain sources are the target by definition and must never be entity-prefixed"
    )


def test_extract_tags_unverified_when_no_entity_name_or_location_found(monkeypatch):
    """Adversarial review finding: an un-prefixed third-party snippet was
    visually IDENTICAL to a genuine own-domain one, and the prompt's own
    'an un-prefixed snippet can be trusted directly' instruction would have
    silently endorsed it -- reproduced live against a real, wrong-entity,
    dollar-figure-bearing source the tagger simply missed. A third-party
    source the tagger can't identify must get a distinct marker, never
    silence indistinguishable from own-domain trust."""
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch)

    # No capitalized name+suffix pattern, no "-based"/dateline location --
    # _entity_prefix returns "" for this, same as the real calcalistech.com case.
    untaggable_content = "led an investment round raising $25 million for the lending platform."
    results = [_fake_result("https://newsdirectory.example/article", untaggable_content)]
    icp_gate._extract("Some Target Firm LLC", results)
    assert "[unverified source] " in captured[0]


def test_extract_deduplicates_repeated_urls(monkeypatch):
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch)

    results = [_fake_result("https://rocketreach.co/dupe", "same content") for _ in range(5)]
    icp_gate._extract("Some Firm LLC", results)
    assert captured[0].count("rocketreach.co/dupe") == 1


def test_extract_dedup_keeps_longest_content_not_last_occurrence(monkeypatch):
    """FIX 27 sub-change C: a real Atlas run showed the SAME own-domain URL
    surfacing 3x with lengths 135/144/156 -- the OLD dedup kept whichever
    happened to be LAST in iteration order, an accident of search-angle
    ordering, not a deliberate choice. The shorter copy is placed LAST here
    specifically to prove the fix is "longest wins", not just "still
    dedups" (test_extract_deduplicates_repeated_urls above only proves the
    latter, since all 5 of its copies are identical)."""
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch)

    results = [
        _fake_result("https://rocketreach.co/dupe", "LONGER_VERSION with real detail " * 5),
        _fake_result("https://rocketreach.co/dupe", "short"),  # last in iteration order
    ]
    icp_gate._extract("Some Firm LLC", results)
    assert "LONGER_VERSION" in captured[0], (
        "dedup must keep the longest content copy, not whichever URL occurrence came last"
    )


def test_extract_high_value_source_survives_the_24_cap_ahead_of_aggregators(monkeypatch):
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch)

    # 30 distinct aggregator hits (no size signal) crowd out the display
    # cap, plus 2 own-domain hits (also no size signal) that FIX 3 must
    # still keep ahead of the aggregators.
    aggregator_noise = [_fake_result(f"https://rocketreach.co/firm-{i}", "generic bio, no figures")
                        for i in range(30)]
    own_domain_hits = ([_fake_result("https://targetfirm.com", "no figures here either")
                        for _ in range(2)]
                       + [_fake_result("https://targetfirm.com/team", "team page")])
    icp_gate._extract("Target Firm LLC", aggregator_noise + own_domain_hits)
    assert "targetfirm.com" in captured[0], (
        "an own-domain source must survive the 24-result display cap ahead "
        "of generic aggregator noise, even with no detected size signal"
    )


def test_extract_adv_no_match_note_appears_in_prompt(monkeypatch):
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch, adv_match=None)

    icp_gate._extract("Some Firm LLC", [_fake_result("https://somefirm.com", "x")])
    assert "No SEC Investment Adviser" in captured[0]


def test_extract_adv_match_appears_in_prompt(monkeypatch):
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    match = {"crd": "555", "matched_name": "SOME FIRM LLC", "all_names": ["SOME FIRM LLC"],
            "scope": "ACTIVE", "address": "NY", "summary_url": "https://adviserinfo.sec.gov/firm/summary/555"}
    _patch_extract_dependencies(monkeypatch, adv_match=match)

    icp_gate._extract("Some Firm LLC", [_fake_result("https://somefirm.com", "x")])
    assert "555" in captured[0]
    assert "SOME FIRM LLC" in captured[0]


# --- TEST_MODE gating for diagnosis-only side effects ------------------------
def test_dump_extraction_debug_is_a_noop_in_test_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TEST_MODE", True)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    result = icp_gate._dump_extraction_debug("Any Firm", "prompt text", [], [])
    assert result is None
    assert not (tmp_path / "qa_out" / "debug").exists()


def test_save_research_payload_is_a_noop_in_test_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TEST_MODE", True)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    icp_gate._save_research_payload("Any Firm", [{"url": "https://x.example", "content": "y"}])
    assert not (tmp_path / "qa_out" / "debug").exists()


# --- FIX 4: research payload cache / replay ----------------------------------
def test_research_payload_cache_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    results = [{"url": "https://kairosrep.com", "title": "t", "content": "c", "score": 0.5}]
    icp_gate._save_research_payload("Kairos Real Estate Partners, LLC", results)
    loaded = icp_gate._load_research_payload("Kairos Real Estate Partners, LLC")
    assert loaded == results


def test_load_research_payload_returns_none_when_no_cache_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    assert icp_gate._load_research_payload("Nobody Cached This Firm") is None


def test_research_and_gate_replays_from_cache_without_calling_tavily(monkeypatch, tmp_path):
    """The actual point of FIX 4: with REPLAY_RESEARCH_FROM_CACHE set and a
    cache present, search_client.search must NEVER be called."""
    from lib_snapshot.researcher import search_client

    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", True)

    cached_results = [{"url": "https://kairosrep.com", "title": "t",
                      "content": "ExampleFirm cached content.", "score": 0.5}]
    icp_gate._save_research_payload("Kairos Real Estate Partners, LLC", cached_results)

    def _fail_if_called(*a, **kw):
        raise AssertionError("search_client.search must not be called when replaying from cache")

    monkeypatch.setattr(search_client, "search", _fail_if_called)
    _patch_extract_dependencies(monkeypatch)

    class _FakeUsage2:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_UNVERIFIED",
                "aum_value_usd": None, "proxy_type": "", "proxy_basis": "", "sources": []},
               _FakeUsage2())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("Kairos Real Estate Partners, LLC", progress=lambda *_a: None)
    assert result.icp_result == "parked"
    assert result.search_calls == 0


def test_research_and_gate_falls_back_to_live_research_when_replay_enabled_but_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", True)

    from lib_snapshot.researcher import search_client
    calls = []

    def _fake_search(query, max_results=6, topic="general", **_kw):
        calls.append(query)
        return [_fake_result("https://somefirm.com", "some content with no size signal at all")]

    monkeypatch.setattr(search_client, "search", _fake_search)
    _patch_extract_dependencies(monkeypatch)

    class _FakeUsage3:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_UNVERIFIED",
                "aum_value_usd": None, "proxy_type": "", "proxy_basis": "", "sources": []},
               _FakeUsage3())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("Never Cached Firm LLC", progress=lambda *_a: None)
    assert len(calls) > 0, "must fall back to a live search when no cache exists for this firm"
    # And the live result must now be cached for next time.
    assert icp_gate._load_research_payload("Never Cached Firm LLC") is not None


# FIX 13 (real-mode Kairos run): personalization.py runs its OWN separate,
# unfiltered search and had no way to reuse THIS phase's already-verified
# entity identity -- own_domain/rejected_crds were computed here and then
# only ever written to the debug dump. Persisted onto the research dict so
# research_personalization() can reuse the SAME identity Phase 2 already
# established instead of re-deriving (and potentially disagreeing with) it
# from a differently-worded set of search results.
def test_research_and_gate_persists_own_domain_and_rejected_crds_onto_research(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client

    def _fake_search(query, max_results=6, topic="general", **_kw):
        return (
            [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
            + [_fake_result("https://kairosrep.com/team", "x") for _ in range(2)]
            + [_fake_result("https://kimc.com", "x")]
        )

    monkeypatch.setattr(search_client, "search", _fake_search)
    _patch_extract_dependencies(monkeypatch, rejected_crds=["999999"])

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_UNVERIFIED",
                "aum_value_usd": None, "proxy_type": "", "proxy_basis": "", "sources": []},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("Kairos Real Estate Partners, LLC", progress=lambda *_a: None)
    assert result.research["own_domain"] == "kairosrep.com"
    assert result.research["rejected_crds"] == ["999999"]


# --- FIX 28 sub-change C: ambiguity parks BEFORE the LLM call ---------------
def test_extract_parks_before_calling_the_llm_when_own_domain_is_ambiguous(monkeypatch):
    """A visible park is strictly better than a confident wrong answer --
    and cheaper: an ambiguous entity identity must never spend an LLM call
    on an extraction that's unreliable regardless of prompt quality."""
    llm_calls = []

    def _fail_if_called(*a, **kw):
        llm_calls.append(1)
        raise AssertionError("the LLM must never be called for an ambiguous own_domain")

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fail_if_called)
    _patch_extract_dependencies(monkeypatch)

    results = (
        [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(3)]
        + [_fake_result(f"https://kairospartners.com/page-{i}", "x") for i in range(3)]
    )
    parsed, tokens, cost, own_domain, rejected_crds, ambiguous_candidates = icp_gate._extract(
        "Kairos Real Estate Partners", results)
    assert not llm_calls
    assert parsed == {}
    assert tokens == 0
    assert cost == 0.0
    assert own_domain is None
    assert set(ambiguous_candidates) == {"kairosrep.com", "kairospartners.com"}


def test_research_and_gate_returns_ambiguity_park(monkeypatch, tmp_path):
    """End-to-end: two domains BOTH scoring as strong, comparable name
    matches (kairosrep.com vs kairospartners.com) must produce a visible
    park, not a confident (and, in the real incident's shape, possibly
    WRONG) qualify/disqualify verdict."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client

    def _fake_search(query, max_results=6, topic="general", **_kw):
        return (
            [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(3)]
            + [_fake_result(f"https://kairospartners.com/page-{i}", "x") for i in range(3)]
        )

    monkeypatch.setattr(search_client, "search", _fake_search)

    llm_calls = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry",
                        lambda *a, **kw: llm_calls.append(1))
    monkeypatch.setattr(icp_gate, "_lookup_sec_adv_hits", lambda firm_name: [])

    result = icp_gate.research_and_gate("Kairos Real Estate Partners", progress=lambda *_a: None)
    assert not llm_calls, "an ambiguous own_domain must never reach the extraction LLM call"
    assert result.icp_result == "parked"
    assert result.park_source == "match"
    assert result.park_reason == "firm_identity_ambiguous"
    assert "ambiguous" in result.icp_reason.lower()
    assert "kairosrep.com" in result.research.get("ambiguous_candidate_domains", [])


def test_research_and_gate_resolves_the_vm_distribution_correctly_without_parking(monkeypatch, tmp_path):
    """The exact VM payload distribution (kimc.com 5, kairosrep.com 3,
    merriam-webster.com 5, youtube.com 4) must resolve CONFIDENTLY to
    kairosrep.com end-to-end -- own_domain has exactly one strong name
    match here (kimc.com/merriam-webster.com/youtube.com all score weak
    or zero), so this must NOT park. Note: this test does NOT exercise
    sub-change C's ambiguity/dominance-override branches at all -- there
    is only ever one strong candidate in this distribution, so the
    "resolves correctly" here is really sub-change A (name-similarity
    beating raw hit-count plurality) proving itself, not sub-change C.
    See test_research_and_gate_resolves_via_dominance_override_without_parking
    immediately below for the end-to-end test that DOES exercise sub-change
    C's dominance-override path (two comparable strong name matches, one
    hit-count-dominant)."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client

    def _fake_search(query, max_results=6, topic="general", **_kw):
        return (
            [_fake_result(f"https://kimc.com/page-{i}", "x") for i in range(5)]
            + [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(3)]
            + [_fake_result(f"https://www.merriam-webster.com/dictionary/kairos-{i}", "x") for i in range(5)]
            + [_fake_result(f"https://www.youtube.com/watch?v=fake{i}", "x") for i in range(4)]
        )

    monkeypatch.setattr(search_client, "search", _fake_search)
    _patch_extract_dependencies(monkeypatch)

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_UNVERIFIED",
                "aum_value_usd": None, "proxy_type": "", "proxy_basis": "", "sources": []},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("Kairos Real Estate Partners", progress=lambda *_a: None)
    assert result.icp_result != "parked" or result.park_reason != "firm_identity_ambiguous"
    assert result.research["own_domain"] == "kairosrep.com"


def test_research_and_gate_resolves_via_dominance_override_without_parking(monkeypatch, tmp_path):
    """End-to-end sub-change C dominance-override test (adversarial review
    finding: the VM-distribution test above never actually has two
    comparable strong name matches, so it never exercises this branch).
    kairosrep.com (6 hits) and kairospartners.com (2 hits) BOTH score a
    full 100 on name similarity -- a genuine "comparable strong matches"
    situation -- but kairosrep.com's hit count is >= 2x the runner-up's,
    so _resolve_own_domain must resolve it outright (NOT ambiguous) via
    the dominance tie-break, and that resolution must reach
    research_and_gate() as a normal, non-parked result."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "REPLAY_RESEARCH_FROM_CACHE", False)

    from lib_snapshot.researcher import search_client

    def _fake_search(query, max_results=6, topic="general", **_kw):
        return (
            [_fake_result(f"https://kairosrep.com/page-{i}", "x") for i in range(6)]
            + [_fake_result(f"https://kairospartners.com/page-{i}", "x") for i in range(2)]
        )

    monkeypatch.setattr(search_client, "search", _fake_search)
    _patch_extract_dependencies(monkeypatch)

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_UNVERIFIED",
                "aum_value_usd": None, "proxy_type": "", "proxy_basis": "", "sources": []},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = icp_gate.research_and_gate("Kairos Real Estate Partners", progress=lambda *_a: None)
    assert result.icp_result != "parked" or result.park_reason != "firm_identity_ambiguous"
    assert result.research["own_domain"] == "kairosrep.com"


# ---------------------------------------------------------------------------
# FIX 5 — entity filtering: DROP other-entity sources, don't just deprioritize.
# QA finding (Kairos Real Estate Partners live run): own_domain and the SEC
# ADV rejection were both computed correctly and then ignored -- the wrong
# entity's sources still reached the prompt (just ranked lower) and the
# extraction LLM used them anyway, attributing "KAIROS"/kimc.com's $4.4B and
# 2005 founding to the target firm. A source that reaches the prompt gets
# used regardless of rank; the fix is exclusion, not ordering.
# ---------------------------------------------------------------------------
def test_brand_tokens_extracts_the_distinctive_word_only():
    assert icp_gate._brand_tokens("Kairos Real Estate Partners, LLC") == {"kairos"}


def test_brand_tokens_drops_generic_words_entirely():
    assert icp_gate._brand_tokens("Real Estate Partners LLC") == set()


def test_identify_other_entity_domains_catches_brand_token_domain():
    """kairoscm.com / kairosgroupllc.com literally spell out the shared
    brand word "kairos" -- caught even with just ONE hit each, since the
    domain-name signal alone is enough."""
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(2)]
        + [_fake_result("https://kairoscm.com", "x")]
        + [_fake_result("https://www.kairosgroupllc.com", "x")]
    )
    other = icp_gate._identify_other_entity_domains(results, "kairosrep.com",
                                                     "Kairos Real Estate Partners, LLC")
    assert other == {"kairoscm.com", "kairosgroupllc.com"}


def test_identify_other_entity_domains_catches_frequency_based_tie_even_without_brand_token():
    """kimc.com shares NO literal substring with "kairos" at all (KIMC is
    Kairos Investment Management Company's INITIALS) -- caught instead by
    clearing the same >=2-hit bar _infer_own_domain itself uses."""
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(2)]
        + [_fake_result("https://kimc.com/associate-asset-management", "x")]
        + [_fake_result("https://kimc.com", "x")]
    )
    other = icp_gate._identify_other_entity_domains(results, "kairosrep.com",
                                                     "Kairos Real Estate Partners, LLC")
    assert "kimc.com" in other


def test_identify_other_entity_domains_never_flags_own_domain():
    results = [_fake_result("https://kairosrep.com", "x") for _ in range(5)]
    other = icp_gate._identify_other_entity_domains(results, "kairosrep.com",
                                                     "Kairos Real Estate Partners, LLC")
    assert "kairosrep.com" not in other


def test_identify_other_entity_domains_returns_empty_when_no_own_domain():
    results = [_fake_result("https://kimc.com", "x")]
    assert icp_gate._identify_other_entity_domains(results, None, "Kairos Real Estate Partners, LLC") == set()


def test_filter_out_other_entities_drops_other_domain_sources():
    results = [
        _fake_result("https://kairosrep.com", "target's own site"),
        _fake_result("https://kimc.com", "different company"),
        _fake_result("https://kimc.com/home", "different company, another page"),
        _fake_result("https://kairoscm.com", "yet another different company"),
    ]
    filtered = icp_gate._filter_out_other_entities(results, "kairosrep.com",
                                                   "Kairos Real Estate Partners, LLC", [])
    urls = {r["url"] for r in filtered}
    assert urls == {"https://kairosrep.com"}


def test_filter_out_other_entities_drops_rejected_crd_documents_on_any_domain():
    """The exact real finding: a general web search independently surfaced
    a MIRRORED copy of the rejected entity's ADV brochure on a domain
    (reports.adviserinfo.sec.gov) that shares neither a brand token nor
    enough raw hits to be caught by the domain-level filter -- only the CRD
    substring match catches it."""
    results = [
        _fake_result("https://kairosrep.com", "target's own site"),
        _fake_result("https://adviserinfo.sec.gov/firm/summary/161321", "wrong entity's ADV page"),
        _fake_result("https://reports.adviserinfo.sec.gov/reports/ADV/161321/PDF/161321.pdf",
                    "wrong entity's mirrored ADV brochure"),
    ]
    filtered = icp_gate._filter_out_other_entities(results, "kairosrep.com",
                                                   "Kairos Real Estate Partners, LLC", ["161321"])
    urls = {r["url"] for r in filtered}
    assert urls == {"https://kairosrep.com"}


def test_extract_drops_other_entity_sources_even_with_a_size_signal(monkeypatch):
    """The core FIX 5 guarantee: a wrong-entity source must be EXCLUDED, not
    merely ranked lower -- even one with a strong size signal that would
    otherwise win top priority under FIX 3's ordering."""
    captured = []
    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry_factory(captured))
    _patch_extract_dependencies(monkeypatch, adv_match=None, rejected_crds=["161321"])

    results = (
        [_fake_result("https://kairosrep.com", "target's own site, no dollar figure here")
        for _ in range(3)]
        + [_fake_result("https://kairosrep.com/team", "team bios") for _ in range(2)]
        + [_fake_result("https://kimc.com", "KIMC discloses $4.4 billion in gross asset value")]
        + [_fake_result("https://kimc.com/home", "KIMC home page, more $4.4 billion detail")]
        + [_fake_result("https://reports.adviserinfo.sec.gov/reports/ADV/161321/PDF/161321.pdf",
                       "wrong entity ADV brochure, $2.4 billion AUM")]
    )
    icp_gate._extract("Kairos Real Estate Partners, LLC", results)
    assert "4.4 billion" not in captured[0]
    assert "2.4 billion" not in captured[0]
    assert "kimc.com" not in captured[0]
    assert "161321" not in captured[0]


# ---------------------------------------------------------------------------
# FIX 6 — visible warning + diagnostic capture when the primary model
# returns empty. QA finding (Kairos live run): gpt-5-mini returned empty on
# attempts 1 and 2 in every recent run, escalating silently to DeepSeek every
# time -- confirmed root cause: finish_reason="length" with
# reasoning_tokens == the full max_completion_tokens budget (a reasoning
# model spending its ENTIRE budget on hidden reasoning before ever producing
# visible output). Do not fix by removing the model -- diagnose and surface.
# ---------------------------------------------------------------------------
def test_chat_captures_finish_reason_and_reasoning_tokens(monkeypatch):
    from lib_snapshot import models

    class _Details:
        reasoning_tokens = 2048

    class _Usage:
        prompt_tokens = 5000
        completion_tokens = 0
        total_tokens = 5000
        completion_tokens_details = _Details()

    class _Message:
        content = None

    class _Choice:
        message = _Message()
        finish_reason = "length"

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    class _FakeCompletions:
        def create(self, **kw):
            return _Resp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(models, "client_for", lambda model=None: _FakeClient())
    result = models.chat([{"role": "user", "content": "x"}], model="gpt-5-mini")
    assert result.text == ""
    assert result.finish_reason == "length"
    assert result.usage.reasoning_tokens == 2048


def test_complete_json_retry_debug_capture_receives_finish_reason_and_reasoning_tokens(monkeypatch):
    from lib_snapshot import models

    class _FakeUsage:
        prompt_tokens = 100
        completion_tokens = 0
        total_tokens = 100
        cost_usd = 0.0
        reasoning_tokens = 2048

    def _fake_complete(prompt, **kw):
        return models.LLMResult(text="", usage=_FakeUsage(), finish_reason="length")

    monkeypatch.setattr(models, "complete", _fake_complete)
    captured = []
    with pytest.raises(models.LLMError):
        models.complete_json_retry("prompt", debug_capture=lambda *a: captured.append(a))
    assert len(captured) == 3  # try, retry, escalate -- all empty
    assert all(c[2] == "length" and c[3] == 2048 for c in captured)


def test_extract_emits_visible_warning_on_empty_primary_response(monkeypatch, capsys):
    """FIX 6's explicit requirement: this must never again pass unnoticed."""
    from lib_snapshot import models

    def _fake_complete_json_retry(prompt, debug_capture=None, **_kw):
        if debug_capture:
            debug_capture("gpt-5-mini", "", "length", 2048)
            debug_capture("gpt-5-mini", "", "length", 2048)
            debug_capture("DeepSeek-V3.2-Speciale", '{"firm_type_ok": true, "region_ok": true, '
                          '"aum_tag": "AUM_UNVERIFIED", "aum_value_usd": null, "proxy_type": "", '
                          '"proxy_basis": "", "sources": []}', "stop", 0)
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_UNVERIFIED",
                "aum_value_usd": None, "proxy_type": "", "proxy_basis": "", "sources": []},
               _FakeUsage())

    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)
    _patch_extract_dependencies(monkeypatch)

    icp_gate._extract("Some Firm LLC", [_fake_result("https://somefirm.com", "x")])
    captured_output = capsys.readouterr()
    assert captured_output.out.count("[EXTRACTION WARNING]") == 2
    assert "gpt-5-mini returned EMPTY" in captured_output.out
    assert "finish_reason='length'" in captured_output.out
    assert "reasoning_tokens=2048" in captured_output.out


# ---------------------------------------------------------------------------
# FIX 7 — reasoning_effort threaded through to the API call, scoped to the
# extraction call only. QA finding (real-mode Kairos run): confirmed live
# against the actual Azure gpt-5-mini deployment that "minimal" reasoning
# effort is accepted and produces reasoning_tokens=0 on the real extraction
# prompt (vs the provider default consuming the entire max_completion_tokens
# budget) while still returning a correct result.
# ---------------------------------------------------------------------------
def test_chat_passes_reasoning_effort_to_the_api_call_when_set(monkeypatch):
    from lib_snapshot import models

    captured_kwargs = {}

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15

    class _Message:
        content = "ok"

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    class _FakeCompletions:
        def create(self, **kw):
            captured_kwargs.update(kw)
            return _Resp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(models, "client_for", lambda model=None: _FakeClient())
    models.chat([{"role": "user", "content": "x"}], model="gpt-5-mini", reasoning_effort="minimal")
    assert captured_kwargs.get("reasoning_effort") == "minimal"


def test_chat_omits_reasoning_effort_when_not_set(monkeypatch):
    """Default (None) must not send the parameter at all -- a targeted
    opt-in, not a global behavior change for every other caller."""
    from lib_snapshot import models

    captured_kwargs = {}

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15

    class _Message:
        content = "ok"

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    class _FakeCompletions:
        def create(self, **kw):
            captured_kwargs.update(kw)
            return _Resp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(models, "client_for", lambda model=None: _FakeClient())
    models.chat([{"role": "user", "content": "x"}], model="gpt-5-mini")
    assert "reasoning_effort" not in captured_kwargs


def test_extract_calls_complete_json_retry_with_minimal_reasoning_effort(monkeypatch):
    """Proves the extraction call site is actually wired to FIX 7, not just
    that models.chat() supports the parameter in isolation."""
    from lib_snapshot import models

    captured_kwargs = {}

    def _fake_complete_json_retry(prompt, **kw):
        captured_kwargs.update(kw)
        return ({"firm_type_ok": True, "region_ok": True, "aum_tag": "AUM_UNVERIFIED",
                "aum_value_usd": None, "proxy_type": "", "proxy_basis": "", "sources": []},
               _FakeUsage())

    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)
    _patch_extract_dependencies(monkeypatch)

    icp_gate._extract("Some Firm LLC", [_fake_result("https://somefirm.com", "x")])
    assert captured_kwargs.get("reasoning_effort") == "minimal"


# ---------------------------------------------------------------------------
# FIX 8 — the extraction prompt's own text must classify proxy rule (b)
# (invested/deployed capital) as non_floor, not floor. apply_gate's own
# floor/non_floor branching is untouched -- verified separately above
# (test_proxy_b_invested_capital_non_floor_type_in_band_qualifies,
# test_proxy_b_invested_capital_below_200m_disqualifies_not_parks).
# ---------------------------------------------------------------------------
def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def test_extraction_prompt_classifies_invested_capital_as_non_floor():
    normalized = _normalize_ws(icp_gate._EXTRACT_PROMPT_TMPL)
    assert '"non_floor" if from proxy rule (b) ("$X invested' in normalized
    assert 'or from proxy rule (b) ("$X invested' not in normalized


def test_extraction_prompt_still_reserves_floor_for_floor_language_only():
    normalized = _normalize_ws(icp_gate._EXTRACT_PROMPT_TMPL)
    assert '"floor" ONLY if this figure came from floor-proxy LANGUAGE' in normalized


def test_icp_scoring_spec_file_classifies_invested_capital_as_non_floor():
    spec_text = _normalize_ws(config.load_prompt(config.ICP_PROMPT_NAME))
    assert 'proxy_type "non_floor", basis "cumulative invested' in spec_text
    assert "FLOOR-type proxy (invested/deployed capital" not in spec_text


# ---------------------------------------------------------------------------
# FIX 24 (adversarial re-review): every test of _normalize_quantity_qualifier/
# _disclosed_figure_and_label injects quantity_qualifier directly into a fake
# `parsed` dict -- none of them prove the extraction PROMPT actually asks the
# model for this field at all. Following the exact same precedent as the
# FIX 8 tests directly above (which assert on the literal prompt/spec text,
# not just on the downstream parsing logic), so a future prompt edit that
# accidentally drops or garbles these instructions is caught here, not
# silently shipped with the model simply never reporting a qualifier again.
# ---------------------------------------------------------------------------
def test_extraction_prompt_asks_for_quantity_qualifier():
    normalized = _normalize_ws(icp_gate._EXTRACT_PROMPT_TMPL)
    assert '"quantity_qualifier"' in normalized
    assert "over" in normalized and "approximately" in normalized


def test_icp_scoring_spec_file_documents_quantity_qualifier():
    spec_text = _normalize_ws(config.load_prompt(config.ICP_PROMPT_NAME))
    assert "quantity_qualifier" in spec_text
    assert "Preserve the source's own precision" in spec_text


# ---------------------------------------------------------------------------
# FIX 9 — a domain that is a company's own site (a bare-root-URL hit), is
# not own_domain, and is not a known aggregator must be dropped even at a
# single hit. QA finding (real-mode Kairos run): kimc.com surfaced only ONE
# hit that run -- below the >=2-hit frequency bar and sharing no brand-token
# overlap with "kairos" (KIMC is initials) -- and leaked into the prompt.
# That single hit was https://kimc.com (bare root), titled "Kairos
# Investment Management: Home".
# ---------------------------------------------------------------------------
def test_is_bare_root_url_true_for_bare_domain():
    assert icp_gate._is_bare_root_url("https://kimc.com") is True


def test_is_bare_root_url_true_for_bare_domain_with_trailing_slash():
    assert icp_gate._is_bare_root_url("https://kimc.com/") is True


def test_is_bare_root_url_false_for_a_deep_path():
    assert icp_gate._is_bare_root_url("https://kimc.com/associate-asset-management") is False


def test_is_bare_root_url_false_with_a_query_string():
    assert icp_gate._is_bare_root_url("https://kimc.com?ref=search") is False


def test_identify_other_entity_domains_drops_own_site_at_a_single_bare_root_hit():
    """The exact real regression: kimc.com at only 1 hit, no brand-token
    overlap, must still be dropped because that 1 hit is its own bare root."""
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(2)]
        + [_fake_result("https://kimc.com", "Kairos Investment Management: Home")]
    )
    other = icp_gate._identify_other_entity_domains(results, "kairosrep.com",
                                                     "Kairos Real Estate Partners, LLC")
    assert "kimc.com" in other


def test_identify_other_entity_domains_does_not_drop_incidental_press_coverage_at_1_hit():
    """A single hit that is a DEEP PATH on a press/news domain (an article
    ABOUT the target, not a claim to BE some other company) must NOT be
    dropped just for appearing once -- only a bare-root hit is strong
    enough evidence to act on at count=1."""
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(2)]
        + [_fake_result("https://labusinessjournal.com/finance/investment-banking/kairos-fund-raises-58m",
                       "Kairos Fund Raises $58M - unrelated venture capital firm")]
    )
    other = icp_gate._identify_other_entity_domains(results, "kairosrep.com",
                                                     "Kairos Real Estate Partners, LLC")
    assert "labusinessjournal.com" not in other


def test_filter_out_other_entities_drops_kimc_at_a_single_bare_root_hit():
    results = [
        _fake_result("https://kairosrep.com", "target's own site"),
        _fake_result("https://kimc.com", "Kairos Investment Management: Home"),
    ]
    filtered = icp_gate._filter_out_other_entities(results, "kairosrep.com",
                                                   "Kairos Real Estate Partners, LLC", [])
    urls = {r["url"] for r in filtered}
    assert urls == {"https://kairosrep.com"}
