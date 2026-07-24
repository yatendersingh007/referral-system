"""Single-source per-firm funnel (§4, §7, §11.10 — mirrors Workflow A's
`process_qualified_company` discipline: ONE function, called by single-firm
mode, batch mode, and post-/qualify continuation. Never forked across entry
points.

Order of operations per firm (HARD RULE 5 — fail cheap): firm match (done
before this module is entered) -> ICP gate -> Apollo email discovery ->
personalization research -> draft + PPT. Each stage bails out cheaply rather
than spending on a firm/POC that already failed an earlier, cheaper stage.
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from . import (apollo_enrich, config, dbc, deck_filler, email_drafter,
               gmail_client, icp_gate, personalization, poc_ranking)
from .matching import firm_matcher
from .matching.normalize import normalize as normalize_company

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def server_log(msg: str) -> None:
    """§6/§11.7: per-lookup/per-phase debug detail goes to service logs
    (stderr/journalctl), NEVER Telegram. This is the default `progress`
    callback for every phase function in this module — Telegram only ever
    sees the aggregate checkpoint/summary messages that drain_queue's
    `notify` callback produces."""
    print(msg, file=sys.stderr, flush=True)


def _first_name(poc_name: str) -> str:
    parts = (poc_name or "").strip().split()
    return parts[0] if parts else "there"


def _collision_note(prior: dict) -> str:
    """Human-readable 'who/what this collided with' string (Feature 3),
    built from the matching contacted_pocs row."""
    when = prior.get("contacted_at")
    when_s = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when or "")
    poc = prior.get("poc_name") or "unknown POC"
    firm = prior.get("firm_name") or "unknown firm"
    src = prior.get("source_workflow") or "C"
    src_note = "Workflow A" if src == "A" else "a prior Workflow C run"
    return f"already contacted as {poc} at {firm} on {when_s} ({src_note})"


# ---------------------------------------------------------------------------
# Circuit breaker (HARD RULE 15): 3 consecutive research failures with a
# quota/rate-limit signature aborts the run rather than parking every
# remaining firm as a bogus "AUM unverifiable".
# ---------------------------------------------------------------------------
class CircuitBreakerTripped(RuntimeError):
    def __init__(self, service: str):
        self.service = service
        super().__init__(f"circuit breaker tripped: {service} looks dead "
                         f"({config.CIRCUIT_BREAKER_CONSECUTIVE_FAILURES} consecutive infra failures)")


class CircuitBreaker:
    def __init__(self):
        self._consecutive: dict[str, int] = {}

    def record_infra_failure(self, service: str) -> None:
        self._consecutive[service] = self._consecutive.get(service, 0) + 1
        if self._consecutive[service] >= config.CIRCUIT_BREAKER_CONSECUTIVE_FAILURES:
            raise CircuitBreakerTripped(service)

    def record_success(self) -> None:
        self._consecutive.clear()


# ---------------------------------------------------------------------------
# Phase 1: match a target-firm list against the connections corpus, enqueue
# ---------------------------------------------------------------------------
def match_and_enqueue(run_id: int, firm_names: list[str], progress: Progress = server_log) -> dict:
    connections = dbc.all_connections()
    company_names = list({c["company_name_raw"] for c in connections if c.get("company_name_raw")})

    dbc.enqueue_firms(run_id, firm_names)
    queue_rows = {q["firm_name_input"]: q for q in dbc.next_pending_chunk(run_id, len(firm_names) + 1)}

    match_llm_fn = None if not config.TEST_MODE else _test_mode_match_llm
    results = firm_matcher.match_firms(firm_names, company_names, llm_batch_fn=match_llm_fn)

    summary = {"no_connection": 0, "referrer_own_firm": 0, "parked_ambiguous": 0, "ready": 0}
    for name, result in zip(firm_names, results):
        qrow = queue_rows.get(name)
        if result.outcome in ("no_connection", "degenerate_name"):
            dbc.set_queue_status(qrow["id"], "no_connection")
            summary["no_connection"] += 1
            continue

        if result.outcome == "parked_ambiguous":
            firm = dbc.create_wc_firm(
                run_id, name, icp_status="parked", park_reason="firm_match_ambiguous",
                park_source="research", icp_reason="firm_match_ambiguous",
            )
            dbc.update_wc_firm(firm["id"], match_method="llm")
            dbc.set_queue_firm(qrow["id"], firm["id"])
            dbc.set_queue_status(qrow["id"], "done")
            summary["parked_ambiguous"] += 1
            continue

        # result.outcome == "matched"
        matched_company = result.matched_company
        if normalize_company(matched_company) in config.REFERRER_FIRMS:
            dbc.set_queue_status(qrow["id"], "done")
            summary["referrer_own_firm"] += 1
            progress(f"{name}: skipped — matches referrer's own firm ({matched_company})")
            continue

        firm = dbc.create_wc_firm(
            run_id, matched_company, match_method=result.match_method,
            match_confidence=result.confidence,
        )
        dbc.set_queue_firm(qrow["id"], firm["id"])
        dbc.set_queue_status(qrow["id"], "pending")
        summary["ready"] += 1

    return summary


def _test_mode_match_llm(batch: list[dict]) -> list[dict]:
    """TEST_MODE Pass-3 stub for batch-mode matching: no LLM-band case is
    expected in the §7 step-12 scenario (all 5 target firms resolve
    deterministically), so this only needs to exist so match_firms() never
    tries a real network call if a target unexpectedly lands in the LLM band."""
    return [{"target": item["target"], "matched_to": None, "confidence": 0.0} for item in batch]


# ---------------------------------------------------------------------------
# The single-source per-firm funnel
# ---------------------------------------------------------------------------
def process_referral_firm(firm_id: int, breaker: Optional[CircuitBreaker] = None,
                          progress: Progress = server_log) -> dict:
    breaker = breaker or CircuitBreaker()
    firm = dbc.get_wc_firm(firm_id)
    if firm is None:
        return {"outcome": "error", "reason": "firm not found"}
    firm_name = firm["firm_name"]

    # --- Phase 2: ICP gate (skip if already gated, e.g. resumed run or a
    # manual /qualify already ran the gate) ---
    if firm.get("icp_status") is None:
        try:
            gate = icp_gate.research_and_gate(firm_name, progress)
        except icp_gate.InfrastructureError as e:
            breaker.record_infra_failure(e.service)
            dbc.update_wc_firm(firm_id, icp_status="parked", park_reason="infra_error",
                              park_source="infrastructure", icp_reason=f"{e.service} unreachable")
            return {"outcome": "parked", "park_source": "infrastructure"}
        breaker.record_success()
        dbc.update_wc_firm(
            firm_id, icp_status=gate.icp_result, icp_reason=gate.icp_reason,
            aum_tag=gate.aum_tag, park_reason=gate.park_reason, park_source=gate.park_source,
            research_confidence=gate.confidence, source_count=gate.source_count,
            search_calls=gate.search_calls, tokens_used=gate.tokens_used,
            research_cost_usd=gate.research_cost_usd, research_raw=gate.research,
            domain=gate.research.get("domain") or None,
        )
        firm = dbc.get_wc_firm(firm_id)

    if firm["icp_status"] != "qualified":
        return {"outcome": firm["icp_status"], "park_reason": firm.get("park_reason")}

    # --- Phase 3a: POC selection ---
    connections_for_firm = dbc.connections_for_company(firm_name)
    top, alternates = poc_ranking.rank_pocs(connections_for_firm)
    if not top:
        dbc.update_wc_firm(firm_id, park_reason="no_eligible_poc")
        progress(f"{firm_name}: qualified, but no higher-level-management POC among matched connections.")
        return {"outcome": "no_eligible_poc"}
    dbc.update_wc_firm(
        firm_id, top_poc_connection_id=top["id"],
        alternate_pocs=[{"id": a["id"], "poc_name": a["poc_name"], "designation": a.get("designation")}
                       for a in alternates],
    )

    # HARD RULE 7 / Feature 3: person-level cross-workflow dedup, matched by
    # LinkedIn URL, fallback email (seed email, if present, at this pre-Apollo
    # point). Person-level, NOT firm-level — this only ever matches on THIS
    # specific POC's identity, so a different person at the same firm is
    # never caught here.
    prior = dbc.find_prior_contact(top.get("linkedin_url"), top.get("poc_email_seed"))
    if prior:
        note = _collision_note(prior)
        dbc.create_wc_draft(firm_id, top["id"], email_status="skipped_duplicate", collision_note=note)
        return {"outcome": "skipped_duplicate", "collision_note": note}

    # Idempotent resume: a prior (possibly crashed) run already drafted this POC.
    if dbc.draft_exists_for_connection(firm_id, top["id"]):
        return {"outcome": "already_drafted"}

    # --- Phase 3b: Apollo email discovery (fail cheap: no email -> stop here) ---
    research = firm.get("research_raw") or {}
    email, _confidence = apollo_enrich.find_email(
        top["poc_name"], firm_name, top.get("linkedin_url"), firm.get("domain"),
        seed_email=top.get("poc_email_seed"), progress=progress,
    )
    if not email:
        dbc.create_wc_draft(firm_id, top["id"], email_status="no_email_found")
        return {"outcome": "no_email_found"}

    # HARD RULE 7 (second check): the pre-Apollo check above only had the
    # SEED email (often blank — §3, ~91% of the export). Apollo can resolve
    # two different LinkedIn URLs to the same real inbox (or simply match
    # what a prior run's Apollo lookup already found), and that's only
    # knowable once `email` exists. Skipping this re-check would let HARD
    # RULE 7's "fallback to email" dedup silently not apply to the one email
    # this pipeline actually sends to.
    prior = dbc.find_prior_contact(top.get("linkedin_url"), email)
    if prior:
        note = _collision_note(prior)
        dbc.create_wc_draft(firm_id, top["id"], poc_email=email, email_status="skipped_duplicate",
                            collision_note=note)
        return {"outcome": "skipped_duplicate", "collision_note": note}

    # --- Phase 4: personalization research ---
    try:
        pers = personalization.research_personalization(firm_name, top["poc_name"], progress)
    except icp_gate.InfrastructureError as e:
        breaker.record_infra_failure(e.service)
        raise
    breaker.record_success()

    # --- Phase 5a: email draft ---
    email_draft = email_drafter.draft_email(
        firm_name, _first_name(top["poc_name"]), email, research, pers, progress,
    )

    # --- Phase 5b: PPT fill (graceful degradation: email-only if master missing) ---
    pdf_path = None
    ppt_attached = False
    if config.PITCH_MASTER_PPTX.exists():
        deck_content = deck_filler.get_deck_content(firm_name, research, pers)
        deck_result = deck_filler.build_and_convert(firm_name, research, deck_content, progress)
        pdf_path = deck_result["pdf_path"]
        ppt_attached = pdf_path is not None
    else:
        progress(f"{firm_name}: PPT skipped, master template not found.")

    # --- Gmail draft (compose-only, HARD RULE 1) ---
    draft_result = gmail_client.create_draft(
        email_draft["to"], email_draft["subject"], email_draft["body"], pdf_path, progress,
    )

    dbc.create_wc_draft(
        firm_id, top["id"], poc_email=email, email_status="drafted",
        thin_personalization=email_draft["thin_personalization"], personalization=pers,
        gmail_draft_id=draft_result["draft_id"], ppt_attached=ppt_attached,
    )
    dbc.record_contacted(top.get("linkedin_url"), email, top["poc_name"], firm_name,
                        draft_result["draft_id"])
    return {"outcome": "drafted", "draft_id": draft_result["draft_id"], "ppt_attached": ppt_attached}


# ---------------------------------------------------------------------------
# Batch drain: chunks of BATCH_CHUNK_SIZE, quota-pause on circuit breaker trip
# ---------------------------------------------------------------------------
_VERDICT_ICON = {"qualified": "✅", "disqualified": "❌", "parked": "◐"}
_VERDICT_LABEL = {"qualified": "QUALIFIED", "disqualified": "DISQUALIFIED", "parked": "PARKED"}


def _short_reason(icp_reason: Optional[str], max_len: int = 70) -> str:
    r = (icp_reason or "").strip()
    if not r:
        return ""
    if len(r) > max_len:
        r = r[:max_len].rsplit(" ", 1)[0].rstrip(",.") + "…"
    return r


def _fmt_chunk_verdict(chunk_num: int, total_chunks: Optional[int], start_idx: int,
                       entries: list[tuple[int, str, Optional[str], Optional[str]]],
                       total_ready: Optional[int]) -> str:
    """Feature 4 — one grouped verdict message per chunk (§4/§6, matches
    workflow_c_telegram_v2_no_cost.html): every firm's gate outcome as one
    line, never one Telegram message per firm."""
    end_idx = start_idx + len(entries) - 1
    header = f"🔍 Chunk {chunk_num}" + (f"/{total_chunks}" if total_chunks else "") + f" — firms {start_idx}–{end_idx}"
    lines = [header, ""]
    for idx, firm_name, icp_status, icp_reason in entries:
        icon = _VERDICT_ICON.get(icp_status, "•")
        label = _VERDICT_LABEL.get(icp_status, (icp_status or "unknown").upper())
        reason = _short_reason(icp_reason)
        of_total = f"/{total_ready}" if total_ready else ""
        line = f"{icon} [{idx}{of_total}] {firm_name} — {label}"
        if reason:
            line += f" ({reason})"
        lines.append(line)
    return "\n".join(lines)


def _fmt_chunk_enrich_draft(entries: list[tuple[str, dict]]) -> str:
    """Feature 4 — one grouped enrichment+drafting message per chunk, covering
    every qualified firm's Phase 3-5 outcome (contact found -> email, no email
    found, duplicate skip by name — Feature 3, or drafted +/- deck)."""
    lines = ["📧 Enriching + drafting…", ""]
    for firm_name, result in entries:
        outcome = result.get("outcome")
        if outcome == "drafted":
            deck_note = " + deck" if result.get("ppt_attached") else " (email only, no deck)"
            lines.append(f"✉️ {firm_name} → draft{deck_note} ✓")
        elif outcome == "no_email_found":
            lines.append(f"⊘ {firm_name} → no email found")
        elif outcome == "skipped_duplicate":
            note = result.get("collision_note") or "already contacted"
            lines.append(f"🔁 {firm_name} → SKIPPED ({note})")
        elif outcome == "no_eligible_poc":
            lines.append(f"⊘ {firm_name} → no higher-level-management POC on file")
        elif outcome == "already_drafted":
            lines.append(f"🔁 {firm_name} → already drafted (resume, no duplicate created)")
        else:
            lines.append(f"• {firm_name} → {outcome}")
    return "\n".join(lines)


def _fmt_chunk_summary(chunk_num: int, chunk_counts: dict, enrich_entries: list[tuple[str, dict]]) -> str:
    drafted = sum(1 for _n, r in enrich_entries if r.get("outcome") == "drafted")
    no_email = sum(1 for _n, r in enrich_entries if r.get("outcome") == "no_email_found")
    dup = sum(1 for _n, r in enrich_entries if r.get("outcome") == "skipped_duplicate")
    lines = [f"📊 Chunk {chunk_num} done — {chunk_counts.get('qualified', 0)} qualified · "
            f"{chunk_counts.get('disqualified', 0)} disqualified · {chunk_counts.get('parked', 0)} parked"]
    extra = f"✉️ {drafted} draft(s) in Gmail"
    if no_email:
        extra += f" · {no_email} no email found"
    if dup:
        extra += f" · {dup} duplicate(s)"
    lines.append(extra)
    return "\n".join(lines)


def drain_queue(run_id: int, progress: Progress = server_log, notify: Progress = _noop,
                chunk_size: Optional[int] = None, chunk_style: bool = False) -> dict:
    """Drains wc_queue in chunks of chunk_size. `progress` is per-firm/per-phase
    debug detail (stderr by default — §11.7, never Telegram).

    `notify` is the Telegram-facing channel. When `chunk_style` is True
    (Feature 4 — all THREE batch entry paths: text batch, file batch, resume),
    it gets exactly 3 grouped messages per chunk (verdict block, enrichment+
    draft block, chunk summary) — never one message per firm, never a bare
    aggregate count. When False (single-firm mode), `notify` is used ONLY for
    the circuit-breaker/crash notice; single-firm mode builds its own
    Feature-1 prospect brief from the final firm state instead. It's also
    used for Feature A's Tavily key-rotation notices (both modes) and
    Feature B's cancellation flag, checked between firms — see below.

    Returns `{"aborted": True, "cancelled": True, ...}` if a Feature-B
    /cancel was picked up mid-drain (the in-flight firm always finishes
    first); `{"aborted": True, "dead_service": ...}` if the circuit breaker
    tripped; otherwise `{"aborted": False, ...}` when the queue fully drains."""
    chunk_size = chunk_size or config.BATCH_CHUNK_SIZE
    breaker = CircuitBreaker()
    totals = {"drafted": 0, "disqualified": 0, "parked": 0, "no_email_found": 0,
             "skipped_duplicate": 0, "no_eligible_poc": 0, "already_drafted": 0}
    processed = 0
    chunk_num = 0
    total_ready = len(dbc.firms_for_run(run_id)) if chunk_style else None
    total_chunks = -(-total_ready // chunk_size) if total_ready else None  # ceil div

    # Feature A: register this run's Telegram-facing notify channel so a
    # Tavily key rotation (fully transparent to process_referral_firm itself)
    # can still surface "Tavily key 1 exhausted, switched to key 2 of 3,
    # continuing." to the operator. search_client never imports telegram_bot
    # — this is the one registration point both single-firm and batch/resume
    # share, since both call drain_queue.
    from lib_snapshot.researcher import search_client
    search_client.set_notify(notify)

    while True:
        chunk = dbc.next_pending_chunk(run_id, chunk_size)
        if not chunk:
            break
        chunk_num += 1
        start_idx = processed + 1
        verdict_entries: list[tuple[int, str, Optional[str], Optional[str]]] = []
        enrich_entries: list[tuple[str, dict]] = []
        chunk_counts = {"qualified": 0, "disqualified": 0, "parked": 0}

        for qrow in chunk:
            # Feature B: check the cancellation flag BETWEEN firms, never
            # mid-firm. Whatever qrow we're about to start is left untouched
            # ('pending') along with everything after it in this and every
            # later chunk — /referral resume drains them exactly like an
            # infra-paused run.
            run_row = dbc.get_wc_run(run_id)
            if run_row and run_row.get("cancel_requested_at"):
                if chunk_style and verdict_entries:
                    notify(_fmt_chunk_verdict(chunk_num, total_chunks, start_idx, verdict_entries, total_ready))
                    if enrich_entries:
                        notify(_fmt_chunk_enrich_draft(enrich_entries))
                dbc.update_wc_run(run_id, status="cancelled")
                return {**totals, "processed": processed, "aborted": True, "cancelled": True}

            dbc.set_queue_status(qrow["id"], "processing")
            firm_id = qrow.get("firm_id")
            if not firm_id:
                dbc.set_queue_status(qrow["id"], "done")
                continue
            firm_name = (dbc.get_wc_firm(firm_id) or {}).get("firm_name", qrow["firm_name_input"])
            try:
                result = process_referral_firm(firm_id, breaker, progress)
            except CircuitBreakerTripped as e:
                dbc.set_queue_status(qrow["id"], "infra_paused")
                dbc.update_wc_run(run_id, status="crashed", crash_detail=f"quota:{e.service}")
                if chunk_style and verdict_entries:
                    notify(_fmt_chunk_verdict(chunk_num, total_chunks, start_idx, verdict_entries, total_ready))
                    if enrich_entries:
                        notify(_fmt_chunk_enrich_draft(enrich_entries))
                if e.service == "Tavily":
                    # Feature A: by construction, a single dead key never
                    # reaches here (it rotates transparently inside
                    # search_client._post) — this only fires once the WHOLE
                    # pool is exhausted, so name it precisely.
                    notify(f"⚠️ All {search_client.pool_size()} Tavily keys exhausted — run "
                          f"paused. Add more keys or wait for quota reset, then /referral resume.")
                else:
                    notify(f"⚠️ Circuit breaker tripped ({e.service} looks dead) at firm "
                          f"{processed + 1} ({firm_name}). Run paused; /referral resume will "
                          f"continue once the service recovers. Already-completed drafts remain valid.")
                return {**totals, "processed": processed, "aborted": True, "dead_service": e.service}
            except Exception as e:  # noqa: BLE001 — HARD RULE 3: no silent failures
                dbc.set_queue_status(qrow["id"], "infra_paused")
                dbc.update_wc_run(run_id, status="crashed", crash_detail=f"{firm_name}: {e}")
                progress(f"CRASH at firm {processed + 1} ({firm_name}): {e}")
                if chunk_style and verdict_entries:
                    notify(_fmt_chunk_verdict(chunk_num, total_chunks, start_idx, verdict_entries, total_ready))
                    if enrich_entries:
                        notify(_fmt_chunk_enrich_draft(enrich_entries))
                notify(f"❌ Run crashed at firm {processed + 1} ({firm_name}): {e}\n"
                      f"Already-completed drafts remain valid. /referral resume will retry this firm.")
                return {**totals, "processed": processed, "aborted": True, "dead_service": None}

            outcome = result["outcome"]
            if outcome == "drafted":
                totals["drafted"] += 1
            elif outcome == "disqualified":
                totals["disqualified"] += 1
            elif outcome == "parked":
                totals["parked"] += 1
            elif outcome in totals:
                totals[outcome] += 1
            dbc.set_queue_status(qrow["id"], "done")
            processed += 1

            if chunk_style:
                firm_row = dbc.get_wc_firm(firm_id) or {}
                icp_status = firm_row.get("icp_status")
                if icp_status in chunk_counts:
                    chunk_counts[icp_status] += 1
                verdict_entries.append((processed, firm_name, icp_status, firm_row.get("icp_reason")))
                if icp_status == "qualified":
                    enrich_entries.append((firm_name, result))

        if chunk_style:
            notify(_fmt_chunk_verdict(chunk_num, total_chunks, start_idx, verdict_entries, total_ready))
            if enrich_entries:
                notify(_fmt_chunk_enrich_draft(enrich_entries))
            notify(_fmt_chunk_summary(chunk_num, chunk_counts, enrich_entries))

    return {**totals, "processed": processed, "aborted": False}
