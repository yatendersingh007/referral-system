"""Telegram interface (§6). Polling mode, mirrors Workflow A's proven
mechanics (single-poller DB lock, 0.3s send pacing, backlog skip on restart)
but with Workflow C's own command set and its own operator allowlist.

HARD RULE / §10d: authorized operators come ONLY from .env
AUTHORIZED_USER_IDS (comma-separated). Never hardcode a single user id.

`_dispatch` is the testable core: a pure command router taking a `send`
callback, so command logic can be exercised without a live bot token.
`run_bot()` wires it to the real Telegram getUpdates/sendMessage polling loop.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

import requests

from . import batch_upload, config, dbc, pipeline, status
from .loader import LoaderError, reload_connections

_TG_LIMIT = 3800
_QUOTES = "\"'“”‘’"

_busy = threading.Lock()

# Feature 2: ephemeral state for the >2000-row confirmation gate and the
# "awaiting a file after a bare /referral batch" handshake (same in-memory-
# dict pattern Workflow A uses for pending profiles/ambiguous-client state —
# no schema needed for something this transient). Keyed by a single-operator
# state key (matches send()'s existing default-to-first-authorized-id
# simplification — this codebase doesn't thread per-chat state anywhere else).
_pending_file_batch: dict[str, dict] = {}
_awaiting_batch_file: dict[str, float] = {}
_PENDING_FILE_BATCH_TTL_SEC = 600  # 10 min
_AWAITING_BATCH_FILE_TTL_SEC = 600  # 10 min


def _state_key() -> str:
    return str(config.AUTHORIZED_USER_IDS[0]) if config.AUTHORIZED_USER_IDS else "default"


def _api_base() -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN_C}"


def send(text: str, chat_id: Optional[str] = None) -> None:
    chat_id = chat_id or (config.AUTHORIZED_USER_IDS[0] if config.AUTHORIZED_USER_IDS else None)
    if not chat_id:
        return
    for i in range(0, len(text), _TG_LIMIT):
        chunk = text[i:i + _TG_LIMIT]
        try:
            requests.post(f"{_api_base()}/sendMessage", json={"chat_id": chat_id, "text": chunk}, timeout=30)
        except requests.RequestException:
            pass
        time.sleep(0.3)  # Workflow A lesson: pace consecutive sends


def _strip_quotes(s: str) -> str:
    s = (s or "").strip()
    while len(s) >= 2 and s[0] in _QUOTES and s[-1] in _QUOTES:
        s = s[1:-1].strip()
    return s


def _parse_quoted(arg: str) -> tuple[str, str]:
    """'"Firm" 800M' -> ('Firm', '800M'). Unquoted: only peel a trailing
    numeric-looking token; otherwise the whole string is the firm name."""
    s = (arg or "").strip()
    if not s:
        return "", ""
    if s[0] in _QUOTES:
        for i in range(1, len(s)):
            if s[i] in _QUOTES:
                return s[1:i].strip(), s[i + 1:].strip()
        return s[1:].strip(), ""
    parts = s.rsplit(maxsplit=1)
    if len(parts) == 2 and re.search(r"\d", parts[1]):
        return parts[0].strip(), parts[1].strip()
    return s, ""


def _fmt_summary(run_id: int, totals: dict) -> str:
    firms = dbc.firms_for_run(run_id)
    total_firms = len(firms)
    parked = [f for f in firms if f.get("icp_status") == "parked"]
    drafts = [dbc.wc_draft_for_firm(f["id"]) for f in firms]
    drafts = [d for d in drafts if d]
    n_ppt = sum(1 for d in drafts if d.get("ppt_attached"))
    n_drafted = totals.get("drafted", 0)
    cancelled = bool(totals.get("cancelled"))

    header = (f"\U0001F6D1 Referral run cancelled — {total_firms} firm(s) processed before stopping"
             if cancelled else f"✅ Referral run done — {total_firms} firm(s) processed")
    lines = [header]
    lines.append(f"   ✉️ {n_drafted} draft(s) delivered to Gmail ({n_ppt} with PPT attached)")
    lines.append(f"   ⛔ {totals.get('disqualified', 0)} disqualified (ICP)")
    if parked:
        lines.append(f"   \U0001F17F️ {len(parked)} parked — REVIEW THESE:")
        for i, f in enumerate(parked, 1):
            lines.append(f"      {i}. {f['firm_name']} — {f.get('icp_reason') or f.get('park_reason')}")
    lines.append(f"   \U0001F4E7 {totals.get('no_email_found', 0)} qualified but no email found")
    dup_drafts = [d for d in drafts if d.get("email_status") == "skipped_duplicate"]
    if dup_drafts:
        # Feature 3: report skips by name, not just a bare count — "single-firm
        # names who they collided with; batch lists them in the skipped section".
        by_firm_id = {f["id"]: f for f in firms}
        lines.append(f"   \U0001F501 {len(dup_drafts)} skipped, already contacted:")
        for d in dup_drafts:
            firm = by_firm_id.get(d["firm_id"], {})
            note = d.get("collision_note") or "already contacted"
            lines.append(f"      - {firm.get('firm_name', '?')} — {note}")
    else:
        lines.append(f"   \U0001F501 {totals.get('skipped_duplicate', 0)} skipped, already contacted")
    if n_drafted and not config.PITCH_MASTER_PPTX.exists():
        # §3 graceful degradation: exact required wording.
        lines.append("   PPT skipped: master template not found.")
    lines.append("")
    if cancelled:
        remaining = dbc.queue_counts(run_id).get("pending", 0)
        lines.append(f"{remaining} firm(s) remain unprocessed in the queue.")
        lines.append("/referral resume will continue from where it stopped.")
    else:
        lines.append("Check Gmail drafts folder.")
    return "\n".join(lines)


def _run_single_firm(firm_query: str) -> None:
    if not _busy.acquire(blocking=False):
        send("A run is already in progress. Please wait for it to finish.")
        return
    try:
        run = dbc.create_wc_run("single_firm", query_firm=firm_query)
        send(f"\U0001F517 Referral run started: matching \"{firm_query}\" against Manpreet's connections")
        summary = pipeline.match_and_enqueue(run["id"], [firm_query])
        if summary["ready"] == 0:
            if summary["no_connection"]:
                send(f'No connections found for "{firm_query}" in Manpreet\'s list.')
            elif summary["referrer_own_firm"]:
                send(f'"{firm_query}" is the referrer\'s own firm — never targeted.')
            elif summary["parked_ambiguous"]:
                send(f'"{firm_query}" matched ambiguously — see /status c "{firm_query}" and '
                    f're-run with the exact firm name.')
            dbc.update_wc_run(run["id"], status="done")
            return
        # chunk_style defaults to False here: single-firm mode gets Feature 1's
        # prospect brief instead of Feature 4's grouped-chunk batch messaging.
        totals = pipeline.drain_queue(run["id"], notify=send, chunk_size=config.BATCH_CHUNK_SIZE)
        if not totals.get("aborted"):
            dbc.update_wc_run(run["id"], status="done")
        send(_fmt_summary(run["id"], totals))
        if totals.get("drafted", 0) >= 1:
            firms = dbc.firms_for_run(run["id"])
            drafted_firm = next((f for f in firms if (dbc.wc_draft_for_firm(f["id"]) or {})
                                .get("email_status") == "drafted"), None)
            if drafted_firm:
                send(status.prospect_brief_text(drafted_firm["id"]))
    except Exception as e:  # noqa: BLE001
        send(f"Referral run failed: {e}")
    finally:
        _busy.release()


def _run_batch(firm_names: list[str]) -> None:
    if not _busy.acquire(blocking=False):
        send("A run is already in progress. Please wait for it to finish.")
        return
    try:
        run = dbc.create_wc_run("batch")
        send(f"\U0001F517 Referral batch started: {len(firm_names)} firm(s) submitted")
        summary = pipeline.match_and_enqueue(run["id"], firm_names)
        send(f"Free filter: {summary['ready']} matched → paid pipeline, "
            f"{summary['no_connection']} no_connection, "
            f"{summary['referrer_own_firm']} referrer's own firm skipped, "
            f"{summary['parked_ambiguous']} ambiguous matches parked.")
        # Feature 4: chunk_style=True — grouped per-chunk verdict/enrichment/
        # summary messages, never one send per firm. All three batch entry
        # paths (text, file-upload, resume) share this SAME call site pattern.
        totals = pipeline.drain_queue(run["id"], notify=send, chunk_size=config.BATCH_CHUNK_SIZE,
                                      chunk_style=True)
        if not totals.get("aborted"):
            dbc.update_wc_run(run["id"], status="done")
        send(_fmt_summary(run["id"], totals))
    except Exception as e:  # noqa: BLE001
        send(f"Referral batch failed: {e}")
    finally:
        _busy.release()


def _run_resume() -> None:
    if not _busy.acquire(blocking=False):
        send("A run is already in progress.")
        return
    try:
        run = dbc.latest_resumable_run()
        if not run:
            send("Nothing to resume — no crashed or cancelled run on record.")
            return
        # Feature A mid-run key top-up: re-read .env from disk (not the
        # process's stale in-memory pool) so a key the operator just added
        # while the run was paused for quota exhaustion is picked up now.
        from lib_snapshot.researcher import search_client
        search_client.reload_keys()
        send(f"Resuming run #{run['id']} ({run['mode']}, was {run['status']})…")
        for q in dbc.infra_paused_firms(run["id"]):
            dbc.set_queue_status(q["id"], "pending")
        dbc.update_wc_run(run["id"], status="running", cancel_requested_at=None)
        totals = pipeline.drain_queue(run["id"], notify=send, chunk_size=config.BATCH_CHUNK_SIZE,
                                      chunk_style=True)
        if not totals.get("aborted"):
            dbc.update_wc_run(run["id"], status="done")
        send(_fmt_summary(run["id"], totals))
    except Exception as e:  # noqa: BLE001
        send(f"Resume failed: {e}")
    finally:
        _busy.release()


# ---------------------------------------------------------------------------
# Feature B: cancel a running batch cleanly. Authorization: inherited for
# free — every command reaches _dispatch/_handle_referral only after
# run_bot()'s polling loop has already filtered senders against
# AUTHORIZED_USER_IDS (§10d), so no separate check is needed here.
# ---------------------------------------------------------------------------
def _handle_cancel() -> None:
    run = dbc.latest_running_run()
    if not run:
        send("No run in progress to cancel.")
        return
    dbc.request_run_cancellation(run["id"])
    send(f"\U0001F6D1 Cancellation requested for run #{run['id']} — finishing the firm "
        f"currently in progress, then stopping. /referral resume will continue from where it stops.")


def _handle_referral(arg: str) -> None:
    arg = (arg or "").strip()
    low = arg.lower()
    if low == "reload":
        try:
            result = reload_connections()
            send(f"Connections reloaded: {result.loaded} rows loaded "
                f"({result.incomplete} incomplete), {result.distinct_companies} distinct companies.")
        except LoaderError as e:
            send(f"Reload failed: {e}")
        return
    if low == "resume":
        threading.Thread(target=_run_resume, daemon=True).start()
        return
    if low == "stop":  # alias for /cancel
        _handle_cancel()
        return
    if low.startswith("batch"):
        rest = arg[len("batch"):].strip()
        rest_low = rest.lower()
        if rest_low == "confirm":
            _confirm_pending_file_batch()
            return
        if rest_low == "cancel":
            _cancel_pending_file_batch()
            return
        names = [n.strip() for part in rest.replace(",", "\n").split("\n") for n in [part.strip()] if n]
        if not names:
            _awaiting_batch_file[_state_key()] = time.time()
            send('Usage: /referral batch\\n<firm 1>\\n<firm 2>\\n...\n\n'
                "Or attach a .csv/.xlsx of firm names (one name per row, or a "
                'column headed "Firm"/"Company"/"Name") — send it now.')
            return
        threading.Thread(target=_run_batch, args=(names,), daemon=True).start()
        return
    firm = _strip_quotes(arg)
    if not firm:
        send('Usage: /referral "<Firm Name>"  |  /referral batch  |  /referral reload  |  /referral resume')
        return
    threading.Thread(target=_run_single_firm, args=(firm,), daemon=True).start()


# ---------------------------------------------------------------------------
# Feature 2: CSV/Excel target-list upload for /referral batch
# ---------------------------------------------------------------------------
def _process_batch_file(file_bytes: bytes, filename: str) -> None:
    """Testable core: given raw file bytes + a filename, parses the firm
    list and either starts the batch directly or requests confirmation for
    an over-threshold file. Never touches the network — call this from a
    test with synthetic bytes, or from _handle_document_upload with a real
    Telegram download."""
    _awaiting_batch_file.pop(_state_key(), None)
    try:
        result = batch_upload.parse_firm_list_file(file_bytes, filename)
    except batch_upload.BatchUploadError as e:
        send(f'Could not read "{filename}": {e}')
        return

    for w in result.warnings:
        send(w)

    if len(result.names) > batch_upload.CONFIRM_THRESHOLD:
        _pending_file_batch[_state_key()] = {"names": result.names, "filename": filename, "ts": time.time()}
        send(f'"{filename}" has {len(result.names)} firm names — over the '
            f'{batch_upload.CONFIRM_THRESHOLD:,} confirmation threshold.\n'
            f"Reply /referral batch confirm to proceed, or /referral batch cancel.")
        return

    send(f"Loaded {result.raw_count} firm names from {filename} "
        f"({len(result.names)} after removing exact duplicate rows). Starting batch...")
    threading.Thread(target=_run_batch, args=(result.names,), daemon=True).start()


def _confirm_pending_file_batch() -> None:
    pending = _pending_file_batch.pop(_state_key(), None)
    if not pending or (time.time() - pending["ts"]) > _PENDING_FILE_BATCH_TTL_SEC:
        send("Nothing awaiting confirmation (or it expired). Re-upload the file to try again.")
        return
    send(f"Confirmed — starting batch of {len(pending['names'])} firms from {pending['filename']}...")
    threading.Thread(target=_run_batch, args=(pending["names"],), daemon=True).start()


def _cancel_pending_file_batch() -> None:
    if _pending_file_batch.pop(_state_key(), None):
        send("Cancelled — batch upload discarded.")
    else:
        send("Nothing to cancel.")


def _download_telegram_file(file_id: str) -> bytes:
    """Real Telegram getFile + download. Not exercised by tests (no live
    token) — mirrors get_updates()/send()'s existing untested-by-design real
    network calls."""
    r = requests.get(f"{_api_base()}/getFile", params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN_C}/{file_path}"
    r2 = requests.get(file_url, timeout=60)
    r2.raise_for_status()
    return r2.content


def _handle_document_upload(document: dict, caption_text: str) -> None:
    low = (caption_text or "").strip().lower()
    key = _state_key()
    awaiting_ts = _awaiting_batch_file.get(key)
    is_awaiting = bool(awaiting_ts) and (time.time() - awaiting_ts) <= _AWAITING_BATCH_FILE_TTL_SEC
    if not (low.startswith("/referral batch") or is_awaiting):
        return  # a document with no batch context — not this bot's concern
    filename = document.get("file_name") or "upload"
    try:
        file_bytes = _download_telegram_file(document["file_id"])
    except Exception as e:  # noqa: BLE001
        send(f'Could not download "{filename}": {e}')
        return
    _process_batch_file(file_bytes, filename)


def _handle_qualify(arg: str) -> None:
    from lib_snapshot import models  # noqa: F401  (parity with Workflow A's parse_aum import site)

    firm, aum_raw = _parse_quoted(arg)
    if not firm:
        send('Usage: /qualify "<Firm>" <AUM>   e.g. /qualify "Aliya Capital Partners" 800M')
        return
    aum = _parse_aum(aum_raw)
    if aum is None:
        send(f'Could not read an AUM from "{aum_raw or "(nothing given)"}". Try 800M, 1.2B, or $500M.')
        return
    row = dbc.parked_firm_by_name(firm)
    if not row:
        send(f'No PARKED firm named "{firm}" on record. /qualify only rescues parked firms.')
        return
    research = dict(row.get("research_raw") or {})
    research["aum_value_usd"] = aum
    research["aum_display"] = _human_usd(aum)
    research["aum_tag"] = "AUM_CONFIRMED"
    in_band = config.AUM_MIN_USD <= aum <= config.AUM_MAX_USD
    if not in_band:
        send(f'{_human_usd(aum)} is outside the ${config.AUM_MIN_USD/1e6:.0f}M–'
            f'${config.AUM_MAX_USD/1e9:.1f}B band — still DISQUALIFIED. Nothing changed.')
        return
    dbc.update_wc_firm(row["id"], icp_status="qualified",
                       icp_reason=f"QUALIFIED (operator-verified AUM {_human_usd(aum)})",
                       aum_tag="AUM_CONFIRMED", park_reason=None, park_source=None,
                       research_raw=research)
    send(f'Qualified "{row["firm_name"]}" at {_human_usd(aum)} — continuing from Phase 3…')
    threading.Thread(target=lambda: send(_fmt_outcome(pipeline.process_referral_firm(row["id"]))),
                     daemon=True).start()


def _fmt_outcome(result: dict) -> str:
    return f"Result: {result.get('outcome')}" + (f" (draft {result['draft_id']})" if result.get("draft_id") else "")


def _handle_disqualify(arg: str) -> None:
    firm, _rest = _parse_quoted(arg)
    if not firm:
        send('Usage: /disqualify "<Firm>"')
        return
    row = dbc.parked_firm_by_name(firm)
    if not row:
        send(f'No PARKED firm named "{firm}" on record.')
        return
    dbc.update_wc_firm(row["id"], icp_status="disqualified",
                       icp_reason="Disqualified by operator override", park_reason=None, park_source=None)
    send(f'Disqualified "{row["firm_name"]}".')


_AUM_RE = re.compile(r"([\d.,]+)\s*(b|bn|billion|m|mm|million)?", re.IGNORECASE)


def _parse_aum(raw: str) -> Optional[float]:
    raw = (raw or "").strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    m = _AUM_RE.match(raw)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit in ("b", "bn", "billion"):
        return n * 1_000_000_000
    if unit in ("m", "mm", "million"):
        return n * 1_000_000
    return n


def _human_usd(n: float) -> str:
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.0f}M"
    return f"${n:,.0f}"


HELP = (
    "Referral Outreach bot (Workflow C, test mode).\n\n"
    '/referral "<Firm Name>"   - single-firm run\n'
    "/referral batch           - target-list batch (list firms after the command, one per line,\n"
    "                            or attach a .csv/.xlsx of firm names)\n"
    "/referral batch confirm/cancel - confirm or cancel an over-2,000-row file upload\n"
    "/referral reload          - re-ingest data/connections.csv\n"
    "/referral resume          - continue a crashed OR cancelled run\n"
    "/cancel (or /referral stop) - stop the active run cleanly, after the in-flight firm finishes\n"
    '/qualify "<Firm>" <AUM>   - rescue a parked firm (800M, 1.2B, $500M)\n'
    '/disqualify "<Firm>"      - reject a parked firm\n'
    "/status c                 - dashboard\n"
    '/status c "<Firm>"        - drill-down\n'
)


def _dispatch(text: str, chat_id: Optional[str] = None) -> None:
    """Pure-ish command router (network side effects go through send()).
    Testable without a live bot token."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "referral":
        _handle_referral(arg)
    elif cmd == "cancel":
        _handle_cancel()
    elif cmd == "qualify":
        _handle_qualify(arg)
    elif cmd == "disqualify":
        _handle_disqualify(arg)
    elif cmd == "status":
        low = arg.strip().lower()
        if low.startswith("c"):
            rest = arg.strip()[1:].strip()
            send(status.detail_text(_strip_quotes(rest)) if rest else status.dashboard_text())
        else:
            send('Usage: /status c   or   /status c "<Firm>"')
    elif cmd in ("start", "help"):
        send(HELP)
    else:
        send(f"Unknown command: {cmd}\n\n{HELP}")


def _dispatch_message(msg: dict) -> None:
    """Feature 2 entry point: routes a full Telegram message object to
    either the file-upload handler (if it carries a document) or the
    existing text command router. Testable without a live bot token — feed
    it a synthetic msg dict with a "document" key."""
    document = msg.get("document")
    if document:
        caption = msg.get("caption") or msg.get("text") or ""
        _handle_document_upload(document, caption)
        return
    _dispatch(msg.get("text", ""), chat_id=str(msg.get("chat", {}).get("id")) if msg.get("chat") else None)


# ---------------------------------------------------------------------------
# Real polling loop (not exercised in TEST_MODE verification — requires a
# live TELEGRAM_BOT_TOKEN_C from BotFather; see SETUP_TODO.md)
# ---------------------------------------------------------------------------
def get_updates(offset: Optional[int], timeout: int = 30) -> list[dict]:
    r = requests.get(f"{_api_base()}/getUpdates", params={"offset": offset, "timeout": timeout},
                     timeout=timeout + 10)
    r.raise_for_status()
    return r.json().get("result", [])


def run_bot() -> None:
    missing = config.missing_keys_c(config.REQUIRED_FOR_BOT_C)
    if missing:
        raise SystemExit(f"Refusing to start - missing config: {missing}")
    if not config.AUTHORIZED_USER_IDS:
        raise SystemExit("Refusing to start - AUTHORIZED_USER_IDS is empty in .env")

    allowed = set(config.AUTHORIZED_USER_IDS)
    offset: Optional[int] = None
    backlog = get_updates(None, timeout=0)
    if backlog:
        offset = backlog[-1]["update_id"] + 1
    send(f"Referral Outreach bot online (test mode).\n\n{HELP}")

    while True:
        for upd in get_updates(offset):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            sender = str(msg.get("from", {}).get("id"))
            if sender not in allowed:
                continue
            if msg.get("text") or msg.get("document"):
                try:
                    _dispatch_message(msg)
                except Exception as e:  # noqa: BLE001
                    send(f"Error handling command: {e}")


if __name__ == "__main__":
    run_bot()
