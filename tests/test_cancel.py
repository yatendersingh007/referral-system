"""Feature B — /cancel (alias /referral stop) stops an active batch cleanly.
Covers the pipeline-level drain_queue cancellation mechanics (checked
between firms, never mid-firm) and the Telegram command layer (_handle_cancel,
the /referral stop alias, authorization inheritance, and resume-after-cancel).
"""
from __future__ import annotations

import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import dbc, gmail_client, pipeline, telegram_bot

_TEST_COMPANIES = [f"Cancel Test Firm {i}" for i in range(6)]


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


def _connection(url: str, name: str, company: str, email_seed: str = "x@example.com") -> None:
    dbc.bulk_insert_connections([{
        "linkedin_url": url, "poc_name": name, "designation": "Managing Director",
        "company_name_raw": company, "company_name_norm": company.lower(),
        "poc_email_seed": email_seed, "incomplete_row": False,
    }])


def _qualified_firm(run_id: int, name: str) -> dict:
    return dbc.create_wc_firm(
        run_id, name, icp_status="qualified",
        icp_reason="AUM ~$500M confirmed, qualifies", aum_tag="AUM_CONFIRMED",
        research_raw={"firm_oneliner": f"{name} is a test firm.", "sources": []},
    )


def _enqueue_batch(names: list[str]) -> dict:
    run = dbc.create_wc_run("batch")
    dbc.enqueue_firms(run["id"], names)
    qrows = sorted(dbc.next_pending_chunk(run["id"], len(names) + 1), key=lambda q: q["position"])
    for qrow, name in zip(qrows, names):
        firm = _qualified_firm(run["id"], name)
        dbc.set_queue_firm(qrow["id"], firm["id"])
    return run


# ---------------------------------------------------------------------------
# Pipeline-level: drain_queue cancellation mechanics
# ---------------------------------------------------------------------------
def test_cancel_stops_draining_after_current_firm_leaves_rest_pending(monkeypatch, _fake_gmail):
    """Cancellation requested WHILE firm 2 of 5 is in flight (simulated by
    calling request_run_cancellation as a side effect right after firm 2's
    REAL processing completes) must: finish firm 2 cleanly (it's for real
    processed and drafted, not abandoned), never touch firms 3-5 at all
    (left 'pending'), and mark the run cancelled."""
    for i in range(5):
        _connection(f"https://linkedin.com/in/cancel-test-{i}", f"Person {i}", f"Cancel Test Firm {i}",
                   email_seed=f"person{i}@canceltest.example")
    names = [f"Cancel Test Firm {i}" for i in range(5)]
    run = _enqueue_batch(names)

    real_process = pipeline.process_referral_firm
    calls = []

    def _wrapped(firm_id, breaker=None, progress=None):
        result = real_process(firm_id, breaker, progress)
        calls.append(firm_id)
        if len(calls) == 2:
            # Simulates an operator's /cancel arriving while firm 2 was
            # mid-flight -- by the time firm 2's processing actually
            # finishes, the flag is set, exactly as a real concurrent
            # Telegram command would leave it.
            dbc.request_run_cancellation(run["id"])
        return result

    monkeypatch.setattr(pipeline, "process_referral_firm", _wrapped)
    totals = pipeline.drain_queue(run["id"], chunk_size=25, chunk_style=True, notify=lambda *_a: None)

    assert totals["cancelled"] is True
    assert totals["aborted"] is True
    assert totals["processed"] == 2, "only the 2 firms fully processed before the flag was noticed"
    assert len(_fake_gmail.created_drafts) == 2, "both already-processed firms kept their real drafts"

    run_row = dbc.get_wc_run(run["id"])
    assert run_row["status"] == "cancelled"

    counts = dbc.queue_counts(run["id"])
    assert counts.get("pending", 0) == 3, "firms 3-5 must never have been touched, left pending"


def test_cancel_flag_checked_before_processing_never_mid_firm(monkeypatch):
    """The flag being ALREADY set before drain_queue even starts must stop
    it before firm 1 is ever touched -- proves the check happens strictly
    BETWEEN firms, at the very first opportunity, never abandoning partial
    work (there's no partial work here since nothing started)."""
    names = ["Cancel Test Firm 0", "Cancel Test Firm 1"]
    run = _enqueue_batch(names)
    dbc.request_run_cancellation(run["id"])

    called = []
    monkeypatch.setattr(pipeline, "process_referral_firm",
                       lambda firm_id, breaker=None, progress=None: called.append(firm_id))
    totals = pipeline.drain_queue(run["id"], chunk_size=25, chunk_style=True, notify=lambda *_a: None)

    assert called == [], "process_referral_firm must never be called once cancellation is already flagged"
    assert totals["cancelled"] is True
    assert totals["processed"] == 0
    assert dbc.queue_counts(run["id"]).get("pending", 0) == 2


def test_cancel_summary_reports_processed_and_remaining_counts(monkeypatch, _fake_gmail):
    """telegram_bot._fmt_summary, given a cancelled run's totals, must report
    what was processed AND how many remain AND the resume note -- not a bare
    'done' message."""
    for i in range(3):
        _connection(f"https://linkedin.com/in/cancel-summary-{i}", f"Summary Person {i}",
                   f"Cancel Test Firm {i}", email_seed=f"summary{i}@canceltest.example")
    names = [f"Cancel Test Firm {i}" for i in range(3)]
    run = _enqueue_batch(names)

    real_process = pipeline.process_referral_firm
    calls = []

    def _wrapped(firm_id, breaker=None, progress=None):
        result = real_process(firm_id, breaker, progress)
        calls.append(firm_id)
        if len(calls) == 1:
            dbc.request_run_cancellation(run["id"])
        return result

    monkeypatch.setattr(pipeline, "process_referral_firm", _wrapped)
    totals = pipeline.drain_queue(run["id"], chunk_size=25, chunk_style=True, notify=lambda *_a: None)
    dbc.update_wc_run(run["id"], status="cancelled")

    summary = telegram_bot._fmt_summary(run["id"], totals)
    assert "cancelled" in summary.lower()
    assert "1 draft(s) delivered" in summary or "✉️ 1 draft" in summary
    assert "2 firm(s) remain unprocessed" in summary
    assert "/referral resume" in summary


# ---------------------------------------------------------------------------
# Telegram command layer
# ---------------------------------------------------------------------------
def test_cancel_with_no_active_run_replies_nothing_to_cancel(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    telegram_bot._handle_cancel()
    assert sent == ["No run in progress to cancel."]


def test_cancel_with_active_run_sets_flag_and_acknowledges(monkeypatch):
    run = dbc.create_wc_run("batch")
    dbc.update_wc_run(run["id"], status="running")
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._handle_cancel()

    assert len(sent) == 1
    assert f"run #{run['id']}" in sent[0]
    assert "resume" in sent[0].lower()
    run_row = dbc.get_wc_run(run["id"])
    assert run_row["cancel_requested_at"] is not None


def test_cancel_dispatch_top_level_command(monkeypatch):
    run = dbc.create_wc_run("batch")
    dbc.update_wc_run(run["id"], status="running")
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/cancel")

    assert dbc.get_wc_run(run["id"])["cancel_requested_at"] is not None
    assert sent


def test_referral_stop_is_an_alias_for_cancel(monkeypatch):
    run = dbc.create_wc_run("batch")
    dbc.update_wc_run(run["id"], status="running")
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral stop")

    assert dbc.get_wc_run(run["id"])["cancel_requested_at"] is not None
    assert sent


def test_cancel_authorization_inherited_from_dispatch_boundary():
    """Per the spec: /cancel obeys the SAME AUTHORIZED_USER_IDS allowlist as
    every other command. _dispatch/_handle_cancel don't re-check it
    themselves -- run_bot()'s polling loop filters senders BEFORE ever
    calling _dispatch, uniformly for every command. Confirmed by source
    inspection: no separate auth branch exists for cancel, matching every
    other command in _dispatch."""
    import inspect

    src = inspect.getsource(telegram_bot.run_bot)
    assert "allowed" in src and "sender" in src, (
        "authorization must be enforced once, upstream, in run_bot()'s polling loop"
    )
    cancel_src = inspect.getsource(telegram_bot._handle_cancel)
    assert "AUTHORIZED_USER_IDS" not in cancel_src, (
        "no separate auth check should exist in _handle_cancel -- it inherits the same gate as every command"
    )


# ---------------------------------------------------------------------------
# Resume after cancel
# ---------------------------------------------------------------------------
def test_cancelled_run_can_be_resumed_and_continues_without_redrafting(monkeypatch):
    for i in range(5):
        _connection(f"https://linkedin.com/in/resume-cancel-{i}", f"Resume Person {i}",
                   f"Cancel Test Firm {i}", email_seed=f"resume{i}@canceltest.example")
    names = [f"Cancel Test Firm {i}" for i in range(5)]
    run = _enqueue_batch(names)

    real_process = pipeline.process_referral_firm
    calls = []

    def _wrapped(firm_id, breaker=None, progress=None):
        result = real_process(firm_id, breaker, progress)
        calls.append(firm_id)
        if len(calls) == 2:
            dbc.request_run_cancellation(run["id"])
        return result

    monkeypatch.setattr(pipeline, "process_referral_firm", _wrapped)
    totals = pipeline.drain_queue(run["id"], chunk_size=25, chunk_style=True, notify=lambda *_a: None)
    dbc.update_wc_run(run["id"], status="cancelled")
    assert totals["processed"] == 2

    firms = dbc.firms_for_run(run["id"])
    first_two_draft_ids = {f["id"]: (dbc.wc_draft_for_firm(f["id"]) or {}).get("gmail_draft_id")
                           for f in firms if dbc.wc_draft_for_firm(f["id"])}
    assert len(first_two_draft_ids) == 2

    # /referral resume must find this CANCELLED run (not just crashed ones).
    resumable = dbc.latest_resumable_run()
    assert resumable and resumable["id"] == run["id"]

    monkeypatch.setattr(pipeline, "process_referral_firm", real_process)  # restore for resume
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    def _fake_reload_keys():
        pass  # no real Tavily pool involved in this test

    from lib_snapshot.researcher import search_client
    monkeypatch.setattr(search_client, "reload_keys", _fake_reload_keys)

    telegram_bot._run_resume()

    final_run = dbc.get_wc_run(run["id"])
    assert final_run["status"] == "done"
    assert final_run["cancel_requested_at"] is None, "resuming must clear the old flag, not re-cancel immediately"

    for firm in dbc.firms_for_run(run["id"]):
        draft = dbc.wc_draft_for_firm(firm["id"])
        assert draft and draft["email_status"] == "drafted"
        if firm["id"] in first_two_draft_ids:
            assert draft["gmail_draft_id"] == first_two_draft_ids[firm["id"]], (
                "the 2 firms drafted before cancellation must NOT be re-drafted on resume"
            )
    assert dbc.queue_counts(run["id"]).get("pending", 0) == 0


def test_resume_calls_reload_keys_for_feature_a_mid_run_key_top_up(monkeypatch):
    """Confirms /referral resume actually calls search_client.reload_keys()
    (Feature A's mid-run key top-up requirement), not just that it happens
    to work when mocked out."""
    run = dbc.create_wc_run("batch")
    dbc.update_wc_run(run["id"], status="cancelled", cancel_requested_at=None)

    from lib_snapshot.researcher import search_client
    reload_calls = []
    monkeypatch.setattr(search_client, "reload_keys", lambda: reload_calls.append(True))
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: None)
    monkeypatch.setattr(pipeline, "drain_queue",
                       lambda *a, **kw: {"processed": 0, "aborted": False})

    telegram_bot._run_resume()
    assert reload_calls == [True]
