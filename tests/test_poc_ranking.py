"""POC eligibility/ranking (§4 Phase 3a). QA finding (real-mode Atlas Real
Estate run): the old exact-phrase whitelist rejected any senior title
outside a small finance/ops list, dead-ending a perfectly matched firm whose
only two contacts were "VP of Capital Markets" and "Investor Relations
Specialist". Eligibility is now by SENIORITY signal, not job function.
"""
from __future__ import annotations

from src import poc_ranking


def _conn(poc_id: int, name: str, designation: str) -> dict:
    return {"id": poc_id, "poc_name": name, "designation": designation}


# ---------------------------------------------------------------------------
# Explicitly requested cases
# ---------------------------------------------------------------------------
def test_vp_in_any_department_is_accepted():
    top, alternates = poc_ranking.rank_pocs([_conn(1, "Mike Hills", "VP of Capital Markets")])
    assert top is not None
    assert top["poc_name"] == "Mike Hills"
    assert "eligibility_flag" not in top


def test_director_is_accepted():
    top, _ = poc_ranking.rank_pocs([_conn(1, "Jane Doe", "Director of Asset Management")])
    assert top is not None
    assert top["poc_name"] == "Jane Doe"


def test_managing_partner_is_accepted():
    top, _ = poc_ranking.rank_pocs([_conn(1, "Alex Kim", "Managing Partner")])
    assert top is not None
    assert top["poc_name"] == "Alex Kim"


def test_investor_relations_specialist_is_rejected():
    top, alternates = poc_ranking.rank_pocs([_conn(1, "Donna Rodriguez", "Investor Relations Specialist")])
    assert top is None
    assert alternates == []


def test_analyst_is_rejected():
    top, _ = poc_ranking.rank_pocs([_conn(1, "Sam Lee", "Investment Analyst")])
    assert top is None


def test_associate_is_rejected():
    top, _ = poc_ranking.rank_pocs([_conn(1, "Pat Chen", "Associate")])
    assert top is None


def test_ambiguous_title_is_accepted_and_flagged():
    """Matches neither the junior denylist nor a senior tier -- better to
    surface a borderline contact for human review than dead-end a firm."""
    top, _ = poc_ranking.rank_pocs([_conn(1, "Robin Park", "Portfolio Manager")])
    assert top is not None
    assert top["poc_name"] == "Robin Park"
    assert top["eligibility_flag"] == "ambiguous"


# ---------------------------------------------------------------------------
# The exact Atlas Real Estate scenario that motivated this fix
# ---------------------------------------------------------------------------
def test_atlas_real_estate_scenario_mike_hills_wins_donna_rejected():
    connections = [
        _conn(1, "Mike Hills", "VP of Capital Markets"),
        _conn(2, "Donna Rodriguez", "Investor Relations Specialist"),
    ]
    top, alternates = poc_ranking.rank_pocs(connections)
    assert top is not None
    assert top["poc_name"] == "Mike Hills"
    assert alternates == []  # Donna is rejected outright, not merely ranked second


# ---------------------------------------------------------------------------
# Ranking preserved: widening eligibility must not change relative order
# ---------------------------------------------------------------------------
def test_c_suite_still_outranks_vp():
    connections = [
        _conn(1, "VP Person", "VP of Capital Markets"),
        _conn(2, "CFO Person", "Chief Financial Officer"),
    ]
    top, alternates = poc_ranking.rank_pocs(connections)
    assert top["poc_name"] == "CFO Person"
    assert alternates[0]["poc_name"] == "VP Person"


def test_confirmed_senior_outranks_ambiguous():
    connections = [
        _conn(1, "Ambiguous Person", "Portfolio Manager"),
        _conn(2, "Director Person", "Director of Acquisitions"),
    ]
    top, alternates = poc_ranking.rank_pocs(connections)
    assert top["poc_name"] == "Director Person"
    assert alternates[0]["poc_name"] == "Ambiguous Person"
    assert alternates[0]["eligibility_flag"] == "ambiguous"


# ---------------------------------------------------------------------------
# Edge case the junior-denylist regex must get right: "Assistant Vice
# President" / "Assistant VP" is a real VP-level title, not a junior one,
# even though it contains the word "assistant".
# ---------------------------------------------------------------------------
def test_assistant_vice_president_is_accepted_not_rejected_as_junior():
    top, _ = poc_ranking.rank_pocs([_conn(1, "Jordan Blake", "Assistant Vice President")])
    assert top is not None
    assert top["poc_name"] == "Jordan Blake"


def test_assistant_vp_abbreviation_is_accepted():
    top, _ = poc_ranking.rank_pocs([_conn(1, "Jordan Blake", "AVP, Capital Markets")])
    assert top is not None


def test_plain_assistant_title_is_still_rejected():
    """The AVP exception must not swallow a genuine junior "assistant"
    title that has nothing to do with VP level."""
    top, _ = poc_ranking.rank_pocs([_conn(1, "Casey Nguyen", "Executive Assistant")])
    assert top is None


# ---------------------------------------------------------------------------
# Empty / no-eligible-connections cases
# ---------------------------------------------------------------------------
def test_no_connections_returns_none_and_empty():
    assert poc_ranking.rank_pocs([]) == (None, [])


def test_all_rejected_returns_none_and_empty():
    connections = [_conn(1, "A", "Analyst"), _conn(2, "B", "Associate"), _conn(3, "C", "Intern")]
    assert poc_ranking.rank_pocs(connections) == (None, [])
