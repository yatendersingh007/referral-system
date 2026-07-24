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


def test_tertiary_unit_count_proxy_small_count_disqualifies_below_floor():
    result, reason = apply_gate(True, True, None, "AUM_ESTIMATED", proxy_type="units",
                               unit_count=500)
    # 500 * 120,000 * 0.4 = $24M -- well below the floor, non-floor-type proxy -> disqualify
    assert result == "disqualified"


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
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete_json_retry)

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
