"""Scratch repro for reviewer finding: does a cancel-then-resume batch get
stuck showing a total that never reaches unity, because _drain_with_live_status
recomputes total = len(dbc.firms_for_run(run_id)) (ALL firms ever created for
the run) while drain_queue's `processed`/ordinal counter restarts at 0 for the
resumed call (which only sees the still-pending queue rows)?
"""
from __future__ import annotations

import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import dbc, gmail_client, pipeline, telegram_bot


@pytest.fixture(autouse=True)
def _clean_state():
    reset_testmode(verbose=False)
    yield


@pytest.fixture(autouse=True)
def _fake_gmail(monkeypatch):
    from src import testmode
    fake = testmode.FakeGmailService()
    monkeypatch.setattr(gmail_client, "get_service", lambda: fake)
    return fake


def _qualified_firm(run_id, name):
    return dbc.create_wc_firm(
        run_id, name, icp_status="qualified",
        icp_reason="AUM ~$500M confirmed, qualifies", aum_tag="AUM_CONFIRMED",
        research_raw={"firm_oneliner": f"{name} is a test firm.", "sources": []},
    )


def _enqueue_ready_firms(run_id, names):
    firms = {name: _qualified_firm(run_id, name) for name in names}
    dbc.enqueue_firms(run_id, names)
    qrows = dbc.next_pending_chunk(run_id, len(names) + 1)
    for q in qrows:
        dbc.set_queue_firm(q["id"], firms[q["firm_name_input"]]["id"])
    return qrows


def test_resume_after_cancel_total_mismatch(monkeypatch):
    run = dbc.create_wc_run("batch")
    names = [f"Cancel Test Firm {i}" for i in range(5)]
    _enqueue_ready_firms(run["id"], names)

    call_count = {"n": 0}

    def _fake_process(firm_id, breaker=None, progress=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            dbc.update_wc_run(run["id"], cancel_requested_at="2026-07-26T00:00:00Z")
        return {"outcome": "drafted"}

    monkeypatch.setattr(pipeline, "process_referral_firm", _fake_process)
    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: 1)
    edits = []
    monkeypatch.setattr(telegram_bot, "_edit_message",
                         lambda mid, text, chat_id=None: edits.append(text) or True)
    monkeypatch.setattr(telegram_bot, "LIVE_STATUS_MIN_INTERVAL_SEC", 0)
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: None)

    # --- Phase 1: initial drain, cancelled after 2 firms ---
    live1 = telegram_bot._LiveBatchStatus()
    live1.start(5)
    totals1 = pipeline.drain_queue(run["id"], progress=live1, notify=lambda m: None,
                                    chunk_size=25, chunk_style=True)
    print("=== totals1 ===", totals1)
    assert totals1["aborted"] is True
    assert totals1["cancelled"] is True
    assert totals1["processed"] == 2

    counts = dbc.queue_counts(run["id"])
    print("=== queue_counts after cancel ===", counts)
    assert counts.get("done") == 2
    assert counts.get("pending") == 3

    # --- Phase 2: resume, exactly as _run_resume does (telegram_bot.py) ---
    for q in dbc.infra_paused_firms(run["id"]):
        dbc.set_queue_status(q["id"], "pending")
    dbc.update_wc_run(run["id"], status="running", cancel_requested_at=None)

    call_count["n"] = 0  # fresh process call-count for the resumed drain; no more cancel this time
    monkeypatch.setattr(pipeline, "process_referral_firm",
                         lambda firm_id, breaker=None, progress=None: {"outcome": "drafted"})

    totals2 = telegram_bot._drain_with_live_status(run["id"])
    print("=== totals2 ===", totals2)
    print("=== all edits ===")
    for e in edits:
        print(repr(e))

    assert totals2["aborted"] is False
    final_text = edits[-1] if edits else None
    print("=== FINAL LIVE STATUS TEXT ===")
    print(final_text)

    # Reviewer's claim: the run genuinely finished (aborted=False, all 5 firms
    # terminal) but the displayed fraction never reaches 5/5.
    all_firms = dbc.firms_for_run(run["id"])
    final_counts = dbc.queue_counts(run["id"])
    print("=== final queue_counts ===", final_counts)
    assert final_counts.get("done") == 5, "run genuinely finished: all 5 queue rows are done"

    print("=== ASSERTION: does final text show 5/5? ===")
    print("5/5" in (final_text or ""))
    print("=== ASSERTION: does final text show 3/5 (stuck) ===")
    print("3/5" in (final_text or ""))
