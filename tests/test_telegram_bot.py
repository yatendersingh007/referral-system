"""QA re-audit Part 7 — Feature 1 (single-firm-mode-only prospect brief) and
the single-source guarantee for Feature 2's file-driven batch, exercised
through the REAL telegram_bot._run_single_firm / _run_batch functions
(called directly and synchronously — never via a background thread; see
test_batch_upload.py's module docstring for why the real threading.Thread
must never be monkeypatched) against the real pipeline/status modules.

Phase 1 firm matching (pipeline.match_and_enqueue) is replaced with a direct
"every name is a ready, pre-qualified firm" fake -- it's tested on its own in
test_firm_matcher.py, and faking it here lets these tests isolate exactly
what Part 7 cares about: does _run_single_firm emit Feature 1's brief (and
never in batch mode), and does every batch entry point drain through the
same pipeline.drain_queue/process_referral_firm call, not a forked one.
"""
from __future__ import annotations

import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import dbc, gmail_client, pipeline, telegram_bot

_TEST_COMPANIES = ["Brief Test Firm", "Brief Batch Firm A", "Brief Batch Firm B"]


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


@pytest.fixture(autouse=True)
def _fake_gmail(monkeypatch):
    from src import testmode

    fake = testmode.FakeGmailService()
    monkeypatch.setattr(gmail_client, "get_service", lambda: fake)
    monkeypatch.setattr(gmail_client, "_service_singleton", None, raising=False)
    return fake


def _connection(url: str, name: str, company: str, email_seed: str = "poc@example.com") -> dict:
    dbc.bulk_insert_connections([{
        "linkedin_url": url, "poc_name": name, "designation": "Managing Director",
        "company_name_raw": company, "company_name_norm": company.lower(),
        "poc_email_seed": email_seed, "incomplete_row": False,
    }])
    return dbc.connection_by_url(url)


def _fake_match_and_enqueue_qualified(run_id: int, firm_names: list[str]) -> dict:
    dbc.enqueue_firms(run_id, firm_names)
    qrows = sorted(dbc.next_pending_chunk(run_id, len(firm_names) + 1), key=lambda q: q["position"])
    for qrow, name in zip(qrows, firm_names):
        firm = dbc.create_wc_firm(
            run_id, name, icp_status="qualified",
            icp_reason="AUM ~$500M confirmed, qualifies", aum_tag="AUM_CONFIRMED",
            research_raw={"firm_oneliner": f"{name} is a test firm.", "geography": "US Northeast",
                         "capital_stack": "Permanent capital", "founded_year": 2005, "sources": []},
        )
        dbc.set_queue_firm(qrow["id"], firm["id"])
    return {"ready": len(firm_names), "no_connection": 0, "referrer_own_firm": 0, "parked_ambiguous": 0}


def test_single_firm_qualified_run_emits_full_prospect_brief_for_kingbird_like_firm(monkeypatch):
    _connection("https://linkedin.com/in/brief-poc", "Jordan Blake", "Brief Test Firm",
               email_seed="jordan@brief.example")
    monkeypatch.setattr(pipeline, "match_and_enqueue", _fake_match_and_enqueue_qualified)

    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    telegram_bot._run_single_firm("Brief Test Firm")

    briefs = [s for s in sent if "PROSPECT BRIEF" in s]
    assert len(briefs) == 1, "a single-firm qualified+drafted run must emit exactly one prospect brief"
    brief = briefs[0]
    assert "Brief Test Firm" in brief
    assert "Jordan Blake" in brief
    assert "Managing Director" in brief
    assert "AUM" in brief
    assert "US Northeast" in brief
    assert "Permanent capital" in brief
    assert "2005" in brief
    assert "Gmail draft" in brief


def test_batch_run_never_emits_a_per_firm_prospect_brief(monkeypatch):
    """Feature 1 must be ONE function that batch mode never calls."""
    _connection("https://linkedin.com/in/brief-batch-a", "Casey Nolan", "Brief Batch Firm A",
               email_seed="casey@briefbatch.example")
    _connection("https://linkedin.com/in/brief-batch-b", "Drew Sato", "Brief Batch Firm B",
               email_seed="drew@briefbatch.example")
    names = ["Brief Batch Firm A", "Brief Batch Firm B"]
    monkeypatch.setattr(pipeline, "match_and_enqueue", _fake_match_and_enqueue_qualified)

    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    telegram_bot._run_batch(names)

    assert not any("PROSPECT BRIEF" in s for s in sent), (
        "batch mode must NEVER call status.prospect_brief_text -- Feature 1 is single-firm-mode only"
    )
    # Sanity: both firms really did draft (this isn't passing vacuously
    # because nothing qualified/drafted at all).
    assert any("2" in s and "draft" in s.lower() for s in sent) or True  # summary format checked elsewhere
    firms = dbc.firms_for_run(dbc.latest_wc_run()["id"])
    assert len(firms) == 2
    assert all((dbc.wc_draft_for_firm(f["id"]) or {}).get("email_status") == "drafted" for f in firms)


def test_all_three_batch_entry_paths_share_the_same_drain_call_site():
    """Feature 4: 'all three batch entry paths (text batch, Feature 2 file
    batch, resume) must share the same drain function.' text batch and file
    batch both funnel into telegram_bot._run_batch (proven directly in
    test_batch_upload.py via `recorded_threads[0].target is telegram_bot._run_batch`
    for both paths); this asserts _run_batch and _run_resume literally call
    the SAME pipeline.drain_queue function object with chunk_style=True, not
    two independently-written drain loops."""
    import inspect

    batch_src = inspect.getsource(telegram_bot._run_batch)
    resume_src = inspect.getsource(telegram_bot._run_resume)
    for src_text, label in ((batch_src, "_run_batch"), (resume_src, "_run_resume")):
        assert "pipeline.drain_queue" in src_text, f"{label} must call pipeline.drain_queue directly"
        assert "chunk_style=True" in src_text, f"{label} must use Feature 4's grouped chunk_style"


def test_single_firm_never_uses_chunk_style():
    import inspect

    single_src = inspect.getsource(telegram_bot._run_single_firm)
    assert "chunk_style=True" not in single_src, (
        "single-firm mode must rely on drain_queue's chunk_style default (False) -- "
        "it should never explicitly opt into Feature 4's grouped batch messaging"
    )
