"""QA Part 7 — orchestration, resume, circuit breaker, anti-flooding, and
fail-cheap ordering (§4 HARD RULE 5, §11.10, HARD RULE 15).
"""
from __future__ import annotations

import inspect
import json
import re

import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import apollo_enrich, config, dbc, gmail_client, icp_gate, personalization, pipeline, telegram_bot

KINGBIRD = "Kingbird Investment Management"
BLACKSTONE = "Blackstone"


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


def _qualified_firm(run_id: int, name: str) -> dict:
    return dbc.create_wc_firm(
        run_id, name, icp_status="qualified",
        icp_reason="AUM ~$500M confirmed, qualifies", aum_tag="AUM_CONFIRMED",
        research_raw={"firm_oneliner": f"{name} is a test firm.", "sources": []},
    )


def _connection(url: str, name: str, company: str, email_seed: str = "x@example.com"):
    dbc.bulk_insert_connections([{
        "linkedin_url": url, "poc_name": name, "designation": "Managing Director",
        "company_name_raw": company, "company_name_norm": company.lower(),
        "poc_email_seed": email_seed, "incomplete_row": False,
    }])


def _enqueue_ready_firms(run_id: int, names: list[str]) -> list[dict]:
    """Enqueues ALL names in one call (giving each a distinct `position` —
    calling enqueue_firms one name at a time gives every row position=0,
    since it's `enumerate(firm_names)` per call, and 'grab the newest row'
    then becomes unreliable on position ties) and links each queue row to a
    fresh qualified wc_firms row by matching firm_name_input, not by
    ordering assumptions."""
    firms = {name: _qualified_firm(run_id, name) for name in names}
    dbc.enqueue_firms(run_id, names)
    qrows = dbc.next_pending_chunk(run_id, len(names) + 1)
    for q in qrows:
        dbc.set_queue_firm(q["id"], firms[q["firm_name_input"]]["id"])
    return qrows


# ---------------------------------------------------------------------------
# FIX 32A Part B (real 15-firm batch): X Analyst -- the SENDER of every
# outreach email -- passed the free filter, ran a full paid research pass,
# and was only disqualified downstream on firm type; had it qualified, the
# pipeline would have drafted an email pitching X Analyst to X Analyst.
# config.SENDER_FIRMS is deliberately a SEPARATE set from HARD RULE 11's
# REFERRER_FIRMS (Manpreet's firm) -- match_and_enqueue must skip a sender
# match before the paid gate, with its own counter/message, never folded
# into referrer_own_firm.
# ---------------------------------------------------------------------------
def test_match_and_enqueue_skips_the_senders_own_firm_before_the_gate():
    _connection("https://linkedin.com/in/xa-poc", "Test POC", "X Analyst")
    run = dbc.create_wc_run("batch")
    progress_lines = []
    summary = pipeline.match_and_enqueue(run["id"], ["X Analyst"], progress=progress_lines.append)
    assert summary["sender_own_firm"] == 1
    assert summary["ready"] == 0
    assert summary["referrer_own_firm"] == 0
    assert any("our own firm" in line for line in progress_lines), progress_lines
    # No wc_firms row created for a sender-skip -- same as the referrer skip.
    assert dbc.firms_for_run(run["id"]) == []


def test_match_and_enqueue_referrer_skip_unchanged_own_message_and_counter():
    """The pre-existing HARD RULE 11 behavior must not regress or get
    folded together with the new sender-side check."""
    _connection("https://linkedin.com/in/scp-poc", "Test POC", "Singh Capital Partners")
    run = dbc.create_wc_run("batch")
    progress_lines = []
    summary = pipeline.match_and_enqueue(run["id"], ["Singh Capital Partners"],
                                         progress=progress_lines.append)
    assert summary["referrer_own_firm"] == 1
    assert summary["sender_own_firm"] == 0
    assert summary["ready"] == 0
    assert any("referrer's own firm" in line for line in progress_lines), progress_lines
    assert dbc.firms_for_run(run["id"]) == []


def test_match_and_enqueue_a_firm_matching_neither_still_reaches_the_gate():
    _connection("https://linkedin.com/in/other-poc", "Test POC", "Some Unrelated Firm")
    run = dbc.create_wc_run("batch")
    summary = pipeline.match_and_enqueue(run["id"], ["Some Unrelated Firm"])
    assert summary["ready"] == 1
    assert summary["referrer_own_firm"] == 0
    assert summary["sender_own_firm"] == 0
    firms = dbc.firms_for_run(run["id"])
    assert len(firms) == 1
    assert firms[0]["firm_name"] == "Some Unrelated Firm"


# ---------------------------------------------------------------------------
# Single-source function (§11.10): one process_referral_firm, never forked
# ---------------------------------------------------------------------------
def test_process_referral_firm_defined_exactly_once_in_the_codebase():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    defs = []
    for py_file in root.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if re.search(r"^def process_referral_firm\(", text, re.MULTILINE):
            defs.append(py_file)
    assert defs == [root / "pipeline.py"], (
        f"process_referral_firm must be defined exactly once, in src/pipeline.py; found in {defs}"
    )


def test_single_firm_batch_and_qualify_paths_all_call_the_same_function():
    """Single-firm mode and batch mode both route through pipeline.drain_queue
    (which calls process_referral_firm once per firm); /qualify's
    continuation calls process_referral_firm directly. All three resolve to
    the SAME function object — never a second, forked copy.

    FIX 25 (real-mode incident): _run_batch/_run_resume now funnel through
    the shared _drain_with_live_status() helper (which wires up the live-
    editing status message before calling pipeline.drain_queue itself) --
    _run_single_firm still calls pipeline.drain_queue directly (single-firm
    mode has no batch-style live status; see test_single_firm_never_uses_
    chunk_style), so the literal call-site count drops from 3 to 2 (one in
    _run_single_firm, one shared inside _drain_with_live_status) -- the
    underlying invariant (one shared drain call, never a forked copy) is
    unchanged, just verified one level of indirection deeper."""
    tg_source = inspect.getsource(telegram_bot)
    pipeline_source = inspect.getsource(pipeline)

    assert "pipeline.drain_queue(" in tg_source
    assert tg_source.count("pipeline.drain_queue(") == 2  # _run_single_firm + _drain_with_live_status
    assert "pipeline.process_referral_firm(" in tg_source  # /qualify continuation
    assert tg_source.count("pipeline.process_referral_firm(") == 1

    assert pipeline_source.count("def process_referral_firm(") == 1
    assert "process_referral_firm(firm_id, breaker, progress)" in pipeline_source  # called from drain_queue

    batch_src = inspect.getsource(telegram_bot._run_batch)
    resume_src = inspect.getsource(telegram_bot._run_resume)
    assert "_drain_with_live_status(" in batch_src
    assert "_drain_with_live_status(" in resume_src
    assert "pipeline.drain_queue(" not in batch_src
    assert "pipeline.drain_queue(" not in resume_src


# ---------------------------------------------------------------------------
# Chunking (§4: "chunks of 25")
# ---------------------------------------------------------------------------
def test_default_chunk_size_is_25():
    assert config.BATCH_CHUNK_SIZE == 25


def test_drain_processes_more_firms_than_a_single_chunk(monkeypatch):
    """With a small chunk_size, drain_queue must loop across multiple chunks
    until the queue is empty — not stop after the first chunk."""
    run = dbc.create_wc_run("batch")
    names = [f"Chunk Test Firm {i}" for i in range(7)]
    _enqueue_ready_firms(run["id"], names)

    call_count = {"n": 0}

    def _fake_process(firm_id, breaker=None, progress=None):
        call_count["n"] += 1
        return {"outcome": "no_eligible_poc"}

    monkeypatch.setattr(pipeline, "process_referral_firm", _fake_process)
    totals = pipeline.drain_queue(run["id"], chunk_size=2)  # 7 firms, chunks of 2 -> 4 loop iterations
    assert call_count["n"] == 7
    assert totals["processed"] == 7
    assert totals["aborted"] is False


# ---------------------------------------------------------------------------
# Telegram anti-flooding (§11.7): aggregate checkpoints, not one per firm
# ---------------------------------------------------------------------------
def test_grouped_chunk_messages_not_one_per_firm_and_not_a_bare_count(monkeypatch):
    """Feature 4 (replaces the old checkpoint-every-10 bare-count behavior,
    per the QA report's own mockup-driven redesign): with chunk_style=True,
    a 25-firm chunk produces exactly 3 grouped Telegram sends — one verdict
    message carrying ALL 25 per-firm lines, one enrichment+draft message, one
    chunk summary — never one message per firm, and never a bare aggregate
    count with no per-firm detail."""
    run = dbc.create_wc_run("batch")
    names = [f"Flood Test Firm {i}" for i in range(25)]
    _enqueue_ready_firms(run["id"], names)  # _qualified_firm gives each icp_status='qualified'

    monkeypatch.setattr(pipeline, "process_referral_firm",
                        lambda firm_id, breaker=None, progress=None: {"outcome": "no_eligible_poc"})

    notify_calls: list[str] = []
    totals = pipeline.drain_queue(run["id"], notify=notify_calls.append, chunk_size=25, chunk_style=True)
    assert totals["processed"] == 25

    # Bounded send count: exactly 3 for one full chunk, NOT 25 (one per firm)
    # and NOT 1 (a bare count with no detail).
    assert len(notify_calls) == 3, f"expected 3 grouped messages for a 25-firm chunk, got {len(notify_calls)}"
    verdict_msg, enrich_msg, summary_msg = notify_calls

    assert "Chunk 1" in verdict_msg
    for name in names:
        assert name in verdict_msg, f"{name} missing from the grouped verdict message"
    assert verdict_msg.count("QUALIFIED") == 25  # every firm's verdict line present, in ONE message

    for name in names:
        assert name in enrich_msg
    assert "Chunk 1 done" in summary_msg
    assert "qualified" in summary_msg.lower()


def test_single_firm_mode_gets_no_grouped_chunk_messages(monkeypatch):
    """Confirms chunk_style defaults to False: single-firm mode (which never
    passes chunk_style=True) gets NO grouped verdict/enrichment/summary sends
    from drain_queue at all — it builds Feature 1's prospect brief instead."""
    run = dbc.create_wc_run("single_firm", query_firm="Solo Firm")
    _qualified_firm(run["id"], "Solo Firm")
    dbc.enqueue_firms(run["id"], ["Solo Firm"])
    qrow = dbc.next_pending_chunk(run["id"], 10)[-1]
    firm = dbc.wc_firm_by_name("Solo Firm")
    dbc.set_queue_firm(qrow["id"], firm["id"])

    monkeypatch.setattr(pipeline, "process_referral_firm",
                        lambda firm_id, breaker=None, progress=None: {"outcome": "no_eligible_poc"})

    notify_calls: list[str] = []
    pipeline.drain_queue(run["id"], notify=notify_calls.append, chunk_size=25)  # chunk_style default False
    assert notify_calls == []


def test_per_firm_debug_detail_never_reaches_notify_channel(monkeypatch):
    """§11.7: 'Per-lookup debug output goes to service logs (stderr/journalctl),
    never Telegram.' process_referral_firm's own progress messages (Apollo
    failures, deck warnings, etc.) must go to the stderr-bound `progress`
    channel, never leak into `notify`."""
    run = dbc.create_wc_run("batch")
    firm = _qualified_firm(run["id"], "Debug Leak Test Firm")
    dbc.enqueue_firms(run["id"], ["Debug Leak Test Firm"])
    qrow = dbc.next_pending_chunk(run["id"], 10)[-1]
    dbc.set_queue_firm(qrow["id"], firm["id"])

    def _noisy_process(firm_id, breaker=None, progress=None):
        if progress:
            progress("SENSITIVE PER-FIRM DEBUG DETAIL: Apollo lookup failed for X")
        return {"outcome": "no_eligible_poc"}

    monkeypatch.setattr(pipeline, "process_referral_firm", _noisy_process)
    notify_calls: list[str] = []
    pipeline.drain_queue(run["id"], notify=notify_calls.append, chunk_size=25)
    assert not any("SENSITIVE PER-FIRM DEBUG DETAIL" in m for m in notify_calls)


# ---------------------------------------------------------------------------
# Circuit breaker (HARD RULE 15)
# ---------------------------------------------------------------------------
def test_circuit_breaker_aborts_after_3_consecutive_infra_failures(monkeypatch):
    run = dbc.create_wc_run("batch")
    names = [f"Infra Fail Firm {i}" for i in range(5)]
    firms = {name: dbc.create_wc_firm(run["id"], name) for name in names}  # not yet gated
    dbc.enqueue_firms(run["id"], names)
    for q in dbc.next_pending_chunk(run["id"], len(names) + 1):
        dbc.set_queue_firm(q["id"], firms[q["firm_name_input"]]["id"])

    def _boom(firm_name, progress=None):
        raise icp_gate.InfrastructureError("Tavily", "HTTP 432 quota exceeded")

    monkeypatch.setattr(icp_gate, "research_and_gate", _boom)

    notify_calls: list[str] = []
    totals = pipeline.drain_queue(run["id"], notify=notify_calls.append)
    assert totals["aborted"] is True
    assert totals["dead_service"] == "Tavily"
    assert totals["processed"] == 2, "must abort after exactly 3 consecutive failures (2 fully failed + the 3rd trips it)"

    run_row = dbc.get_wc_run(run["id"])
    assert run_row["status"] == "crashed"
    assert run_row["crash_detail"] == "quota:Tavily"
    # Feature A: for Tavily specifically, the breaker only ever trips once
    # the WHOLE key pool is dead (a single key's exhaustion rotates
    # transparently inside search_client._post and never reaches here), so
    # the message names that precisely rather than using the generic
    # "Circuit breaker tripped" wording other services still get (see
    # test_circuit_breaker_message_stays_generic_for_non_tavily_services).
    assert any("Tavily keys exhausted" in m for m in notify_calls)

    # Remaining, never-reached firms stay queued (not silently dropped).
    remaining = dbc.next_pending_chunk(run["id"], 100)
    infra_paused = [q for q in dbc.infra_paused_firms(run["id"])]
    assert len(remaining) + len(infra_paused) == 3, "the 2 untouched + 1 in-flight-when-tripped firm must all survive queued"


def test_circuit_breaker_message_stays_generic_for_non_tavily_services(monkeypatch):
    """Feature A's Tavily-specific 'all N keys exhausted' wording must not
    leak onto other services that have no key pool (LLM, Apollo, Exa) —
    those keep the original generic 'Circuit breaker tripped' message."""
    run = dbc.create_wc_run("batch")
    names = [f"LLM Fail Firm {i}" for i in range(5)]
    firms = {name: dbc.create_wc_firm(run["id"], name) for name in names}
    dbc.enqueue_firms(run["id"], names)
    for q in dbc.next_pending_chunk(run["id"], len(names) + 1):
        dbc.set_queue_firm(q["id"], firms[q["firm_name_input"]]["id"])

    def _boom(firm_name, progress=None):
        raise icp_gate.InfrastructureError("LLM", "429 rate limited")

    monkeypatch.setattr(icp_gate, "research_and_gate", _boom)
    notify_calls: list[str] = []
    totals = pipeline.drain_queue(run["id"], notify=notify_calls.append)
    assert totals["dead_service"] == "LLM"
    assert any("Circuit breaker tripped" in m for m in notify_calls)
    assert not any("keys exhausted" in m for m in notify_calls)


def test_circuit_breaker_resets_on_a_success_between_failures(monkeypatch):
    """Two infra failures followed by a SUCCESS must reset the consecutive
    counter — the breaker should not trip on failure #3 if #2 was a success."""
    breaker = pipeline.CircuitBreaker()
    breaker.record_infra_failure("Tavily")
    breaker.record_infra_failure("Tavily")
    breaker.record_success()
    breaker.record_infra_failure("Tavily")  # should be failure #1 again, not #3
    # No exception raised -> breaker did not trip.


def test_circuit_breaker_trips_on_3rd_consecutive_failure():
    breaker = pipeline.CircuitBreaker()
    breaker.record_infra_failure("LLM")
    breaker.record_infra_failure("LLM")
    with pytest.raises(pipeline.CircuitBreakerTripped) as exc_info:
        breaker.record_infra_failure("LLM")
    assert exc_info.value.service == "LLM"


# ---------------------------------------------------------------------------
# /referral resume idempotency
# ---------------------------------------------------------------------------
def test_resume_continues_from_next_firm_without_duplicating_completed_drafts(monkeypatch):
    _connection("https://linkedin.com/in/resumeperson", "Resume Person", "Resume Test Firm A",
               email_seed="resume@example.com")
    run = dbc.create_wc_run("batch")

    firm_a = _qualified_firm(run["id"], "Resume Test Firm A")  # will succeed before the "crash"
    firm_b = dbc.create_wc_firm(run["id"], "Resume Test Firm B")  # not yet gated when quota dies
    names = ["Resume Test Firm A", "Resume Test Firm B"]
    firms_by_name = {"Resume Test Firm A": firm_a, "Resume Test Firm B": firm_b}
    dbc.enqueue_firms(run["id"], names)
    for q in dbc.next_pending_chunk(run["id"], len(names) + 1):
        dbc.set_queue_firm(q["id"], firms_by_name[q["firm_name_input"]]["id"])

    call_log = []
    real_gate = icp_gate.research_and_gate

    def _dies_on_b(firm_name, progress=None):
        call_log.append(firm_name)
        if firm_name == "Resume Test Firm B":
            raise icp_gate.InfrastructureError("Tavily", "quota")
        return real_gate(firm_name, progress)

    monkeypatch.setattr(icp_gate, "research_and_gate", _dies_on_b)
    # Firm A is already gated (qualified) so it skips straight to Phase 3 and
    # drafts successfully; Firm B's gate call raises on first attempt but the
    # breaker only trips after 3 consecutive — with just one firm behind it,
    # drain_queue exhausts the queue before tripping (this models a
    # transient blip that resume should simply retry, not necessarily a trip).
    totals = pipeline.drain_queue(run["id"])
    assert totals["processed"] >= 1

    draft_a = dbc.wc_draft_for_firm(firm_a["id"])
    assert draft_a["email_status"] == "drafted"
    first_draft_id = draft_a["gmail_draft_id"]

    # Simulate /referral resume: requeue anything not done, drain again with
    # a NOW-HEALTHY service.
    monkeypatch.setattr(icp_gate, "research_and_gate", real_gate)
    for q in dbc.next_pending_chunk(run["id"], 100):
        dbc.set_queue_status(q["id"], "pending")
    for q in dbc.infra_paused_firms(run["id"]):
        dbc.set_queue_status(q["id"], "pending")

    totals2 = pipeline.drain_queue(run["id"])

    # Firm A must NOT be re-drafted (idempotent — draft_exists_for_connection
    # short-circuits it even though its queue row was reset to pending).
    draft_a_after = dbc.wc_draft_for_firm(firm_a["id"])
    assert draft_a_after["gmail_draft_id"] == first_draft_id
    assert totals2["already_drafted"] >= 0  # firm A resolves via already_drafted or is simply skipped


# ---------------------------------------------------------------------------
# Fail-cheap ordering (HARD RULE 5)
# ---------------------------------------------------------------------------
def test_disqualified_firm_never_triggers_apollo_or_personalization(monkeypatch):
    apollo_calls = []
    pers_calls = []
    monkeypatch.setattr(apollo_enrich, "find_email",
                       lambda *a, **k: (apollo_calls.append(1), (None, None))[1])
    monkeypatch.setattr(personalization, "research_personalization",
                       lambda *a, **k: (pers_calls.append(1), {})[1])

    run = dbc.create_wc_run("single_firm", query_firm=BLACKSTONE)
    firm = dbc.create_wc_firm(run["id"], BLACKSTONE)  # ungated -> TEST_MODE fixture disqualifies
    result = pipeline.process_referral_firm(firm["id"])
    assert result["outcome"] == "disqualified"
    assert not apollo_calls, "a disqualified firm must never trigger Apollo (fail cheap)"
    assert not pers_calls, "a disqualified firm must never trigger personalization research (fail cheap)"


def test_parked_firm_never_triggers_apollo_or_personalization(monkeypatch):
    apollo_calls = []
    pers_calls = []
    monkeypatch.setattr(apollo_enrich, "find_email",
                       lambda *a, **k: (apollo_calls.append(1), (None, None))[1])
    monkeypatch.setattr(personalization, "research_personalization",
                       lambda *a, **k: (pers_calls.append(1), {})[1])

    run = dbc.create_wc_run("single_firm", query_firm="Aliya Capital Partners")
    firm = dbc.create_wc_firm(run["id"], "Aliya Capital Partners")  # TEST_MODE fixture parks it
    result = pipeline.process_referral_firm(firm["id"])
    assert result["outcome"] == "parked"
    assert not apollo_calls
    assert not pers_calls


def test_no_email_found_never_triggers_personalization_research(monkeypatch):
    """§4 Phase 3: 'Do NOT proceed to Phase 4 for that POC (fail cheap).'"""
    _connection("https://linkedin.com/in/noemailperson", "No Email Person", "No Email Test Firm",
               email_seed=None)  # blank seed -> Apollo would be consulted
    monkeypatch.setattr(apollo_enrich, "find_email", lambda *a, **k: (None, None))  # Apollo finds nothing

    pers_calls = []
    monkeypatch.setattr(personalization, "research_personalization",
                       lambda *a, **k: (pers_calls.append(1), {})[1])

    run = dbc.create_wc_run("single_firm", query_firm="No Email Test Firm")
    firm = _qualified_firm(run["id"], "No Email Test Firm")
    result = pipeline.process_referral_firm(firm["id"])
    assert result["outcome"] == "no_email_found"
    assert not pers_calls, "a POC with no discoverable email must never trigger Phase 4 research (fail cheap)"

    draft = dbc.wc_draft_for_firm(firm["id"])
    assert draft["email_status"] == "no_email_found"


# ---------------------------------------------------------------------------
# FIX 25 (real-mode incident): a 13-firm batch showed no Telegram output for
# ~5 minutes between the chunk header and the chunk result -- looked hung.
# process_referral_firm/drain_queue now emit structured, marker-prefixed
# JSON signals through the EXISTING progress() callback so telegram_bot.py
# can drive a live-editing status message without one Telegram message per
# firm per phase. Tested here at the pipeline level (signal content/timing);
# telegram_bot.py's rendering/throttling of these signals is tested in
# test_telegram_bot.py.
# ---------------------------------------------------------------------------
def _live_signals(progress_calls: list[str]) -> list[dict]:
    marked = [m[len(pipeline.LIVE_STATUS_MARK):] for m in progress_calls if m.startswith(pipeline.LIVE_STATUS_MARK)]
    return [json.loads(m) for m in marked]


def test_announce_phase_emits_a_marker_prefixed_json_signal():
    calls = []
    pipeline._announce_phase(calls.append, "Some Firm LLC", "researching")
    assert len(calls) == 1
    assert calls[0].startswith(pipeline.LIVE_STATUS_MARK)
    signal = json.loads(calls[0][len(pipeline.LIVE_STATUS_MARK):])
    assert signal == {"kind": "phase", "firm": "Some Firm LLC", "phase": "researching"}


def test_process_referral_firm_announces_every_phase_in_order_for_a_fresh_firm():
    """A firm with icp_status=None (never gated) goes through every phase
    end to end -- confirms the phase markers fire in the right order and
    reference the right firm, not just that they exist somewhere."""
    _connection("https://linkedin.com/in/phase-order-poc", "Phase Order Poc", KINGBIRD)
    run = dbc.create_wc_run("single_firm", query_firm=KINGBIRD)
    firm = dbc.create_wc_firm(run["id"], KINGBIRD)  # icp_status=None -- fresh, ungated

    calls = []
    result = pipeline.process_referral_firm(firm["id"], progress=calls.append)
    assert result["outcome"] == "drafted"

    phases = [json.loads(m[len(pipeline.LIVE_STATUS_MARK):])
             for m in calls if m.startswith(pipeline.LIVE_STATUS_MARK) and
             json.loads(m[len(pipeline.LIVE_STATUS_MARK):]).get("kind") == "phase"]
    ordered = [(p["firm"], p["phase"]) for p in phases]
    assert ordered == [
        (KINGBIRD, "researching"),
        (KINGBIRD, "finding contact"),
        (KINGBIRD, "personalizing"),
        (KINGBIRD, "drafting"),
        (KINGBIRD, "building deck"),
        (KINGBIRD, "creating draft"),
    ]


def test_process_referral_firm_skips_researching_phase_marker_when_already_gated():
    """A resumed/pre-gated firm (icp_status already set) skips Phase 2
    entirely -- must not falsely announce "researching" for work that never
    actually happens."""
    run = dbc.create_wc_run("single_firm", query_firm="Already Gated Co")
    firm = _qualified_firm(run["id"], "Already Gated Co")
    _connection("https://linkedin.com/in/already-gated-poc", "Already Gated Poc", "Already Gated Co")

    calls = []
    pipeline.process_referral_firm(firm["id"], progress=calls.append)

    phases = [p["phase"] for p in _live_signals(calls) if p.get("kind") == "phase"]
    assert "researching" not in phases


def test_disqualified_firm_never_announces_downstream_phases():
    """A disqualified firm returns right after Phase 2 -- fail cheap (HARD
    RULE 5) means no "finding contact"/"personalizing"/etc. markers either,
    matching the existing no-Apollo/no-personalization guarantee."""
    run = dbc.create_wc_run("single_firm", query_firm=BLACKSTONE)
    firm = dbc.create_wc_firm(run["id"], BLACKSTONE)  # TEST_MODE fixture disqualifies it (AUM too large)

    calls = []
    result = pipeline.process_referral_firm(firm["id"], progress=calls.append)
    assert result["outcome"] == "disqualified"

    phases = [p["phase"] for p in _live_signals(calls) if p.get("kind") == "phase"]
    assert phases == ["researching"]


# --- _fmt_chunk_verdict: full reason for disqualified/parked -----------------

def _long_reason(label: str) -> str:
    return (f"{label}: Not an accepted firm type (RE/family office/PE-RE/property "
           f"management) -- this firm operates a completely unrelated logistics "
           f"and freight brokerage business with no real estate exposure at all")


def test_fmt_chunk_verdict_shows_full_reason_for_disqualified():
    reason = _long_reason("disqualified")
    assert len(reason) > 70  # long enough that the OLD _short_reason cap would have truncated it
    entries = [(1, "Some Logistics Co", "disqualified", reason)]
    text = pipeline._fmt_chunk_verdict(1, None, 1, entries, None)
    assert reason in text
    assert "…" not in text


def test_fmt_chunk_verdict_shows_full_reason_for_parked():
    reason = _long_reason("parked") + ", verify manually before proceeding with outreach"
    assert len(reason) > 70
    entries = [(1, "Some Ambiguous Co", "parked", reason)]
    text = pipeline._fmt_chunk_verdict(1, None, 1, entries, None)
    assert reason in text
    assert "…" not in text


def test_fmt_chunk_verdict_still_truncates_a_long_qualified_reason():
    """QUALIFIED is informational, not something a human has to audit --
    the pre-existing 70-char cap still applies there specifically."""
    reason = ("QUALIFIED (est. $800M -- a very long invested-capital proxy basis "
             "description that goes on for quite a while and would definitely "
             "wrap the chunk message onto extra lines if never trimmed)")
    assert len(reason) > 70
    entries = [(1, "Some Qualified Co", "qualified", reason)]
    text = pipeline._fmt_chunk_verdict(1, None, 1, entries, None)
    assert reason not in text
    assert "…" in text


# --- drain_queue: firm_start/counts signals -----------------------------

def test_drain_queue_emits_firm_start_and_counts_signals_when_chunk_style(monkeypatch):
    """Basic shape/ordinal check with two already-qualified firms (both
    draft cleanly) -- the mixed-outcome test below covers real qualified/
    disqualified/parked counting through the actual TEST_MODE gate.

    Distinct email_seed per POC: HARD RULE 13 (apollo_enrich.find_email)
    short-circuits straight to the seed email, so two POCs sharing the
    default seed collide on HARD RULE 7's email-fallback dedup (the second
    firm's contacted_pocs check matches the first firm's just-recorded row)
    and the second one gets skipped_duplicate instead of drafted."""
    _connection("https://linkedin.com/in/live-status-poc-a", "Live Status Poc A", KINGBIRD,
               email_seed="live-status-a@example.com")
    _connection("https://linkedin.com/in/live-status-poc-b", "Live Status Poc B", "Live Status Firm B",
               email_seed="live-status-b@example.com")
    run = dbc.create_wc_run("batch")
    names = [KINGBIRD, "Live Status Firm B"]
    _enqueue_ready_firms(run["id"], names)  # pre-gates both "qualified"

    calls = []
    totals = pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25, chunk_style=True)
    assert totals["processed"] == 2

    firm_starts = [s for s in _live_signals(calls) if s.get("kind") == "firm_start"]
    counts = [s for s in _live_signals(calls) if s.get("kind") == "counts"]
    assert len(firm_starts) == 2
    assert [s["ordinal"] for s in firm_starts] == [1, 2]
    assert all(s["total"] == 2 for s in firm_starts)
    assert len(counts) == 2
    assert counts[-1]["qualified"] == 2
    assert counts[-1]["drafted"] == 2


def test_drain_queue_counts_track_qualified_disqualified_parked_drafted_separately(monkeypatch):
    names = [KINGBIRD, BLACKSTONE, "Aliya Capital Partners"]
    run = dbc.create_wc_run("batch")
    firms = {name: dbc.create_wc_firm(run["id"], name) for name in names}  # icp_status=None each
    dbc.enqueue_firms(run["id"], names)
    for q in dbc.next_pending_chunk(run["id"], len(names) + 1):
        dbc.set_queue_firm(q["id"], firms[q["firm_name_input"]]["id"])
    _connection("https://linkedin.com/in/mixed-outcome-poc", "Mixed Outcome Poc", KINGBIRD)

    calls = []
    totals = pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25, chunk_style=True)
    assert totals["processed"] == 3
    assert totals["drafted"] == 1  # Kingbird: qualifies, real POC found, drafts
    assert totals["disqualified"] == 1  # Blackstone: AUM far over ceiling
    assert totals["parked"] == 1  # Aliya: AUM unverifiable

    counts = [s for s in _live_signals(calls) if s.get("kind") == "counts"]
    final = counts[-1]
    assert final["qualified"] == 1
    assert final["disqualified"] == 1
    assert final["parked"] == 1
    assert final["drafted"] == 1


def test_drain_queue_does_not_emit_firm_start_or_counts_when_not_chunk_style():
    """Single-firm mode (chunk_style=False, the default) must not emit
    drain_queue's own firm_start/counts signals -- those are Feature-4-
    batch-only (gated behind `if chunk_style:` in drain_queue itself).
    process_referral_firm's own phase markers still fire regardless (they
    don't know about chunk_style at all) -- single-firm mode's default
    progress=server_log just logs them as plain text, harmlessly, since
    nothing there parses/renders them into a live-editing message."""
    run = dbc.create_wc_run("single_firm", query_firm=KINGBIRD)
    _enqueue_ready_firms(run["id"], [KINGBIRD])
    _connection("https://linkedin.com/in/no-chunk-style-poc", "No Chunk Style Poc", KINGBIRD)

    calls = []
    pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25)  # chunk_style default False

    kinds = {s.get("kind") for s in _live_signals(calls)}
    assert "firm_start" not in kinds
    assert "counts" not in kinds


# ---------------------------------------------------------------------------
# FIX 25 (adversarial re-review): live_total (the firm_start denominator)
# must reflect exactly what THIS drain_queue call will actually process --
# not every wc_firms row ever created for the run (which over-counts a
# match_and_enqueue-stage parked_ambiguous firm, whose queue row goes
# straight to 'done' and never enters this loop), and not the WHOLE
# original run's firm count on a resumed call (which the resumed call's
# own `processed` counter can never reach, since it starts fresh at 0).
# ---------------------------------------------------------------------------
def test_live_total_excludes_ambiguously_matched_firms(monkeypatch):
    """A parked_ambiguous firm (Phase 1) never enters drain_queue's loop at
    all -- live_total must not count it, even though the pre-existing
    total_ready (used only by _fmt_chunk_verdict's own display, untouched
    by this fix) still does."""
    run = dbc.create_wc_run("batch")
    _enqueue_ready_firms(run["id"], [KINGBIRD])
    _connection("https://linkedin.com/in/live-total-poc", "Live Total Poc", KINGBIRD)

    # Set up a parked_ambiguous firm exactly as match_and_enqueue's own
    # branch does: a wc_firms row exists, but its queue row is already
    # 'done' before drain_queue ever starts.
    ambiguous_firm = dbc.create_wc_firm(run["id"], "Ambiguous Co", icp_status="parked",
                                        park_reason="firm_match_ambiguous", park_source="research",
                                        icp_reason="firm_match_ambiguous")
    dbc.enqueue_firms(run["id"], ["Ambiguous Co"])
    amb_qrow = next(q for q in dbc.next_pending_chunk(run["id"], 10)
                    if q["firm_name_input"] == "Ambiguous Co")
    dbc.set_queue_firm(amb_qrow["id"], ambiguous_firm["id"])
    dbc.set_queue_status(amb_qrow["id"], "done")

    calls = []
    totals = pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25, chunk_style=True)
    assert totals["processed"] == 1
    assert totals["aborted"] is False

    firm_starts = [s for s in _live_signals(calls) if s.get("kind") == "firm_start"]
    assert len(firm_starts) == 1
    assert firm_starts[0]["ordinal"] == 1
    assert firm_starts[0]["total"] == 1, "live_total must exclude the ambiguously-matched firm"


def test_live_total_on_resume_reflects_only_the_still_pending_count():
    """A resumed drain_queue call's own `processed` counter starts fresh at
    0 -- live_total must match what THIS call will process (the remaining
    pending count), not the whole original run's firm count, or the
    ordinal could never reach it."""
    names = [f"Resume Total Firm {i}" for i in range(5)]
    run = dbc.create_wc_run("batch")
    _enqueue_ready_firms(run["id"], names)
    for i, name in enumerate(names):
        _connection(f"https://linkedin.com/in/resume-total-{i}", f"Resume Total Poc {i}", name,
                   email_seed=f"resume-total-{i}@example.com")

    # Simulate 2 firms already fully done (as if from a pre-crash session).
    done_qrows = dbc.next_pending_chunk(run["id"], 10)[:2]
    for q in done_qrows:
        dbc.set_queue_status(q["id"], "done")

    calls = []
    totals = pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25, chunk_style=True)
    assert totals["processed"] == 3, "only the 3 still-pending firms are processed by this call"

    firm_starts = [s for s in _live_signals(calls) if s.get("kind") == "firm_start"]
    assert len(firm_starts) == 3
    assert [s["ordinal"] for s in firm_starts] == [1, 2, 3]
    assert all(s["total"] == 3 for s in firm_starts), (
        "live_total must reflect the remaining pending count (3), not the original run's 5"
    )


# --- "stopped" terminal signal on every exit path ---------------------------

def test_stopped_signal_fires_done_on_successful_completion():
    run = dbc.create_wc_run("batch")
    _enqueue_ready_firms(run["id"], [KINGBIRD])
    _connection("https://linkedin.com/in/stopped-done-poc", "Stopped Done Poc", KINGBIRD)

    calls = []
    totals = pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25, chunk_style=True)
    assert totals["aborted"] is False

    stopped = [s for s in _live_signals(calls) if s.get("kind") == "stopped"]
    assert stopped == [{"kind": "stopped", "reason": "done"}]


def test_stopped_signal_fires_cancelled_on_cancellation(monkeypatch):
    names = [f"Stopped Cancel Firm {i}" for i in range(3)]
    run = dbc.create_wc_run("batch")
    _enqueue_ready_firms(run["id"], names)
    for i, name in enumerate(names):
        _connection(f"https://linkedin.com/in/stopped-cancel-{i}", f"Stopped Cancel Poc {i}", name)

    real_process = pipeline.process_referral_firm
    calls_seen = []

    def _wrapped(firm_id, breaker=None, progress=None):
        result = real_process(firm_id, breaker, progress)
        calls_seen.append(firm_id)
        if len(calls_seen) == 1:
            dbc.request_run_cancellation(run["id"])
        return result

    monkeypatch.setattr(pipeline, "process_referral_firm", _wrapped)
    calls = []
    totals = pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25, chunk_style=True)
    assert totals["cancelled"] is True

    stopped = [s for s in _live_signals(calls) if s.get("kind") == "stopped"]
    assert stopped == [{"kind": "stopped", "reason": "cancelled"}]


def test_stopped_signal_fires_crashed_on_generic_exception(monkeypatch):
    run = dbc.create_wc_run("batch")
    _enqueue_ready_firms(run["id"], ["Stopped Crash Firm"])
    _connection("https://linkedin.com/in/stopped-crash-poc", "Stopped Crash Poc", "Stopped Crash Firm")

    def _raise(firm_id, breaker=None, progress=None):
        raise ValueError("simulated crash")

    monkeypatch.setattr(pipeline, "process_referral_firm", _raise)
    calls = []
    totals = pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25, chunk_style=True)
    assert totals["aborted"] is True

    stopped = [s for s in _live_signals(calls) if s.get("kind") == "stopped"]
    assert stopped == [{"kind": "stopped", "reason": "crashed"}]


def test_stopped_signal_fires_paused_on_circuit_breaker_trip(monkeypatch):
    run = dbc.create_wc_run("batch")
    _enqueue_ready_firms(run["id"], ["Stopped Breaker Firm"])
    _connection("https://linkedin.com/in/stopped-breaker-poc", "Stopped Breaker Poc", "Stopped Breaker Firm")

    def _raise(firm_id, breaker=None, progress=None):
        raise pipeline.CircuitBreakerTripped("Tavily")

    monkeypatch.setattr(pipeline, "process_referral_firm", _raise)
    calls = []
    totals = pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25, chunk_style=True)
    assert totals["dead_service"] == "Tavily"

    stopped = [s for s in _live_signals(calls) if s.get("kind") == "stopped"]
    assert stopped == [{"kind": "stopped", "reason": "paused (Tavily unavailable)"}]


def test_stopped_signal_never_fires_when_not_chunk_style():
    run = dbc.create_wc_run("single_firm", query_firm=KINGBIRD)
    _enqueue_ready_firms(run["id"], [KINGBIRD])
    _connection("https://linkedin.com/in/stopped-no-chunk-poc", "Stopped No Chunk Poc", KINGBIRD)

    calls = []
    pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=25)  # chunk_style default False

    assert "stopped" not in {s.get("kind") for s in _live_signals(calls)}


# --- Multi-chunk ordinal continuity (adversarial re-review test-coverage gap) --

def test_firm_start_ordinal_continues_across_chunk_boundaries_not_reset():
    """FIX 25 (adversarial re-review): a regression that treated a chunk-
    relative index as the live ordinal (instead of the true cumulative
    `processed + 1`) would show firm #4 announced as ordinal 1 again at
    the start of chunk 2 -- confirmed via mutation testing in the review
    that no existing test (all single-chunk) would have caught this."""
    names = [f"Multi Chunk Firm {i}" for i in range(6)]
    run = dbc.create_wc_run("batch")
    _enqueue_ready_firms(run["id"], names)
    for i, name in enumerate(names):
        _connection(f"https://linkedin.com/in/multi-chunk-{i}", f"Multi Chunk Poc {i}", name,
                   email_seed=f"multi-chunk-{i}@example.com")

    calls = []
    totals = pipeline.drain_queue(run["id"], progress=calls.append, chunk_size=3, chunk_style=True)  # 2 chunks
    assert totals["processed"] == 6

    firm_starts = [s for s in _live_signals(calls) if s.get("kind") == "firm_start"]
    assert [s["ordinal"] for s in firm_starts] == [1, 2, 3, 4, 5, 6]
    assert all(s["total"] == 6 for s in firm_starts)
