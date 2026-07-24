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
from src import config
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


def test_proxy_b_invested_capital_floor_type_in_band_qualifies():
    """Proxy rule (b): '$800M invested' -> FLOOR estimate, tag AUM_ESTIMATED,
    basis 'cumulative invested capital, proxy'. In-band -> qualify, and the
    proxy basis string appears in the reason (auditability)."""
    result, reason = apply_gate(True, True, 800_000_000, "AUM_ESTIMATED", proxy_type="floor",
                               proxy_basis="cumulative invested capital, proxy")
    assert result == "qualified"
    assert "invested capital" in reason


def test_proxy_b_invested_capital_below_200m_parks():
    """This is the exact 'Seyon Group' example from §10a: '$800M+ invested'
    style proxy below $200M must PARK, not disqualify."""
    result, reason = apply_gate(True, True, 150_000_000, "AUM_ESTIMATED", proxy_type="floor",
                               proxy_basis="cumulative invested capital, proxy")
    assert result == "parked"
    assert "verify manually" in reason


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


def test_no_dollar_figure_at_all_value_null_parks():
    """'Square footage or asset counts ALONE (no dollar figure) are NOT
    proxies — value_usd stays null, firm PARKS.' No unit-count/sq-ft field
    exists in apply_gate's signature at all — the only way to represent 'no
    dollar figure found' is aum_value_usd=None, which must always PARK
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
    (800_000_000, "AUM_ESTIMATED", "floor", "invested capital proxy"),
    (150_000_000, "AUM_ESTIMATED", "floor", "invested capital proxy"),
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
