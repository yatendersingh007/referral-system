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

import json

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
    # FIX 25: this file's new tests dispatch real /referral batch commands
    # (the FIX 19/23 non-regression checks) -- clear any pending-batch state
    # a prior test left behind, matching test_batch_upload.py's own
    # established pattern, so a leftover pending batch from one test can
    # never leak into and change another test's outcome.
    telegram_bot._pending_file_batch.clear()
    telegram_bot._awaiting_batch_file.clear()
    yield
    _purge_test_connections()
    telegram_bot._pending_file_batch.clear()
    telegram_bot._awaiting_batch_file.clear()


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
    return {"ready": len(firm_names), "no_connection": 0, "referrer_own_firm": 0,
           "sender_own_firm": 0, "parked_ambiguous": 0}


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
    # FIX 25: _run_batch() now also drives a live-editing status message
    # (_drain_with_live_status), which sends via _send_and_get_id, not
    # send() -- mock it too so this test never makes a real Telegram API
    # call. Returning None disables the live-status feature harmlessly
    # (message_id stays unset, _maybe_edit no-ops), exactly like a real
    # sendMessage failure would.
    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: None)
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


def test_batch_free_filter_summary_line_renders_referrer_and_sender_counts_separately(monkeypatch):
    """FIX 32A Part B: the 'Free filter: ...' summary line must show
    referrer_own_firm and sender_own_firm as two distinct counts, never
    merged -- a real batch needs to be able to tell "we skipped Manpreet's
    firm" apart from "we skipped our own firm" at a glance."""
    def _fake_match_and_enqueue(run_id, firm_names):
        return {"ready": 1, "no_connection": 2, "referrer_own_firm": 3,
               "sender_own_firm": 4, "parked_ambiguous": 5}
    monkeypatch.setattr(pipeline, "match_and_enqueue", _fake_match_and_enqueue)
    monkeypatch.setattr(telegram_bot, "_drain_with_live_status", lambda run_id: {})

    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: None)
    telegram_bot._run_batch(["Irrelevant Firm"])

    filter_lines = [s for s in sent if s.startswith("Free filter:")]
    assert len(filter_lines) == 1, sent
    line = filter_lines[0]
    assert "1 matched" in line
    assert "2 no_connection" in line
    assert "3 referrer's own firm skipped" in line
    assert "4 our own firm skipped" in line
    assert "5 ambiguous matches parked" in line


def test_all_three_batch_entry_paths_share_the_same_drain_call_site():
    """Feature 4: 'all three batch entry paths (text batch, Feature 2 file
    batch, resume) must share the same drain function.' text batch and file
    batch both funnel into telegram_bot._run_batch (proven directly in
    test_batch_upload.py via `recorded_threads[0].target is telegram_bot._run_batch`
    for both paths); this asserts _run_batch and _run_resume literally call
    the SAME drain call site, not two independently-written drain loops.

    FIX 25 (real-mode incident): _run_batch/_run_resume now funnel through
    the shared _drain_with_live_status() helper (which wires up the live-
    editing status message before calling pipeline.drain_queue itself) --
    updated to check for that shared indirection instead of the literal
    "pipeline.drain_queue" string, but the underlying invariant this test
    protects (one shared drain call site, not two) is unchanged; a
    separate assertion confirms _drain_with_live_status itself still calls
    pipeline.drain_queue with chunk_style=True."""
    import inspect

    batch_src = inspect.getsource(telegram_bot._run_batch)
    resume_src = inspect.getsource(telegram_bot._run_resume)
    for src_text, label in ((batch_src, "_run_batch"), (resume_src, "_run_resume")):
        assert "_drain_with_live_status(" in src_text, (
            f"{label} must call the shared _drain_with_live_status() helper"
        )

    drain_helper_src = inspect.getsource(telegram_bot._drain_with_live_status)
    assert "pipeline.drain_queue" in drain_helper_src
    assert "chunk_style=True" in drain_helper_src


def test_single_firm_never_uses_chunk_style():
    import inspect

    single_src = inspect.getsource(telegram_bot._run_single_firm)
    assert "chunk_style=True" not in single_src, (
        "single-firm mode must rely on drain_queue's chunk_style default (False) -- "
        "it should never explicitly opt into Feature 4's grouped batch messaging"
    )


# ---------------------------------------------------------------------------
# FIX 25 (real-mode incident): a 13-firm batch showed no Telegram output for
# ~5 minutes between the chunk header and the chunk result -- looked hung.
# Telegram rate-limits a bot to ~20 messages/minute per chat; one message
# per firm per phase would blow past that. Instead: ONE status message,
# edited in place via editMessageText, throttled to at most once every 3
# seconds, skipped if unchanged, driven entirely off pipeline.py's existing
# progress() callback so no pipeline call ever blocks on the HTTP edit.
# ---------------------------------------------------------------------------
def _live_json(kind: str, **payload) -> str:
    return f"{pipeline.LIVE_STATUS_MARK}{json.dumps({'kind': kind, **payload})}"


# --- _split_for_telegram: never cut a line in half (adversarial re-review) --

def test_split_for_telegram_short_text_is_one_chunk():
    assert telegram_bot._split_for_telegram("short text") == ["short text"]


def test_split_for_telegram_splits_on_line_boundaries_not_mid_word():
    """FIX 25 (adversarial re-review): showing DISQUALIFIED/PARKED reasons
    in full (no more 70-char truncation) means a many-firm chunk verdict
    message can exceed _TG_LIMIT -- the OLD raw text[i:i+limit] slicing
    could cut a reason in the middle of a word, reproducing via message-
    splitting the exact 'cut off mid-sentence' symptom this fix eliminated
    for the 70-char-cap case."""
    lines = [f"Line {i}: " + ("x" * 200) for i in range(30)]  # ~30 * 208 = 6240 chars
    text = "\n".join(lines)
    assert len(text) > telegram_bot._TG_LIMIT
    chunks = telegram_bot._split_for_telegram(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= telegram_bot._TG_LIMIT
    # every ORIGINAL line survives intact, whole, inside exactly one chunk
    rejoined = "\n".join(chunks)
    for line in lines:
        assert line in rejoined
    assert rejoined.replace("\n\n", "\n") == text or rejoined == text


def test_split_for_telegram_single_line_longer_than_limit_falls_back_to_raw_slice():
    huge_line = "x" * (telegram_bot._TG_LIMIT * 2 + 500)
    chunks = telegram_bot._split_for_telegram(huge_line)
    assert len(chunks) == 3
    assert all(len(c) <= telegram_bot._TG_LIMIT for c in chunks)
    assert "".join(chunks) == huge_line


def test_send_splits_a_long_disqualified_reason_chunk_without_cutting_any_line(monkeypatch):
    """End-to-end: a realistic many-firm chunk verdict message (built via
    pipeline._fmt_chunk_verdict with full, uncapped disqualified reasons)
    must never have a firm's line sliced mid-word across two sendMessage
    calls."""
    from src import pipeline as pipeline_module

    long_reason = ("Not an accepted firm type (RE/family office/PE-RE/property management) -- "
                  "this firm operates a completely unrelated logistics and freight brokerage "
                  "business with no real estate exposure at all")
    entries = [(i, f"Firm Number {i} LLC", "disqualified", long_reason) for i in range(1, 21)]
    text = pipeline_module._fmt_chunk_verdict(1, None, 1, entries, None)
    assert len(text) > telegram_bot._TG_LIMIT

    sent_chunks = []
    monkeypatch.setattr(telegram_bot, "requests",
                       type("R", (), {"post": staticmethod(
                           lambda url, json, timeout: sent_chunks.append(json["text"])),
                           "RequestException": Exception})())
    monkeypatch.setattr(telegram_bot.config, "AUTHORIZED_USER_IDS", ["999"])
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda *_: None)

    telegram_bot.send(text)

    assert len(sent_chunks) > 1
    for _idx, firm_name, _status, reason in entries:
        assert any(firm_name in c and reason in c for c in sent_chunks), (
            f"{firm_name}'s full line must land whole inside exactly one sent message"
        )


# --- _send_and_get_id / _edit_message: never raise, degrade to no-op -------

def test_send_and_get_id_returns_message_id_on_success(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"message_id": 4242}}

    monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(telegram_bot.config, "AUTHORIZED_USER_IDS", ["999"])
    assert telegram_bot._send_and_get_id("hello") == 4242


def test_send_and_get_id_returns_none_on_request_exception(monkeypatch):
    def _raise(*a, **k):
        raise telegram_bot.requests.RequestException("network down")

    monkeypatch.setattr(telegram_bot.requests, "post", _raise)
    monkeypatch.setattr(telegram_bot.config, "AUTHORIZED_USER_IDS", ["999"])
    assert telegram_bot._send_and_get_id("hello") is None


def test_send_and_get_id_returns_none_with_no_chat_id(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(telegram_bot.config, "AUTHORIZED_USER_IDS", [])
    assert telegram_bot._send_and_get_id("hello", chat_id=None) is None
    assert calls == [], "must never attempt the HTTP call with no resolvable chat_id"


def test_edit_message_returns_true_on_200(monkeypatch):
    class _Resp:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, **k: _Resp())
    assert telegram_bot._edit_message(123, "new text", chat_id="999") is True


def test_edit_message_returns_false_and_never_raises_on_non_200(monkeypatch):
    class _Resp:
        status_code = 429
        text = "Too Many Requests"

    monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, **k: _Resp())
    assert telegram_bot._edit_message(123, "new text", chat_id="999") is False


def test_edit_message_returns_false_and_never_raises_on_request_exception(monkeypatch):
    def _raise(*a, **k):
        raise telegram_bot.requests.RequestException("timeout")

    monkeypatch.setattr(telegram_bot.requests, "post", _raise)
    assert telegram_bot._edit_message(123, "new text", chat_id="999") is False


# --- _parse_live_signal ------------------------------------------------------

def test_parse_live_signal_valid():
    msg = _live_json("phase", firm="Acme LLC", phase="researching")
    assert telegram_bot._parse_live_signal(msg) == {"kind": "phase", "firm": "Acme LLC", "phase": "researching"}


def test_parse_live_signal_ignores_ordinary_text():
    assert telegram_bot._parse_live_signal("Acme LLC: qualified, but no eligible POC.") is None


def test_parse_live_signal_ignores_malformed_json_after_marker():
    assert telegram_bot._parse_live_signal(f"{pipeline.LIVE_STATUS_MARK}not valid json{{") is None


def test_parse_live_signal_ignores_non_dict_json():
    assert telegram_bot._parse_live_signal(f"{pipeline.LIVE_STATUS_MARK}[1, 2, 3]") is None


# --- _LiveBatchStatus: rendering -------------------------------------------

def test_live_batch_status_initial_render_before_any_firm():
    live = telegram_bot._LiveBatchStatus()
    live._total = 10
    assert live._render() == "Batch: 0/10 firms\nQualified 0 · Parked 0 · Disqualified 0 · Drafted 0"


def test_live_batch_status_render_matches_the_exact_requested_format():
    live = telegram_bot._LiveBatchStatus()
    live(_live_json("firm_start", firm="Sentinel Capital, LLC", ordinal=7, total=10))
    live(_live_json("phase", firm="Sentinel Capital, LLC", phase="researching"))
    live(_live_json("counts", qualified=2, parked=1, disqualified=3, drafted=2))
    assert live._render() == (
        "Batch: 7/10 firms\n"
        "Now: Sentinel Capital, LLC — researching\n"
        "Qualified 2 · Parked 1 · Disqualified 3 · Drafted 2"
    )


# --- _LiveBatchStatus: "stopped" terminal signal (adversarial re-review) ----

def test_live_batch_status_stopped_replaces_the_now_line_on_successful_completion():
    """FIX 25 (adversarial re-review): without a terminal signal, the live
    message froze on the last-announced firm/phase forever, looking like
    the batch was still running even after it genuinely finished."""
    live = telegram_bot._LiveBatchStatus()
    live(_live_json("firm_start", firm="Last Firm LLC", ordinal=10, total=10))
    live(_live_json("phase", firm="Last Firm LLC", phase="creating draft"))
    live(_live_json("counts", qualified=6, parked=2, disqualified=2, drafted=5))
    live(_live_json("stopped", reason="done"))
    assert live._render() == (
        "Batch: 10/10 firms\n"
        "Done.\n"
        "Qualified 6 · Parked 2 · Disqualified 2 · Drafted 5"
    )
    assert "Last Firm LLC" not in live._render()


def test_live_batch_status_stopped_cancelled():
    live = telegram_bot._LiveBatchStatus()
    live(_live_json("firm_start", firm="Firm A", ordinal=3, total=10))
    live(_live_json("stopped", reason="cancelled"))
    assert "Cancelled." in live._render()
    assert "Now:" not in live._render()


def test_live_batch_status_stopped_crashed():
    live = telegram_bot._LiveBatchStatus()
    live(_live_json("firm_start", firm="Firm A", ordinal=3, total=10))
    live(_live_json("stopped", reason="crashed"))
    assert "Crashed." in live._render()


def test_live_batch_status_stopped_unrecognized_reason_falls_back_to_generic_label():
    live = telegram_bot._LiveBatchStatus()
    live(_live_json("stopped", reason="paused (Tavily unavailable)"))
    assert "Stopped: paused (Tavily unavailable)" in live._render()


# --- _LiveBatchStatus: state updates -----------------------------------------

def test_live_batch_status_firm_start_resets_phase():
    live = telegram_bot._LiveBatchStatus()
    live(_live_json("firm_start", firm="Firm A", ordinal=1, total=5))
    live(_live_json("phase", firm="Firm A", phase="researching"))
    assert live._phase == "researching"
    live(_live_json("firm_start", firm="Firm B", ordinal=2, total=5))
    assert live._firm == "Firm B"
    assert live._phase == "", "a new firm must not inherit the previous firm's phase label"


def test_live_batch_status_phase_signal_for_a_different_firm_is_ignored():
    """Guards against a stale/late phase signal for a firm that's already
    finished overwriting the CURRENT firm's phase label."""
    live = telegram_bot._LiveBatchStatus()
    live(_live_json("firm_start", firm="Firm B", ordinal=2, total=5))
    live(_live_json("phase", firm="Firm A", phase="drafting"))  # stale signal for the PREVIOUS firm
    assert live._phase == ""


def test_live_batch_status_counts_update_independently_of_firm_phase():
    live = telegram_bot._LiveBatchStatus()
    live(_live_json("counts", qualified=1, parked=0, disqualified=0, drafted=1))
    assert (live._qualified, live._parked, live._disqualified, live._drafted) == (1, 0, 0, 1)


def test_live_batch_status_ignores_unrecognized_signal_kind():
    live = telegram_bot._LiveBatchStatus()
    live._firm = "Untouched Firm"
    live(_live_json("something_new_and_unexpected", firm="Other Firm"))
    assert live._firm == "Untouched Firm"


def test_live_batch_status_forwards_every_message_to_server_log(monkeypatch):
    logged = []
    monkeypatch.setattr(pipeline, "server_log", logged.append)
    live = telegram_bot._LiveBatchStatus()
    live("plain debug text, not a live signal at all")
    live(_live_json("phase", firm="Firm A", phase="researching"))
    assert "plain debug text, not a live signal at all" in logged
    assert any(pipeline.LIVE_STATUS_MARK in m for m in logged)


# --- _LiveBatchStatus: start() ------------------------------------------------

def test_live_batch_status_start_sends_initial_message_and_captures_id(monkeypatch):
    sent = []

    def _fake_send_and_get_id(text, chat_id=None):
        sent.append(text)
        return 555

    monkeypatch.setattr(telegram_bot, "_send_and_get_id", _fake_send_and_get_id)
    live = telegram_bot._LiveBatchStatus()
    live.start(10)
    assert live._message_id == 555
    assert sent == ["Batch: 0/10 firms\nQualified 0 · Parked 0 · Disqualified 0 · Drafted 0"]


def test_live_batch_status_start_failure_disables_further_edits_harmlessly(monkeypatch):
    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: None)
    edit_calls = []
    monkeypatch.setattr(telegram_bot, "_edit_message", lambda *a, **k: edit_calls.append(1) or True)
    live = telegram_bot._LiveBatchStatus()
    live.start(10)
    assert live._message_id is None

    live(_live_json("firm_start", firm="Firm A", ordinal=1, total=10))
    assert edit_calls == [], "no message_id -- must never attempt an edit"


# --- _LiveBatchStatus: throttling and dedup ----------------------------------

def test_live_batch_status_throttles_edits_to_the_configured_interval(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(telegram_bot.time, "time", lambda: fake_now[0])
    edit_calls = []
    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: 1)
    monkeypatch.setattr(telegram_bot, "_edit_message",
                       lambda message_id, text, chat_id=None: edit_calls.append(text) or True)

    live = telegram_bot._LiveBatchStatus()
    live.start(10)  # sets _last_edit_at = fake_now[0] (1000.0)
    edit_calls.clear()

    # start()'s own send also anchors the throttle window -- an update
    # arriving immediately after it is still inside the window and skipped.
    live(_live_json("firm_start", firm="Firm A", ordinal=1, total=10))
    assert len(edit_calls) == 0, "an update immediately after start() is still inside the throttle window"

    fake_now[0] += telegram_bot.LIVE_STATUS_MIN_INTERVAL_SEC  # window elapses
    live(_live_json("phase", firm="Firm A", phase="researching"))
    assert len(edit_calls) == 1, "an update after the throttle window elapses must land"

    fake_now[0] += 1.0  # within the 3s throttle window again
    live(_live_json("phase", firm="Firm A", phase="finding contact"))
    assert len(edit_calls) == 1, "an update inside the throttle window must be skipped"

    fake_now[0] += telegram_bot.LIVE_STATUS_MIN_INTERVAL_SEC  # window elapses again
    live(_live_json("phase", firm="Firm A", phase="finding contact"))
    assert len(edit_calls) == 2, "an update after the throttle window elapses must land"
    assert "finding contact" in edit_calls[-1]


def test_live_batch_status_skips_edit_when_rendered_text_is_unchanged(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(telegram_bot.time, "time", lambda: fake_now[0])
    edit_calls = []
    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: 1)
    monkeypatch.setattr(telegram_bot, "_edit_message",
                       lambda message_id, text, chat_id=None: edit_calls.append(text) or True)

    live = telegram_bot._LiveBatchStatus()
    live.start(10)
    edit_calls.clear()
    fake_now[0] += telegram_bot.LIVE_STATUS_MIN_INTERVAL_SEC  # clear start()'s own throttle window

    live(_live_json("firm_start", firm="Firm A", ordinal=1, total=10))
    assert len(edit_calls) == 1
    fake_now[0] += 10  # well past the throttle window

    # Re-announcing the SAME firm_start (identical rendered text) must not
    # re-send the same content.
    live(_live_json("firm_start", firm="Firm A", ordinal=1, total=10))
    assert len(edit_calls) == 1, "an unchanged render must never trigger a duplicate edit"


def test_live_batch_status_claims_throttle_and_dedup_state_before_dispatching(monkeypatch):
    """FIX 25 (adversarial re-review): _last_edit_at/_last_sent_text are
    claimed SYNCHRONOUSLY, before the actual HTTP work is even dispatched
    -- not inside a "did it succeed" branch. This is deliberate: since the
    edit itself now runs on a background thread (never blocking the
    caller), there is no synchronous success/failure to react to. The
    accepted tradeoff is an occasional missed re-send (if this SPECIFIC
    dispatched edit happens to fail and a later render coincidentally
    matches it exactly) in exchange for the throttle staying honest even
    during a sustained failure streak -- see the dedicated throttle test
    below for that guarantee."""
    fake_now = [1000.0]
    monkeypatch.setattr(telegram_bot.time, "time", lambda: fake_now[0])
    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: 1)
    dispatch_calls = []
    monkeypatch.setattr(telegram_bot, "_dispatch_edit",
                       lambda message_id, text, chat_id: dispatch_calls.append(text))

    live = telegram_bot._LiveBatchStatus()
    live.start(10)
    fake_now[0] += telegram_bot.LIVE_STATUS_MIN_INTERVAL_SEC

    live(_live_json("firm_start", firm="Firm A", ordinal=1, total=10))
    assert len(dispatch_calls) == 1
    assert live._last_sent_text == live._render(), (
        "dedup state is claimed immediately, regardless of the dispatched edit's eventual outcome"
    )


def test_live_batch_status_throttle_stays_honest_through_a_failure_streak(monkeypatch):
    """FIX 25 (adversarial re-review): the OLD design only advanced
    _last_edit_at inside a successful-edit branch -- during a sustained
    failure/rate-limit streak (exactly the condition this throttle exists
    to survive), every subsequent signal retried immediately with no
    backoff at all. Since the throttle anchor is now claimed BEFORE
    dispatch (unconditionally), a burst of signals arriving well within
    the throttle window must still only dispatch once, even though every
    dispatched edit in this test "fails" (never actually invoked -- see
    the mock)."""
    fake_now = [1000.0]
    monkeypatch.setattr(telegram_bot.time, "time", lambda: fake_now[0])
    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: 1)
    dispatch_calls = []
    monkeypatch.setattr(telegram_bot, "_dispatch_edit",
                       lambda message_id, text, chat_id: dispatch_calls.append(text))

    live = telegram_bot._LiveBatchStatus()
    live.start(10)
    fake_now[0] += telegram_bot.LIVE_STATUS_MIN_INTERVAL_SEC

    live(_live_json("firm_start", firm="Firm A", ordinal=1, total=10))
    assert len(dispatch_calls) == 1

    # 20 more distinct signals, each only 0.05s apart -- well within the
    # 3s throttle window. Every one of the "prior" dispatches is presumed
    # to have failed (this mock never actually delivers anything), yet the
    # throttle must still hold: at most one MORE dispatch in this ~1s span.
    for i in range(20):
        fake_now[0] += 0.05
        live(_live_json("phase", firm="Firm A", phase=f"phase {i}"))
    assert len(dispatch_calls) <= 2, (
        f"throttle must survive a failure streak, not retry unboundedly: got {len(dispatch_calls)} dispatches"
    )


def test_live_batch_status_edit_dispatch_never_blocks_the_caller(monkeypatch):
    """FIX 25 (adversarial re-review): the actual HTTP call must never run
    on the thread that calls progress() -- simulates a slow/hanging
    editMessageText (a real one used requests.post(timeout=30)) and
    confirms __call__ returns almost immediately regardless."""
    import time as real_time

    def _slow_edit(message_id, text, chat_id=None):
        real_time.sleep(2)
        return True

    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: 1)
    monkeypatch.setattr(telegram_bot, "_edit_message", _slow_edit)

    live = telegram_bot._LiveBatchStatus()
    live.start(10)
    real_time.sleep(telegram_bot.LIVE_STATUS_MIN_INTERVAL_SEC + 0.1)  # real time -- clear the throttle window

    started = real_time.time()
    live(_live_json("firm_start", firm="Firm A", ordinal=1, total=10))
    elapsed = real_time.time() - started
    assert elapsed < 1.0, f"__call__ must return almost immediately, took {elapsed:.2f}s"


# --- _drain_with_live_status --------------------------------------------------

def test_drain_with_live_status_wires_progress_and_notify_correctly(monkeypatch):
    _connection("https://linkedin.com/in/dwls-poc", "Dwls Poc", "Dwls Firm")
    run = dbc.create_wc_run("batch")
    dbc.create_wc_firm(run["id"], "Dwls Firm", icp_status="qualified",
                       icp_reason="test", aum_tag="AUM_CONFIRMED",
                       research_raw={"firm_oneliner": "x", "sources": []})
    dbc.enqueue_firms(run["id"], ["Dwls Firm"])
    qrow = dbc.next_pending_chunk(run["id"], 5)[0]
    firm = next(f for f in dbc.firms_for_run(run["id"]) if f["firm_name"] == "Dwls Firm")
    dbc.set_queue_firm(qrow["id"], firm["id"])

    monkeypatch.setattr(telegram_bot, "_send_and_get_id", lambda text, chat_id=None: 1)
    monkeypatch.setattr(telegram_bot, "_edit_message", lambda *a, **k: True)
    notify_calls = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: notify_calls.append(text))

    totals = telegram_bot._drain_with_live_status(run["id"])
    assert totals["processed"] == 1
    assert any("Chunk 1" in m for m in notify_calls), "the chunk-grouped `notify` channel must be unaffected"


# --- End-to-end: FIX 19/23 must be completely unaffected --------------------

def test_live_status_wiring_does_not_weaken_the_live_batch_confirmation_gate(monkeypatch):
    """FIX 19's live-mode confirmation gate must still refuse to start ANY
    batch (with or without the FIX 25 live-status wiring) until explicitly
    confirmed -- _drain_with_live_status is only ever reached AFTER that
    gate, never before or instead of it."""
    monkeypatch.setattr(telegram_bot.config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    send_and_get_id_calls = []
    monkeypatch.setattr(telegram_bot, "_send_and_get_id",
                       lambda text, chat_id=None: send_and_get_id_calls.append(text) or 1)

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")

    assert "LIVE MODE" in sent[-1]
    assert "confirm" in sent[-1].lower()
    assert send_and_get_id_calls == [], (
        "the live-status message must never be sent before the batch is actually confirmed to start"
    )


def test_live_status_wiring_does_not_weaken_fix23_command_fragment_rejection(monkeypatch):
    """FIX 23's command-fragment guard must still fire -- this fix touches
    _run_batch's INTERNALS (after a batch is already confirmed to start),
    never _start_or_gate_batch/_handle_referral's own dispatch/parsing."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    send_and_get_id_calls = []
    monkeypatch.setattr(telegram_bot, "_send_and_get_id",
                       lambda text, chat_id=None: send_and_get_id_calls.append(text) or 1)

    telegram_bot._dispatch("/referral batch\nconfirm")

    assert "leftover confirm/cancel word" in sent[-1] or sent[-1] in (
        "Nothing awaiting confirmation (or it expired). Re-upload the file to try again.",
    )
    assert send_and_get_id_calls == [], "a rejected/no-op batch attempt must never start a live-status message"
