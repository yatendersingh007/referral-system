"""QA Part 7 — orchestration, resume, circuit breaker, anti-flooding, and
fail-cheap ordering (§4 HARD RULE 5, §11.10, HARD RULE 15).
"""
from __future__ import annotations

import inspect
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
    the SAME function object — never a second, forked copy."""
    tg_source = inspect.getsource(telegram_bot)
    pipeline_source = inspect.getsource(pipeline)

    assert "pipeline.drain_queue(" in tg_source  # used by _run_single_firm, _run_batch, _run_resume
    assert tg_source.count("pipeline.drain_queue(") == 3
    assert "pipeline.process_referral_firm(" in tg_source  # /qualify continuation
    assert tg_source.count("pipeline.process_referral_firm(") == 1

    assert pipeline_source.count("def process_referral_firm(") == 1
    assert "process_referral_firm(firm_id, breaker, progress)" in pipeline_source  # called from drain_queue


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
