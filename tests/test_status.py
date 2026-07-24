"""QA re-audit Part 7 (Feature 1) — src/status.prospect_brief_text unit
coverage: every required section (firm details incl. AUM value/verification
method/proxy basis/geography/capital stack/founded year, why-this-prospect,
who-we're-reaching incl. alternate count, personalization hooks w/ sources
or the thin-personalization note, draft status) at the data layer,
independent of the Telegram wiring (covered in test_telegram_bot.py).
"""
from __future__ import annotations

import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import dbc, status

_TEST_COMPANIES = ["Status Brief Firm"]


def _purge_test_connections() -> None:
    with dbc.conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM connections WHERE company_name_raw = ANY(%s)", (_TEST_COMPANIES,))
        c.commit()


@pytest.fixture(autouse=True)
def _clean_state():
    reset_testmode(verbose=False)
    _purge_test_connections()
    yield
    _purge_test_connections()


def _connection(url: str, name: str, company: str, designation: str = "Managing Director",
               email_seed: str | None = "poc@example.com") -> dict:
    dbc.bulk_insert_connections([{
        "linkedin_url": url, "poc_name": name, "designation": designation,
        "company_name_raw": company, "company_name_norm": company.lower(),
        "poc_email_seed": email_seed, "incomplete_row": False,
    }])
    return dbc.connection_by_url(url)


def _firm(**overrides) -> dict:
    run = dbc.create_wc_run("single_firm", query_firm="Status Brief Firm")
    fields = dict(
        icp_status="qualified", icp_reason="AUM ~$500M confirmed, qualifies",
        aum_tag="AUM_CONFIRMED",
        research_raw={"firm_oneliner": "Status Brief Firm is a test firm.",
                      "aum_display": "$500M", "geography": "US Northeast",
                      "capital_stack": "Permanent capital", "founded_year": 2005, "sources": []},
    )
    fields.update(overrides)
    return dbc.create_wc_firm(run["id"], "Status Brief Firm", **fields)


def test_prospect_brief_covers_every_required_section():
    top = _connection("https://linkedin.com/in/status-poc", "Morgan Ito", "Status Brief Firm")
    alt = _connection("https://linkedin.com/in/status-poc-alt", "Sam Reyes", "Status Brief Firm",
                      designation="Vice President")
    firm = _firm()
    dbc.update_wc_firm(firm["id"], top_poc_connection_id=top["id"],
                       alternate_pocs=[{"id": alt["id"], "poc_name": alt["poc_name"],
                                       "designation": alt.get("designation")}])
    dbc.create_wc_draft(
        firm["id"], top["id"], poc_email="morgan@statusbrief.example",
        email_status="drafted", gmail_draft_id="draft-123", ppt_attached=True,
        thin_personalization=False,
        personalization={"hook_1": "Recently closed Fund III", "hook_1_source": "https://example.com/a",
                        "hook_2": "Expanding into industrial", "hook_2_source": "https://example.com/b",
                        "thin_personalization": False},
    )
    firm = dbc.get_wc_firm(firm["id"])

    brief = status.prospect_brief_text(firm["id"])

    assert "PROSPECT BRIEF" in brief and "Status Brief Firm" in brief
    assert "Status Brief Firm is a test firm." in brief          # firm oneliner
    assert "$500M" in brief and "confirmed" in brief             # AUM value + verification method
    assert "US Northeast" in brief                                # geography
    assert "Permanent capital" in brief                           # capital stack
    assert "2005" in brief                                        # founded year
    assert "AUM ~$500M confirmed, qualifies" in brief             # why this prospect (icp_reason)
    assert "Morgan Ito" in brief and "Managing Director" in brief  # who we're reaching: name/title
    assert "Picked as top POC" in brief                           # why picked
    assert "1 alternate POC(s)" in brief                          # alternate count
    assert "Recently closed Fund III" in brief and "https://example.com/a" in brief  # hook + source
    assert "Expanding into industrial" in brief and "https://example.com/b" in brief
    assert "draft-123" in brief and "with PDF deck" in brief      # draft status: Gmail id, with deck


def test_prospect_brief_thin_personalization_note_when_research_too_thin():
    top = _connection("https://linkedin.com/in/status-thin", "Taylor Finch", "Status Brief Firm")
    firm = _firm()
    dbc.update_wc_firm(firm["id"], top_poc_connection_id=top["id"], alternate_pocs=[])
    dbc.create_wc_draft(
        firm["id"], top["id"], poc_email="taylor@statusbrief.example",
        email_status="drafted", gmail_draft_id="draft-thin", ppt_attached=False,
        thin_personalization=True,
        personalization={"hook_1": "", "hook_2": "", "thin_personalization": True},
    )
    firm = dbc.get_wc_firm(firm["id"])

    brief = status.prospect_brief_text(firm["id"])
    assert "thin_personalization fallback used" in brief
    assert "email only, no deck" in brief


def test_prospect_brief_estimated_aum_includes_proxy_basis():
    top = _connection("https://linkedin.com/in/status-est", "Reese Dunn", "Status Brief Firm")
    firm = _firm(
        aum_tag="AUM_ESTIMATED",
        icp_reason="QUALIFIED (est. $800M — invested capital proxy)",
        research_raw={"firm_oneliner": "Status Brief Firm is a test firm.",
                     "aum_display": "~$800M (estimated)", "sources": []},
    )
    dbc.update_wc_firm(firm["id"], top_poc_connection_id=top["id"], alternate_pocs=[])

    brief = status.prospect_brief_text(firm["id"])
    assert "estimated" in brief
    assert "invested capital proxy" in brief, "the proxy basis must be surfaced, not just the word 'estimated'"


def test_prospect_brief_no_draft_on_record():
    top = _connection("https://linkedin.com/in/status-nodraft", "Jesse Cole", "Status Brief Firm")
    firm = _firm()
    dbc.update_wc_firm(firm["id"], top_poc_connection_id=top["id"], alternate_pocs=[])

    brief = status.prospect_brief_text(firm["id"])
    assert "(no draft on record)" in brief
    assert "(no draft created)" in brief


def test_prospect_brief_missing_firm_returns_clear_message():
    brief = status.prospect_brief_text(999_999_999)
    assert "No prospect brief available" in brief
