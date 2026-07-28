"""FIX 13 (real-mode Kairos Real Estate Partners run): personalization.py ran
its own, completely independent Tavily search with NO entity filter at all --
icp_gate.py's own_domain/other-entity/rejected-CRD filtering never applied
here, so a wrong-entity fact could reach the email/deck through hook_1/hook_2
or (worse, since it had no source field at all before this fix)
pain_point_angle. These tests exercise _filter_results (the FIX 13 wiring)
and research_personalization()'s use of it, with no network calls.
"""
from __future__ import annotations

import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import config, personalization


@pytest.fixture(autouse=True)
def _clean_wc_state():
    reset_testmode(verbose=False)
    yield


def _fake_result(url: str, content: str, title: str = "") -> dict:
    return {"title": title, "url": url, "content": content, "score": 0.5}


# ---------------------------------------------------------------------------
# _filter_results: same own_domain / other-entity / rejected-CRD logic as
# icp_gate.py, reused (not reimplemented) here.
# ---------------------------------------------------------------------------
def test_filter_results_drops_a_different_entitys_domain():
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
        + [_fake_result("https://kairosrep.com/team", "x") for _ in range(2)]
        + [_fake_result("https://kimc.com", "x")]  # a different, similarly-named firm's own root URL
    )
    filtered = personalization._filter_results(results, "Kairos Real Estate Partners, LLC", research=None)
    urls = {r["url"] for r in filtered}
    assert "https://kimc.com" not in urls
    assert "https://kairosrep.com" in urls


def test_filter_results_prefers_own_domain_from_research_over_fresh_inference():
    """When Phase 2's research dict already carries own_domain, reuse it
    rather than re-deriving (and potentially disagreeing) from this phase's
    own, differently-worded, smaller result set."""
    results = [
        _fake_result("https://kimc.com", "x"),
        _fake_result("https://kimc.com/about", "x"),
        _fake_result("https://kairosrep.com", "x"),  # only 1 hit here -- would lose a fresh frequency contest
    ]
    research = {"own_domain": "kairosrep.com", "rejected_crds": []}
    filtered = personalization._filter_results(results, "Kairos Real Estate Partners, LLC", research)
    urls = {r["url"] for r in filtered}
    assert "https://kairosrep.com" in urls
    assert "https://kimc.com" not in urls
    assert "https://kimc.com/about" not in urls


def test_filter_results_drops_a_rejected_crd_mirror_url():
    results = [
        _fake_result("https://kairosrep.com", "x"),
        _fake_result("https://reports.adviserinfo.sec.gov/reports/ADV/123456/x.pdf", "wrong entity's brochure"),
    ]
    research = {"own_domain": "kairosrep.com", "rejected_crds": ["123456"]}
    filtered = personalization._filter_results(results, "Kairos Real Estate Partners, LLC", research)
    urls = {r["url"] for r in filtered}
    assert "https://reports.adviserinfo.sec.gov/reports/ADV/123456/x.pdf" not in urls


def test_filter_results_falls_back_to_fresh_inference_when_research_missing_own_domain():
    """Backward compat: an older cached research object (or a call site that
    doesn't have Phase 2's research yet) has no own_domain key at all -- must
    still filter using THIS phase's own results, not skip filtering."""
    results = (
        [_fake_result("https://kairosrep.com", "x") for _ in range(3)]
        + [_fake_result("https://kimc.com", "x")]
    )
    filtered = personalization._filter_results(results, "Kairos Real Estate Partners, LLC", research={})
    urls = {r["url"] for r in filtered}
    assert "https://kimc.com" not in urls


# ---------------------------------------------------------------------------
# research_personalization(): real-mode wiring -- filtered results reach the
# prompt, pain_point_angle carries a source (unlike before FIX 13), and the
# LinkedIn-source guard (HARD RULE 2) covers pain_point_angle too.
# ---------------------------------------------------------------------------
def test_research_personalization_builds_snippets_from_filtered_results_only(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    from lib_snapshot.researcher import search_client

    def _fake_search(query, max_results=5, **_kw):
        return [
            _fake_result("https://kairosrep.com", "the real firm's own site") for _ in range(2)
        ] + [_fake_result("https://kimc.com", "a different Kairos-named firm's own homepage")]

    monkeypatch.setattr(search_client, "search", _fake_search)

    captured_prompt = {}

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        captured_prompt["text"] = prompt
        return ({"hook_1": "", "hook_1_source": "", "hook_2": "", "hook_2_source": "",
                "firm_niche": "", "recent_event": "", "pain_point_angle": "",
                "pain_point_angle_source": "", "service_fit": ""},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    personalization.research_personalization("Kairos Real Estate Partners, LLC", "Jane Doe",
                                              research={"own_domain": "kairosrep.com", "rejected_crds": []})
    assert "kimc.com" not in captured_prompt["text"], (
        "a different entity's domain must be filtered out before the prompt is built"
    )
    assert "kairosrep.com" in captured_prompt["text"]


def test_research_personalization_returns_pain_point_angle_source(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "search",
                        lambda query, max_results=5, **_kw: [_fake_result("https://kairosrep.com", "x")])

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"hook_1": "", "hook_1_source": "", "hook_2": "", "hook_2_source": "",
                "firm_niche": "", "recent_event": "",
                "pain_point_angle": "property management and accounting oversight",
                "pain_point_angle_source": "https://kairosrep.com/property-services",
                "service_fit": ""},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = personalization.research_personalization("Kairos Real Estate Partners, LLC", "Jane Doe",
                                                       research={"own_domain": "kairosrep.com"})
    assert result["pain_point_angle_source"] == "https://kairosrep.com/property-services"


def test_research_personalization_blanks_pain_point_angle_with_linkedin_source(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "search",
                        lambda query, max_results=5, **_kw: [_fake_result("https://kairosrep.com", "x")])

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **_kw):
        return ({"hook_1": "", "hook_1_source": "", "hook_2": "", "hook_2_source": "",
                "firm_niche": "", "recent_event": "",
                "pain_point_angle": "a fact that slipped through from a LinkedIn snippet",
                "pain_point_angle_source": "https://www.linkedin.com/company/kairos",
                "service_fit": ""},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    result = personalization.research_personalization("Kairos Real Estate Partners, LLC", "Jane Doe",
                                                       research={"own_domain": "kairosrep.com"})
    assert result["pain_point_angle"] == ""
    assert result["pain_point_angle_source"] == ""


def test_research_personalization_no_results_returns_blank_pain_point_angle_source(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "search", lambda query, max_results=5, **_kw: [])

    result = personalization.research_personalization("No Results Co", "Jane Doe", research=None)
    assert result["pain_point_angle_source"] == ""
    assert result["thin_personalization"] is True


# ---------------------------------------------------------------------------
# FIX 15 follow-up (real-mode Kairos run): across 3 diagnostic runs this call
# consumed 1600/1344/1600 reasoning tokens against the same 2048 ceiling that
# produced empty responses in deck_filler.get_deck_content() -- margin, not
# (yet) a failure signature. reasoning_effort="minimal" applied on that basis
# and re-measured: hook completeness (9/9 fields non-empty) was identical
# across 3 before-runs and 3 after-runs, so kept rather than reverted.
# ---------------------------------------------------------------------------
def test_research_personalization_real_mode_passes_reasoning_effort_minimal(monkeypatch):
    monkeypatch.setattr(config, "TEST_MODE", False)

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "search",
                        lambda query, max_results=5, **_kw: [_fake_result("https://kairosrep.com", "x")])

    captured = {}

    class _FakeUsage:
        total_tokens = 50
        cost_usd = 0.0001

    def _fake_complete(prompt, **kwargs):
        captured.update(kwargs)
        return ({"hook_1": "", "hook_1_source": "", "hook_2": "", "hook_2_source": "",
                "firm_niche": "", "recent_event": "", "pain_point_angle": "",
                "pain_point_angle_source": "", "service_fit": ""},
               _FakeUsage())

    from lib_snapshot import models
    monkeypatch.setattr(models, "complete_json_retry", _fake_complete)

    personalization.research_personalization("Kairos Real Estate Partners, LLC", "Jane Doe",
                                              research={"own_domain": "kairosrep.com"})
    assert captured.get("reasoning_effort") == "minimal"
